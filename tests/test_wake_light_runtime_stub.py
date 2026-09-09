"""Lightweight coordinator tests without installing Home Assistant."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import types
import unittest


def _install_homeassistant_stubs() -> None:
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    const = types.ModuleType("homeassistant.const")
    const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
    const.EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
    core = types.ModuleType("homeassistant.core")

    class Event:
        def __init__(self, data=None):
            self.data = data or {}

    core.Event = Event
    core.HomeAssistant = object
    core.callback = lambda function: function

    class CoreState:
        starting = object()
        running = object()

    core.CoreState = CoreState

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    event = types.ModuleType("homeassistant.helpers.event")
    event.async_track_point_in_utc_time = (
        lambda _hass, _callback, _when: (lambda: None)
    )
    event.async_track_state_change_event = (
        lambda _hass, _entities, _callback: (lambda: None)
    )
    update = types.ModuleType("homeassistant.helpers.update_coordinator")

    class DataUpdateCoordinator:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

        def __init__(
            self,
            hass,
            _logger,
            *,
            config_entry,
            name,
            update_interval,
        ):
            self.hass = hass
            self.config_entry = config_entry
            self.name = name
            self.update_interval = update_interval
            self.data = None

        def async_set_updated_data(self, data):
            self.data = data

        async def async_shutdown(self):
            return None

    update.DataUpdateCoordinator = DataUpdateCoordinator
    storage = types.ModuleType("homeassistant.helpers.storage")

    class Store:
        @classmethod
        def __class_getitem__(cls, _item):
            return cls

        def __init__(self, *_args, **_kwargs):
            self.data = None

        async def async_load(self):
            return self.data

        async def async_save(self, data):
            self.data = data

    storage.Store = Store

    sys.modules.update(
        {
            "homeassistant": homeassistant,
            "homeassistant.const": const,
            "homeassistant.core": core,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.event": event,
            "homeassistant.helpers.storage": storage,
            "homeassistant.helpers.update_coordinator": update,
        }
    )


_install_homeassistant_stubs()
sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components"),
)

from wake_light import coordinator as coordinator_module  # noqa: E402
from homeassistant.core import CoreState  # noqa: E402
from wake_light.const import (  # noqa: E402
    FAILURE_MANUAL_REVOKE,
    FAILURE_VACATION_BLOCKED,
)
from wake_light.coordinator import WakeLightCoordinator  # noqa: E402
from wake_light.model import (  # noqa: E402
    ProfileState,
    SourceSnapshot,
    WakeLightAlarm,
    WakeLightDefaults,
    WakeLightProfile,
)


class _State:
    def __init__(self, state: str, attributes=None) -> None:
        self.state = state
        self.attributes = attributes or {}


class _States:
    def __init__(self, values) -> None:
        self.values = values

    def get(self, entity_id):
        return self.values.get(entity_id)


class _Services:
    def __init__(self, clock) -> None:
        self.calls = []
        self._clock = clock
        self._generation = 0
        self._lease_shape = None
        self._lease_expires_at = None
        self.deny_next_acquire = False
        self.next_dispatch_response = None

    async def async_call(
        self,
        domain,
        service,
        data,
        *,
        blocking,
        return_response=False,
    ):
        self.calls.append((domain, service, data))
        if domain == "presence_based_lighting" and service == "acquire_control":
            if self.deny_next_acquire:
                self.deny_next_acquire = False
                return {
                    "outcome": "denied",
                    "blockers": ["target_unavailable"],
                }
            now = self._clock()
            lease_shape = (
                data["lease_id"],
                tuple(sorted(data["occurrence_ids"])),
                tuple(sorted(data["target_entity_ids"])),
            )
            if lease_shape != self._lease_shape:
                self._generation += 1
                self._lease_shape = lease_shape
            if self._lease_expires_at is None:
                self._lease_expires_at = now + timedelta(hours=2)
            return {
                "outcome": "acquired" if self._generation == 1 else "updated",
                "generation": self._generation,
                "acquired_at": now.isoformat(),
                "expires_at": self._lease_expires_at.isoformat(),
            }
        if domain == "presence_based_lighting" and service == "dispatch_control":
            if self.next_dispatch_response is not None:
                response = self.next_dispatch_response
                self.next_dispatch_response = None
                return response
            if data["expected_generation"] != self._generation:
                return {
                    "outcome": "blocked",
                    "blockers": ["inactive_or_mismatched_lease"],
                }
            return {
                "outcome": "dispatched",
                "generation": self._generation,
                "target_entity_ids": data["target_entity_ids"],
            }
        if domain == "presence_based_lighting" and service == "release_control":
            if data["expected_generation"] != self._generation:
                return {"outcome": "token_mismatch"}
            return {"outcome": "released"}
        return None


class _Bus:
    def __init__(self) -> None:
        self.events = []

    def async_listen(self, _event, _callback):
        return lambda: None

    def async_listen_once(self, _event, _callback):
        return lambda: None

    def async_fire(self, event, data):
        self.events.append((event, data))


class _Entry:
    entry_id = "entry-1"


class _Hass:
    def __init__(self, values, clock) -> None:
        self.is_running = True
        self.state = CoreState.running
        self.config = types.SimpleNamespace(time_zone="UTC")
        self.states = _States(values)
        self.services = _Services(clock)
        self.bus = _Bus()

    def async_create_task(self, coroutine):
        return asyncio.create_task(coroutine)


def _profile() -> WakeLightProfile:
    return WakeLightProfile(
        profile_id="master-bedroom",
        name="Master Bedroom",
        root_light_entity_id="light.master_bedroom",
        target_light_entity_ids=(
            "light.master_bedroom_window_light",
            "light.master_bedroom_door_light",
            "light.stephen_nightstand_light",
            "light.steph_nightstand_light",
        ),
        pbl_switch_entity_id="switch.master_bedroom_presence_allowed",
        vacation_entity_id="input_boolean.vacation_mode",
        blocker_entity_ids=("switch.adaptive_lighting_master_bedroom",),
        defaults=WakeLightDefaults(),
        legacy_brightness_lifecycle_safe=True,
    )


def _states(profile: WakeLightProfile):
    values = {
        profile.root_light_entity_id: _State(
            "off",
            {"brightness": 0, "friendly_name": "Master Bedroom Lights"},
        ),
        profile.pbl_switch_entity_id: _State(
            "on",
            {"control_lease_mode": "enforce"},
        ),
        profile.vacation_entity_id: _State("off"),
        profile.blocker_entity_ids[0]: _State("off"),
    }
    values.update(
        {entity_id: _State("off") for entity_id in profile.target_light_entity_ids}
    )
    return values


def _alarm(now: datetime) -> WakeLightAlarm:
    wake = now + timedelta(minutes=30)
    return WakeLightAlarm(
        id="once",
        label="Once",
        kind="once",
        local_time=wake.strftime("%H:%M"),
        ramp_minutes=30,
        date=wake.date().isoformat(),
    )


def _alarm_at(
    now: datetime,
    *,
    alarm_id: str,
    wake_offset_minutes: int,
) -> WakeLightAlarm:
    wake = now + timedelta(minutes=wake_offset_minutes)
    return WakeLightAlarm(
        id=alarm_id,
        label=alarm_id,
        kind="once",
        local_time=wake.strftime("%H:%M"),
        ramp_minutes=30,
        date=wake.date().isoformat(),
    )


class WakeLightRuntimeStubTests(unittest.IsolatedAsyncioTestCase):
    async def _coordinator(self):
        clock_value = [datetime(2030, 1, 1, 6, 0, tzinfo=UTC)]
        wake_profile = _profile()
        hass = _Hass(_states(wake_profile), lambda: clock_value[0])
        coordinator = WakeLightCoordinator(hass, _Entry(), wake_profile)
        coordinator.state = replace(
            ProfileState.initial(wake_profile),
            alarms=(_alarm(clock_value[0]),),
        )
        coordinator_module.utc_now = lambda: clock_value[0]
        return coordinator, hass, clock_value

    async def test_dispatches_only_through_pbl_and_releases_after_hold(self) -> None:
        coordinator, hass, clock = await self._coordinator()
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")
        acquire = hass.services.calls[0]
        dispatch = hass.services.calls[1]
        self.assertEqual(
            acquire[:2],
            ("presence_based_lighting", "acquire_control"),
        )
        self.assertEqual(
            dispatch[:2],
            ("presence_based_lighting", "dispatch_control"),
        )
        self.assertEqual(
            dispatch[2]["service_data"],
            {"brightness_pct": 1.0, "transition": 0.0},
        )
        self.assertNotIn(
            "light.master_bedroom_bathroom_light",
            dispatch[2]["target_entity_ids"],
        )

        clock[0] += timedelta(minutes=30)
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "deadline")
        final_dispatch = [
            call
            for call in hass.services.calls
            if call[:2]
            == ("presence_based_lighting", "dispatch_control")
        ][-1]
        self.assertEqual(
            final_dispatch[2]["service_data"],
            {"brightness_pct": 100.0, "transition": 0.0},
        )

        clock[0] += timedelta(minutes=5)
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "hold-complete")
        release = [
            call
            for call in hass.services.calls
            if call[:2]
            == ("presence_based_lighting", "release_control")
        ][-1]
        self.assertEqual(release[2]["outcome"], "completed")
        self.assertEqual(release[2]["cause"], "hold_complete")
        self.assertIsNone(coordinator.state.active_run)

    async def test_excluded_root_members_do_not_raise_the_wake_floor(self) -> None:
        coordinator, hass, clock = await self._coordinator()
        hass.states.values[coordinator.profile.root_light_entity_id] = _State(
            "on",
            {"brightness": 255},
        )
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "bathroom-on")

        dispatch = [
            call
            for call in hass.services.calls
            if call[:2]
            == ("presence_based_lighting", "dispatch_control")
        ][-1]
        self.assertEqual(
            dispatch[2]["service_data"]["brightness_pct"],
            1.0,
        )

        explicit_target = coordinator.profile.target_light_entity_ids[0]
        hass.states.values[explicit_target] = _State(
            "on",
            {"brightness": 128},
        )
        clock[0] += timedelta(seconds=30)
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "target-brightened")
        dispatch = [
            call
            for call in hass.services.calls
            if call[:2]
            == ("presence_based_lighting", "dispatch_control")
        ][-1]
        self.assertGreaterEqual(
            dispatch[2]["service_data"]["brightness_pct"],
            51.0,
        )

    async def test_transient_remembered_brightness_does_not_raise_ramp_floor(
        self,
    ) -> None:
        coordinator, hass, clock = await self._coordinator()
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")
        run = coordinator.state.active_run
        assert run is not None
        target = run.target_entity_ids[0]
        old_target = hass.states.values[target]
        stale_echo = _State("on", {"brightness": 219})
        hass.states.values[target] = stale_echo
        clock[0] += timedelta(seconds=1)

        await coordinator._async_handle_state_event(
            coordinator_module.Event(
                {
                    "entity_id": target,
                    "new_state": stale_echo,
                    "old_state": old_target,
                }
            ),
            observed_lease_id=coordinator.state.active_run.lease_id,
        )
        active = coordinator.state.active_run
        assert active is not None
        self.assertEqual(active.observed_floor_pct, 1)

        hass.states.values[target] = _State("on", {"brightness": 3})
        clock[0] += timedelta(seconds=29)
        async with coordinator._lock:
            await coordinator._reconcile_locked(
                clock[0],
                "settled-target",
            )
        dispatch = [
            call
            for call in hass.services.calls
            if call[:2]
            == ("presence_based_lighting", "dispatch_control")
        ][-1]
        self.assertLess(
            dispatch[2]["service_data"]["brightness_pct"],
            10,
        )

    async def test_late_catchup_dispatches_full_and_holds_from_catchup(self) -> None:
        coordinator, hass, clock = await self._coordinator()
        coordinator.state = replace(
            coordinator.state,
            alarms=(
                _alarm_at(
                    clock[0],
                    alarm_id="late",
                    wake_offset_minutes=-6,
                ),
            ),
        )
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "late-catchup")

        active = coordinator.state.active_run
        assert active is not None
        self.assertEqual(
            active.occurrences[0].hold_until,
            clock[0] + timedelta(minutes=5),
        )
        dispatch = [
            call
            for call in hass.services.calls
            if call[:2]
            == ("presence_based_lighting", "dispatch_control")
        ][-1]
        self.assertEqual(
            dispatch[2]["service_data"]["brightness_pct"],
            100.0,
        )
        self.assertTrue(active.occurrences[0].final_dispatched)

        clock[0] += timedelta(minutes=5)
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "late-hold-complete")
        release = [
            call
            for call in hass.services.calls
            if call[:2]
            == ("presence_based_lighting", "release_control")
        ][-1]
        self.assertEqual(release[2]["outcome"], "completed")
        self.assertEqual(release[2]["cause"], "hold_complete")

    async def test_vacation_safe_off_and_manual_revoke_paths(self) -> None:
        coordinator, hass, clock = await self._coordinator()
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")
            await coordinator._cancel_active_locked(
                FAILURE_VACATION_BLOCKED,
                revoked=False,
                turn_off_owned=True,
            )
        self.assertEqual(
            hass.services.calls[-2][:2],
            ("presence_based_lighting", "release_control"),
        )
        self.assertEqual(hass.services.calls[-2][2]["outcome"], "failed")
        self.assertEqual(hass.services.calls[-2][2]["cause"], "owner_failed")
        self.assertEqual(hass.services.calls[-1][:2], ("light", "turn_off"))
        self.assertEqual(
            set(hass.services.calls[-1][2]["entity_id"]),
            set(_profile().target_light_entity_ids),
        )

        coordinator, hass, clock = await self._coordinator()
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")
            count_before = len(hass.services.calls)
            await coordinator._cancel_active_locked(
                FAILURE_MANUAL_REVOKE,
                revoked=True,
                turn_off_owned=False,
            )
        self.assertEqual(len(hass.services.calls), count_before)
        self.assertIsNone(coordinator.state.active_run)

    async def test_unknown_vacation_cancels_without_forcing_lights_off(self) -> None:
        coordinator, hass, clock = await self._coordinator()
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")
        hass.services.calls.clear()
        old_state = hass.states.values[coordinator.profile.vacation_entity_id]
        new_state = _State("unavailable")
        hass.states.values[coordinator.profile.vacation_entity_id] = new_state

        await coordinator._async_handle_state_event(
            coordinator_module.Event(
                {
                    "entity_id": coordinator.profile.vacation_entity_id,
                    "new_state": new_state,
                    "old_state": old_state,
                }
            ),
            observed_lease_id=coordinator.state.active_run.lease_id,
        )

        self.assertIsNone(coordinator.state.active_run)
        self.assertTrue(
            any(
                call[:2]
                == ("presence_based_lighting", "release_control")
                for call in hass.services.calls
            )
        )
        self.assertFalse(
            any(
                call[:2] == ("light", "turn_off")
                for call in hass.services.calls
            )
        )

    async def test_group_off_leaf_cascade_does_not_reacquire_retired_lease(
        self,
    ) -> None:
        coordinator, hass, clock = await self._coordinator()
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")
        run = coordinator.state.active_run
        assert run is not None
        leaf = run.target_entity_ids[0]
        old_leaf = _State("on", {"brightness": 3})
        hass.states.values[coordinator.profile.root_light_entity_id] = _State(
            "off"
        )
        hass.states.values[leaf] = _State("off")
        acquire_count = len(
            [
                call
                for call in hass.services.calls
                if call[:2]
                == ("presence_based_lighting", "acquire_control")
            ]
        )

        await coordinator._async_handle_state_event(
            coordinator_module.Event(
                {
                    "entity_id": leaf,
                    "new_state": hass.states.values[leaf],
                    "old_state": old_leaf,
                }
            ),
            observed_lease_id=coordinator.state.active_run.lease_id,
        )

        self.assertIsNone(coordinator.state.active_run)
        self.assertEqual(
            len(
                [
                    call
                    for call in hass.services.calls
                    if call[:2]
                    == ("presence_based_lighting", "acquire_control")
                ]
            ),
            acquire_count,
        )
        self.assertEqual(
            coordinator.state.failures,
            (),
        )
        self.assertEqual(
            coordinator.state.last_outcome,
            "cancelled_by_user",
        )
        self.assertIsNotNone(coordinator.state.last_cancellation)
        self.assertIsNotNone(
            coordinator.state.auto_relight_blocked_until
        )

    async def test_revoked_pbl_state_blocks_lagging_leaf_reacquire(self) -> None:
        coordinator, hass, clock = await self._coordinator()
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")
        run = coordinator.state.active_run
        assert run is not None
        leaf = run.target_entity_ids[0]
        old_leaf = _State("on", {"brightness": 3})
        hass.states.values[leaf] = _State("off")
        hass.states.values[
            coordinator.profile.pbl_switch_entity_id
        ].attributes["control_lease_state"] = "inactive"
        acquire_count = len(
            [
                call
                for call in hass.services.calls
                if call[:2]
                == ("presence_based_lighting", "acquire_control")
            ]
        )

        await coordinator._async_handle_state_event(
            coordinator_module.Event(
                {
                    "entity_id": leaf,
                    "new_state": hass.states.values[leaf],
                    "old_state": old_leaf,
                }
            ),
            observed_lease_id=coordinator.state.active_run.lease_id,
        )

        self.assertIsNone(coordinator.state.active_run)
        self.assertEqual(
            len(
                [
                    call
                    for call in hass.services.calls
                    if call[:2]
                    == ("presence_based_lighting", "acquire_control")
                ]
            ),
            acquire_count,
        )
    async def test_leaf_release_updates_same_lease_and_restart_reacquires_it(self) -> None:
        coordinator, hass, clock = await self._coordinator()
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")
            original_run = coordinator.state.active_run
            assert original_run is not None
            await coordinator._release_foreign_off_leaf_locked(
                "light.master_bedroom_window_light"
            )
        updated_run = coordinator.state.active_run
        assert updated_run is not None
        self.assertEqual(updated_run.lease_id, original_run.lease_id)
        latest_acquire = [
            call
            for call in hass.services.calls
            if call[:2]
            == ("presence_based_lighting", "acquire_control")
        ][-1]
        self.assertNotIn(
            "light.master_bedroom_window_light",
            latest_acquire[2]["target_entity_ids"],
        )

        recovered = WakeLightCoordinator(
            hass,
            _Entry(),
            _profile(),
        )
        recovered.state = replace(
            ProfileState.initial(_profile()),
            active_run=updated_run,
        )
        hass.states.values[_profile().root_light_entity_id] = _State(
            "on",
            {"brightness": 3},
        )
        for entity_id in updated_run.target_entity_ids:
            hass.states.values[entity_id] = _State("on", {"brightness": 3})
        hass.services.calls.clear()
        clock[0] += timedelta(minutes=1)
        coordinator_module.utc_now = lambda: clock[0]
        async with recovered._lock:
            await recovered._recover_locked(clock[0])
        recovery_acquire = hass.services.calls[0]
        self.assertEqual(
            recovery_acquire[:2],
            ("presence_based_lighting", "acquire_control"),
        )
        self.assertEqual(
            recovery_acquire[2]["lease_id"],
            original_run.lease_id,
        )
        self.assertNotIn(
            "light.master_bedroom_window_light",
            recovery_acquire[2]["target_entity_ids"],
        )

    async def test_recovery_waits_for_temporarily_missing_entities(self) -> None:
        coordinator, hass, clock = await self._coordinator()
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")
        persisted_run = coordinator.state.active_run
        assert persisted_run is not None
        hass.states.values[coordinator.profile.root_light_entity_id] = _State(
            "on",
            {"brightness": 3},
        )
        for entity_id in persisted_run.target_entity_ids:
            hass.states.values.pop(entity_id)

        recovered = WakeLightCoordinator(
            hass,
            _Entry(),
            coordinator.profile,
        )
        recovered.state = replace(
            ProfileState.initial(coordinator.profile),
            active_run=persisted_run,
        )
        async with recovered._lock:
            await recovered._recover_locked(clock[0])
        pending = recovered.state.active_run
        assert pending is not None
        self.assertEqual(pending.recovery_decision, "pending_reacquire")
        self.assertEqual(recovered.state.terminal_occurrence_ids, ())
        self.assertIsNotNone(recovered._blocked_retry_at)

        for entity_id in persisted_run.target_entity_ids:
            hass.states.values[entity_id] = _State(
                "on",
                {"brightness": 3},
            )
        clock[0] += timedelta(seconds=30)
        async with recovered._lock:
            await recovered._reconcile_locked(clock[0], "entities-ready")
        active = recovered.state.active_run
        assert active is not None
        self.assertEqual(
            active.recovery_decision,
            "resumed_within_catchup",
        )
        self.assertEqual(recovered.state.terminal_occurrence_ids, ())

    async def test_startup_defers_until_core_is_running(self) -> None:
        coordinator, hass, clock = await self._coordinator()
        hass.state = CoreState.starting
        coordinator.store._store.data = coordinator.state.to_dict()

        await coordinator.async_start()
        self.assertFalse(coordinator._started)
        self.assertIsNotNone(coordinator._started_unsubscribe)
        self.assertEqual(hass.services.calls, [])
        self.assertEqual(coordinator.sensor_model().state, "unavailable")

        hass.state = CoreState.running
        coordinator._handle_hass_started(coordinator_module.Event())
        for _attempt in range(10):
            if coordinator._started and hass.services.calls:
                break
            await asyncio.sleep(0)
        self.assertTrue(coordinator._started)
        self.assertIsNone(coordinator._started_unsubscribe)
        self.assertEqual(
            hass.services.calls[0][:2],
            ("presence_based_lighting", "acquire_control"),
        )

    async def test_recovery_retries_unknown_vacation_without_turning_lights_off(
        self,
    ) -> None:
        coordinator, hass, clock = await self._coordinator()
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")
        persisted_run = coordinator.state.active_run
        assert persisted_run is not None
        hass.states.values[coordinator.profile.root_light_entity_id] = _State(
            "on",
            {"brightness": 3},
        )
        for entity_id in persisted_run.target_entity_ids:
            hass.states.values[entity_id] = _State(
                "on",
                {"brightness": 3},
            )
        hass.states.values.pop(coordinator.profile.vacation_entity_id)
        hass.services.calls.clear()

        recovered = WakeLightCoordinator(
            hass,
            _Entry(),
            coordinator.profile,
        )
        recovered.state = replace(
            ProfileState.initial(coordinator.profile),
            active_run=persisted_run,
        )
        async with recovered._lock:
            await recovered._recover_locked(clock[0])
        pending = recovered.state.active_run
        assert pending is not None
        self.assertEqual(pending.recovery_decision, "pending_reacquire")
        self.assertFalse(
            any(
                call[:2] == ("light", "turn_off")
                for call in hass.services.calls
            )
        )
        self.assertEqual(recovered.state.terminal_occurrence_ids, ())

        hass.states.values[coordinator.profile.vacation_entity_id] = _State(
            "off"
        )
        clock[0] += timedelta(seconds=30)
        async with recovered._lock:
            await recovered._reconcile_locked(clock[0], "vacation-ready")
        active = recovered.state.active_run
        assert active is not None
        self.assertEqual(
            active.recovery_decision,
            "resumed_within_catchup",
        )

    async def test_overlap_and_completion_dispatch_with_latest_lease_generation(
        self,
    ) -> None:
        coordinator, hass, clock = await self._coordinator()
        coordinator.state = replace(
            coordinator.state,
            alarms=(
                _alarm_at(
                    clock[0],
                    alarm_id="first",
                    wake_offset_minutes=30,
                ),
                _alarm_at(
                    clock[0],
                    alarm_id="second",
                    wake_offset_minutes=60,
                ),
            ),
        )
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "first-start")

        clock[0] += timedelta(minutes=30)
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "second-start")
        active = coordinator.state.active_run
        assert active is not None
        self.assertEqual(active.generation, 2)
        self.assertEqual(len(active.occurrences), 2)
        latest_dispatch = [
            call
            for call in hass.services.calls
            if call[:2]
            == ("presence_based_lighting", "dispatch_control")
        ][-1]
        self.assertEqual(latest_dispatch[2]["expected_generation"], 2)
        self.assertEqual(
            latest_dispatch[2]["service_data"]["brightness_pct"],
            100.0,
        )

        clock[0] += timedelta(minutes=5)
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "first-complete")
        remaining = coordinator.state.active_run
        assert remaining is not None
        self.assertEqual(remaining.generation, 3)
        self.assertEqual(len(remaining.occurrences), 1)
        latest_dispatch = [
            call
            for call in hass.services.calls
            if call[:2]
            == ("presence_based_lighting", "dispatch_control")
        ][-1]
        self.assertEqual(latest_dispatch[2]["expected_generation"], 3)
        self.assertEqual(
            latest_dispatch[2]["service_data"]["brightness_pct"],
            100.0,
        )

    async def test_transient_overlap_update_failure_keeps_the_existing_run(
        self,
    ) -> None:
        coordinator, hass, clock = await self._coordinator()
        coordinator.state = replace(
            coordinator.state,
            alarms=(
                _alarm_at(
                    clock[0],
                    alarm_id="first",
                    wake_offset_minutes=30,
                ),
                _alarm_at(
                    clock[0],
                    alarm_id="second",
                    wake_offset_minutes=60,
                ),
            ),
        )
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "first-start")

        hass.services.deny_next_acquire = True
        clock[0] += timedelta(minutes=30)
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "merge-denied")
        active = coordinator.state.active_run
        assert active is not None
        self.assertEqual(active.generation, 1)
        self.assertEqual(len(active.occurrences), 1)
        self.assertEqual(
            coordinator.state.terminal_occurrence_ids,
            (),
        )

        clock[0] += timedelta(seconds=30)
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "merge-retry")
        active = coordinator.state.active_run
        assert active is not None
        self.assertEqual(active.generation, 2)
        self.assertEqual(len(active.occurrences), 2)

    async def test_manual_off_boundaries_fence_the_connected_alarm_chain(
        self,
    ) -> None:
        for mode in ("before_boundary", "at_boundary", "after_merge"):
            with self.subTest(mode=mode):
                coordinator, hass, clock = await self._coordinator()
                start = clock[0]
                coordinator.state = replace(
                    coordinator.state,
                    alarms=(
                        _alarm_at(
                            start,
                            alarm_id="first",
                            wake_offset_minutes=30,
                        ),
                        _alarm_at(
                            start,
                            alarm_id="second",
                            wake_offset_minutes=60,
                        ),
                        _alarm_at(
                            start,
                            alarm_id="third",
                            wake_offset_minutes=90,
                        ),
                    ),
                )
                async with coordinator._lock:
                    await coordinator._reconcile_locked(start, "first-start")

                if mode == "before_boundary":
                    clock[0] = start + timedelta(minutes=29, seconds=59)
                else:
                    clock[0] = start + timedelta(minutes=30)
                if mode == "after_merge":
                    async with coordinator._lock:
                        await coordinator._reconcile_locked(
                            clock[0],
                            "merge-second",
                        )
                    self.assertEqual(
                        len(coordinator.state.active_run.occurrences),
                        2,
                    )

                acquire_count = len(
                    [
                        call
                        for call in hass.services.calls
                        if call[:2]
                        == (
                            "presence_based_lighting",
                            "acquire_control",
                        )
                    ]
                )
                async with coordinator._lock:
                    await coordinator._cancel_active_locked(
                        "manual_group_off",
                        revoked=True,
                        turn_off_owned=False,
                    )

                expected_cutoff = start + timedelta(minutes=95)
                self.assertEqual(
                    coordinator.state.auto_relight_blocked_until,
                    expected_cutoff,
                )
                self.assertEqual(
                    coordinator.state.last_cancellation.occurrence_count,
                    3,
                )
                self.assertEqual(coordinator.state.failures, ())

                for offset in (30, 60, 95):
                    clock[0] = start + timedelta(minutes=offset)
                    async with coordinator._lock:
                        await coordinator._reconcile_locked(
                            clock[0],
                            "fence-check",
                        )
                    self.assertIsNone(coordinator.state.active_run)
                self.assertEqual(
                    len(
                        [
                            call
                            for call in hass.services.calls
                            if call[:2]
                            == (
                                "presence_based_lighting",
                                "acquire_control",
                            )
                        ]
                    ),
                    acquire_count,
                )

                clock[0] = expected_cutoff + timedelta(seconds=1)
                async with coordinator._lock:
                    await coordinator._reconcile_locked(
                        clock[0],
                        "fence-expired",
                    )
                self.assertIsNone(
                    coordinator.state.auto_relight_blocked_until
                )
                self.assertIsNone(coordinator.state.active_run)
                self.assertTrue(
                    all(not alarm.enabled for alarm in coordinator.state.alarms)
                )

    async def test_restart_during_relight_fence_remains_suppressed(self) -> None:
        coordinator, _hass, clock = await self._coordinator()
        start = clock[0]
        coordinator.state = replace(
            coordinator.state,
            alarms=(
                _alarm_at(
                    start,
                    alarm_id="first",
                    wake_offset_minutes=30,
                ),
                _alarm_at(
                    start,
                    alarm_id="second",
                    wake_offset_minutes=60,
                ),
            ),
        )
        async with coordinator._lock:
            await coordinator._reconcile_locked(start, "first-start")
            await coordinator._cancel_active_locked(
                "manual_group_off",
                revoked=True,
                turn_off_owned=False,
            )
        restored_state = ProfileState.from_dict(
            coordinator.state.to_dict(),
            coordinator.profile,
        )
        clock[0] = start + timedelta(minutes=45)
        hass = _Hass(_states(coordinator.profile), lambda: clock[0])
        recovered = WakeLightCoordinator(
            hass,
            _Entry(),
            coordinator.profile,
        )
        recovered.state = restored_state
        coordinator_module.utc_now = lambda: clock[0]

        async with recovered._lock:
            await recovered._reconcile_locked(clock[0], "restart-fence")

        self.assertIsNone(recovered.state.active_run)
        self.assertEqual(hass.services.calls, [])
        self.assertEqual(
            recovered.state.auto_relight_blocked_until,
            start + timedelta(minutes=65),
        )

    async def test_restart_detected_group_off_builds_the_full_fence(self) -> None:
        coordinator, _hass, clock = await self._coordinator()
        start = clock[0]
        coordinator.state = replace(
            coordinator.state,
            alarms=(
                _alarm_at(
                    start,
                    alarm_id="first",
                    wake_offset_minutes=30,
                ),
                _alarm_at(
                    start,
                    alarm_id="second",
                    wake_offset_minutes=60,
                ),
            ),
        )
        async with coordinator._lock:
            await coordinator._reconcile_locked(start, "first-start")
        persisted = coordinator.state.to_dict()
        hass = _Hass(_states(coordinator.profile), lambda: clock[0])
        recovered = WakeLightCoordinator(
            hass,
            _Entry(),
            coordinator.profile,
        )
        recovered.store._store.data = persisted
        coordinator_module.utc_now = lambda: clock[0]

        await recovered.async_start()

        self.assertIsNone(recovered.state.active_run)
        self.assertEqual(
            recovered.state.auto_relight_blocked_until,
            start + timedelta(minutes=65),
        )
        self.assertEqual(
            recovered.state.last_cancellation.occurrence_count,
            2,
        )
        self.assertEqual(recovered.state.failures, ())

    async def test_end_episode_releases_then_safely_turns_off_owned_targets(
        self,
    ) -> None:
        coordinator, hass, clock = await self._coordinator()
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")
        run = coordinator.state.active_run
        assert run is not None
        before = len(hass.services.calls)

        response = await coordinator.async_handle_command(
            {
                "profile_id": coordinator.profile.profile_id,
                "expected_revision": coordinator.state.revision,
                "request_id": "end-episode-1",
                "operation": "end_episode",
            }
        )

        self.assertEqual(response["outcome"], "accepted")
        calls = hass.services.calls[before:]
        release_index = next(
            index
            for index, call in enumerate(calls)
            if call[:2]
            == ("presence_based_lighting", "release_control")
        )
        off_index = next(
            index
            for index, call in enumerate(calls)
            if call[:2] == ("light", "turn_off")
        )
        self.assertLess(release_index, off_index)
        self.assertEqual(calls[release_index][2]["outcome"], "cancelled")
        self.assertEqual(
            calls[release_index][2]["cause"],
            "occurrence_cancelled",
        )
        self.assertEqual(
            set(calls[off_index][2]["entity_id"]),
            set(run.wake_owned_target_ids),
        )
        self.assertFalse(any(call[0] == "switch" for call in calls))
        self.assertIsNone(coordinator.state.active_run)
        self.assertEqual(len(coordinator.state.alarms), 1)
        self.assertFalse(coordinator.state.alarms[0].enabled)
        self.assertEqual(coordinator.state.failures, ())
        self.assertEqual(
            coordinator.state.last_outcome,
            "cancelled_by_user",
        )
        cancellation = coordinator.state.last_cancellation
        assert cancellation is not None
        self.assertEqual(cancellation.occurrence_count, 1)
        self.assertNotIn(
            run.occurrence_ids[0],
            repr(cancellation.to_dict()),
        )

    async def test_post_wake_end_episode_deletes_fired_one_time_alarm(
        self,
    ) -> None:
        coordinator, _hass, clock = await self._coordinator()
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")
            clock[0] += timedelta(minutes=30)
            await coordinator._reconcile_locked(clock[0], "wake-deadline")
        run = coordinator.state.active_run
        assert run is not None
        self.assertTrue(run.occurrences[0].final_dispatched)
        coordinator.state = replace(
            coordinator.state,
            alarms=(replace(coordinator.state.alarms[0], enabled=False),),
        )

        response = await coordinator.async_handle_command(
            {
                "profile_id": coordinator.profile.profile_id,
                "expected_revision": coordinator.state.revision,
                "request_id": "end-fired-one-time",
                "operation": "end_episode",
            }
        )

        self.assertEqual(response["outcome"], "accepted")
        self.assertEqual(coordinator.state.alarms, ())

    async def test_revoke_uses_previous_generation_and_user_causes_are_normal(
        self,
    ) -> None:
        coordinator, hass, clock = await self._coordinator()
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")
        run = coordinator.state.active_run
        assert run is not None
        hass.states.values[coordinator.profile.root_light_entity_id] = _State(
            "on",
            {"brightness": 3},
        )
        call_count = len(hass.services.calls)

        await coordinator._async_handle_pbl_revoke(
            coordinator_module.Event(
                {
                    "root_entity_id": coordinator.profile.root_light_entity_id,
                    "generation": run.generation,
                    "previous_generation": run.generation + 1,
                    "cause": "manual_brightness_control",
                }
            )
        )
        self.assertIsNotNone(coordinator.state.active_run)

        await coordinator._async_handle_pbl_revoke(
            coordinator_module.Event(
                {
                    "root_entity_id": coordinator.profile.root_light_entity_id,
                    "generation": run.generation + 1,
                    "previous_generation": run.generation,
                    "cause": "manual_brightness_control",
                }
            )
        )
        self.assertIsNone(coordinator.state.active_run)
        self.assertEqual(len(hass.services.calls), call_count)
        self.assertEqual(coordinator.state.failures, ())
        self.assertEqual(
            coordinator.state.last_outcome,
            "cancelled_by_user",
        )

        legacy, _legacy_hass, legacy_clock = await self._coordinator()
        async with legacy._lock:
            await legacy._reconcile_locked(
                legacy_clock[0],
                "legacy-start",
            )
        legacy_run = legacy.state.active_run
        assert legacy_run is not None
        await legacy._async_handle_pbl_revoke(
            coordinator_module.Event(
                {
                    "root_entity_id": legacy.profile.root_light_entity_id,
                    "generation": legacy_run.generation,
                    "cause": "manual_group_off",
                }
            )
        )
        self.assertIsNone(legacy.state.active_run)
        self.assertEqual(legacy.state.failures, ())

        diagnostic, _diagnostic_hass, diagnostic_clock = (
            await self._coordinator()
        )
        async with diagnostic._lock:
            await diagnostic._reconcile_locked(
                diagnostic_clock[0],
                "diagnostic-start",
            )
        diagnostic_run = diagnostic.state.active_run
        assert diagnostic_run is not None
        await diagnostic._async_handle_pbl_revoke(
            coordinator_module.Event(
                {
                    "root_entity_id": (
                        diagnostic.profile.root_light_entity_id
                    ),
                    "generation": diagnostic_run.generation,
                    "cause": "lease_expired",
                }
            )
        )
        self.assertEqual(
            diagnostic.state.failures[-1].code,
            "pbl_lease_revoked:lease_expired",
        )
        self.assertIsNone(diagnostic.state.last_cancellation)

    async def test_dispatch_revoke_race_waits_for_authoritative_user_event(
        self,
    ) -> None:
        for dispatch_response in (
            {"outcome": "revoked_in_flight"},
            {
                "outcome": "blocked",
                "blockers": ["inactive_or_mismatched_lease"],
            },
        ):
            with self.subTest(dispatch_response=dispatch_response):
                coordinator, hass, clock = await self._coordinator()
                async with coordinator._lock:
                    await coordinator._reconcile_locked(
                        clock[0],
                        "test-start",
                    )
                run = coordinator.state.active_run
                assert run is not None
                hass.services.next_dispatch_response = dispatch_response
                clock[0] += timedelta(seconds=30)

                async with coordinator._lock:
                    await coordinator._reconcile_locked(
                        clock[0],
                        "dispatch-race",
                    )

                self.assertIsNotNone(coordinator.state.active_run)
                self.assertEqual(coordinator.state.failures, ())
                await coordinator._async_handle_pbl_revoke(
                    coordinator_module.Event(
                        {
                            "root_entity_id": (
                                coordinator.profile.root_light_entity_id
                            ),
                            "generation": run.generation + 1,
                            "previous_generation": run.generation,
                            "cause": "manual_group_off",
                        }
                    )
                )

                self.assertIsNone(coordinator.state.active_run)
                self.assertEqual(coordinator.state.failures, ())
                self.assertEqual(
                    coordinator.state.last_outcome,
                    "cancelled_by_user",
                )
                self.assertIsNotNone(
                    coordinator.state.auto_relight_blocked_until
                )
                self.assertIsNotNone(coordinator.state.last_cancellation)

    async def test_unmatched_dispatch_revoke_wait_is_bounded(
        self,
    ) -> None:
        coordinator, hass, clock = await self._coordinator()
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")

        dispatch_count = len(
            [
                call
                for call in hass.services.calls
                if call[:2]
                == ("presence_based_lighting", "dispatch_control")
            ]
        )
        blocked = {
            "outcome": "blocked",
            "blockers": ["inactive_or_mismatched_lease"],
        }
        clock[0] += timedelta(seconds=30)
        hass.services.next_dispatch_response = blocked
        async with coordinator._lock:
            await coordinator._reconcile_locked(
                clock[0],
                "dispatch-race",
            )

        active = coordinator.state.active_run
        assert active is not None
        self.assertEqual(
            active.next_deadline,
            clock[0] + timedelta(seconds=30),
        )
        self.assertEqual(coordinator.state.failures, ())

        hass.services.next_dispatch_response = blocked
        for second in range(1, 31):
            clock[0] += timedelta(seconds=1)
            async with coordinator._lock:
                await coordinator._reconcile_locked(
                    clock[0],
                    f"bounded-retry-{second}",
                )
        self.assertIsNotNone(coordinator.state.active_run)
        self.assertEqual(coordinator.state.failures, ())
        self.assertEqual(
            len(
                [
                    call
                    for call in hass.services.calls
                    if call[:2]
                    == (
                        "presence_based_lighting",
                        "dispatch_control",
                    )
                ]
            ),
            dispatch_count + 2,
        )

        hass.services.next_dispatch_response = blocked
        for second in range(1, 31):
            clock[0] += timedelta(seconds=1)
            async with coordinator._lock:
                await coordinator._reconcile_locked(
                    clock[0],
                    f"revoke-wait-expiry-{second}",
                )
        self.assertIsNone(coordinator.state.active_run)
        self.assertEqual(
            coordinator.state.failures[-1].code,
            "pbl_dispatch_failed",
        )
        self.assertEqual(
            len(
                [
                    call
                    for call in hass.services.calls
                    if call[:2]
                    == (
                        "presence_based_lighting",
                        "dispatch_control",
                    )
                ]
            ),
            dispatch_count + 3,
        )

    async def test_revoke_wait_throttles_first_and_final_dispatches(
        self,
    ) -> None:
        blocked = {
            "outcome": "blocked",
            "blockers": ["inactive_or_mismatched_lease"],
        }
        for mode in ("first", "final"):
            with self.subTest(mode=mode):
                coordinator, hass, clock = await self._coordinator()
                if mode == "first":
                    hass.services.next_dispatch_response = blocked
                    async with coordinator._lock:
                        await coordinator._reconcile_locked(
                            clock[0],
                            "first-dispatch",
                        )
                    initial_dispatches = 1
                else:
                    async with coordinator._lock:
                        await coordinator._reconcile_locked(
                            clock[0],
                            "test-start",
                        )
                    clock[0] += timedelta(minutes=30)
                    hass.services.next_dispatch_response = blocked
                    async with coordinator._lock:
                        await coordinator._reconcile_locked(
                            clock[0],
                            "final-dispatch",
                        )
                    initial_dispatches = 2

                hass.services.next_dispatch_response = blocked
                for second in range(1, 31):
                    clock[0] += timedelta(seconds=1)
                    async with coordinator._lock:
                        await coordinator._reconcile_locked(
                            clock[0],
                            f"{mode}-grace-1-{second}",
                        )
                self.assertIsNotNone(coordinator.state.active_run)
                dispatches = [
                    call
                    for call in hass.services.calls
                    if call[:2]
                    == (
                        "presence_based_lighting",
                        "dispatch_control",
                    )
                ]
                self.assertEqual(
                    len(dispatches),
                    initial_dispatches + 1,
                )

                hass.services.next_dispatch_response = blocked
                for second in range(1, 31):
                    clock[0] += timedelta(seconds=1)
                    async with coordinator._lock:
                        await coordinator._reconcile_locked(
                            clock[0],
                            f"{mode}-grace-2-{second}",
                        )
                self.assertIsNone(coordinator.state.active_run)
                dispatches = [
                    call
                    for call in hass.services.calls
                    if call[:2]
                    == (
                        "presence_based_lighting",
                        "dispatch_control",
                    )
                ]
                self.assertEqual(
                    len(dispatches),
                    initial_dispatches + 2,
                )
                self.assertEqual(
                    coordinator.state.failures[-1].code,
                    "pbl_dispatch_failed",
                )

    async def test_legacy_snapshot_does_not_swallow_a_new_correlated_stop(
        self,
    ) -> None:
        coordinator, _hass, clock = await self._coordinator()
        source_ref = "sleepypod:right"
        occurrence = coordinator_module.ScheduledOccurrence(
            occurrence_id="wake-current",
            alarm_id="source-current",
            source_schedule_id=101,
            source="sleepypod",
            source_ref=source_ref,
            wake_at=clock[0],
            ramp_start_at=clock[0] - timedelta(minutes=30),
            ramp_minutes=30,
            hold_minutes=5,
        )
        run = coordinator_module.start_run(
            coordinator.profile,
            (occurrence,),
            now=clock[0],
            observed_floor_pct=1,
        )
        coordinator.state = replace(
            coordinator.state,
            active_run=run,
            source_cache={
                source_ref: SourceSnapshot(
                    lifecycle_state="idle",
                    terminal_reason="stopped",
                )
            },
        )
        async with coordinator._lock:
            self.assertTrue(
                await coordinator._acquire_locked(run, recovery=False)
            )
            await coordinator._handle_source_lifecycle_locked(
                source_ref,
                "idle",
                clock[0],
                attributes={
                    "terminal_reason": "stopped",
                    "occurrence_id": "pod-stale",
                },
            )

        self.assertIsNotNone(coordinator.state.active_run)
        self.assertEqual(
            coordinator.state.source_cache[
                source_ref
            ].last_stopped_occurrence_id,
            None,
        )

        async with coordinator._lock:
            await coordinator._handle_source_lifecycle_locked(
                source_ref,
                "idle",
                clock[0] + timedelta(seconds=1),
                attributes={
                    "terminal_reason": "stopped",
                    "occurrence_id": "pod-current",
                    "schedule_id": 101,
                    "scheduled_for": clock[0].timestamp(),
                    "ts": (clock[0] + timedelta(seconds=1)).timestamp() * 1000,
                },
            )
        self.assertIsNone(coordinator.state.active_run)

    async def test_sleepypod_lifecycle_only_changes_due_occurrences(self) -> None:
        coordinator, hass, clock = await self._coordinator()
        source_ref = "sleepypod:right"
        first = coordinator_module.ScheduledOccurrence(
            occurrence_id="source-first",
            alarm_id="source-alarm-first",
            source_schedule_id=101,
            source="sleepypod",
            source_ref=source_ref,
            wake_at=clock[0],
            ramp_start_at=clock[0] - timedelta(minutes=30),
            ramp_minutes=30,
            hold_minutes=5,
        )
        second = coordinator_module.ScheduledOccurrence(
            occurrence_id="source-second",
            alarm_id="source-alarm-second",
            source_schedule_id=102,
            source="sleepypod",
            source_ref=source_ref,
            wake_at=clock[0] + timedelta(minutes=30),
            ramp_start_at=clock[0],
            ramp_minutes=30,
            hold_minutes=5,
        )
        run = coordinator_module.start_run(
            coordinator.profile,
            (first, second),
            now=clock[0],
            observed_floor_pct=1,
        )
        coordinator.state = replace(coordinator.state, active_run=run)
        async with coordinator._lock:
            self.assertTrue(
                await coordinator._acquire_locked(run, recovery=False)
            )
            await coordinator._handle_source_lifecycle_locked(
                source_ref,
                "snoozed",
                clock[0],
                attributes={
                    "occurrence_id": "pod-first",
                    "schedule_id": 101,
                    "scheduled_for": clock[0].timestamp(),
                    "ts": clock[0].timestamp() * 1000,
                    "snoozed_until": (clock[0] + timedelta(minutes=5)).timestamp(),
                },
            )
        active = coordinator.state.active_run
        assert active is not None
        first_active, second_active = active.occurrences
        self.assertIsNotNone(first_active.snoozed_until)
        self.assertIsNone(second_active.snoozed_until)
        self.assertEqual(
            second_active.schedule.wake_at,
            clock[0] + timedelta(minutes=30),
        )

        async with coordinator._lock:
            await coordinator._handle_source_lifecycle_locked(
                source_ref,
                "idle",
                clock[0] + timedelta(seconds=1),
                attributes={
                    "terminal_reason": "expired",
                    "occurrence_id": "pod-first",
                },
            )
        active = coordinator.state.active_run
        assert active is not None
        self.assertEqual(
            active.occurrence_ids,
            ("source-first", "source-second"),
        )

        async with coordinator._lock:
            await coordinator._handle_source_lifecycle_locked(
                source_ref,
                "idle",
                clock[0] + timedelta(seconds=2),
            )
        active = coordinator.state.active_run
        assert active is not None
        self.assertEqual(
            active.occurrence_ids,
            ("source-first", "source-second"),
        )

        async with coordinator._lock:
            await coordinator._handle_source_lifecycle_locked(
                source_ref,
                "idle",
                clock[0] + timedelta(seconds=3),
                attributes={
                    "terminal_reason": "stopped",
                    "occurrence_id": "pod-first",
                    "schedule_id": 101,
                    "scheduled_for": clock[0].timestamp(),
                    "ts": (clock[0] + timedelta(seconds=3)).timestamp() * 1000,
                },
            )
        active = coordinator.state.active_run
        assert active is not None
        self.assertEqual(active.occurrence_ids, ("source-second",))

        async with coordinator._lock:
            await coordinator._handle_source_lifecycle_locked(
                source_ref,
                "idle",
                clock[0] + timedelta(minutes=30),
                attributes={
                    "terminal_reason": "stopped",
                    "occurrence_id": "pod-first",
                },
            )
        self.assertIsNotNone(coordinator.state.active_run)

        async with coordinator._lock:
            await coordinator._handle_source_lifecycle_locked(
                source_ref,
                "idle",
                clock[0] + timedelta(minutes=30, seconds=1),
                attributes={
                    "terminal_reason": "stopped",
                    "occurrence_id": "pod-second",
                    "schedule_id": 102,
                    "scheduled_for": (clock[0] + timedelta(minutes=30)).timestamp(),
                    "ts": (clock[0] + timedelta(minutes=30, seconds=1)).timestamp() * 1000,
                },
            )
        self.assertIsNone(coordinator.state.active_run)

    async def test_unavailable_source_is_retained_but_does_not_start(self) -> None:
        now = datetime(2030, 1, 1, 6, 0, tzinfo=UTC)
        wake_profile = replace(
            _profile(),
            sleepypod_schedule_entity_id="sensor.sleepypod_schedules",
            sleepypod_source_sides=("left",),
        )
        hass = _Hass(_states(wake_profile), lambda: now)
        coordinator = WakeLightCoordinator(hass, _Entry(), wake_profile)
        wake = now + timedelta(minutes=30)
        source_alarm = WakeLightAlarm(
            id="source-once",
            label="Source Once",
            kind="once",
            local_time=wake.strftime("%H:%M"),
            ramp_minutes=30,
            date=wake.date().isoformat(),
            source="sleepypod",
            source_ref="sleepypod:left",
        )
        coordinator.state = replace(
            ProfileState.initial(wake_profile),
            source_cache={
                "sleepypod:left": SourceSnapshot(
                    alarms=(source_alarm,),
                    available=False,
                    last_success_at=now - timedelta(minutes=1),
                    last_observed_at=now,
                    failure_code="source_unavailable",
                )
            },
        )
        coordinator_module.utc_now = lambda: now
        async with coordinator._lock:
            await coordinator._reconcile_locked(now, "source-unavailable")
        self.assertEqual(hass.services.calls, [])
        self.assertIsNone(coordinator.state.active_run)
        self.assertIsNotNone(coordinator._blocked_retry_at)
        self.assertIn(
            "source_unavailable",
            [failure.code for failure in coordinator.state.failures],
        )

    async def test_zero_ramp_dispatches_full_brightness_only_at_wake_time(self) -> None:
        coordinator, hass, clock = await self._coordinator()
        coordinator.state = replace(
            coordinator.state,
            alarms=(replace(coordinator.state.alarms[0], ramp_minutes=0),),
        )
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "test-start")
        self.assertFalse(any(
            call[:2] == ("presence_based_lighting", "dispatch_control")
            for call in hass.services.calls
        ))
        clock[0] += timedelta(minutes=30)
        async with coordinator._lock:
            await coordinator._reconcile_locked(clock[0], "wake-deadline")
        dispatches = [
            call
            for call in hass.services.calls
            if call[:2]
            == ("presence_based_lighting", "dispatch_control")
        ]
        self.assertEqual(
            dispatches[-1][2]["service_data"],
            {"brightness_pct": 100.0, "transition": 0.0},
        )


if __name__ == "__main__":
    unittest.main()
