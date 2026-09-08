"""Pure room state machine and safety decisions for Wake Light."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import math
from typing import Iterable, Mapping

from .const import (
    DEFAULT_MISSED_ALARM_CATCHUP_SECONDS,
    DEFAULT_RAMP_STEP_SECONDS,
    FAILURE_BLOCKER_NOT_OFF,
    FAILURE_INTEGRATION_UNAVAILABLE,
    FAILURE_INVALID_SCHEDULE,
    FAILURE_LEGACY_LIFECYCLE_UNRESOLVED,
    FAILURE_PBL_NOT_READY,
    FAILURE_TARGET_UNAVAILABLE,
    FAILURE_VACATION_BLOCKED,
    MAX_ACTIVE_OCCURRENCES,
    MAX_EPISODE_SECONDS,
    MAX_PBL_TTL_SECONDS,
    MAX_SNOOZE_BUDGET_SECONDS,
    PHASE_HOLDING,
    PHASE_RAMPING,
    PHASE_SNOOZED,
    UNAVAILABLE_STATES,
    USER_CANCELLATION_CAUSES,
)
from .model import (
    ActiveOccurrence,
    ActiveRun,
    ScheduledOccurrence,
    WakeLightProfile,
    opaque_ref,
)


@dataclass(frozen=True)
class PreflightInputs:
    """Current states required to start a room occurrence."""

    integration_available: bool
    vacation_state: str
    pbl_state: str
    target_states: Mapping[str, str]
    blocker_states: Mapping[str, str]
    valid_schedule: bool
    legacy_brightness_lifecycle_safe: bool


@dataclass(frozen=True)
class PreflightResult:
    """Stable fail-closed preflight result."""

    allowed: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class RunView:
    """Derived active-run phase, curve, and timer information."""

    phase: str
    progress: float
    desired_brightness_pct: float
    force_final_dispatch: bool
    next_deadline: datetime | None
    completed_occurrence_ids: tuple[str, ...]


@dataclass(frozen=True)
class OccurrenceView:
    """One occurrence's independent phase and curve progress."""

    phase: str
    progress: float


@dataclass(frozen=True)
class ConnectedEpisode:
    """Occurrence IDs and inclusive end of one connected interval component."""

    occurrence_ids: tuple[str, ...]
    end_at: datetime
    start_at: datetime


@dataclass(frozen=True)
class RecoveryDecision:
    """Restart decision for one persisted run."""

    action: str
    reason: str
    desired_brightness_pct: float


@dataclass(frozen=True)
class CancellationDecision:
    """Pure cancellation policy for external and safety events."""

    reason: str
    terminal_occurrence_ids: tuple[str, ...]
    release_required: bool
    turn_off_owned: bool
    user_initiated: bool


def evaluate_preflight(inputs: PreflightInputs) -> PreflightResult:
    """Require every execution safety gate while treating occupancy as advisory."""
    blockers: list[str] = []
    if not inputs.integration_available:
        blockers.append(FAILURE_INTEGRATION_UNAVAILABLE)
    if inputs.vacation_state != "off":
        blockers.append(FAILURE_VACATION_BLOCKED)
    if inputs.pbl_state not in {"on", "ready"}:
        blockers.append(FAILURE_PBL_NOT_READY)
    if not inputs.valid_schedule:
        blockers.append(FAILURE_INVALID_SCHEDULE)
    if not inputs.legacy_brightness_lifecycle_safe:
        blockers.append(FAILURE_LEGACY_LIFECYCLE_UNRESOLVED)
    if not inputs.target_states or any(
        state in UNAVAILABLE_STATES or state == "missing"
        for state in inputs.target_states.values()
    ):
        blockers.append(FAILURE_TARGET_UNAVAILABLE)
    if any(state != "off" for state in inputs.blocker_states.values()):
        blockers.append(FAILURE_BLOCKER_NOT_OFF)
    stable = tuple(dict.fromkeys(blockers))
    return PreflightResult(not stable, stable)


def fixed_lease_ttl_seconds() -> int:
    """Return a bounded acquire-only TTL for all supported v1 behavior."""
    return min(MAX_PBL_TTL_SECONDS, MAX_EPISODE_SECONDS)


def _clamp_brightness(value: float) -> float:
    return min(100.0, max(1.0, value))


def observed_brightness_pct(state: str, brightness: object) -> float:
    """Convert an observed HA root brightness into a non-dimming floor."""
    if state != "on":
        return 1.0
    try:
        raw = float(brightness)
    except (TypeError, ValueError):
        return 1.0
    return _clamp_brightness(math.ceil((raw / 255.0) * 100.0))


