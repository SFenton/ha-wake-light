"""Pure command, persistence, and read-model tests."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components"),
)

from wake_light.commands import apply_command  # noqa: E402
from wake_light.engine import mark_acquired, mark_dispatched, start_run  # noqa: E402
from wake_light.model import (  # noqa: E402
    CancellationRecord,
    ProfileState,
    ScheduledOccurrence,
    SourceSnapshot,
    WakeLightAlarm,
    WakeLightDefaults,
    WakeLightProfile,
)
from wake_light.read_model import build_sensor_read_model  # noqa: E402
from wake_light.scheduler import resolve_alarm_occurrence  # noqa: E402


def profile(*, legacy_safe: bool = True) -> WakeLightProfile:
    return WakeLightProfile(
        profile_id="master-bedroom",
        name="Bedroom",
        root_light_entity_id="light.bedroom",
        target_light_entity_ids=(
            "light.bedroom_window_light",
            "light.bedroom_door_light",
            "light.left_nightstand_light",
            "light.right_nightstand_light",
        ),
        occupancy_entity_id="binary_sensor.bedroom_occupancy_sensors",
        pbl_switch_entity_id="switch.bedroom_presence_allowed",
        vacation_entity_id="input_boolean.vacation",
        blocker_entity_ids=("switch.adaptive_lighting_bedroom",),
        sleepypod_schedule_entity_id="sensor.bedroom_sleepypod_schedules",
        sleepypod_source_sides=("left", "right"),
        defaults=WakeLightDefaults(),
        legacy_brightness_lifecycle_safe=legacy_safe,
    )


def upsert_command(request_id: str = "request-1") -> dict:
    return {
        "profile_id": "master-bedroom",
        "expected_revision": 0,
        "request_id": request_id,
        "operation": "upsert_alarm",
        "alarm": {
            "id": "weekday",
            "label": "Weekday Wake",
            "kind": "weekly",
            "local_time": "06:30",
            "ramp_minutes": 30,
            "revision": 0,
            "enabled": True,
            "date": None,
            "weekdays": ["monday", "tuesday"],
            "source": "native",
            "source_ref": None,
        },
    }


class WakeLightContractTests(unittest.TestCase):
    def test_revision_and_request_id_idempotency(self) -> None:
        wake_profile = profile()
        initial = ProfileState.initial(wake_profile)
        first = apply_command(initial, wake_profile, upsert_command())
        self.assertEqual(first.response["outcome"], "accepted")
        self.assertEqual(first.response["request_id"], "request-1")
        self.assertEqual(first.state.revision, 1)
        self.assertEqual(first.state.alarms[0].revision, 1)
        self.assertNotIn("request-1", repr(first.state.to_dict()))

        duplicate = apply_command(first.state, wake_profile, upsert_command())
        self.assertTrue(duplicate.response["idempotent"])
        self.assertEqual(duplicate.response["request_id"], "request-1")
        self.assertEqual(duplicate.state.revision, 1)

        changed_payload = upsert_command()
        changed_payload["alarm"] = {
            **changed_payload["alarm"],
            "label": "Different",
        }
        conflict = apply_command(first.state, wake_profile, changed_payload)
        self.assertEqual(
            conflict.response["outcome"],
            "request_id_conflict",
        )

        stale_revision = {
            "profile_id": "master-bedroom",
            "expected_revision": 0,
            "request_id": "request-2",
            "operation": "delete_alarm",
            "alarm_id": "weekday",
        }
        revision_conflict = apply_command(
            first.state,
            wake_profile,
            stale_revision,
        )
        self.assertEqual(
            revision_conflict.response["outcome"],
            "revision_conflict",
        )

    def test_source_bound_alarm_is_read_only(self) -> None:
        wake_profile = profile()
        initial = ProfileState.initial(wake_profile)
        command = upsert_command()
        command["alarm"] = {
            **command["alarm"],
            "source": "sleepypod",
            "source_ref": "sleepypod:left",
        }
        result = apply_command(initial, wake_profile, command)
        self.assertEqual(result.response["outcome"], "read_only_source")
        self.assertEqual(result.state.alarms, ())

    def test_legacy_profile_enabled_is_ignored_and_old_global_command_is_unsupported(self) -> None:
        wake_profile = profile()
        legacy = ProfileState.initial(wake_profile).to_dict()
        legacy["enabled"] = False
        restored = ProfileState.from_dict(legacy, wake_profile)
        self.assertNotIn("enabled", restored.to_dict())
        result = apply_command(restored, wake_profile, {
            "profile_id": wake_profile.profile_id,
            "expected_revision": restored.revision,
            "request_id": "legacy-enable",
            "operation": "set_enabled",
            "enabled": True,
        })
        self.assertEqual(result.response["outcome"], "invalid_request")
        self.assertEqual(result.response["error"], "unsupported_operation")

    def test_legacy_ramp_value_loads_but_new_commands_require_product_choices(self) -> None:
        wake_profile = profile()
        legacy_alarm = {
            **upsert_command()["alarm"],
            "ramp_minutes": 45,
        }
        restored = WakeLightAlarm.from_dict(
            legacy_alarm,
            wake_profile.defaults,
            require_native=True,
        )
        self.assertEqual(restored.ramp_minutes, 45)
        command = upsert_command()
        command["alarm"] = legacy_alarm
        result = apply_command(ProfileState.initial(wake_profile), wake_profile, command)
        self.assertEqual(result.response["outcome"], "invalid_request")
        self.assertEqual(result.response["error"], "unsupported_ramp_minutes")

    def test_native_snooze_command_is_unsupported(self) -> None:
        wake_profile = profile()
        state = ProfileState.initial(wake_profile)
        result = apply_command(state, wake_profile, {
            "profile_id": wake_profile.profile_id,
            "expected_revision": state.revision,
            "request_id": "legacy-native-snooze",
            "operation": "snooze",
            "occurrence_id": "occurrence-1",
        })
        self.assertEqual(result.response["outcome"], "invalid_request")
        self.assertEqual(result.response["error"], "unsupported_operation")

    def test_cancel_occurrence_updates_shared_run_and_requests_release(self) -> None:
        wake_profile = profile()
        now = datetime(2030, 1, 1, 6, 0, tzinfo=UTC)
        scheduled = ScheduledOccurrence(
            occurrence_id="occurrence-1",
            alarm_id="weekday",
            source="native",
            source_ref=None,
            wake_at=now + timedelta(minutes=30),
            ramp_start_at=now,
            ramp_minutes=30,
            hold_minutes=5,
        )
        run = start_run(
            wake_profile,
            (scheduled,),
            now=now,
            observed_floor_pct=1,
        )
        state = ProfileState.initial(wake_profile)
        state = replace(state, active_run=run)
        result = apply_command(
            state,
            wake_profile,
            {
                "profile_id": wake_profile.profile_id,
                "expected_revision": 0,
                "request_id": "cancel-1",
                "operation": "cancel_occurrence",
                "occurrence_id": "occurrence-1",
            },
            now=now,
        )
        self.assertEqual(result.response["outcome"], "accepted")
        self.assertIsNone(result.state.active_run)
        self.assertEqual(result.effect.kind, "release_cancelled")
        self.assertIn(
            "occurrence-1",
            result.state.terminal_occurrence_ids,
        )

    def test_end_episode_is_accepted_without_an_occurrence_id(self) -> None:
        wake_profile = profile()
        now = datetime(2030, 1, 1, 6, 0, tzinfo=UTC)
        scheduled = ScheduledOccurrence(
            occurrence_id="occurrence-1",
            alarm_id="weekday",
            source="native",
            source_ref=None,
            wake_at=now + timedelta(minutes=30),
            ramp_start_at=now,
            ramp_minutes=30,
            hold_minutes=5,
        )
        run = start_run(
            wake_profile,
            (scheduled,),
            now=now,
            observed_floor_pct=1,
        )
        state = replace(
            ProfileState.initial(wake_profile),
            active_run=run,
        )

        result = apply_command(
            state,
            wake_profile,
            {
                "profile_id": wake_profile.profile_id,
                "expected_revision": 0,
                "request_id": "end-episode-1",
                "operation": "end_episode",
            },
            now=now,
        )

        self.assertEqual(result.response["outcome"], "accepted")
        self.assertEqual(result.state.last_outcome, "cancelled_by_user")
        self.assertIsNone(result.state.active_run)
        self.assertEqual(result.effect.kind, "end_episode")

    def test_state_serialization_round_trip_keeps_recovery_and_instrumentation(self) -> None:
        wake_profile = profile()
        now = datetime(2030, 1, 1, 6, 0, tzinfo=UTC)
        alarm = WakeLightAlarm(
            id="weekday",
            label="Weekday Wake",
            kind="weekly",
            local_time="06:30",
            ramp_minutes=30,
            weekdays=("tuesday",),
        )
        occurrence = ScheduledOccurrence(
            occurrence_id="occurrence-1",
            alarm_id=alarm.id,
            source="native",
            source_ref=None,
            wake_at=now + timedelta(minutes=30),
            ramp_start_at=now,
            ramp_minutes=30,
            hold_minutes=5,
        )
        run = start_run(
            wake_profile,
            (occurrence,),
            now=now,
            observed_floor_pct=20,
        )
        run = mark_acquired(
            run,
            generation=3,
            acquired_at=now,
            expires_at=now + timedelta(hours=1),
            outcome="acquired",
        )
        run = replace(
            run,
            next_deadline=now + timedelta(seconds=30),
            last_pbl_outcome="acquired",
            recovery_decision="within_catchup",
        )
        state = ProfileState(
            profile_id=wake_profile.profile_id,
            revision=4,
            defaults=wake_profile.defaults,
            alarms=(alarm,),
            source_bindings={
                "sleepypod:left": True,
                "sleepypod:right": False,
            },
            source_cache={
                "sleepypod:left": SourceSnapshot(
                    alarms=(),
                    available=False,
                    last_success_at=now,
                    last_observed_at=now + timedelta(minutes=1),
                    failure_code="source_unavailable",
                    terminal_reason="expired",
                    last_stopped_occurrence_id="pod-occurrence-1",
                ),
                "sleepypod:right": SourceSnapshot(),
            },
            active_run=run,
            auto_relight_blocked_until=now + timedelta(hours=2),
            last_cancellation=CancellationRecord(
                at=now - timedelta(hours=1),
                occurrence_count=2,
                occurrence_refs=("occ-ref-1", "occ-ref-2"),
                suppressed_until=now + timedelta(hours=2),
            ),
        ).with_failure("pbl_dispatch_failed", at=now)
        state = state.remember_request(
            "request-1",
            {"operation": "update_defaults"},
            {"outcome": "accepted"},
            at=now,
        )
        restored = ProfileState.from_dict(state.to_dict(), wake_profile)
        self.assertEqual(restored.to_dict(), state.to_dict())
        self.assertEqual(restored.active_run.generation, 3)
        self.assertEqual(
            restored.active_run.next_deadline,
            now + timedelta(seconds=30),
        )
        self.assertEqual(
            restored.active_run.recovery_decision,
            "within_catchup",
        )
        self.assertEqual(
            restored.source_cache["sleepypod:left"].failure_code,
            "source_unavailable",
        )
        self.assertEqual(
            restored.source_cache["sleepypod:left"].terminal_reason,
            "expired",
        )
        self.assertEqual(
            restored.source_cache[
                "sleepypod:left"
            ].last_stopped_occurrence_id,
            "pod-occurrence-1",
        )

    def test_version_one_state_without_new_fields_migrates_safely(self) -> None:
        wake_profile = profile()
        legacy = ProfileState.initial(wake_profile).to_dict()
        legacy.pop("auto_relight_blocked_until")
        legacy.pop("last_cancellation")
        for snapshot in legacy["source_cache"].values():
            snapshot.pop("terminal_reason")
            snapshot.pop("last_stopped_occurrence_id")
        legacy["failures"] = [
            {
                "at": "2030-01-01T06:00:00+00:00",
                "code": "manual_group_off",
                "lease_ref": "lease-old",
                "occurrence_ref": "occ-old",
                "request_ref": None,
            },
            {
                "at": "2030-01-01T06:01:00+00:00",
                "code": "pbl_dispatch_failed",
                "lease_ref": "lease-real",
                "occurrence_ref": "occ-real",
                "request_ref": None,
            },
        ]
        legacy["version"] = 1

        restored = ProfileState.from_dict(legacy, wake_profile)

        self.assertIsNone(restored.auto_relight_blocked_until)
        self.assertIsNone(restored.last_cancellation)
        self.assertTrue(
            all(
                snapshot.terminal_reason is None
                for snapshot in restored.source_cache.values()
            )
        )
        self.assertTrue(
            all(
                snapshot.last_stopped_occurrence_id is None
                for snapshot in restored.source_cache.values()
            )
        )
        self.assertEqual(
            [failure.code for failure in restored.failures],
            ["pbl_dispatch_failed"],
        )

    def test_sensor_serialization_matches_exact_react_attribute_contract(self) -> None:
        wake_profile = profile()
        state = ProfileState.initial(wake_profile)
        state = apply_command(
            state,
            wake_profile,
            upsert_command(),
        ).state
        alarm = state.alarms[0]
        occurrence = resolve_alarm_occurrence(
            wake_profile.profile_id,
            alarm,
            datetime(2030, 1, 7, 5, tzinfo=ZoneInfo("America/Los_Angeles")),
            ZoneInfo("America/Los_Angeles"),
            state.defaults,
        )
        self.assertIsNotNone(occurrence)
        entity_states = {
            wake_profile.root_light_entity_id: "off",
            wake_profile.pbl_switch_entity_id: "on",
            wake_profile.vacation_entity_id: "off",
            wake_profile.occupancy_entity_id: "off",
            wake_profile.blocker_entity_ids[0]: "off",
            **{
                entity_id: "off"
                for entity_id in wake_profile.target_light_entity_ids
            },
        }
        read_model = build_sensor_read_model(
            state,
            wake_profile,
            entity_states=entity_states,
            light_target_name="Bedroom Lights",
            next_occurrence=occurrence,
            now=datetime(2030, 1, 7, 5, tzinfo=UTC),
        )
        self.assertEqual(read_model.state, "scheduled")
        self.assertEqual(
            set(read_model.attributes),
            {
                "contract_version",
                "command_available",
                "episode_ref",
                "next_ramp_minutes",
                "current_blockers",
                "last_failure",
                "limits",
                "available",
                "profile_id",
                "revision",
                "alarms",
                "defaults",
                "next_wake_at",
                "progress",
                "commanded_brightness_pct",
                "active_occurrences",
                "auto_relight_blocked_until",
                "last_outcome",
                "last_cancellation",
                "failures",
                "safety",
                "source_bindings",
            },
        )
        self.assertEqual(
            set(read_model.attributes["safety"]),
            {
                "vacation_state",
                "pbl_state",
                "occupancy_state",
                "light_state",
                "light_target_name",
            },
        )
        self.assertEqual(read_model.attributes["safety"]["pbl_state"], "ready")
        self.assertEqual(
            read_model.attributes["commanded_brightness_pct"],
            0,
        )

    def test_episode_progress_stays_complete_while_later_curve_is_ramping(
        self,
    ) -> None:
        wake_profile = profile()
        start = datetime(2030, 1, 1, 6, 0, tzinfo=UTC)
        first = ScheduledOccurrence(
            occurrence_id="occurrence-1",
            alarm_id="first",
            source="native",
            source_ref=None,
            wake_at=start + timedelta(minutes=30),
            ramp_start_at=start,
            ramp_minutes=30,
            hold_minutes=5,
        )
        second = ScheduledOccurrence(
            occurrence_id="occurrence-2",
            alarm_id="second",
            source="native",
            source_ref=None,
            wake_at=start + timedelta(minutes=60),
            ramp_start_at=start + timedelta(minutes=30),
            ramp_minutes=30,
            hold_minutes=5,
        )
        run = start_run(
            wake_profile,
            (first, second),
            now=start,
            observed_floor_pct=1,
        )
        run = mark_dispatched(
            run,
            now=first.wake_at,
            brightness_pct=100,
            target_entity_ids=run.target_entity_ids,
            pbl_outcome="dispatched",
            final_dispatch=True,
        )
        run = replace(run, occurrences=(run.occurrences[1],))
        state = replace(
            ProfileState.initial(wake_profile),
            active_run=run,
        )
        entity_states = {
            wake_profile.root_light_entity_id: "on",
            wake_profile.pbl_switch_entity_id: "on",
            wake_profile.vacation_entity_id: "off",
            wake_profile.occupancy_entity_id: "off",
            wake_profile.blocker_entity_ids[0]: "off",
            **{
                entity_id: "on"
                for entity_id in wake_profile.target_light_entity_ids
            },
        }

        read_model = build_sensor_read_model(
            state,
            wake_profile,
            entity_states=entity_states,
            light_target_name="Bedroom Lights",
            next_occurrence=None,
            now=start + timedelta(minutes=40),
        )

        self.assertEqual(read_model.state, "holding")
        self.assertEqual(read_model.attributes["progress"], 100)
        self.assertEqual(
            read_model.attributes["commanded_brightness_pct"],
            100,
        )
        self.assertEqual(
            read_model.attributes["active_occurrences"][0]["phase"],
            "ramping",
        )
        self.assertAlmostEqual(
            read_model.attributes["active_occurrences"][0]["progress"],
            33.33,
            places=2,
        )

    def test_runtime_source_never_uses_direct_light_turn_on(self) -> None:
        coordinator_path = (
            Path(__file__).resolve().parents[1]
            / "custom_components"
            / "wake_light"
            / "coordinator.py"
        )
        source = coordinator_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        direct_turn_on = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "async_call":
                continue
            first, second = node.args[:2]
            if (
                isinstance(first, ast.Constant)
                and first.value == "light"
                and isinstance(second, ast.Constant)
                and second.value == "turn_on"
            ):
                direct_turn_on.append(node.lineno)
        self.assertEqual(direct_turn_on, [])
        self.assertIn('"brightness_pct": brightness', source)
        self.assertIn('"transition": transition', source)
        self.assertIn("PBL_SERVICE_DISPATCH", source)


if __name__ == "__main__":
    unittest.main()
