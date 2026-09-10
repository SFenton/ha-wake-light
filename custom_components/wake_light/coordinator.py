"""Home Assistant runtime coordinator for one Wake Light room profile."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
import json
import logging
import math
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .bed_schedule import (
    add_temporary_alarm_payload,
    bind_temporary_alarm_id,
    execution_weekday,
    remove_temporary_alarm_payload,
)
from .commands import CommandEffect, apply_command
from .const import (
    DEFAULT_MISSED_ALARM_CATCHUP_SECONDS,
    DEFAULT_RAMP_STEP_SECONDS,
    DOMAIN,
    EVENT_WAKE_LIGHT_OUTCOME,
    FAILURE_BLOCKER_NOT_OFF,
    FAILURE_MISSED,
    FAILURE_NO_TARGETS,
    FAILURE_OCCURRENCE_LIMIT,
    FAILURE_PBL_ACQUIRE,
    FAILURE_PBL_DISPATCH,
    FAILURE_PBL_NOT_READY,
    FAILURE_PBL_RELEASE,
    FAILURE_RECOVERY_EXPIRED,
    FAILURE_SOURCE_UNAVAILABLE,
    FAILURE_TARGET_UNAVAILABLE,
    FAILURE_VACATION_BLOCKED,
    GROUP_OFF_SETTLE_SECONDS,
    MIN_BRIGHTNESS_SETTLE_SECONDS,
    MAX_ACTIVE_OCCURRENCES,
    OUTCOME_ACCEPTED,
    OUTCOME_CANCELLED_BY_USER,
    PBL_ACQUIRE_SUCCESS_OUTCOMES,
    PBL_DISPATCH_SUCCESS_OUTCOMES,
    PBL_DOMAIN,
    PBL_EVENT_LEASE_REVOKED,
    PBL_OWNER,
    PBL_RELEASE_CAUSE_HOLD_COMPLETE,
    PBL_RELEASE_CAUSE_EXTERNAL_OFF,
    PBL_RELEASE_CAUSE_OCCURRENCE_CANCELLED,
    PBL_RELEASE_CAUSE_OWNER_FAILED,
    PBL_RELEASE_CAUSE_OWNER_SHUTDOWN,
    PBL_RELEASE_OUTCOME_CANCELLED,
    PBL_RELEASE_OUTCOME_COMPLETED,
    PBL_RELEASE_OUTCOME_FAILED,
    PBL_RELEASE_SUCCESS_OUTCOMES,
    PBL_SERVICE_ACQUIRE,
    PBL_SERVICE_DISPATCH,
    PBL_SERVICE_RELEASE,
    RECOVERY_RETRY_BUDGET_SECONDS,
    SOURCE_REF_PREFIX,
    TEMPORARY_BED_ALARM_CLEANUP_GRACE_SECONDS,
    TEMPORARY_BED_ALARM_RETRY_SECONDS,
    UNAVAILABLE_STATES,
    USER_CANCELLATION_CAUSES,
)
from .engine import (
    PreflightInputs,
    PreflightResult,
    cancellation_decision,
    connected_episode,
    evaluate_preflight,
    fixed_lease_ttl_seconds,
    mark_acquired,
    mark_dispatched,
    mark_hold_dispatched,
    merge_occurrences,
    observe_brighter_root,
    observed_brightness_pct,
    release_leaf,
    refresh_recovery_holds,
    remove_completed,
    remove_occurrence,
    restart_decision,
    run_view,
    snooze_source_occurrence,
    start_run,
)
from .model import (
    ActiveRun,
    ProfileState,
    ScheduledOccurrence,
    SourceSnapshot,
    TemporaryBedAlarm,
    WakeLightAlarm,
    WakeLightProfile,
    opaque_ref,
    parse_datetime,
    utc_now,
)
from .read_model import SensorReadModel, build_sensor_read_model
from .scheduler import (
    calendar_windows,
    expand_relight_fence,
    normalize_source_lifecycle,
    normalize_source_terminal_reason,
    refresh_source_snapshot,
    resolve_profile_occurrences,
    resolve_wall_datetime,
    runnable_alarms,
    stale_once_alarm_ids,
    source_event_matches,
    source_timestamp,
    terminal_once_alarm_ids,
    unsupported_episode_ids,
)
from .store import WakeLightStore

_LOGGER = logging.getLogger(__package__)


class WakeLightCoordinator(DataUpdateCoordinator[SensorReadModel]):
    """Own scheduling, one occurrence set, and one root PBL lease."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: Any,
        profile: WakeLightProfile,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}:{profile.profile_id}",
            update_interval=None,
        )
        self.entry = entry
        self.profile = profile
        self.store = WakeLightStore(hass, entry.entry_id, profile)
        self.state = ProfileState.initial(profile)
        self._timezone = ZoneInfo(hass.config.time_zone)
        self._scheduled: tuple[ScheduledOccurrence, ...] = ()
        self._timer_cancel: Any = None
        self._started_unsubscribe: Any = None
        self._unsubscribers: list[Any] = []
        self._lock = asyncio.Lock()
        self._last_saved_payload: dict[str, Any] | None = None
        self._recovery_retry_deadline: datetime | None = None
        self._started = getattr(hass, "state", None) is CoreState.running
        self._stopping = False
        self._blocked_retry_at: datetime | None = None
        self._brightness_observation_blocked_until: datetime | None = None
        self._dispatch_revoke_wait_until: datetime | None = None
        self._observed_foreign_off: dict[str, str] = {}

    async def async_start(self) -> None:
        """Load state, attach listeners, recover, and schedule work."""
        self.state = await self.store.async_load()
        self._last_saved_payload = self.state.to_dict()
        self._started = self.hass.state is CoreState.running
        self._attach_listeners()
        if not self._started:
            self._started_unsubscribe = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED,
                self._handle_hass_started,
            )
            self.async_set_updated_data(self._build_read_model(utc_now()))
            return
        async with self._lock:
            now = utc_now()
            self._refresh_source_cache_locked(now)
            self._scheduled = self._calculate_occurrences_locked(now)
            await self._recover_locked(now)
            await self._reconcile_locked(now, "startup")

    async def async_shutdown(self) -> None:
        """Unload a profile without converting HA shutdown into a missed alarm."""
        self._cancel_timer()
        if self._started_unsubscribe is not None:
            self._started_unsubscribe()
            self._started_unsubscribe = None
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        async with self._lock:
            if self.state.active_run is not None and not self._stopping:
                old_run = self.state.active_run
                await self._release_locked(
                    old_run,
                    outcome=PBL_RELEASE_OUTCOME_CANCELLED,
                    cause=PBL_RELEASE_CAUSE_OWNER_SHUTDOWN,
                )
                self.state = replace(self.state, active_run=None)
                self.state = self.state.remember_terminal_occurrences(
                    old_run.occurrence_ids
                )
            await self.store.async_save(self.state)
            self._last_saved_payload = self.state.to_dict()
        await super().async_shutdown()

    async def async_handle_command(
        self,
        command: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Apply the public command service and its lease-side effect."""
        async with self._lock:
            command_now = utc_now()
            previous_state = self.state
            operation = command.get("operation")
            previous_alarm = next(
                (
                    alarm for alarm in previous_state.alarms
                    if alarm.id == (
                        command.get("alarm", {}).get("id")
                        if isinstance(command.get("alarm"), Mapping)
                        else command.get("alarm_id")
                    )
                ),
                None,
            )
            decision = apply_command(
                self.state,
                self.profile,
                command,
                now=command_now,
                timezone=self._timezone,
            )
            self.state = decision.state
            changed = self.state.revision != previous_state.revision
            if operation == "update_defaults":
                self._refresh_source_cache_locked(command_now)
            if changed and operation in {"upsert_alarm", "delete_alarm"}:
                current_alarm = None
                if operation == "upsert_alarm":
                    current_alarm = next(
                        (
                            alarm for alarm in self.state.alarms
                            if isinstance(command.get("alarm"), Mapping)
                            and alarm.id == command["alarm"].get("id")
                        ),
                        None,
                    )
                    if current_alarm is not None:
                        try:
                            self._validate_temporary_bed_alarm_locked(
                                current_alarm
                            )
                        except ValueError as err:
                            self.state = previous_state
                            return {
                                **decision.response,
                                "outcome": "invalid_request",
                                "error": str(err),
                            }
                alarm_id = previous_alarm.id if previous_alarm is not None else None
                bed_schedule_attributes = self._bed_schedule_attributes_locked()
                if alarm_id is not None:
                    _, bed_schedule_attributes = (
                        await self._cleanup_temporary_bed_alarms_from_attributes_locked(
                            command_now,
                            alarm_id=alarm_id,
                            force=True,
                            attributes=bed_schedule_attributes,
                        )
                    )
                if current_alarm is not None:
                    await self._provision_temporary_bed_alarms_locked(
                        current_alarm, command_now,
                        attributes=bed_schedule_attributes,
                    )
            if decision.effect is not None:
                await self._apply_command_effect_locked(
                    decision.effect,
                    command_now,
                )
            await self._reconcile_locked(command_now, "command")
            if decision.response.get("idempotent"):
                return decision.response
            response = {**decision.response, "revision": self.state.revision}
            if isinstance(command.get("request_id"), str):
                self.state = self.state.remember_request(
                    command["request_id"],
                    {key: value for key, value in command.items() if key != "request_id"},
                    {key: value for key, value in response.items() if key != "request_id"},
                    at=command_now,
                )
                await self._finish_locked(command_now)
            return response

    def sensor_model(self) -> SensorReadModel:
        """Return the latest published sensor model."""
        return self.data or self._build_read_model(utc_now())

    @property
    def source_freshness(self) -> Mapping[str, datetime | None]:
        """Expose source freshness for focused runtime tests/diagnostics."""
        return {
            source_ref: snapshot.last_success_at
            for source_ref, snapshot in self.state.source_cache.items()
        }

    def _attach_listeners(self) -> None:
        entity_ids = {
            self.profile.root_light_entity_id,
            self.profile.pbl_switch_entity_id,
            self.profile.vacation_entity_id,
            *self.profile.target_light_entity_ids,
            *self.profile.blocker_entity_ids,
            *self.profile.blocker_entity_ids,
            *self.profile.source_state_entity_ids.values(),
        }
        if self.profile.occupancy_entity_id:
            entity_ids.add(self.profile.occupancy_entity_id)
        if self.profile.sleepypod_schedule_entity_id:
            entity_ids.add(self.profile.sleepypod_schedule_entity_id)
        self._unsubscribers.append(
            async_track_state_change_event(
                self.hass,
                sorted(entity_ids),
                self._handle_state_event,
            )
        )
        self._unsubscribers.append(
            self.hass.bus.async_listen(
                PBL_EVENT_LEASE_REVOKED,
                self._handle_pbl_revoke_event,
            )
        )
        self._unsubscribers.append(
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP,
                self._handle_hass_stop,
            )
        )

    @callback
    def _handle_hass_stop(self, _event: Event) -> None:
        self._stopping = True
        self._cancel_timer()
        self.hass.async_create_task(self._async_persist_stop())

    @callback
    def _handle_hass_started(self, _event: Event) -> None:
        self._started_unsubscribe = None
        self._create_entry_task(
            self._async_handle_hass_started(),
            "wake_light_started",
        )

    async def _async_handle_hass_started(self) -> None:
        async with self._lock:
            if self._stopping:
                return
            self._started = True
            now = utc_now()
            self._refresh_source_cache_locked(now)
            self._scheduled = self._calculate_occurrences_locked(now)
            await self._recover_locked(now)
            await self._reconcile_locked(now, "home_assistant_started")

    async def _async_persist_stop(self) -> None:
        async with self._lock:
            await self.store.async_save(self.state)
            self._last_saved_payload = self.state.to_dict()

    @callback
    def _handle_state_event(self, event: Event) -> None:
        run = self.state.active_run
        entity_id = event.data.get("entity_id")
        if run is not None and entity_id in run.target_entity_ids:
            if (
                getattr(event.data.get("old_state"), "state", None) == "on"
                and getattr(event.data.get("new_state"), "state", None) == "off"
            ):
                self._observed_foreign_off[entity_id] = run.lease_id
        self._create_entry_task(
            self._async_handle_state_event(
                event,
                observed_lease_id=run.lease_id if run is not None else None,
            ),
            "wake_light_state_event",
        )

    @callback
    def _handle_pbl_revoke_event(self, event: Event) -> None:
        if event.data.get("root_entity_id") != self.profile.root_light_entity_id:
            return
        self._create_entry_task(
            self._async_handle_pbl_revoke(event),
            "wake_light_pbl_revoke",
        )

    def _create_entry_task(
        self,
        coroutine,
        name: str,
    ) -> None:
        create_background_task = getattr(
            self.entry,
            "async_create_background_task",
            None,
        )
        if create_background_task is not None:
            create_background_task(self.hass, coroutine, name)
            return
        self.hass.async_create_task(coroutine)

    async def _async_handle_pbl_revoke(self, event: Event) -> None:
        if not self._started or self._stopping:
            return
        async with self._lock:
            run = self.state.active_run
            if run is None:
                return
            if "previous_generation" in event.data:
                generation = event.data.get("previous_generation")
            else:
                generation = event.data.get("generation")
            if (
                isinstance(generation, bool)
                or not isinstance(generation, int)
                or run.generation is None
                or generation != run.generation
            ):
                return
            self._dispatch_revoke_wait_until = None
            cause = str(event.data.get("cause", "unknown"))[:64]
            reason = (
                cause
                if cause in USER_CANCELLATION_CAUSES
                else f"pbl_lease_revoked:{cause}"
            )
            await self._cancel_active_locked(
                reason,
                revoked=True,
                turn_off_owned=False,
            )
            await self._finish_locked(utc_now())

    async def _async_handle_state_event(
        self, event: Event, *, observed_lease_id: str | None,
    ) -> None:
        if not self._started or self._stopping:
            return
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if not isinstance(entity_id, str):
            return
        async with self._lock:
            now = utc_now()
            if entity_id == self.profile.sleepypod_schedule_entity_id:
                self._refresh_source_cache_locked(now)
            source_ref = next(
                (
                    ref
                    for ref, configured_entity_id
                    in self.profile.source_state_entity_ids.items()
                    if configured_entity_id == entity_id
                ),
                None,
            )
            if source_ref is not None:
                await self._handle_source_lifecycle_locked(
                    source_ref,
                    getattr(new_state, "state", None),
                    now,
                    attributes=(
                        new_state.attributes
                        if new_state is not None
                        and isinstance(new_state.attributes, Mapping)
                        else None
                    ),
                )

            run = self.state.active_run
            new_value = getattr(new_state, "state", "missing")
            old_value = getattr(old_state, "state", "missing")
            if (
                run is not None
                and entity_id == self.profile.root_light_entity_id
                and new_value == "off"
                and old_value != "off"
            ):
                await self._cancel_active_locked(
                    "manual_group_off",
                    revoked=True,
                    turn_off_owned=False,
                )
            elif (
                run is not None
                and entity_id == self.profile.root_light_entity_id
                and new_value == "on"
            ):
                self.state = replace(
                    self.state,
                    active_run=self._observe_target_floor_if_settled(
                        run,
                        now,
                    ),
                )
            elif (
                run is not None
                and entity_id in run.target_entity_ids
                and new_value == "on"
            ):
                self.state = replace(
                    self.state,
                    active_run=self._observe_target_floor_if_settled(
                        run,
                        now,
                    ),
                )
            elif (
                run is not None
                and observed_lease_id == run.lease_id
                and entity_id in run.target_entity_ids
                and new_value == "off"
                and old_value == "on"
            ):
                await asyncio.sleep(GROUP_OFF_SETTLE_SECONDS)
                if self.state.active_run is not None:
                    if (
                        self._entity_state(
                            self.profile.root_light_entity_id
                        )
                        == "off"
                        or self._pbl_control_lease_state() != "active"
                    ):
                        await self._cancel_active_locked(
                            "manual_group_off",
                            revoked=True,
                            turn_off_owned=False,
                        )
                    else:
                        await self._release_foreign_off_leaf_locked(
                            entity_id
                        )
            elif (
                run is not None
                and entity_id == self.profile.vacation_entity_id
                and new_value != "off"
            ):
                await self._cancel_active_locked(
                    FAILURE_VACATION_BLOCKED,
                    revoked=False,
                    turn_off_owned=self._vacation_requires_safe_off(
                        new_value
                    ),
                )
            elif run is not None and (
                (
                    entity_id == self.profile.pbl_switch_entity_id
                    and self._pbl_preflight_state() != "on"
                )
                or (
                    entity_id in self.profile.blocker_entity_ids
                    and new_value != "off"
                )
                or (
                    entity_id == self.profile.root_light_entity_id
                    and new_value in {*UNAVAILABLE_STATES, "missing"}
                )
                or (
                    entity_id in run.target_entity_ids
                    and new_value in {*UNAVAILABLE_STATES, "missing"}
                )
            ):
                reason = (
                    FAILURE_PBL_NOT_READY
                    if entity_id == self.profile.pbl_switch_entity_id
                    else FAILURE_BLOCKER_NOT_OFF
                    if entity_id in self.profile.blocker_entity_ids
                    else FAILURE_TARGET_UNAVAILABLE
                )
                await self._cancel_active_locked(
                    reason,
                    revoked=False,
                    turn_off_owned=False,
                )
            await self._reconcile_locked(now, f"state:{entity_id}")

    def _refresh_source_cache_locked(self, now: datetime) -> None:
        if not self.profile.sleepypod_schedule_entity_id:
            return
        source_state = self.hass.states.get(
            self.profile.sleepypod_schedule_entity_id
        )
        available = bool(
            source_state
            and source_state.state not in UNAVAILABLE_STATES
        )
        attributes = (
            source_state.attributes
            if source_state is not None
            and isinstance(source_state.attributes, Mapping)
            else None
        )
        cache = dict(self.state.source_cache)
        for source_ref in self.profile.source_refs:
            cache[source_ref] = refresh_source_snapshot(
                cache.get(source_ref, SourceSnapshot()),
                source_ref,
                attributes=attributes,
                available=available,
                now=now,
                defaults=self.state.defaults,
            )
        self.state = replace(self.state, source_cache=cache)
        self._bind_temporary_bed_alarm_ids_locked(attributes)

    def _bed_schedule_attributes_locked(self) -> Mapping[str, Any] | None:
        entity_id = self.profile.sleepypod_schedule_entity_id
        source_state = self.hass.states.get(entity_id) if entity_id else None
        if (
            source_state is None
            or source_state.state in UNAVAILABLE_STATES
            or not isinstance(source_state.attributes, Mapping)
        ):
            return None
        return source_state.attributes

    def _bind_temporary_bed_alarm_ids_locked(
        self, attributes: Mapping[str, Any] | None,
    ) -> None:
        if attributes is None or not self.state.temporary_bed_alarms:
            return
        changed = False
        records = []
        for record in self.state.temporary_bed_alarms:
            if record.schedule_id is not None:
                records.append(record)
                continue
            schedule_id = bind_temporary_alarm_id(
                attributes,
                record.side,
                record.weekday,
                record.local_time,
                record.baseline_schedule_ids,
            )
            if schedule_id is None:
                records.append(record)
                continue
            records.append(replace(record, schedule_id=schedule_id))
            changed = True
        if changed:
            self.state = replace(self.state, temporary_bed_alarms=tuple(records))

    async def _publish_bed_schedule_locked(
        self, payload: Mapping[str, Any],
    ) -> None:
        await self.hass.services.async_call(
            "mqtt",
            "publish",
            {
                "topic": self.profile.sleepypod_schedule_set_topic,
                "payload": json.dumps(payload, separators=(",", ":")),
            },
            blocking=True,
        )

    async def _provision_temporary_bed_alarms_locked(
        self,
        alarm: WakeLightAlarm,
        now: datetime,
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        if not alarm.enabled or not alarm.bed_sides or alarm.date is None:
            return
        if attributes is None:
            self._validate_temporary_bed_alarm_locked(alarm)
            attributes = self._bed_schedule_attributes_locked()
        assert attributes is not None
        weekday = execution_weekday(alarm.date)
        wake_at = resolve_wall_datetime(
            date.fromisoformat(alarm.date), alarm.local_time, self._timezone,
        ).value.astimezone(UTC)
        cleanup_at = wake_at + timedelta(
            seconds=TEMPORARY_BED_ALARM_CLEANUP_GRACE_SECONDS
        )
        records = list(self.state.temporary_bed_alarms)
        for side in alarm.bed_sides:
            payload, baseline_ids = add_temporary_alarm_payload(
                attributes, side, weekday, alarm.local_time,
            )
            if payload is None:
                continue
            record = TemporaryBedAlarm(
                alarm_id=alarm.id,
                source_ref=f"{SOURCE_REF_PREFIX}{side}",
                date=alarm.date,
                weekday=weekday,
                local_time=alarm.local_time,
                cleanup_at=cleanup_at,
                baseline_schedule_ids=baseline_ids,
            )
            records.append(record)
            self.state = replace(
                self.state, temporary_bed_alarms=tuple(records)
            )
            await self.store.async_save(self.state)
            await self._publish_bed_schedule_locked(payload)

    def _validate_temporary_bed_alarm_locked(
        self, alarm: WakeLightAlarm,
    ) -> None:
        if not alarm.enabled or not alarm.bed_sides or alarm.date is None:
            return
        if self._bed_schedule_attributes_locked() is None:
            raise ValueError("bed_schedule_unavailable")
        if any(
            side not in self.profile.sleepypod_source_sides
            for side in alarm.bed_sides
        ):
            raise ValueError("bed_side_unavailable")

    async def _cleanup_temporary_bed_alarms_locked(
        self,
        now: datetime,
        *,
        alarm_id: str | None = None,
        force: bool = False,
    ) -> bool:
        cleaned, _ = await self._cleanup_temporary_bed_alarms_from_attributes_locked(
            now,
            alarm_id=alarm_id,
            force=force,
            attributes=self._bed_schedule_attributes_locked(),
        )
        return cleaned

    async def _cleanup_temporary_bed_alarms_from_attributes_locked(
        self,
        now: datetime,
        *,
        alarm_id: str | None,
        force: bool,
        attributes: Mapping[str, Any] | None,
    ) -> tuple[bool, Mapping[str, Any] | None]:
        records = list(self.state.temporary_bed_alarms)
        if not records:
            return True, attributes
        changed = False
        retained: list[TemporaryBedAlarm] = []
        for record in records:
            selected = alarm_id is None or record.alarm_id == alarm_id
            due = force or record.cleanup_at <= now.astimezone(UTC)
            if not selected or not due:
                retained.append(record)
                continue
            if attributes is None:
                retained.append(
                    replace(
                        record,
                        cleanup_at=now.astimezone(UTC)
                        + timedelta(seconds=TEMPORARY_BED_ALARM_RETRY_SECONDS),
                    )
                )
                changed = True
                continue
            payload = remove_temporary_alarm_payload(
                attributes,
                record.side,
                record.weekday,
                record.local_time,
                record.baseline_schedule_ids,
                record.schedule_id,
            )
            if payload is None and force and record.schedule_id is None:
                retained.append(record)
                continue
            if payload is not None:
                await self._publish_bed_schedule_locked(payload)
                attributes = payload
            changed = True
        if changed:
            self.state = replace(
                self.state, temporary_bed_alarms=tuple(retained)
            )
        cleaned = not any(
            record.alarm_id == alarm_id for record in retained
        ) if alarm_id is not None else True
        return cleaned, attributes

    async def _handle_source_lifecycle_locked(
        self,
        source_ref: str,
        raw_state: str | None,
        now: datetime,
        *,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        lifecycle = normalize_source_lifecycle(raw_state)
        terminal_reason = (
            None
            if lifecycle in {"ringing", "snoozed"}
            else normalize_source_terminal_reason(raw_state, attributes)
        )
        attributes = attributes or {}
        cache = dict(self.state.source_cache)
        previous = cache.get(source_ref, SourceSnapshot())
        cache[source_ref] = replace(
            previous,
            lifecycle_state=lifecycle,
            terminal_reason=terminal_reason,
        )
        self.state = replace(self.state, source_cache=cache)
        if lifecycle not in {"ringing", "snoozed", "idle"} and terminal_reason not in {"stopped", "expired"}:
            return
        run = self.state.active_run
        source_id = attributes.get("occurrence_id")
        published = source_timestamp(attributes.get("ts"), milliseconds=True)
        if run is None:
            pending = [
                item for item in self._scheduled
                if item.occurrence_id not in self.state.terminal_occurrence_ids
                and source_event_matches(item, source_ref, attributes, now)
            ]
            if terminal_reason == "stopped" and len(pending) == 1:
                self.state = self.state.remember_terminal_occurrences(
                    (pending[0].occurrence_id,)
                )
            return
        matching = [
            item for item in run.occurrences
            if source_event_matches(item.schedule, source_ref, attributes, now)
            and item.source_occurrence_id in {None, source_id}
            and published is not None
            and (item.source_updated_at is None or published >= item.source_updated_at)
        ]
        if len(matching) != 1:
            return
        current = matching[0]
        identified = replace(
            current,
            source_occurrence_id=source_id,
            source_updated_at=published,
        )
        run = replace(run, occurrences=tuple(
            identified if item is current else item for item in run.occurrences
        ))
        self.state = replace(self.state, active_run=run)
        if terminal_reason == "stopped":
            remaining, _ = remove_occurrence(run, current.schedule.occurrence_id)
            self.state = self.state.remember_terminal_occurrences(
                (current.schedule.occurrence_id,)
            )
            cache = dict(self.state.source_cache)
            cache[source_ref] = replace(cache[source_ref], last_stopped_occurrence_id=source_id)
            self.state = replace(self.state, active_run=remaining, source_cache=cache)
            if remaining is None:
                await self._release_locked(
                    run,
                    outcome=PBL_RELEASE_OUTCOME_CANCELLED,
                    cause=PBL_RELEASE_CAUSE_OCCURRENCE_CANCELLED,
                )
            elif not await self._acquire_locked(remaining, recovery=False):
                await self._cancel_active_locked(
                    FAILURE_PBL_ACQUIRE, revoked=False, turn_off_owned=False,
                )
        elif lifecycle == "snoozed":
            until = source_timestamp(attributes.get("snoozed_until"))
            if until is None:
                self._record_failure_locked("source_snooze:invalid_deadline", run=run)
                return
            updated, outcome = snooze_source_occurrence(
                run, current.schedule.occurrence_id,
                source_occurrence_id=source_id,
                snoozed_until=until, now=now,
            )
            if updated is not None:
                self.state = replace(self.state, active_run=updated)
                if outcome == OUTCOME_ACCEPTED and updated.recovery_decision != "pending_reacquire":
                    await self._dispatch_snooze_hold_locked(updated, now)
            else:
                self._record_failure_locked(f"source_snooze:{outcome}", run=run)

    async def _sync_source_lifecycle_locked(self, now: datetime) -> None:
        """Consume retained state before any first/recovered dispatch, not only new events."""
        run = self.state.active_run
        if run is not None:
            planned = {item.occurrence_id: item for item in self._scheduled}
            run = replace(run, occurrences=tuple(
                replace(item, schedule=replace(
                    item.schedule,
                    source_schedule_id=planned[item.schedule.occurrence_id].source_schedule_id,
                ))
                if item.source_occurrence_id is None
                and item.schedule.occurrence_id in planned
                and planned[item.schedule.occurrence_id].source_schedule_id is not None
                else item
                for item in run.occurrences
            ))
            self.state = replace(self.state, active_run=run)
        for source_ref, entity_id in self.profile.source_state_entity_ids.items():
            state = self.hass.states.get(entity_id)
            if state is not None:
                await self._handle_source_lifecycle_locked(
                    source_ref, state.state, now, attributes=state.attributes,
                )

    async def _apply_command_effect_locked(
        self,
        effect: CommandEffect,
        now: datetime,
    ) -> None:
        if effect.previous_run is not None:
            self._delete_fired_once_alarms_locked(
                effect.previous_run,
                effect.occurrence_ids,
                now,
            )
        if effect.kind == "release_cancelled" and effect.previous_run is not None:
            await self._release_locked(
                effect.previous_run,
                outcome=PBL_RELEASE_OUTCOME_CANCELLED,
                cause=PBL_RELEASE_CAUSE_OCCURRENCE_CANCELLED,
            )
        elif effect.kind == "update_lease" and self.state.active_run is not None:
            if not await self._acquire_locked(
                self.state.active_run, recovery=False
            ):
                await self._cancel_active_locked(
                    FAILURE_PBL_ACQUIRE,
                    revoked=False,
                    turn_off_owned=False,
                )
        elif effect.kind == "end_episode" and effect.previous_run is not None:
            await self._end_episode_locked(effect.previous_run, now)

    def _recovery_blockers_are_retryable(
        self,
        blockers: tuple[str, ...],
    ) -> bool:
        if not blockers:
            return False
        for blocker in blockers:
            if blocker in {
                FAILURE_PBL_NOT_READY,
                FAILURE_TARGET_UNAVAILABLE,
            }:
                continue
            if blocker == FAILURE_BLOCKER_NOT_OFF:
                blocking_states = {
                    self._entity_state(entity_id)
                    for entity_id in self.profile.blocker_entity_ids
                    if self._entity_state(entity_id) != "off"
                }
                if blocking_states and blocking_states.issubset(
                    {*UNAVAILABLE_STATES, "missing"}
                ):
                    continue
            if (
                blocker == FAILURE_VACATION_BLOCKED
                and not self._vacation_requires_safe_off()
            ):
                continue
            return False
        return True

    def _schedule_recovery_retry_locked(
        self,
        run: ActiveRun,
        now: datetime,
        reason: str,
    ) -> bool:
        now_utc = now.astimezone(UTC)
        if self._recovery_retry_deadline is None:
            candidates = [
                now_utc + timedelta(seconds=RECOVERY_RETRY_BUDGET_SECONDS),
                max(
                    occurrence.snoozed_until or occurrence.schedule.wake_at
                    for occurrence in run.occurrences
                )
                + timedelta(seconds=DEFAULT_MISSED_ALARM_CATCHUP_SECONDS),
            ]
            if run.lease_expires_at is not None:
                candidates.append(run.lease_expires_at.astimezone(UTC))
            self._recovery_retry_deadline = min(candidates)
        if now_utc >= self._recovery_retry_deadline:
            return False
        pending = replace(run, recovery_decision="pending_reacquire")
        self.state = replace(self.state, active_run=pending)
        self._blocked_retry_at = min(
            now_utc + timedelta(seconds=DEFAULT_RAMP_STEP_SECONDS),
            self._recovery_retry_deadline,
        )
        self._record_failure_locked(reason, run=run)
        return True

    async def _fail_recovery_locked(
        self,
        run: ActiveRun,
        reason: str,
    ) -> None:
        await self._release_locked(
            run,
            outcome=PBL_RELEASE_OUTCOME_FAILED,
            cause=PBL_RELEASE_CAUSE_OWNER_FAILED,
        )
        self.state = self.state.remember_terminal_occurrences(
            run.occurrence_ids
        )
        self.state = replace(self.state, active_run=None)
        self._blocked_retry_at = None
        self._recovery_retry_deadline = None
        self._record_failure_locked(f"recovery:{reason}", run=run)
        self._record_failure_locked(FAILURE_RECOVERY_EXPIRED, run=run)

    async def _recover_locked(self, now: datetime) -> None:
        run = self.state.active_run
        if run is None:
            return
        if any(
            item.schedule.source_ref is not None
            and item.schedule.source_schedule_id is None
            for item in run.occurrences
        ):
            await self._fail_recovery_locked(run, "source_identity_unavailable")
            return
        if (
            run.wake_owned_target_ids
            and self._entity_state(self.profile.root_light_entity_id) == "off"
        ):
            await self._cancel_active_locked(
                "manual_group_off",
                revoked=True,
                turn_off_owned=False,
            )
            return
        original_run = run
        for entity_id in tuple(run.wake_owned_target_ids):
            if self._entity_state(entity_id) != "off":
                continue
            updated, _outcome = release_leaf(run, entity_id)
            if updated is None:
                await self._release_locked(
                    original_run,
                    outcome=PBL_RELEASE_OUTCOME_FAILED,
                    cause=PBL_RELEASE_CAUSE_OWNER_FAILED,
                )
                self.state = self.state.remember_terminal_occurrences(
                    original_run.occurrence_ids
                )
                self.state = replace(self.state, active_run=None)
                self._record_failure_locked(
                    FAILURE_NO_TARGETS,
                    run=original_run,
                )
                return
            run = updated
        run = self._observe_target_floor_if_settled(run, now)
        self.state = replace(self.state, active_run=run)
        preflight = self._preflight_locked(valid_schedule=True)
        if not preflight.allowed:
            reason = preflight.blockers[0]
            if self._recovery_blockers_are_retryable(preflight.blockers):
                if self._schedule_recovery_retry_locked(run, now, reason):
                    return
                await self._fail_recovery_locked(run, reason)
                return
            await self._cancel_active_locked(
                reason,
                revoked=False,
                turn_off_owned=(
                    reason == FAILURE_VACATION_BLOCKED
                    and self._vacation_requires_safe_off()
                ),
            )
            return
        decision = restart_decision(run, now)
        run = replace(run, recovery_decision=decision.reason)
        self.state = replace(self.state, active_run=run)
        if decision.action != "resume":
            await self._fail_recovery_locked(run, decision.reason)
            return
        pending = replace(run, recovery_decision="pending_reacquire")
        self.state = replace(self.state, active_run=pending)
        if await self._acquire_locked(pending, recovery=True):
            recovered = self.state.active_run
            if recovered is not None:
                recovered = refresh_recovery_holds(recovered, now)
                self.state = replace(
                    self.state,
                    active_run=replace(
                        recovered,
                        recovery_decision="resumed_within_catchup",
                    ),
                )
                self._recovery_retry_deadline = None
        else:
            if self.state.last_pbl_outcome in {"stale_lease", "expired", "token_mismatch"}:
                await self._fail_recovery_locked(pending, "lease_retired")
                return
            if not self._schedule_recovery_retry_locked(
                pending,
                now,
                FAILURE_PBL_ACQUIRE,
            ):
                await self._fail_recovery_locked(
                    pending,
                    FAILURE_PBL_ACQUIRE,
                )

    def _calculate_occurrences_locked(
        self,
        now: datetime,
    ) -> tuple[ScheduledOccurrence, ...]:
        if self.state.auto_relight_blocked_until is not None or self.state.last_cancellation is not None:
            anchor = self.state.auto_relight_blocked_from or (
                self.state.last_cancellation.at
                if self.state.last_cancellation else now
            )
            self.state = expand_relight_fence(
                self.state,
                calendar_windows(
                    self.profile.profile_id, runnable_alarms(self.state),
                    anchor, self._timezone, self.state.defaults,
                ),
                now,
            )
        self.state = self.state.clear_expired_relight_fence(now)
        self._disable_completed_once_alarms_locked(self.state.terminal_occurrence_ids)
        stale_ids = stale_once_alarm_ids(
            self.state.alarms,
            now,
            self._timezone,
            profile_id=self.profile.profile_id,
            terminal_occurrence_ids=self.state.terminal_occurrence_ids,
        )
        if stale_ids:
            stale = set(stale_ids)
            alarms = tuple(
                replace(alarm, enabled=False, revision=alarm.revision + 1)
                if alarm.id in stale
                else alarm
                for alarm in self.state.alarms
            )
            self.state = replace(
                self.state,
                alarms=alarms,
                revision=self.state.revision + 1,
            )
            self._record_failure_locked(FAILURE_MISSED)
        return resolve_profile_occurrences(
            self.profile.profile_id,
            runnable_alarms(self.state),
            now,
            self._timezone,
            self.state.defaults,
            terminal_occurrence_ids=self.state.terminal_occurrence_ids,
            auto_relight_blocked_until=(
                self.state.auto_relight_blocked_until
            ),
        )

    async def _reconcile_locked(self, now: datetime, trigger: str) -> None:
        now_utc = now.astimezone(UTC)
        await self._cleanup_temporary_bed_alarms_locked(now_utc)
        self._scheduled = self._calculate_occurrences_locked(now_utc)
        await self._apply_observed_foreign_off_locked()
        run = self.state.active_run
        if run is not None and run.recovery_decision == "pending_reacquire":
            if (
                self._blocked_retry_at is not None
                and self._blocked_retry_at > now_utc
            ):
                await self._finish_locked(now_utc)
                return
            decision = restart_decision(run, now_utc)
            if (
                decision.action != "resume"
                or (
                    self._recovery_retry_deadline is not None
                    and now_utc >= self._recovery_retry_deadline
                )
            ):
                reason = (
                    decision.reason
                    if decision.action != "resume"
                    else "retry_budget_expired"
                )
                await self._fail_recovery_locked(run, reason)
                await self._finish_locked(now_utc)
                return
            preflight = self._preflight_locked(valid_schedule=True)
            if not preflight.allowed:
                reason = preflight.blockers[0]
                if self._recovery_blockers_are_retryable(preflight.blockers):
                    if not self._schedule_recovery_retry_locked(
                        run,
                        now_utc,
                        reason,
                    ):
                        await self._fail_recovery_locked(run, reason)
                    await self._finish_locked(now_utc)
                    return
                await self._cancel_active_locked(
                    reason,
                    revoked=False,
                    turn_off_owned=(
                        reason == FAILURE_VACATION_BLOCKED
                        and self._vacation_requires_safe_off()
                    ),
                )
                await self._finish_locked(now_utc)
                return
            if not await self._acquire_locked(run, recovery=True):
                if self.state.last_pbl_outcome in {"stale_lease", "expired", "token_mismatch"}:
                    await self._fail_recovery_locked(run, "lease_retired")
                    await self._finish_locked(now_utc)
                    return
                if not self._schedule_recovery_retry_locked(
                    run,
                    now_utc,
                    FAILURE_PBL_ACQUIRE,
                ):
                    await self._fail_recovery_locked(
                        run,
                        FAILURE_PBL_ACQUIRE,
                    )
                await self._finish_locked(now_utc)
                return
            recovered = self.state.active_run
            run = (
                replace(
                    refresh_recovery_holds(recovered, now_utc),
                    recovery_decision="resumed_within_catchup",
                )
                if recovered is not None
                else None
            )
            self.state = replace(self.state, active_run=run)
            self._blocked_retry_at = None
            self._recovery_retry_deadline = None

        await self._sync_source_lifecycle_locked(now_utc)
        run = self.state.active_run
        if run is not None:
            self._blocked_retry_at = None
            run = self._observe_target_floor_if_settled(run, now_utc)
            self.state = replace(self.state, active_run=run)
            if self._entity_state(self.profile.vacation_entity_id) != "off":
                await self._cancel_active_locked(
                    FAILURE_VACATION_BLOCKED,
                    revoked=False,
                    turn_off_owned=self._vacation_requires_safe_off(),
                )
                run = None
            else:
                due_new = tuple(
                    occurrence
                    for occurrence in self._scheduled
                    if occurrence.occurrence_id not in set(run.occurrence_ids)
                    and occurrence.occurrence_id
                    not in set(self.state.terminal_occurrence_ids)
                    and occurrence.ramp_start_at <= now_utc
                    and occurrence.wake_at
                    + timedelta(
                        seconds=DEFAULT_MISSED_ALARM_CATCHUP_SECONDS
                    )
                    >= now_utc
                    and self._occurrence_source_available(occurrence)
                )
                if due_new:
                    connected_ids = set(
                        connected_episode(
                            run,
                            due_new,
                            now=now_utc,
                        ).occurrence_ids
                    )
                    due_new = tuple(
                        occurrence
                        for occurrence in due_new
                        if occurrence.occurrence_id in connected_ids
                    )
                unavailable_source_due = any(
                    occurrence.occurrence_id not in set(run.occurrence_ids)
                    and occurrence.ramp_start_at <= now_utc
                    and occurrence.wake_at
                    + timedelta(
                        seconds=DEFAULT_MISSED_ALARM_CATCHUP_SECONDS
                    )
                    >= now_utc
                    and not self._occurrence_source_available(occurrence)
                    for occurrence in self._scheduled
                )
                if unavailable_source_due:
                    self._blocked_retry_at = now_utc + timedelta(
                        seconds=DEFAULT_RAMP_STEP_SECONDS
                    )
                    self._record_failure_locked(FAILURE_SOURCE_UNAVAILABLE)
                if due_new:
                    over_budget = tuple(
                        item for item in due_new
                        if run.lease_expires_at is not None
                        and max(item.wake_at, now_utc) + timedelta(minutes=item.hold_minutes)
                        > run.lease_expires_at
                    )
                    if over_budget:
                        self.state = self.state.remember_terminal_occurrences(
                            item.occurrence_id for item in over_budget
                        )
                        self._record_failure_locked("episode_duration_exceeded", run=run)
                        blocked_ids = {item.occurrence_id for item in over_budget}
                        due_new = tuple(item for item in due_new if item.occurrence_id not in blocked_ids)
                if due_new:
                    if len(run.occurrences) + len(due_new) > MAX_ACTIVE_OCCURRENCES:
                        self._record_failure_locked(FAILURE_OCCURRENCE_LIMIT)
                    previous_run = run
                    run = merge_occurrences(run, due_new, now=now_utc)
                    self.state = replace(self.state, active_run=run)
                    if not await self._acquire_locked(run, recovery=False):
                        self.state = replace(
                            self.state,
                            active_run=previous_run,
                        )
                        run = previous_run
                    else:
                        run = self.state.active_run

        if run is not None:
            view = run_view(run, now_utc)
            if view.completed_occurrence_ids:
                missing_final = tuple(
                    item.schedule.occurrence_id
                    for item in run.occurrences
                    if item.schedule.occurrence_id
                    in set(view.completed_occurrence_ids)
                    and not item.final_dispatched
                )
                self._delete_fired_once_alarms_locked(
                    run,
                    view.completed_occurrence_ids,
                    now_utc,
                )
                self.state = self.state.remember_terminal_occurrences(
                    view.completed_occurrence_ids
                )
                remaining = remove_completed(
                    run,
                    view.completed_occurrence_ids,
                )
                self._disable_completed_once_alarms_locked(
                    view.completed_occurrence_ids
                )
                if missing_final:
                    self._record_failure_locked(FAILURE_MISSED, run=run)
                if remaining is None:
                    await self._release_locked(
                        run,
                        outcome=(
                            PBL_RELEASE_OUTCOME_FAILED
                            if missing_final
                            else PBL_RELEASE_OUTCOME_COMPLETED
                        ),
                        cause=(
                            PBL_RELEASE_CAUSE_OWNER_FAILED
                            if missing_final
                            else PBL_RELEASE_CAUSE_HOLD_COMPLETE
                        ),
                    )
                    self.state = replace(self.state, active_run=None)
                    run = None
                else:
                    previous_run = run
                    self.state = replace(self.state, active_run=remaining)
                    run = remaining
                    if not await self._acquire_locked(run, recovery=False):
                        self.state = replace(
                            self.state,
                            active_run=previous_run,
                        )
                        run = previous_run
                    else:
                        run = self.state.active_run

        if run is None:
            due_candidates = tuple(
                occurrence
                for occurrence in self._scheduled
                if occurrence.occurrence_id
                not in set(self.state.terminal_occurrence_ids)
                and occurrence.ramp_start_at <= now_utc
                and occurrence.wake_at
                + timedelta(
                    seconds=DEFAULT_MISSED_ALARM_CATCHUP_SECONDS
                )
                >= now_utc
            )
            due = tuple(
                occurrence
                for occurrence in due_candidates
                if self._occurrence_source_available(occurrence)
            )
            if due_candidates and not due:
                self._blocked_retry_at = now_utc + timedelta(
                    seconds=DEFAULT_RAMP_STEP_SECONDS
                )
                self._record_failure_locked(FAILURE_SOURCE_UNAVAILABLE)
            if due:
                preflight = self._preflight_locked(bool(due))
                if preflight.allowed:
                    self._blocked_retry_at = None
                    await self._start_occurrences_locked(due, now_utc)
                    await self._sync_source_lifecycle_locked(now_utc)
                    run = self.state.active_run
                else:
                    self._blocked_retry_at = now_utc + timedelta(
                        seconds=DEFAULT_RAMP_STEP_SECONDS
                    )
                    for blocker in preflight.blockers:
                        self._record_failure_locked(blocker)
            elif not due_candidates:
                self._blocked_retry_at = None

        if run is not None:
            await self._dispatch_if_due_locked(run, now_utc, trigger)
        await self._finish_locked(now_utc)

    def _preflight_locked(self, valid_schedule: bool):
        if unsupported_episode_ids(calendar_windows(
            self.profile.profile_id, runnable_alarms(self.state), utc_now(),
            self._timezone, self.state.defaults,
        ), utc_now()):
            return PreflightResult(False, ("episode_duration_exceeded",))
        target_states = {
            entity_id: self._entity_state(entity_id)
            for entity_id in (
                self.profile.root_light_entity_id,
                *self.profile.target_light_entity_ids,
            )
        }
        blocker_states = {
            entity_id: self._entity_state(entity_id)
            for entity_id in self.profile.blocker_entity_ids
        }
        return evaluate_preflight(
            PreflightInputs(
                integration_available=self._started and not self._stopping,
                vacation_state=self._entity_state(
                    self.profile.vacation_entity_id
                ),
                pbl_state=self._pbl_preflight_state(),
                target_states=target_states,
                blocker_states=blocker_states,
                valid_schedule=valid_schedule,
                legacy_brightness_lifecycle_safe=(
                    self.profile.legacy_brightness_lifecycle_safe
                ),
            )
        )

    async def _start_occurrences_locked(
        self,
        occurrences: tuple[ScheduledOccurrence, ...],
        now: datetime,
    ) -> bool:
        if len(occurrences) > MAX_ACTIVE_OCCURRENCES:
            self._record_failure_locked(FAILURE_OCCURRENCE_LIMIT)
            occurrences = occurrences[:MAX_ACTIVE_OCCURRENCES]
        floor = self._observed_target_floor(
            self.profile.target_light_entity_ids
        )
        run = start_run(
            self.profile,
            occurrences,
            now=now,
            observed_floor_pct=floor,
        )
        self.state = replace(self.state, active_run=run)
        if not await self._acquire_locked(run, recovery=False):
            self.state = replace(self.state, active_run=None)
            self._record_failure_locked(FAILURE_PBL_ACQUIRE, run=run)
            self._blocked_retry_at = now + timedelta(
                seconds=DEFAULT_RAMP_STEP_SECONDS
            )
            return False
        return True

    async def _acquire_locked(
        self,
        run: ActiveRun,
        *,
        recovery: bool,
    ) -> bool:
        now = utc_now()
        if run.lease_expires_at is None:
            ttl_seconds = fixed_lease_ttl_seconds()
        else:
            ttl_seconds = max(
                1,
                min(
                    fixed_lease_ttl_seconds(),
                    math.ceil(
                        (
                            run.lease_expires_at.astimezone(UTC) - now
                        ).total_seconds()
                    ),
                ),
            )
        request_id = opaque_ref(
            "wlacq",
            run.lease_id,
            ",".join(run.occurrence_ids),
            ",".join(run.target_entity_ids),
            "recovery" if recovery else "active",
        )
        response = await self._call_pbl_service(
            PBL_SERVICE_ACQUIRE,
            {
                "entity_id": self.profile.pbl_switch_entity_id,
                "controlled_entity_id": self.profile.root_light_entity_id,
                "lease_id": run.lease_id,
                "controller_id": run.controller_id,
                "request_id": request_id,
                "owner": PBL_OWNER,
                "occurrence_ids": list(run.occurrence_ids),
                "ttl_seconds": ttl_seconds,
                "target_entity_ids": list(run.target_entity_ids),
            },
        )
        outcome = str(response.get("outcome", "service_error"))
        self.state = replace(self.state, last_pbl_outcome=outcome)
        if outcome not in PBL_ACQUIRE_SUCCESS_OUTCOMES:
            self._record_pbl_failure_locked(
                "acquire",
                response,
                FAILURE_PBL_ACQUIRE,
                run,
            )
            return False
        self._dispatch_revoke_wait_until = None
        generation = response.get("generation")
        expires_at = parse_datetime(response.get("expires_at"))
        acquired_at = parse_datetime(response.get("acquired_at"))
        if not isinstance(generation, int) or generation < 1 or expires_at is None:
            self._record_failure_locked(FAILURE_PBL_ACQUIRE, run=run)
            return False
        active = mark_acquired(
            run,
            generation=generation,
            acquired_at=acquired_at or now,
            expires_at=expires_at,
            outcome=outcome,
        )
        self.state = replace(
            self.state,
            active_run=active,
            last_pbl_outcome=outcome,
        )
        self._log_correlation("acquire", active, outcome)
        return True

    async def _dispatch_if_due_locked(
        self,
        run: ActiveRun,
        now: datetime,
        trigger: str,
    ) -> None:
        await self._apply_observed_foreign_off_locked()
        current = self.state.active_run
        if current is None or current.lease_id != run.lease_id:
            return
        run = current
        if run.generation is None or not run.target_entity_ids:
            return
        view = run_view(run, now)
        should_dispatch = (
            run.last_command_at is None
            or view.force_final_dispatch
            or run.next_deadline is None
            or now >= run.next_deadline
        )
        if (
            self._dispatch_revoke_wait_until is not None
            and run.next_deadline is not None
            and now < run.next_deadline
        ):
            should_dispatch = False
        if view.phase == "snoozed" and not view.force_final_dispatch:
            should_dispatch = False
        if not should_dispatch:
            next_deadline = view.next_deadline
            if (
                self._dispatch_revoke_wait_until is not None
                and run.next_deadline is not None
            ):
                next_deadline = run.next_deadline
            self.state = replace(
                self.state,
                active_run=replace(run, next_deadline=next_deadline),
            )
            return
        brightness = (
            100.0
            if view.force_final_dispatch
            else round(view.desired_brightness_pct, 2)
        )
        transition = (
            0.0
            if view.force_final_dispatch or run.last_command_at is None
            else float(DEFAULT_RAMP_STEP_SECONDS)
        )
        command_id = opaque_ref(
            "wlcmd",
            run.lease_id,
            run.command_sequence + 1,
            f"{brightness:.2f}",
            trigger,
        )
        response = await self._call_pbl_service(
            PBL_SERVICE_DISPATCH,
            {
                "entity_id": self.profile.pbl_switch_entity_id,
                "controlled_entity_id": self.profile.root_light_entity_id,
                "lease_id": run.lease_id,
                "controller_id": run.controller_id,
                "expected_generation": run.generation,
                "command_id": command_id,
                "owner": PBL_OWNER,
                "target_entity_ids": list(run.target_entity_ids),
                "service_data": {
                    "brightness_pct": brightness,
                    "transition": transition,
                },
            },
        )
        outcome = str(response.get("outcome", "service_error"))
        self.state = replace(self.state, last_pbl_outcome=outcome)
        if outcome not in PBL_DISPATCH_SUCCESS_OUTCOMES:
            if self._dispatch_waits_for_revoke(response, run, now):
                return
            self._record_pbl_failure_locked(
                "dispatch",
                response,
                FAILURE_PBL_DISPATCH,
                run,
            )
            await self._cancel_active_locked(
                FAILURE_PBL_DISPATCH,
                revoked=outcome == "revoked_in_flight",
                turn_off_owned=False,
            )
            return
        self._dispatch_revoke_wait_until = None
        response_targets = response.get("target_entity_ids")
        targets = (
            tuple(
                entity_id
                for entity_id in response_targets
                if isinstance(entity_id, str)
                and entity_id in set(run.target_entity_ids)
            )
            if isinstance(response_targets, list)
            else run.target_entity_ids
        )
        if not targets:
            await self._cancel_active_locked(
                FAILURE_NO_TARGETS,
                revoked=False,
                turn_off_owned=False,
            )
            return
        effective_run = run
        for entity_id in set(run.target_entity_ids) - set(targets):
            reduced, _outcome = release_leaf(effective_run, entity_id)
            if reduced is None:
                await self._cancel_active_locked(
                    FAILURE_NO_TARGETS,
                    revoked=False,
                    turn_off_owned=False,
                )
                return
            effective_run = reduced
        dispatched = mark_dispatched(
            effective_run,
            now=now,
            brightness_pct=brightness,
            target_entity_ids=targets,
            pbl_outcome=outcome,
            final_dispatch=view.force_final_dispatch,
        )
        next_view = run_view(dispatched, now)
        dispatched = replace(
            dispatched,
            next_deadline=next_view.next_deadline,
        )
        self.state = replace(
            self.state,
            active_run=dispatched,
            last_pbl_outcome=outcome,
            last_outcome="dispatched",
        )
        self._block_brightness_observation(now, transition)
        self._log_correlation("dispatch", dispatched, outcome)

    async def _dispatch_snooze_hold_locked(
        self,
        run: ActiveRun,
        now: datetime,
    ) -> None:
        await self._apply_observed_foreign_off_locked()
        current = self.state.active_run
        if current is None or current.lease_id != run.lease_id:
            return
        run = current
        if run.generation is None or not run.target_entity_ids:
            return
        root = self.hass.states.get(self.profile.root_light_entity_id)
        if (
            run.wake_owned_target_ids
            and root is not None
            and root.state == "off"
        ):
            await self._cancel_active_locked(
                "manual_group_off",
                revoked=True,
                turn_off_owned=False,
            )
            return
        observed = self._observed_target_floor(
            run.target_entity_ids
        )
        brightness = round(
            max(
                run.last_brightness_pct,
                run.observed_floor_pct,
                observed,
                1.0,
            ),
            2,
        )
        command_id = opaque_ref(
            "wlhold",
            run.lease_id,
            run.command_sequence + 1,
            f"{brightness:.2f}",
        )
        response = await self._call_pbl_service(
            PBL_SERVICE_DISPATCH,
            {
                "entity_id": self.profile.pbl_switch_entity_id,
                "controlled_entity_id": self.profile.root_light_entity_id,
                "lease_id": run.lease_id,
                "controller_id": run.controller_id,
                "expected_generation": run.generation,
                "command_id": command_id,
                "owner": PBL_OWNER,
                "target_entity_ids": list(run.target_entity_ids),
                "service_data": {
                    "brightness_pct": brightness,
                    "transition": 0.0,
                },
            },
        )
        outcome = str(response.get("outcome", "service_error"))
        self.state = replace(self.state, last_pbl_outcome=outcome)
        if outcome not in PBL_DISPATCH_SUCCESS_OUTCOMES:
            if self._dispatch_waits_for_revoke(response, run, now):
                return
            self._record_pbl_failure_locked(
                "dispatch",
                response,
                FAILURE_PBL_DISPATCH,
                run,
            )
            await self._cancel_active_locked(
                FAILURE_PBL_DISPATCH,
                revoked=outcome == "revoked_in_flight",
                turn_off_owned=False,
            )
            return
        self._dispatch_revoke_wait_until = None
        response_targets = response.get("target_entity_ids")
        targets = (
            tuple(
                entity_id
                for entity_id in response_targets
                if isinstance(entity_id, str)
                and entity_id in set(run.target_entity_ids)
            )
            if isinstance(response_targets, list)
            else run.target_entity_ids
        )
        if not targets:
            await self._cancel_active_locked(
                FAILURE_NO_TARGETS,
                revoked=False,
                turn_off_owned=False,
            )
            return
        effective_run = run
        for entity_id in set(run.target_entity_ids) - set(targets):
            reduced, _outcome = release_leaf(effective_run, entity_id)
            if reduced is None:
                await self._cancel_active_locked(
                    FAILURE_NO_TARGETS,
                    revoked=False,
                    turn_off_owned=False,
                )
                return
            effective_run = reduced

        effective_run = replace(
            effective_run,
            occurrences=tuple(
                replace(
                    occurrence,
                    held_brightness_pct=brightness,
                )
                if occurrence.snoozed_until is not None
                and occurrence.snoozed_until > now
                else occurrence
                for occurrence in effective_run.occurrences
            ),
        )
        held = mark_hold_dispatched(
            effective_run,
            now=now,
            brightness_pct=brightness,
            target_entity_ids=targets,
            pbl_outcome=outcome,
        )
        held = replace(held, next_deadline=run_view(held, now).next_deadline)
        self.state = replace(
            self.state,
            active_run=held,
            last_pbl_outcome=outcome,
            last_outcome="snoozed",
        )
        self._block_brightness_observation(now, 0)
        self._log_correlation("snooze_hold", held, outcome)

    def _dispatch_waits_for_revoke(
        self,
        response: Mapping[str, Any],
        run: ActiveRun,
        now: datetime,
    ) -> bool:
        outcome = response.get("outcome")
        blockers = response.get("blockers")
        is_revoke_race = outcome == "revoked_in_flight" or (
            outcome == "blocked"
            and isinstance(blockers, list)
            and "inactive_or_mismatched_lease" in blockers
        )
        if not is_revoke_race:
            self._dispatch_revoke_wait_until = None
            return False
        if self._dispatch_revoke_wait_until is None:
            self._dispatch_revoke_wait_until = now + timedelta(
                seconds=2 * DEFAULT_RAMP_STEP_SECONDS
            )
        if now >= self._dispatch_revoke_wait_until:
            self._dispatch_revoke_wait_until = None
            return False
        current = self.state.active_run
        if current is not None and current.lease_id == run.lease_id:
            self.state = replace(
                self.state,
                active_run=replace(
                    current,
                    next_deadline=min(
                        self._dispatch_revoke_wait_until,
                        now
                        + timedelta(seconds=DEFAULT_RAMP_STEP_SECONDS),
                    ),
                ),
            )
        return True

    async def _release_locked(
        self,
        run: ActiveRun,
        *,
        outcome: str,
        cause: str,
    ) -> bool:
        if run.generation is None:
            return True
        request_id = opaque_ref(
            "wlrel",
            run.lease_id,
            run.generation,
            outcome,
            cause,
        )
        response = await self._call_pbl_service(
            PBL_SERVICE_RELEASE,
            {
                "entity_id": self.profile.pbl_switch_entity_id,
                "controlled_entity_id": self.profile.root_light_entity_id,
                "lease_id": run.lease_id,
                "controller_id": run.controller_id,
                "expected_generation": run.generation,
                "request_id": request_id,
                "owner": PBL_OWNER,
                "outcome": outcome,
                "cause": cause,
            },
        )
        service_outcome = str(response.get("outcome", "service_error"))
        self.state = replace(
            self.state,
            last_pbl_outcome=service_outcome,
            last_outcome=cause,
        )
        self._log_correlation("release", run, service_outcome)
        if service_outcome not in PBL_RELEASE_SUCCESS_OUTCOMES:
            self._record_pbl_failure_locked(
                "release",
                response,
                FAILURE_PBL_RELEASE,
                run,
            )
            return False
        return True

    async def _call_pbl_service(
        self,
        service: str,
        data: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            response = await self.hass.services.async_call(
                PBL_DOMAIN,
                service,
                dict(data),
                blocking=True,
                return_response=True,
            )
        except Exception:
            _LOGGER.exception(
                "PBL service failed for profile=%s service=%s",
                self.profile.profile_id,
                service,
            )
            return {"outcome": "service_error"}
        return response if isinstance(response, Mapping) else {
            "outcome": "invalid_response"
        }

    async def _cancel_active_locked(
        self,
        reason: str,
        *,
        revoked: bool,
        turn_off_owned: bool,
    ) -> None:
        self._dispatch_revoke_wait_until = None
        run = self.state.active_run
        if run is None:
            return
        policy = cancellation_decision(run, reason)
        turn_off_owned = turn_off_owned or policy.turn_off_owned
        now = utc_now()
        self._delete_fired_once_alarms_locked(
            run,
            policy.terminal_occurrence_ids,
            now,
        )
        self._cancel_timer()
        if not revoked:
            await self._release_locked(
                run,
                outcome=(
                    PBL_RELEASE_OUTCOME_CANCELLED
                    if policy.user_initiated
                    else PBL_RELEASE_OUTCOME_FAILED
                ),
                cause=(
                    PBL_RELEASE_CAUSE_EXTERNAL_OFF
                    if reason == "manual_group_off"
                    else PBL_RELEASE_CAUSE_OCCURRENCE_CANCELLED
                    if policy.user_initiated
                    else PBL_RELEASE_CAUSE_OWNER_FAILED
                ),
            )
        if policy.user_initiated:
            component = connected_episode(
                run,
                self._scheduled,
                now=now,
            )
            self._delete_fired_once_alarms_locked(
                run,
                run.occurrence_ids,
                now,
            )
            self.state = self.state.remember_user_cancellation(
                component.occurrence_ids,
                at=now,
                suppressed_until=component.end_at,
                suppressed_from=component.start_at,
            )
        else:
            self.state = self.state.remember_terminal_occurrences(
                policy.terminal_occurrence_ids
            )
            self.state = replace(self.state, active_run=None)
            self._record_failure_locked(reason, run=run)
        self._brightness_observation_blocked_until = None
        if turn_off_owned:
            await self._safe_off_wake_owned_locked(run)
        self._emit_outcome(
            OUTCOME_CANCELLED_BY_USER
            if policy.user_initiated
            else reason,
            run,
        )

    async def _end_episode_locked(
        self,
        run: ActiveRun,
        now: datetime,
    ) -> None:
        """Release, safely darken owned targets, and fence one user-ended episode."""
        self._cancel_timer()
        component = connected_episode(
            run,
            self._scheduled,
            now=now,
        )
        await self._release_locked(
            run,
            outcome=PBL_RELEASE_OUTCOME_CANCELLED,
            cause=PBL_RELEASE_CAUSE_OCCURRENCE_CANCELLED,
        )
        await self._safe_off_wake_owned_locked(run)
        self.state = self.state.remember_user_cancellation(
            component.occurrence_ids,
            at=now,
            suppressed_until=component.end_at,
            suppressed_from=component.start_at,
        )
        self._brightness_observation_blocked_until = None
        self._emit_outcome(OUTCOME_CANCELLED_BY_USER, run)

    async def _safe_off_wake_owned_locked(self, run: ActiveRun) -> None:
        """Turn off only targets previously confirmed as dispatched by Wake Light."""
        targets = tuple(
            entity_id
            for entity_id in run.wake_owned_target_ids
            if entity_id not in set(run.released_target_ids)
        )
        if not targets:
            return
        try:
            await self.hass.services.async_call(
                "light",
                "turn_off",
                {
                    "entity_id": list(targets),
                    "transition": 0,
                },
                blocking=True,
            )
        except Exception:
            _LOGGER.exception(
                "Safe wake-owned off failed for profile=%s lease_ref=%s",
                self.profile.profile_id,
                opaque_ref("lease", run.lease_id),
            )
            self._record_failure_locked("safe_turn_off_failed", run=run)

    async def _apply_observed_foreign_off_locked(self) -> None:
        """Drain observed OFF intent before issuing another owner command."""
        while self._observed_foreign_off:
            run = self.state.active_run
            if run is None:
                self._observed_foreign_off.clear()
                return
            pending = tuple(self._observed_foreign_off.items())
            for entity_id, lease_id in pending:
                if lease_id != run.lease_id or entity_id not in run.target_entity_ids:
                    self._observed_foreign_off.pop(entity_id, None)
            candidates = [
                entity_id for entity_id, lease_id in pending
                if lease_id == run.lease_id and entity_id in run.target_entity_ids
            ]
            if not candidates:
                return
            for entity_id in candidates:
                self._observed_foreign_off.pop(entity_id, None)
                current = self.state.active_run
                if current is None or current.lease_id != run.lease_id:
                    return
                if (
                    entity_id in current.target_entity_ids
                    and self._entity_state(entity_id) == "off"
                ):
                    await self._release_foreign_off_leaf_locked(entity_id)

    async def _release_foreign_off_leaf_locked(
        self,
        entity_id: str,
    ) -> None:
        run = self.state.active_run
        if run is None:
            return
        if self._pbl_control_lease_state() != "active":
            await self._cancel_active_locked(
                "manual_group_off",
                revoked=True,
                turn_off_owned=False,
            )
            return
        # Startup-OFF targets are not evidence that the whole room was switched off.
        if all(
            self._entity_state(target) == "off"
            and (
                target == entity_id
                or self._observed_foreign_off.get(target) == run.lease_id
            )
            for target in run.target_entity_ids
        ):
            await self._cancel_active_locked(
                "manual_group_off",
                revoked=False,
                turn_off_owned=False,
            )
            return
        updated, outcome = release_leaf(run, entity_id)
        if updated is None:
            # Foreign OFF exhausted the leased leaves, not a system target dropout.
            await self._cancel_active_locked(
                "manual_group_off",
                revoked=False,
                turn_off_owned=False,
            )
            return
        if outcome == "updated":
            self.state = replace(self.state, active_run=updated)
            if not await self._acquire_locked(updated, recovery=False):
                await self._cancel_active_locked(
                    FAILURE_PBL_ACQUIRE,
                    revoked=False,
                    turn_off_owned=False,
                )

    def _disable_completed_once_alarms_locked(
        self,
        occurrence_ids: tuple[str, ...],
    ) -> None:
        completed_alarm_ids = terminal_once_alarm_ids(
            self.profile.profile_id, self.state.alarms, self._timezone,
            occurrence_ids,
        )
        changed = False
        alarms = []
        for alarm in self.state.alarms:
            if (
                alarm.id in completed_alarm_ids
                and alarm.kind == "once"
                and alarm.enabled
            ):
                alarms.append(
                    replace(
                        alarm,
                        enabled=False,
                        revision=alarm.revision + 1,
                    )
                )
                changed = True
            else:
                alarms.append(alarm)
        if changed:
            self.state = replace(
                self.state,
                alarms=tuple(alarms),
                revision=self.state.revision + 1,
            )

    def _delete_fired_once_alarms_locked(
        self,
        run: ActiveRun,
        occurrence_ids: tuple[str, ...],
        now: datetime,
    ) -> None:
        terminal = set(occurrence_ids)
        fired_ids = tuple(
            occurrence.schedule.occurrence_id
            for occurrence in run.occurrences
            if occurrence.schedule.occurrence_id in terminal
            and occurrence.schedule.wake_at <= now.astimezone(UTC)
            and occurrence.final_dispatched
        )
        if not fired_ids:
            return
        alarm_ids = terminal_once_alarm_ids(
            self.profile.profile_id,
            self.state.alarms,
            self._timezone,
            fired_ids,
            enabled_only=False,
        )
        if not alarm_ids:
            return
        alarms = tuple(
            alarm for alarm in self.state.alarms if alarm.id not in alarm_ids
        )
        if len(alarms) == len(self.state.alarms):
            return
        self.state = replace(
            self.state,
            alarms=alarms,
            revision=self.state.revision + 1,
        )

    def _record_failure_locked(
        self,
        code: str,
        *,
        run: ActiveRun | None = None,
    ) -> None:
        if self.state.failures and self.state.failures[-1].code == code:
            self.state = replace(self.state, last_outcome=code)
            return
        self.state = self.state.with_failure(
            code,
            at=utc_now(),
            occurrence_ref=(
                opaque_ref("occset", ",".join(run.occurrence_ids))
                if run is not None
                else None
            ),
            lease_ref=(
                opaque_ref("lease", run.lease_id) if run is not None else None
            ),
        )

    def _record_pbl_failure_locked(
        self,
        action: str,
        response: Mapping[str, Any],
        fallback: str,
        run: ActiveRun,
    ) -> None:
        blockers = response.get("blockers")
        if isinstance(blockers, list):
            for blocker in blockers[:4]:
                if not isinstance(blocker, str):
                    continue
                stable = "".join(
                    character
                    for character in blocker[:96]
                    if character.isalnum() or character in "._:-"
                )
                if stable:
                    self._record_failure_locked(
                        f"pbl_{action}:{stable}",
                        run=run,
                    )
        self._record_failure_locked(fallback, run=run)

    async def _finish_locked(self, now: datetime) -> None:
        self._scheduled = self._calculate_occurrences_locked(now)
        payload = self.state.to_dict()
        if payload != self._last_saved_payload:
            await self.store.async_save(self.state)
            self._last_saved_payload = payload
        read_model = self._build_read_model(now)
        if self._read_model_changed(read_model):
            self.async_set_updated_data(read_model)
        self._schedule_next_locked(now)

    def _read_model_changed(self, read_model: SensorReadModel) -> bool:
        previous = self.data
        if previous is None or previous.state != read_model.state:
            return True
        previous_attributes = dict(previous.attributes)
        current_attributes = dict(read_model.attributes)
        previous_progress = float(previous_attributes.pop("progress", 0))
        current_progress = float(current_attributes.pop("progress", 0))
        return (
            previous_attributes != current_attributes
            or abs(current_progress - previous_progress) >= 0.5
        )

    def _build_read_model(self, now: datetime) -> SensorReadModel:
        entity_states = {
            entity_id: self._entity_state(entity_id)
            for entity_id in {
                self.profile.root_light_entity_id,
                self.profile.pbl_switch_entity_id,
                self.profile.vacation_entity_id,
                *self.profile.target_light_entity_ids,
                *self.profile.blocker_entity_ids,
                *self.profile.source_state_entity_ids.values(),
                *(
                    (self.profile.occupancy_entity_id,)
                    if self.profile.occupancy_entity_id
                    else ()
                ),
            }
        }
        entity_states[self.profile.pbl_switch_entity_id] = (
            self._pbl_preflight_state()
        )
        next_occurrence = self._next_sensor_occurrence()
        if next_occurrence is not None:
            next_occurrence = replace(
                next_occurrence,
                wake_at=next_occurrence.wake_at.astimezone(self._timezone),
                ramp_start_at=next_occurrence.ramp_start_at.astimezone(
                    self._timezone
                ),
            )
        root = self.hass.states.get(self.profile.root_light_entity_id)
        friendly_name = (
            root.attributes.get("friendly_name")
            if root is not None
            else None
        )
        return build_sensor_read_model(
            self.state,
            self.profile,
            entity_states=entity_states,
            light_target_name=(
                str(friendly_name) if friendly_name is not None else None
            ),
            next_occurrence=next_occurrence,
            now=now,
            integration_available=self._started and not self._stopping,
            configuration_blockers=(
                ("episode_duration_exceeded",)
                if unsupported_episode_ids(calendar_windows(
                    self.profile.profile_id, runnable_alarms(self.state), now,
                    self._timezone, self.state.defaults,
                ), now)
                else ()
            ),
        )

    def _next_sensor_occurrence(self) -> ScheduledOccurrence | None:
        values = list(self._scheduled)
        if self.state.active_run is not None:
            values.extend(
                item.schedule for item in self.state.active_run.occurrences
            )
        return min(
            values,
            key=lambda item: (item.wake_at, item.occurrence_id),
            default=None,
        )

    def _schedule_next_locked(self, now: datetime) -> None:
        self._cancel_timer()
        if self._stopping:
            return
        candidates: list[datetime] = []
        run = self.state.active_run
        if run is not None:
            view = run_view(run, now)
            if self._dispatch_revoke_wait_until is not None:
                candidates.append(
                    min(
                        self._dispatch_revoke_wait_until,
                        run.next_deadline
                        or now
                        + timedelta(seconds=DEFAULT_RAMP_STEP_SECONDS),
                    )
                )
            elif view.next_deadline is not None:
                candidates.append(view.next_deadline)
        active_ids = set(run.occurrence_ids) if run is not None else set()
        candidates.extend(
            occurrence.ramp_start_at
            for occurrence in self._scheduled
            if occurrence.occurrence_id not in active_ids
            and occurrence.ramp_start_at > now.astimezone(UTC)
        )
        if (
            self.state.auto_relight_blocked_until is not None
            and now.astimezone(UTC)
            <= self.state.auto_relight_blocked_until.astimezone(UTC)
        ):
            candidates.append(
                self.state.auto_relight_blocked_until.astimezone(UTC)
                + timedelta(microseconds=1)
            )
        if self._blocked_retry_at is not None:
            candidates.append(self._blocked_retry_at)
        candidates.extend(
            record.cleanup_at for record in self.state.temporary_bed_alarms
        )
        future = [
            value.astimezone(UTC)
            for value in candidates
            if value is not None
        ]
        if not future:
            return
        next_at = min(future)
        if next_at <= now.astimezone(UTC):
            next_at = now.astimezone(UTC) + timedelta(seconds=1)
        self._timer_cancel = async_track_point_in_utc_time(
            self.hass,
            self._async_timer_fired,
            next_at,
        )

    async def _async_timer_fired(self, now: datetime) -> None:
        self._timer_cancel = None
        async with self._lock:
            self._blocked_retry_at = None
            await self._reconcile_locked(now, "timer")

    def _cancel_timer(self) -> None:
        if self._timer_cancel is not None:
            self._timer_cancel()
            self._timer_cancel = None

    def _entity_state(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        return state.state if state is not None else "missing"

    def _vacation_requires_safe_off(
        self,
        state: str | None = None,
    ) -> bool:
        current = (
            state
            if state is not None
            else self._entity_state(self.profile.vacation_entity_id)
        )
        return current not in {"off", *UNAVAILABLE_STATES, "missing"}

    def _observed_target_floor(
        self,
        entity_ids: tuple[str, ...],
    ) -> float:
        values = []
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            values.append(
                observed_brightness_pct(
                    getattr(state, "state", "missing"),
                    getattr(state, "attributes", {}).get("brightness"),
                )
            )
        return max(values, default=1.0)

    def _observe_target_floor_if_settled(
        self,
        run: ActiveRun,
        now: datetime,
    ) -> ActiveRun:
        blocked_until = self._brightness_observation_blocked_until
        if (
            blocked_until is not None
            and now.astimezone(UTC) < blocked_until
        ):
            return run
        return observe_brighter_root(
            run,
            self._observed_target_floor(run.target_entity_ids),
        )

    def _block_brightness_observation(
        self,
        now: datetime,
        transition_seconds: float,
    ) -> None:
        settle_seconds = max(
            MIN_BRIGHTNESS_SETTLE_SECONDS,
            transition_seconds - 1,
        )
        self._brightness_observation_blocked_until = (
            now.astimezone(UTC) + timedelta(seconds=settle_seconds)
        )

    def _pbl_preflight_state(self) -> str:
        state = self.hass.states.get(self.profile.pbl_switch_entity_id)
        if state is None:
            return "missing"
        if state.state != "on":
            return state.state
        mode = state.attributes.get("control_lease_mode")
        return "on" if mode == "enforce" else "not_ready"

    def _pbl_control_lease_state(self) -> str:
        state = self.hass.states.get(self.profile.pbl_switch_entity_id)
        if state is None:
            return "missing"
        lease_state = state.attributes.get("control_lease_state")
        if isinstance(lease_state, str):
            return lease_state
        run = self.state.active_run
        return (
            "active"
            if run is not None and run.generation is not None
            else "inactive"
        )

    def _occurrence_source_available(
        self,
        occurrence: ScheduledOccurrence,
    ) -> bool:
        if occurrence.source_ref is None:
            return True
        snapshot = self.state.source_cache.get(occurrence.source_ref)
        lifecycle_entity = self.profile.source_state_entity_ids.get(occurrence.source_ref)
        return bool(
            snapshot and snapshot.available and lifecycle_entity
            and self._entity_state(lifecycle_entity) not in {*UNAVAILABLE_STATES, "missing"}
        )

    def _log_correlation(
        self,
        action: str,
        run: ActiveRun,
        outcome: str,
    ) -> None:
        _LOGGER.info(
            "Wake Light %s profile=%s occurrence_ref=%s lease_ref=%s outcome=%s",
            action,
            self.profile.profile_id,
            opaque_ref("occset", ",".join(run.occurrence_ids)),
            opaque_ref("lease", run.lease_id),
            outcome,
        )

    def _emit_outcome(self, outcome: str, run: ActiveRun) -> None:
        self.hass.bus.async_fire(
            EVENT_WAKE_LIGHT_OUTCOME,
            {
                "profile_id": self.profile.profile_id,
                "outcome": outcome,
                "occurrence_ref": opaque_ref(
                    "occset",
                    ",".join(run.occurrence_ids),
                ),
                "lease_ref": opaque_ref("lease", run.lease_id),
            },
        )