def start_run(
    profile: WakeLightProfile,
    occurrences: Iterable[ScheduledOccurrence],
    *,
    now: datetime,
    observed_floor_pct: float,
) -> ActiveRun:
    """Create one deterministic room run and one stable PBL lease ID."""
    now_utc = now.astimezone(UTC)
    occurrence_values = tuple(
        ActiveOccurrence(
            schedule=occurrence,
            hold_until=max(occurrence.wake_at, now_utc)
            + timedelta(minutes=occurrence.hold_minutes),
        )
        for occurrence in occurrences
    )
    if not occurrence_values:
        raise ValueError("active_occurrence_required")
    first_id = occurrence_values[0].schedule.occurrence_id
    return ActiveRun(
        lease_id=opaque_ref("wllease", profile.profile_id, first_id),
        controller_id=profile.controller_id,
        occurrences=occurrence_values[:MAX_ACTIVE_OCCURRENCES],
        target_entity_ids=profile.target_light_entity_ids,
        observed_floor_pct=_clamp_brightness(observed_floor_pct),
        next_deadline=now.astimezone(UTC),
    )


def merge_occurrences(
    run: ActiveRun,
    occurrences: Iterable[ScheduledOccurrence],
    *,
    now: datetime | None = None,
) -> ActiveRun:
    """Add overlapping occurrences without replacing the room lease or curve floor."""
    now_utc = now.astimezone(UTC) if now is not None else None
    existing = set(run.occurrence_ids)
    merged = list(run.occurrences)
    for occurrence in occurrences:
        if occurrence.occurrence_id in existing:
            continue
        merged.append(
            ActiveOccurrence(
                schedule=occurrence,
                hold_until=(
                    max(occurrence.wake_at, now_utc)
                    if now_utc is not None
                    else occurrence.wake_at
                )
                + timedelta(minutes=occurrence.hold_minutes),
            )
        )
        existing.add(occurrence.occurrence_id)
        if len(merged) >= MAX_ACTIVE_OCCURRENCES:
            break
    return replace(run, occurrences=tuple(merged))


def _scheduled_hold_until(
    occurrence: ScheduledOccurrence,
    now: datetime,
) -> datetime:
    """Mirror the effective hold used if a scheduled occurrence starts now."""
    return max(occurrence.wake_at, now.astimezone(UTC)) + timedelta(
        minutes=occurrence.hold_minutes
    )


def connected_episode(
    run: ActiveRun,
    scheduled: Iterable[ScheduledOccurrence],
    *,
    now: datetime,
) -> ConnectedEpisode:
    """Expand active intervals through every scheduled interval they touch."""
    now_utc = now.astimezone(UTC)
    active_intervals = tuple(
        (
            occurrence.schedule.occurrence_id,
            occurrence.schedule.ramp_start_at.astimezone(UTC),
            (
                occurrence.hold_until.astimezone(UTC)
                if occurrence.hold_until is not None
                else _scheduled_hold_until(occurrence.schedule, now_utc)
            ),
        )
        for occurrence in run.occurrences
    )
    component_ids = {item[0] for item in active_intervals}
    component_start = min(item[1] for item in active_intervals)
    component_end = max(item[2] for item in active_intervals)
    candidates = sorted(
        (
            (
                occurrence.occurrence_id,
                occurrence.ramp_start_at.astimezone(UTC),
                _scheduled_hold_until(occurrence, now_utc),
            )
            for occurrence in scheduled
            if occurrence.occurrence_id not in component_ids
        ),
        key=lambda item: (item[1], item[2], item[0]),
    )
    remaining = list(candidates)
    changed = True
    while changed:
        changed = False
        next_remaining = []
        for occurrence_id, starts_at, ends_at in remaining:
            if starts_at <= component_end and ends_at >= component_start:
                component_ids.add(occurrence_id)
                component_start = min(component_start, starts_at)
                component_end = max(component_end, ends_at)
                changed = True
            else:
                next_remaining.append((occurrence_id, starts_at, ends_at))
        remaining = next_remaining
    ordered_ids = tuple(
        occurrence_id
        for occurrence_id, _starts_at, _ends_at in sorted(
            (*active_intervals, *candidates),
            key=lambda item: (item[1], item[2], item[0]),
        )
        if occurrence_id in component_ids
    )
    return ConnectedEpisode(
        occurrence_ids=tuple(dict.fromkeys(ordered_ids)),
        end_at=component_end,
        start_at=component_start,
    )


