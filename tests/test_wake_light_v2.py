"""Regression contracts shared with the Pod, without sockets or real HA storage."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import unittest
from zoneinfo import ZoneInfo

import test_wake_light_runtime_stub as runtime
from wake_light.commands import apply_command
from wake_light.engine import (
    connected_episode, fixed_lease_ttl_seconds, start_run,
)
from wake_light.model import (
    ProfileState, SourceSnapshot, WakeLightDefaults, opaque_ref,
)
from wake_light.scheduler import (
    calendar_windows, parse_sleepypod_side, refresh_source_snapshot,
    resolve_profile_occurrences, unsupported_episode_ids,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures/sleepypod-alarm-v2.json").read_text())


def source_event(schedule_id, wake, now, *, until=None, terminal=None, identity=None):
    return {
        "schedule_id": schedule_id,
        "occurrence_id": identity or f"schedule-{schedule_id}-{int(wake.timestamp() * 1000)}",
        "scheduled_for": int(wake.timestamp()),
        "ts": int(now.timestamp() * 1000),
        "snoozed_until": int(until.timestamp()) if until else None,
        "terminal_reason": terminal,
    }


class WakeLightSourceContractTests(unittest.TestCase):
    def test_golden_calendar_uses_execution_days_and_keeps_duplicate_row_identity(self):
        alarms = parse_sleepypod_side(FIXTURE, "right", WakeLightDefaults())
        self.assertEqual(len(alarms), 2)
        self.assertEqual(alarms[0].weekdays, ("sunday", "monday", "tuesday", "wednesday", "thursday"))
        self.assertEqual(alarms[0].source_schedule_ids["sunday"], 101)
        self.assertEqual(alarms[1].source_schedule_ids, {"sunday": 102})
        changed_power = json.loads(json.dumps(FIXTURE))
        changed_power["right"]["sunday"]["power"]["off"] = "13:00"
        self.assertEqual(alarms, parse_sleepypod_side(changed_power, "right", WakeLightDefaults()))
        wake = datetime(2026, 9, 6, 14, tzinfo=UTC)
        occurrences = resolve_profile_occurrences(
            "master-bedroom", alarms, wake - timedelta(minutes=30),
            ZoneInfo("America/Los_Angeles"), WakeLightDefaults(),
        )
        self.assertEqual([item.wake_at for item in occurrences], [wake, wake])
        self.assertEqual({item.source_schedule_id for item in occurrences}, {101, 102})

    def test_unidentified_source_stays_visible_but_cannot_execute(self):
        payload = {"right": {"sunday": {"alarms": [{"time": "07:00", "enabled": True}]}}}
        snapshot = refresh_source_snapshot(
            SourceSnapshot(), "sleepypod:right", attributes=payload, available=True,
            now=datetime(2026, 9, 6, 13, tzinfo=UTC), defaults=WakeLightDefaults(),
        )
        self.assertFalse(snapshot.available)
        self.assertEqual(snapshot.failure_code, "source_identity_unavailable")
        self.assertEqual(len(snapshot.alarms), 1)

    def test_duplicate_source_row_ids_fail_closed(self):
        payload = json.loads(json.dumps(FIXTURE))
        payload["right"]["sunday"]["alarms"][1]["id"] = 101
        snapshot = refresh_source_snapshot(
            SourceSnapshot(), "sleepypod:right", attributes=payload, available=True,
            now=datetime(2026, 9, 6, 13, tzinfo=UTC), defaults=WakeLightDefaults(),
        )
        self.assertFalse(snapshot.available)
        self.assertEqual(snapshot.failure_code, "source_identity_ambiguous")

    def test_native_ramp_choices_include_no_pre_wake_fade(self):
        profile = runtime._profile()
        now = datetime(2030, 1, 1, 7, tzinfo=UTC)
        alarm = replace(runtime._alarm(now), ramp_minutes=0)
        decision = apply_command(ProfileState.initial(profile), profile, {
            "profile_id": profile.profile_id,
            "expected_revision": 0,
            "request_id": "zero-ramp",
            "operation": "upsert_alarm",
            "alarm": alarm.to_dict(),
        }, now=now)
        self.assertEqual(decision.response["outcome"], "accepted")
        self.assertEqual(decision.state.alarms[0].ramp_minutes, 0)

    def test_connected_calendar_cannot_exceed_the_fixed_lease_budget(self):
        profile = runtime._profile()
        now = datetime(2030, 1, 1, 6, tzinfo=UTC)
        alarms = tuple(runtime._alarm_at(now, alarm_id=f"a{i}", wake_offset_minutes=minutes)
                       for i, minutes in enumerate((30, 60, 90, 120)))
        windows = calendar_windows(profile.profile_id, alarms, now, ZoneInfo("UTC"), profile.defaults)
        self.assertEqual(len(unsupported_episode_ids(windows, now)), 4)
        run = start_run(profile, (windows[0],), now=now, observed_floor_pct=1)
        self.assertEqual((connected_episode(run, windows, now=now).end_at - now).total_seconds(), 7500)
        self.assertEqual(fixed_lease_ttl_seconds(), 6300)
        state = replace(ProfileState.initial(profile), alarms=alarms[:3])
        decision = apply_command(state, profile, {
            "profile_id": profile.profile_id, "expected_revision": 0, "request_id": "long",
            "operation": "upsert_alarm", "alarm": alarms[3].to_dict(),
        }, now=now)
        self.assertEqual(decision.response["error"], "episode_duration_exceeded")
        self.assertEqual(decision.state.alarms, state.alarms)


class WakeLightRuntimeV2Tests(unittest.IsolatedAsyncioTestCase):
    async def make(self, *, source=False):
        clock = [datetime(2026, 9, 6, 13, 30, tzinfo=UTC)]
        profile = runtime._profile()
        if source:
            profile = replace(
                profile, sleepypod_schedule_entity_id="sensor.schedules",
                sleepypod_source_sides=("right",),
                source_state_entity_ids={"sleepypod:right": "sensor.right_alarm"},
            )
        values = runtime._states(profile)
        values["sensor.schedules"] = runtime._State("ready", FIXTURE)
        values["sensor.right_alarm"] = runtime._State("idle")
        hass = runtime._Hass(values, lambda: clock[0])
        hass.config.time_zone = "America/Los_Angeles"
        coordinator = runtime.WakeLightCoordinator(hass, runtime._Entry(), profile)
        runtime.coordinator_module.utc_now = lambda: clock[0]
        if source:
            coordinator.state = replace(coordinator.state, source_bindings={"sleepypod:right": True})
            coordinator._refresh_source_cache_locked(clock[0])
        return coordinator, hass, clock

    async def start_source(self):
        coordinator, hass, clock = await self.make(source=True)
        await coordinator._reconcile_locked(clock[0], "start")
        self.assertIsNotNone(coordinator.state.active_run)
        for entity_id in (coordinator.profile.root_light_entity_id, *coordinator.profile.target_light_entity_ids):
            hass.states.values[entity_id] = runtime._State("on", {"brightness": 3})
        return coordinator, hass, clock

    async def test_stopped_source_b_removes_only_b_and_replays_do_not_remove_a(self):
        coordinator, hass, clock = await self.start_source()
        clock[0] += timedelta(minutes=30)
        run = coordinator.state.active_run
        a = next(item for item in run.occurrences if item.schedule.source_schedule_id == 101)
        b = next(item for item in run.occurrences if item.schedule.source_schedule_id == 102)
        event = source_event(102, b.schedule.wake_at, clock[0], terminal="stopped")
        await coordinator._handle_source_lifecycle_locked("sleepypod:right", "idle", clock[0], attributes=event)
        self.assertEqual(coordinator.state.active_run.occurrence_ids, (a.schedule.occurrence_id,))
        await coordinator._handle_source_lifecycle_locked("sleepypod:right", "idle", clock[0], attributes=event)
        old = source_event(101, a.schedule.wake_at - timedelta(days=1), clock[0], terminal="stopped")
        await coordinator._handle_source_lifecycle_locked("sleepypod:right", "idle", clock[0], attributes=old)
        self.assertEqual(coordinator.state.active_run.occurrence_ids, (a.schedule.occurrence_id,))
        self.assertFalse(any(call[1] == "release_control" for call in hass.services.calls))

    async def test_absolute_source_snooze_is_deduplicated_and_does_not_dim(self):
        coordinator, _hass, clock = await self.start_source()
        clock[0] += timedelta(minutes=30)
        await coordinator._reconcile_locked(clock[0], "deadline")
        wake = clock[0]
        event = source_event(101, wake, wake + timedelta(minutes=1), until=wake + timedelta(minutes=5))
        await coordinator._handle_source_lifecycle_locked("sleepypod:right", "snoozed", wake + timedelta(minutes=1), attributes=event)
        current = next(item for item in coordinator.state.active_run.occurrences if item.schedule.source_schedule_id == 101)
        self.assertEqual(current.snoozed_until, wake + timedelta(minutes=5))
        self.assertEqual(coordinator.state.active_run.cumulative_snooze_seconds, 300)
        for seconds in (90, 120, 330, 360):
            event["ts"] = int((wake + timedelta(seconds=seconds)).timestamp() * 1000)
            await coordinator._handle_source_lifecycle_locked(
                "sleepypod:right", "snoozed", wake + timedelta(seconds=seconds), attributes=event,
            )
        self.assertEqual(coordinator.state.active_run.cumulative_snooze_seconds, 300)
        self.assertEqual(coordinator.state.active_run.last_brightness_pct, 100)
        self.assertEqual(coordinator.state.failures, ())

    async def test_expired_source_does_not_dismiss_and_out_of_order_events_are_ignored(self):
        coordinator, _hass, clock = await self.start_source()
        clock[0] += timedelta(minutes=30)
        wake = clock[0]
        current = source_event(101, wake, wake + timedelta(seconds=20))
        await coordinator._handle_source_lifecycle_locked("sleepypod:right", "ringing", wake + timedelta(seconds=20), attributes=current)
        stale = source_event(101, wake, wake + timedelta(seconds=10), terminal="stopped")
        await coordinator._handle_source_lifecycle_locked("sleepypod:right", "idle", wake + timedelta(seconds=30), attributes=stale)
        expired = source_event(101, wake, wake + timedelta(seconds=40), terminal="expired")
        await coordinator._handle_source_lifecycle_locked("sleepypod:right", "idle", wake + timedelta(seconds=40), attributes=expired)
        late_stop = source_event(101, wake, wake + timedelta(seconds=35), terminal="stopped")
        await coordinator._handle_source_lifecycle_locked("sleepypod:right", "idle", wake + timedelta(seconds=45), attributes=late_stop)
        self.assertEqual(len(coordinator.state.active_run.occurrences), 2)

    async def test_late_source_discovery_expands_fence_across_restart_and_preserves_disconnected_alarm(self):
        coordinator, hass, clock = await self.make()
        start = clock[0]
        first = runtime._alarm_at(start, alarm_id="first", wake_offset_minutes=30)
        # Fixture helper uses UTC wall times; this case deliberately uses a UTC room.
        coordinator._timezone = ZoneInfo("UTC")
        coordinator.state = replace(coordinator.state, alarms=(first,))
        await coordinator._reconcile_locked(start, "start")
        clock[0] = start + timedelta(minutes=31)
        await coordinator._cancel_active_locked("manual_group_off", revoked=True, turn_off_owned=False)
        second = runtime._alarm_at(start, alarm_id="late", wake_offset_minutes=60)
        later = runtime._alarm_at(start, alarm_id="disconnected", wake_offset_minutes=120)
        coordinator.state = replace(coordinator.state, alarms=(*coordinator.state.alarms, second, later))
        coordinator.state = ProfileState.from_dict(coordinator.state.to_dict(), coordinator.profile)
        calls_before = len(hass.services.calls)
        clock[0] = start + timedelta(minutes=35, microseconds=1)
        await coordinator._reconcile_locked(clock[0], "restored")
        self.assertIsNone(coordinator.state.active_run)
        self.assertEqual(coordinator.state.auto_relight_blocked_until, start + timedelta(minutes=65))
        self.assertEqual(len(hass.services.calls), calls_before)
        records = {alarm.id: alarm for alarm in coordinator.state.alarms}
        self.assertFalse(records["first"].enabled)
        self.assertFalse(records["late"].enabled)
        self.assertTrue(records["disconnected"].enabled)
        clock[0] = start + timedelta(minutes=90)
        await coordinator._reconcile_locked(clock[0], "disconnected")
        self.assertIsNotNone(coordinator.state.active_run)

    async def test_old_completion_does_not_disable_rescheduled_one_shot(self):
        coordinator, _hass, clock = await self.make()
        coordinator._timezone = ZoneInfo("UTC")
        first = runtime._alarm(clock[0])
        coordinator.state = replace(coordinator.state, alarms=(first,))
        await coordinator._reconcile_locked(clock[0], "start")
        old_ids = coordinator.state.active_run.occurrence_ids
        tomorrow = replace(first, date=(clock[0] + timedelta(days=1)).date().isoformat(), revision=2)
        coordinator.state = replace(coordinator.state, alarms=(tomorrow,))
        coordinator._disable_completed_once_alarms_locked(old_ids)
        self.assertEqual(coordinator.state.alarms, (tomorrow,))
        coordinator.state = coordinator.state.remember_terminal_occurrences(old_ids)
        coordinator._calculate_occurrences_locked(clock[0])
        self.assertEqual(coordinator.state.alarms, (tomorrow,))

    async def test_fired_native_one_time_is_deleted_after_clean_hold(self):
        coordinator, _hass, clock = await self.make()
        coordinator._timezone = ZoneInfo("UTC")
        alarm = runtime._alarm(clock[0])
        coordinator.state = replace(coordinator.state, alarms=(alarm,))
        await coordinator._reconcile_locked(clock[0], "start")
        clock[0] += timedelta(minutes=30)
        await coordinator._reconcile_locked(clock[0], "wake")
        clock[0] += timedelta(minutes=5)
        await coordinator._reconcile_locked(clock[0], "hold-complete")
        self.assertEqual(coordinator.state.alarms, ())

    async def test_post_wake_user_stop_deletes_but_pre_wake_stop_retains_one_time(self):
        post, _post_hass, post_clock = await self.make()
        post._timezone = ZoneInfo("UTC")
        post_alarm = runtime._alarm(post_clock[0])
        post.state = replace(post.state, alarms=(post_alarm,))
        await post._reconcile_locked(post_clock[0], "start")
        post_clock[0] += timedelta(minutes=30)
        await post._reconcile_locked(post_clock[0], "wake")
        post_clock[0] += timedelta(minutes=1)
        await post._cancel_active_locked("manual_group_off", revoked=True, turn_off_owned=False)
        self.assertEqual(post.state.alarms, ())

        pre, _pre_hass, pre_clock = await self.make()
        pre._timezone = ZoneInfo("UTC")
        pre_alarm = runtime._alarm(pre_clock[0])
        pre.state = replace(pre.state, alarms=(pre_alarm,))
        await pre._reconcile_locked(pre_clock[0], "start")
        pre_clock[0] += timedelta(minutes=15)
        await pre._cancel_active_locked("manual_group_off", revoked=True, turn_off_owned=False)
        pre._calculate_occurrences_locked(pre_clock[0])
        self.assertEqual(len(pre.state.alarms), 1)
        self.assertFalse(pre.state.alarms[0].enabled)

    async def test_source_discovered_after_original_fence_expiry_is_still_connected(self):
        coordinator, hass, clock = await self.start_source()
        clock[0] += timedelta(minutes=31)
        await coordinator._cancel_active_locked("manual_group_off", revoked=True, turn_off_owned=False)
        clock[0] = datetime(2026, 9, 6, 14, 5, 1, tzinfo=UTC)
        await coordinator._reconcile_locked(clock[0], "original-fence-ended")
        self.assertIsNone(coordinator.state.auto_relight_blocked_until)
        payload = json.loads(json.dumps(FIXTURE))
        template = payload["right"]["sunday"]["alarms"][0]
        payload["right"]["sunday"]["alarms"].extend([
            {**template, "id": 107, "time": "07:30"},
            {**template, "id": 108, "time": "08:30"},
        ])
        clock[0] = datetime(2026, 9, 6, 14, 10, tzinfo=UTC)
        hass.states.values["sensor.schedules"] = runtime._State("ready", payload)
        coordinator._refresh_source_cache_locked(clock[0])
        coordinator.state = ProfileState.from_dict(coordinator.state.to_dict(), coordinator.profile)
        before = len(hass.services.calls)
        await coordinator._reconcile_locked(clock[0], "late-source-after-restore")
        self.assertIsNone(coordinator.state.active_run)
        self.assertEqual(coordinator.state.auto_relight_blocked_until, datetime(2026, 9, 6, 14, 35, tzinfo=UTC))
        self.assertEqual(len(hass.services.calls), before)
        clock[0] = datetime(2026, 9, 6, 15, tzinfo=UTC)
        await coordinator._reconcile_locked(clock[0], "disconnected-source")
        self.assertEqual(coordinator.state.active_run.occurrences[0].schedule.source_schedule_id, 108)

    async def test_full_source_budget_republications_are_no_change_not_new_failures(self):
        coordinator, _hass, clock = await self.start_source()
        clock[0] += timedelta(minutes=30)
        await coordinator._reconcile_locked(clock[0], "wake-deadline")
        wake = clock[0]
        for seconds in range(0, 1800, 30):
            now = wake + timedelta(seconds=seconds)
            await coordinator._handle_source_lifecycle_locked(
                "sleepypod:right", "snoozed", now,
                attributes=source_event(101, wake, now, until=wake + timedelta(minutes=30)),
            )
        self.assertEqual(coordinator.state.active_run.cumulative_snooze_seconds, 1800)
        self.assertEqual(coordinator.state.active_run.last_brightness_pct, 100)
        self.assertEqual(coordinator.state.failures, ())

    async def test_episode_anchored_stop_survives_config_revision_but_not_a_different_episode(self):
        coordinator, _hass, clock = await self.make()
        coordinator._timezone = ZoneInfo("UTC")
        coordinator.state = replace(coordinator.state, alarms=(runtime._alarm(clock[0]),))
        await coordinator._reconcile_locked(clock[0], "start")
        episode = opaque_ref("episode", coordinator.state.active_run.lease_id)
        coordinator.state = replace(coordinator.state, revision=8)
        command = {
            "profile_id": coordinator.profile.profile_id, "expected_revision": 0,
            "request_id": "urgent", "operation": "end_episode", "episode_ref": "old-episode",
        }
        rejected = apply_command(coordinator.state, coordinator.profile, command, now=clock[0])
        self.assertEqual(rejected.response["outcome"], "no_active_occurrence")
        accepted = apply_command(coordinator.state, coordinator.profile, {
            **command, "request_id": "current", "episode_ref": episode,
        }, now=clock[0])
        self.assertEqual(accepted.response["outcome"], "accepted")

    async def test_recovered_source_failure_is_history_not_a_current_blocker(self):
        coordinator, _hass, clock = await self.make(source=True)
        coordinator.state = coordinator.state.with_failure("source_unavailable", at=clock[0])
        model = coordinator._build_read_model(clock[0] + timedelta(hours=1))
        self.assertNotEqual(model.state, "degraded")
        self.assertIn("source_unavailable", model.attributes["failures"])
        self.assertEqual(model.attributes["current_blockers"], [])
        self.assertEqual(model.attributes["contract_version"], 3)
