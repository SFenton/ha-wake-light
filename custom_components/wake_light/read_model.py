"""Pure sensor state and attribute serialization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .const import (
    FAILURE_BLOCKER_NOT_OFF,
    FAILURE_INTEGRATION_UNAVAILABLE,
    FAILURE_LEGACY_LIFECYCLE_UNRESOLVED,
    FAILURE_PBL_NOT_READY,
    FAILURE_VACATION_BLOCKED,
    MAX_EPISODE_SECONDS,
    PHASE_BLOCKED_VACATION,
    PHASE_DEGRADED,
    PHASE_IDLE,
    PHASE_SCHEDULED,
    PHASE_UNAVAILABLE,
    UNAVAILABLE_STATES,
)
from .engine import occurrence_view, run_view
from .model import ProfileState, ScheduledOccurrence, WakeLightProfile, opaque_ref


@dataclass(frozen=True)
class SensorReadModel:
    """One exact sensor state/attribute payload."""

    state: str
    attributes: Mapping[str, Any]


def _state_or_missing(
    states: Mapping[str, str],
    entity_id: str | None,
) -> str:
    if not entity_id:
        return "missing"
    return states.get(entity_id, "missing")


def _required_available(
    states: Mapping[str, str],
    entity_ids: tuple[str, ...],
) -> bool:
    return all(
        states.get(entity_id, "missing")
        not in {*UNAVAILABLE_STATES, "missing"}
        for entity_id in entity_ids
    )


def _commanded_progress(brightness_pct: float) -> float:
    if brightness_pct <= 0:
        return 0.0
    return min(100.0, max(0.0, ((brightness_pct - 1.0) / 99.0) * 100.0))


def build_sensor_read_model(
    state: ProfileState,
    profile: WakeLightProfile,
    *,
    entity_states: Mapping[str, str],
    light_target_name: str | None,
    next_occurrence: ScheduledOccurrence | None,
    now: datetime,
    integration_available: bool = True,
    configuration_blockers: tuple[str, ...] = (),
) -> SensorReadModel:
    """Serialize the exact React contract without exposing internal lease data."""
    required_entities = (
        profile.root_light_entity_id,
        profile.pbl_switch_entity_id,
        profile.vacation_entity_id,
        *profile.target_light_entity_ids,
    )
    available = integration_available and _required_available(
        entity_states,
        required_entities,
    )
    vacation_state = _state_or_missing(
        entity_states,
        profile.vacation_entity_id,
    )
    pbl_raw = _state_or_missing(
        entity_states,
        profile.pbl_switch_entity_id,
    )
    pbl_state = "ready" if pbl_raw == "on" else pbl_raw
    target_states = [
        _state_or_missing(entity_states, entity_id)
        for entity_id in profile.target_light_entity_ids
    ]
    light_state = (
        "ready"
        if _required_available(entity_states, required_entities[:1])
        and all(
            item not in {*UNAVAILABLE_STATES, "missing"}
            for item in target_states
        )
        else "unavailable"
    )
    occupancy_state = (
        _state_or_missing(entity_states, profile.occupancy_entity_id)
        if profile.occupancy_entity_id
        else "not_configured"
    )
    blocker_not_off = any(
        _state_or_missing(entity_states, entity_id) != "off"
        for entity_id in profile.blocker_entity_ids
    )
    pbl_not_ready = pbl_raw != "on"
    current_blockers = list(configuration_blockers)
    if not integration_available:
        current_blockers.append(FAILURE_INTEGRATION_UNAVAILABLE)
    if light_state != "ready":
        current_blockers.append("target_unavailable")
    if vacation_state != "off":
        current_blockers.append(FAILURE_VACATION_BLOCKED)
    if pbl_not_ready:
        current_blockers.append(FAILURE_PBL_NOT_READY)
    if blocker_not_off:
        current_blockers.append(FAILURE_BLOCKER_NOT_OFF)
    if not profile.legacy_brightness_lifecycle_safe:
        current_blockers.append(FAILURE_LEGACY_LIFECYCLE_UNRESOLVED)
    for source_ref in profile.source_refs:
        if not state.has_linked_alarms(source_ref):
            continue
        snapshot = state.source_cache.get(source_ref)
        if snapshot is None or not snapshot.available:
            current_blockers.append(
                snapshot.failure_code if snapshot and snapshot.failure_code else "source_unavailable"
            )
        if _state_or_missing(
            entity_states, profile.source_state_entity_ids.get(source_ref),
        ) in {*UNAVAILABLE_STATES, "missing"}:
            current_blockers.append("source_lifecycle_unavailable")

    commanded_brightness_pct = (
        state.active_run.last_brightness_pct
        if state.active_run is not None
        else 0.0
    )
    progress = _commanded_progress(commanded_brightness_pct)
    if not available:
        phase = PHASE_UNAVAILABLE
    elif state.active_run is not None:
        active_view = run_view(state.active_run, now)
        phase = (
            "recovering"
            if state.active_run.recovery_decision == "pending_reacquire"
            else active_view.phase
        )
    elif next_occurrence is not None and vacation_state != "off":
        phase = PHASE_BLOCKED_VACATION
    elif current_blockers:
        phase = PHASE_DEGRADED
    elif next_occurrence is not None:
        phase = PHASE_SCHEDULED
    else:
        phase = PHASE_IDLE

    failure_codes = [failure.code for failure in state.failures]
    if (
        not profile.legacy_brightness_lifecycle_safe
        and FAILURE_LEGACY_LIFECYCLE_UNRESOLVED not in failure_codes
    ):
        failure_codes.append(FAILURE_LEGACY_LIFECYCLE_UNRESOLVED)
    if blocker_not_off and FAILURE_BLOCKER_NOT_OFF not in failure_codes:
        failure_codes.append(FAILURE_BLOCKER_NOT_OFF)
    if pbl_not_ready and FAILURE_PBL_NOT_READY not in failure_codes:
        failure_codes.append(FAILURE_PBL_NOT_READY)
    for source_ref in profile.source_refs:
        if not state.has_linked_alarms(source_ref):
            continue
        snapshot = state.source_cache.get(source_ref)
        if (
            snapshot is not None
            and snapshot.failure_code
            and snapshot.failure_code not in failure_codes
        ):
            failure_codes.append(snapshot.failure_code)

    return SensorReadModel(
        state=phase,
        attributes={
            "contract_version": 4,
            "available": available,
            "command_available": integration_available,
            "profile_id": profile.profile_id,
            "revision": state.revision,
            "alarms": [alarm.to_dict() for alarm in state.public_alarms()],
            "defaults": state.defaults.to_dict(),
            "next_wake_at": (
                next_occurrence.wake_at.isoformat()
                if next_occurrence is not None
                else None
            ),
            "next_ramp_minutes": next_occurrence.ramp_minutes if next_occurrence else None,
            "episode_ref": (
                opaque_ref("episode", state.active_run.lease_id)
                if state.active_run is not None else None
            ),
            "progress": round(min(100.0, max(0.0, progress)), 2),
            "commanded_brightness_pct": round(
                min(100.0, max(0.0, commanded_brightness_pct)),
                2,
            ),
            "active_occurrences": (
                [
                    {
                        "alarm_id": occurrence.schedule.alarm_id,
                        "occurrence_id": occurrence.schedule.occurrence_id,
                        "phase": occurrence_view(occurrence, now).phase,
                        "progress": round(
                            occurrence_view(occurrence, now).progress,
                            2,
                        ),
                        "snoozed_until": (
                            occurrence.snoozed_until.isoformat()
                            if occurrence.snoozed_until is not None
                            else None
                        ),
                        "source_ref": occurrence.schedule.source_ref,
                        "wake_at": occurrence.schedule.wake_at.isoformat(),
                    }
                    for occurrence in state.active_run.occurrences
                ]
                if state.active_run is not None
                else []
            ),
            "auto_relight_blocked_until": (
                state.auto_relight_blocked_until.isoformat()
                if state.auto_relight_blocked_until is not None
                else None
            ),
            "last_outcome": state.last_outcome,
            "last_failure": state.failures[-1].to_dict() if state.failures else None,
            "current_blockers": list(dict.fromkeys(current_blockers)),
            "limits": {
                "maximum_episode_minutes": MAX_EPISODE_SECONDS // 60,
                "interruption_policy": "fail_closed",
            },
            "last_cancellation": (
                state.last_cancellation.to_dict()
                if state.last_cancellation is not None
                else None
            ),
            "failures": failure_codes[-16:],
            "safety": {
                "vacation_state": vacation_state,
                "pbl_state": pbl_state,
                "occupancy_state": occupancy_state,
                "light_state": light_state,
                "light_target_name": light_target_name,
            },
            "alarm_links": dict(state.alarm_links),
        },
    )