def _occurrence_phase(
    occurrence: ActiveOccurrence,
    now: datetime,
) -> str:
    if (
        occurrence.snoozed_until is not None
        and now < occurrence.snoozed_until
    ):
        return PHASE_SNOOZED
    if occurrence.snoozed_until is not None:
        return PHASE_HOLDING
    if now < occurrence.schedule.wake_at:
        return PHASE_RAMPING
    return PHASE_HOLDING


def _occurrence_brightness(
    occurrence: ActiveOccurrence,
    now: datetime,
) -> float:
    if (
        occurrence.snoozed_until is not None
        and now < occurrence.snoozed_until
    ):
        return _clamp_brightness(occurrence.held_brightness_pct or 1.0)
    if occurrence.snoozed_until is not None:
        return 100.0
    if now >= occurrence.schedule.wake_at:
        return 100.0
    duration = (
        occurrence.schedule.wake_at - occurrence.schedule.ramp_start_at
    ).total_seconds()
    if duration <= 0:
        return 100.0
    elapsed = (
        now - occurrence.schedule.ramp_start_at
    ).total_seconds()
    ratio = min(1.0, max(0.0, elapsed / duration))
    return _clamp_brightness(1.0 + 99.0 * ratio)


def _occurrence_progress(
    occurrence: ActiveOccurrence,
    now: datetime,
) -> float:
    if (
        occurrence.snoozed_until is not None
        and now < occurrence.snoozed_until
    ):
        return _clamp_brightness(occurrence.held_brightness_pct or 1.0)
    if now >= occurrence.schedule.wake_at:
        return 100.0
    duration = (
        occurrence.schedule.wake_at - occurrence.schedule.ramp_start_at
    ).total_seconds()
    if duration <= 0:
        return 100.0
    elapsed = (
        now - occurrence.schedule.ramp_start_at
    ).total_seconds()
    return min(100.0, max(0.0, (elapsed / duration) * 100.0))


def occurrence_view(
    occurrence: ActiveOccurrence,
    now: datetime,
) -> OccurrenceView:
    """Return one active occurrence's phase and progress."""
    now_utc = now.astimezone(UTC)
    return OccurrenceView(
        phase=_occurrence_phase(occurrence, now_utc),
        progress=_occurrence_progress(occurrence, now_utc),
    )


def _next_step_deadline(
    run: ActiveRun,
    occurrence: ActiveOccurrence,
    now: datetime,
    step_seconds: int,
) -> datetime | None:
    phase = _occurrence_phase(occurrence, now)
    if phase == PHASE_SNOOZED:
        return occurrence.snoozed_until
    if phase == PHASE_HOLDING:
        return occurrence.hold_until
    base = run.last_command_at or occurrence.schedule.ramp_start_at
    return min(
        occurrence.schedule.wake_at,
        max(now, base + timedelta(seconds=step_seconds)),
    )


def run_view(
    run: ActiveRun,
    now: datetime,
    *,
    step_seconds: int = DEFAULT_RAMP_STEP_SECONDS,
) -> RunView:
    """Derive monotonic/max brightness and exact deadline behavior."""
    now_utc = now.astimezone(UTC)
    completed = tuple(
        occurrence.schedule.occurrence_id
        for occurrence in run.occurrences
        if occurrence.hold_until is not None and now_utc >= occurrence.hold_until
    )
    active = tuple(
        occurrence
        for occurrence in run.occurrences
        if occurrence.schedule.occurrence_id not in set(completed)
    )
    if not active:
        return RunView(
            phase=PHASE_HOLDING,
            progress=100.0,
            desired_brightness_pct=max(
                run.last_brightness_pct,
                run.observed_floor_pct,
            ),
            force_final_dispatch=False,
            next_deadline=None,
            completed_occurrence_ids=completed,
        )

    phases = tuple(_occurrence_phase(item, now_utc) for item in active)
    if PHASE_HOLDING in phases:
        phase = PHASE_HOLDING
    elif all(item == PHASE_SNOOZED for item in phases):
        phase = PHASE_SNOOZED
    else:
        phase = PHASE_RAMPING
    desired = max(
        run.observed_floor_pct,
        run.last_brightness_pct,
        *(_occurrence_brightness(item, now_utc) for item in active),
    )
    if phase == PHASE_RAMPING and desired >= 100.0:
        phase = PHASE_HOLDING
    final_due = any(
        now_utc >= (
            item.snoozed_until
            if item.snoozed_until is not None
            else item.schedule.wake_at
        )
        and not item.final_dispatched
        for item in active
    )
    deadlines = [
        deadline
        for item in active
        if (
            deadline := _next_step_deadline(
                run,
                item,
                now_utc,
                step_seconds,
            )
        )
        is not None
    ]
    return RunView(
        phase=phase,
        progress=max(_occurrence_progress(item, now_utc) for item in active),
        desired_brightness_pct=_clamp_brightness(desired),
        force_final_dispatch=final_due,
        next_deadline=min(deadlines, default=None),
        completed_occurrence_ids=completed,
    )


def remove_completed(
    run: ActiveRun,
    completed_occurrence_ids: Iterable[str],
) -> ActiveRun | None:
    """Remove terminal occurrences while retaining the shared lease for overlaps."""
    completed = set(completed_occurrence_ids)
    remaining = tuple(
        occurrence
        for occurrence in run.occurrences
        if occurrence.schedule.occurrence_id not in completed
    )
    return replace(run, occurrences=remaining) if remaining else None


def mark_acquired(
    run: ActiveRun,
    *,
    generation: int,
    acquired_at: datetime,
    expires_at: datetime,
    outcome: str,
) -> ActiveRun:
    """Record the latest compare-and-set lease generation."""
    return replace(
        run,
        generation=generation,
        lease_acquired_at=acquired_at.astimezone(UTC),
        lease_expires_at=expires_at.astimezone(UTC),
        last_pbl_outcome=outcome,
    )


def mark_dispatched(
    run: ActiveRun,
    *,
    now: datetime,
    brightness_pct: float,
    target_entity_ids: Iterable[str],
    pbl_outcome: str,
    final_dispatch: bool,
) -> ActiveRun:
    """Record one successful PBL-owned command and final-deadline coverage."""
    now_utc = now.astimezone(UTC)
    final_ids = (
        {
            item.schedule.occurrence_id
            for item in run.occurrences
            if now_utc
            >= (
                item.snoozed_until
                if item.snoozed_until is not None
                else item.schedule.wake_at
            )
        }
        if final_dispatch
        else set()
    )
    occurrences = tuple(
        replace(
            item,
            final_dispatched=(
                item.final_dispatched
                or item.schedule.occurrence_id in final_ids
            ),
        )
        for item in run.occurrences
    )
    owned = tuple(
        dict.fromkeys((*run.wake_owned_target_ids, *target_entity_ids))
    )
    return replace(
        run,
        occurrences=occurrences,
        wake_owned_target_ids=owned,
        last_brightness_pct=max(
            run.last_brightness_pct,
            _clamp_brightness(brightness_pct),
        ),
        last_command_at=now_utc,
        command_sequence=run.command_sequence + 1,
        last_pbl_outcome=pbl_outcome,
    )


def mark_hold_dispatched(
    run: ActiveRun,
    *,
    now: datetime,
    brightness_pct: float,
    target_entity_ids: Iterable[str],
    pbl_outcome: str,
) -> ActiveRun:
    """Record a snooze hold command without satisfying the final deadline."""
    owned = tuple(
        dict.fromkeys((*run.wake_owned_target_ids, *target_entity_ids))
    )
    return replace(
        run,
        wake_owned_target_ids=owned,
        last_brightness_pct=max(
            run.last_brightness_pct,
            _clamp_brightness(brightness_pct),
        ),
        last_command_at=now.astimezone(UTC),
        command_sequence=run.command_sequence + 1,
        last_pbl_outcome=pbl_outcome,
    )


def observe_brighter_root(
    run: ActiveRun,
    brightness_pct: float,
) -> ActiveRun:
    """Raise, but never lower, the observed non-dimming floor."""
    return replace(
        run,
        observed_floor_pct=max(
            run.observed_floor_pct,
            _clamp_brightness(brightness_pct),
        ),
    )


def snooze_source_occurrence(
    run: ActiveRun,
    occurrence_id: str,
    *,
    source_occurrence_id: str,
    snoozed_until: datetime,
    now: datetime,
) -> tuple[ActiveRun | None, str]:
    """Consume an absolute source deadline once, preserving any longer local hold."""
    current = next(
        (item for item in run.occurrences if item.schedule.occurrence_id == occurrence_id),
        None,
    )
    now_utc = now.astimezone(UTC)
    until = snoozed_until.astimezone(UTC)
    if (
        current is None
        or current.schedule.wake_at > now_utc
        or current.source_occurrence_id not in {None, source_occurrence_id}
    ):
        return None, "no_active_occurrence"
    if current.source_snoozed_until == until or until <= now_utc:
        return run, "no_change"
    previous_deadline = max(
        current.schedule.wake_at,
        current.snoozed_until or current.schedule.wake_at,
        current.source_snoozed_until or current.schedule.wake_at,
    )
    charge = max(0, math.ceil((until - previous_deadline).total_seconds()))
    if run.cumulative_snooze_seconds + charge > MAX_SNOOZE_BUDGET_SECONDS:
        return None, "snooze_budget_exhausted"
    effective_until = max(until, current.snoozed_until or until)
    hold_until = effective_until + timedelta(minutes=current.schedule.hold_minutes)
    if run.lease_expires_at is not None and hold_until > run.lease_expires_at:
        return None, "episode_duration_exceeded"
    updated = replace(
        current,
        source_occurrence_id=source_occurrence_id,
        source_snoozed_until=until,
        snoozed_until=effective_until,
        hold_until=hold_until,
        held_brightness_pct=max(run.observed_floor_pct, run.last_brightness_pct, 1.0),
        final_dispatched=False,
    )
    return replace(
        run,
        occurrences=tuple(updated if item is current else item for item in run.occurrences),
        cumulative_snooze_seconds=run.cumulative_snooze_seconds + charge,
        next_deadline=effective_until,
    ), "accepted"


def remove_occurrence(
    run: ActiveRun,
    occurrence_id: str,
) -> tuple[ActiveRun | None, str]:
    """Cancel or dismiss one occurrence from the shared room set."""
    remaining = tuple(
        item
        for item in run.occurrences
        if item.schedule.occurrence_id != occurrence_id
    )
    if len(remaining) == len(run.occurrences):
        return None, "no_active_occurrence"
    return (replace(run, occurrences=remaining) if remaining else None, "accepted")


def release_leaf(
    run: ActiveRun,
    entity_id: str,
) -> tuple[ActiveRun | None, str]:
    """Permanently remove one foreign-off leaf for the current occurrence set."""
    if entity_id not in run.target_entity_ids:
        return run, "no_change"
    targets = tuple(item for item in run.target_entity_ids if item != entity_id)
    owned = tuple(
        item for item in run.wake_owned_target_ids if item != entity_id
    )
    released = tuple(dict.fromkeys((*run.released_target_ids, entity_id)))
    if not targets:
        return None, "no_owned_targets"
    return (
        replace(
            run,
            target_entity_ids=targets,
            wake_owned_target_ids=owned,
            released_target_ids=released,
        ),
        "updated",
    )


def cancellation_decision(
    run: ActiveRun,
    reason: str,
) -> CancellationDecision:
    """Map fail-closed hazards to release and safe-off behavior."""
    if reason in USER_CANCELLATION_CAUSES:
        return CancellationDecision(
            reason=reason,
            terminal_occurrence_ids=run.occurrence_ids,
            release_required=False,
            turn_off_owned=False,
            user_initiated=True,
        )
    return CancellationDecision(
        reason=reason,
        terminal_occurrence_ids=run.occurrence_ids,
        release_required=True,
        turn_off_owned=False,
        user_initiated=False,
    )


def restart_decision(
    run: ActiveRun,
    now: datetime,
    *,
    catchup_seconds: int = DEFAULT_MISSED_ALARM_CATCHUP_SECONDS,
) -> RecoveryDecision:
    """Decide whether restart recovery may resume the existing curve."""
    now_utc = now.astimezone(UTC)
    if run.lease_expires_at is None:
        return RecoveryDecision(
            "missed",
            "lease_expiry_missing",
            run.last_brightness_pct,
        )
    if now_utc >= run.lease_expires_at:
        return RecoveryDecision(
            "missed",
            "lease_expired",
            run.last_brightness_pct,
        )
    latest_recoverable = max(
        item.snoozed_until or item.schedule.wake_at
        for item in run.occurrences
    ) + timedelta(seconds=catchup_seconds)
    if now_utc > latest_recoverable:
        return RecoveryDecision(
            "missed",
            "catchup_expired",
            run.last_brightness_pct,
        )
    view = run_view(run, now_utc)
    return RecoveryDecision(
        "resume",
        "within_catchup",
        view.desired_brightness_pct,
    )


def refresh_recovery_holds(
    run: ActiveRun,
    now: datetime,
    *,
    catchup_seconds: int = DEFAULT_MISSED_ALARM_CATCHUP_SECONDS,
) -> ActiveRun:
    """Give a missed final dispatch its full hold after recovery succeeds."""
    now_utc = now.astimezone(UTC)
    occurrences = []
    for occurrence in run.occurrences:
        deadline = occurrence.snoozed_until or occurrence.schedule.wake_at
        if (
            not occurrence.final_dispatched
            and now_utc >= deadline
            and now_utc <= deadline + timedelta(seconds=catchup_seconds)
        ):
            occurrence = replace(
                occurrence,
                hold_until=now_utc
                + timedelta(minutes=occurrence.schedule.hold_minutes),
            )
        occurrences.append(occurrence)
    return replace(run, occurrences=tuple(occurrences))
