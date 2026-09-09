"""Pure occurrence resolution and SleepyPod source normalization."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo
import math

from .const import (
    ALARM_KIND_ONCE,
    ALARM_SOURCE_NATIVE,
    ALARM_SOURCE_SLEEPYPOD,
    DEFAULT_MISSED_ALARM_CATCHUP_SECONDS,
    MAX_CANCELLATION_OCCURRENCE_REFS,
    MAX_EPISODE_SECONDS,
    MAX_TERMINAL_OCCURRENCES,
    MAX_SOURCE_RECORDS_PER_SIDE,
    SOURCE_REF_PREFIX,
    SOURCE_SIDES,
    WEEKDAYS,
    WEEKDAY_TO_PYTHON,
)
from .model import (
    ProfileState,
    ScheduledOccurrence,
    SourceSnapshot,
    WakeLightAlarm,
    WakeLightDefaults,
    opaque_ref,
)


@dataclass(frozen=True)
class WallTimeResolution:
    """Resolution of one local wall time across DST transitions."""

    value: datetime
    policy: str
    ambiguous: bool = False
    shifted_minutes: int = 0


def _valid_local_candidates(
    naive: datetime,
    timezone: ZoneInfo,
) -> list[datetime]:
    candidates: dict[float, datetime] = {}
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=timezone, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(timezone)
        if round_trip.replace(tzinfo=None) != naive:
            continue
        candidates[candidate.timestamp()] = candidate
    return [candidates[key] for key in sorted(candidates)]


def resolve_wall_datetime(
    local_date: date,
    local_time: str,
    timezone: ZoneInfo,
    *,
    fold_policy: str = "first",
    gap_policy: str = "shift_forward",
) -> WallTimeResolution:
    """Resolve a local date/time with explicit deterministic DST policy.

    Ambiguous folds use the earlier physical instant by default. Nonexistent
    gap times advance minute-by-minute to the first valid wall time.
    """
    try:
        parsed_time = time.fromisoformat(local_time)
    except ValueError as err:
        raise ValueError("invalid_local_time") from err
    naive = datetime.combine(
        local_date,
        time(parsed_time.hour, parsed_time.minute),
    )
    candidates = _valid_local_candidates(naive, timezone)
    if candidates:
        if len(candidates) == 1:
            return WallTimeResolution(candidates[0], "exact")
        if fold_policy not in {"first", "second"}:
            raise ValueError("invalid_fold_policy")
        selected = candidates[0] if fold_policy == "first" else candidates[-1]
        return WallTimeResolution(
            selected,
            f"fold_{fold_policy}",
            ambiguous=True,
        )

    if gap_policy != "shift_forward":
        raise ValueError("nonexistent_local_time")
    for shifted_minutes in range(1, 181):
        shifted = naive + timedelta(minutes=shifted_minutes)
        candidates = _valid_local_candidates(shifted, timezone)
        if candidates:
            return WallTimeResolution(
                candidates[0],
                "gap_shift_forward",
                shifted_minutes=shifted_minutes,
            )
    raise ValueError("unresolvable_local_time")


def occurrence_id(
    profile_id: str,
    alarm: WakeLightAlarm,
    wake_at: datetime,
) -> str:
    """Return a stable opaque occurrence ID."""
    return opaque_ref(
        "wlocc",
        profile_id,
        alarm.id,
        alarm.source,
        alarm.source_ref or "",
        wake_at.astimezone(UTC).isoformat(),
    )


def _candidate_dates(
    alarm: WakeLightAlarm,
    local_now: datetime,
) -> Iterable[date]:
    if alarm.kind == ALARM_KIND_ONCE:
        if alarm.date is None:
            return ()
        return (date.fromisoformat(alarm.date),)
    start = local_now.date() - timedelta(days=1)
    return tuple(start + timedelta(days=offset) for offset in range(10))


def resolve_alarm_occurrence(
    profile_id: str,
    alarm: WakeLightAlarm,
    now: datetime,
    timezone: ZoneInfo,
    defaults: WakeLightDefaults,
    *,
    catchup_seconds: int = DEFAULT_MISSED_ALARM_CATCHUP_SECONDS,
    terminal_occurrence_ids: Iterable[str] = (),
    auto_relight_blocked_until: datetime | None = None,
) -> ScheduledOccurrence | None:
    """Resolve the next runnable occurrence for one alarm."""
    if not alarm.enabled:
        return None
    now_utc = now.astimezone(UTC)
    local_now = now_utc.astimezone(timezone)
    terminal = set(terminal_occurrence_ids)
    candidates: list[ScheduledOccurrence] = []
    for candidate_date in _candidate_dates(alarm, local_now):
        if (
            alarm.kind != ALARM_KIND_ONCE
            and candidate_date.weekday()
            not in {WEEKDAY_TO_PYTHON[day] for day in alarm.weekdays}
        ):
            continue
        resolution = resolve_wall_datetime(
            candidate_date,
            alarm.local_time,
            timezone,
        )
        wake_at = resolution.value.astimezone(UTC)
        ramp_start_at = wake_at - timedelta(minutes=alarm.ramp_minutes)
        resolved_id = occurrence_id(profile_id, alarm, wake_at)
        if resolved_id in terminal:
            continue
        if (
            auto_relight_blocked_until is not None
            and ramp_start_at
            <= auto_relight_blocked_until.astimezone(UTC)
        ):
            continue
        if wake_at + timedelta(seconds=catchup_seconds) < now_utc:
            continue
        candidates.append(
            ScheduledOccurrence(
                occurrence_id=resolved_id,
                alarm_id=alarm.id,
                source=alarm.source,
                source_ref=alarm.source_ref,
                wake_at=wake_at,
                ramp_start_at=ramp_start_at,
                ramp_minutes=alarm.ramp_minutes,
                hold_minutes=defaults.post_wake_hold_minutes,
                source_schedule_id=alarm.source_schedule_ids.get(
                    WEEKDAYS[(candidate_date.weekday() + 1) % 7]
                ),
            )
        )
    return min(
        candidates,
        key=lambda item: (item.ramp_start_at, item.wake_at, item.occurrence_id),
        default=None,
    )


def resolve_profile_occurrences(
    profile_id: str,
    alarms: Iterable[WakeLightAlarm],
    now: datetime,
    timezone: ZoneInfo,
    defaults: WakeLightDefaults,
    *,
    catchup_seconds: int = DEFAULT_MISSED_ALARM_CATCHUP_SECONDS,
    terminal_occurrence_ids: Iterable[str] = (),
    auto_relight_blocked_until: datetime | None = None,
) -> tuple[ScheduledOccurrence, ...]:
    """Resolve one next occurrence per enabled alarm."""
    occurrences = [
        occurrence
        for alarm in alarms
        if (
            occurrence := resolve_alarm_occurrence(
                profile_id,
                alarm,
                now,
                timezone,
                defaults,
                catchup_seconds=catchup_seconds,
                terminal_occurrence_ids=terminal_occurrence_ids,
                auto_relight_blocked_until=auto_relight_blocked_until,
            )
        )
        is not None
    ]
    return tuple(
        sorted(
            occurrences,
            key=lambda item: (
                item.ramp_start_at,
                item.wake_at,
                item.occurrence_id,
            ),
        )
    )


def stale_once_alarm_ids(
    alarms: Iterable[WakeLightAlarm],
    now: datetime,
    timezone: ZoneInfo,
    *,
    catchup_seconds: int = DEFAULT_MISSED_ALARM_CATCHUP_SECONDS,
    profile_id: str | None = None,
    terminal_occurrence_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return enabled one-time records whose only occurrence is no longer runnable."""
    now_utc = now.astimezone(UTC)
    terminal = set(terminal_occurrence_ids)
    stale: list[str] = []
    for alarm in alarms:
        if not alarm.enabled or alarm.kind != ALARM_KIND_ONCE or not alarm.date:
            continue
        wake_at = resolve_wall_datetime(
            date.fromisoformat(alarm.date),
            alarm.local_time,
            timezone,
        ).value.astimezone(UTC)
        if (
            profile_id is not None
            and occurrence_id(profile_id, alarm, wake_at) in terminal
        ):
            continue
        if wake_at + timedelta(seconds=catchup_seconds) < now_utc:
            stale.append(alarm.id)
    return tuple(stale)


def runnable_alarms(state: ProfileState) -> tuple[WakeLightAlarm, ...]:
    """Return native and enabled source records, trimmed to linked weekdays."""
    runnable: list[WakeLightAlarm] = []
    for alarm in state.public_alarms():
        if not alarm.enabled:
            continue
        if alarm.source == ALARM_SOURCE_NATIVE:
            runnable.append(alarm)
            continue
        if not state.source_alarm_linked(alarm):
            continue
        linked = state.linked_weekdays(alarm)
        if linked == alarm.weekdays:
            runnable.append(alarm)
            continue
        runnable.append(
            replace(
                alarm,
                weekdays=linked,
                source_schedule_ids={
                    day: schedule_id
                    for day, schedule_id in alarm.source_schedule_ids.items()
                    if day in linked
                },
            )
        )
    return tuple(runnable)


def day_offset(day: str, offset: int) -> str:
    """Shift one Sunday-first weekday key."""
    index = WEEKDAYS.index(day)
    return WEEKDAYS[(index + offset) % len(WEEKDAYS)]


def _normalized_time(value: Any, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    parts = value.strip().split(":")
    if len(parts) < 2:
        return fallback
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return fallback
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return fallback
    return f"{hour:02d}:{minute:02d}"


def sleepypod_alarm_executes_next_morning(
    day_schedule: Mapping[str, Any],
) -> bool:
    """SleepyPod alarm rows use execution days, independently of power rows."""
    return False


def sleepypod_wake_day(
    schedule_day: str,
    day_schedule: Mapping[str, Any],
) -> str:
    """Return the execution day used by the Pod's cron scheduler."""
    return schedule_day


def _source_alarm_values(
    day_schedule: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    alarms = day_schedule.get("alarms")
    if isinstance(alarms, list):
        records = [item for item in alarms if isinstance(item, Mapping)]
        if records:
            return records
    legacy = day_schedule.get("alarm")
    if isinstance(legacy, Mapping) and legacy.get("enabled") is True:
        return [legacy]
    return []


def parse_sleepypod_side(
    attributes: Mapping[str, Any],
    side: str,
    defaults: WakeLightDefaults,
) -> tuple[WakeLightAlarm, ...]:
    """Normalize one SleepyPod side without mutating its source schedule."""
    if side not in SOURCE_SIDES:
        raise ValueError("invalid_source_side")
    if attributes.get("alarm_day_semantics", "execution") != "execution":
        raise ValueError("source_day_semantics_unsupported")
    side_value = attributes.get(side)
    if not isinstance(side_value, Mapping):
        raise ValueError("source_side_unavailable")
    source_ref = f"{SOURCE_REF_PREFIX}{side}"
    source_label = f"SleepyPod {side.title()}"
    grouped_days: dict[tuple[str, int], dict[str, set[str]]] = {}
    schedule_ids: dict[tuple[str, int], dict[str, int]] = {}
    seen_schedule_ids: set[int] = set()
    for schedule_day in WEEKDAYS:
        day_schedule = side_value.get(schedule_day)
        if not isinstance(day_schedule, Mapping):
            continue
        wake_day = sleepypod_wake_day(schedule_day, day_schedule)
        time_ordinals: dict[str, int] = {}
        for raw_alarm in _source_alarm_values(day_schedule):
            local_time = _normalized_time(raw_alarm.get("time"))
            if not local_time:
                continue
            ordinal = time_ordinals.get(local_time, 0)
            time_ordinals[local_time] = ordinal + 1
            enabled = raw_alarm.get("enabled") is True
            group = grouped_days.setdefault(
                (local_time, ordinal),
                {"all": set(), "enabled": set()},
            )
            group["all"].add(wake_day)
            raw_id = raw_alarm.get("id")
            if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0:
                if raw_id in seen_schedule_ids:
                    raise ValueError("source_identity_ambiguous")
                seen_schedule_ids.add(raw_id)
                schedule_ids.setdefault((local_time, ordinal), {})[wake_day] = raw_id
            if enabled:
                group["enabled"].add(wake_day)

    records: list[WakeLightAlarm] = []
    for local_time, ordinal in sorted(grouped_days):
        group = grouped_days[(local_time, ordinal)]
        enabled = bool(group["enabled"])
        included_days = group["enabled"] if enabled else group["all"]
        records.append(
            WakeLightAlarm(
                id=opaque_ref(
                    "sp",
                    source_ref,
                    local_time,
                    *(() if ordinal == 0 else (ordinal,)),
                ),
                label=(
                    f"{source_label} Wake"
                    if ordinal == 0
                    else f"{source_label} Wake {ordinal + 1}"
                ),
                kind="weekly",
                local_time=local_time,
                ramp_minutes=defaults.ramp_minutes,
                revision=0,
                enabled=enabled,
                date=None,
                weekdays=tuple(
                    day for day in WEEKDAYS if day in included_days
                ),
                source=ALARM_SOURCE_SLEEPYPOD,
                source_ref=source_ref,
                source_label=source_label,
                source_schedule_ids=schedule_ids.get((local_time, ordinal), {}),
            )
        )
        if len(records) >= MAX_SOURCE_RECORDS_PER_SIDE:
            break
    return tuple(records)


def refresh_source_snapshot(
    previous: SourceSnapshot,
    source_ref: str,
    *,
    attributes: Mapping[str, Any] | None,
    available: bool,
    now: datetime,
    defaults: WakeLightDefaults,
) -> SourceSnapshot:
    """Refresh a source while retaining last-known records on unavailability."""
    observed_at = now.astimezone(UTC)
    if not available or attributes is None:
        return replace(
            previous,
            available=False,
            last_observed_at=observed_at,
            failure_code="source_unavailable",
        )
    if not source_ref.startswith(SOURCE_REF_PREFIX):
        return replace(
            previous,
            available=False,
            last_observed_at=observed_at,
            failure_code="invalid_source_ref",
        )
    side = source_ref.removeprefix(SOURCE_REF_PREFIX)
    try:
        alarms = parse_sleepypod_side(attributes, side, defaults)
    except ValueError as err:
        return replace(
            previous,
            available=False,
            last_observed_at=observed_at,
            failure_code=str(err),
        )
    identified = all(
        all(day in alarm.source_schedule_ids for day in alarm.weekdays)
        for alarm in alarms
    )
    return SourceSnapshot(
        alarms=alarms,
        available=identified,
        last_success_at=observed_at,
        last_observed_at=observed_at,
        failure_code=None if identified else "source_identity_unavailable",
        lifecycle_state=previous.lifecycle_state,
        terminal_reason=previous.terminal_reason,
        last_stopped_occurrence_id=previous.last_stopped_occurrence_id,
    )


def source_freshness_seconds(
    snapshot: SourceSnapshot,
    now: datetime,
) -> float | None:
    """Return seconds since the last successful source parse."""
    if snapshot.last_success_at is None:
        return None
    return max(
        0.0,
        (
            now.astimezone(UTC)
            - snapshot.last_success_at.astimezone(UTC)
        ).total_seconds(),
    )


def normalize_source_lifecycle(value: str | None) -> str:
    """Normalize SleepyPod lifecycle states used by the room coordinator."""
    if value in {"ringing", "snoozed", "stopped", "idle"}:
        return value
    if value in {"unknown", "unavailable", None}:
        return "unavailable"
    return "idle"


def normalize_source_terminal_reason(
    value: str | None,
    attributes: Mapping[str, Any] | None = None,
) -> str | None:
    """Normalize only explicit SleepyPod alarm terminal reasons."""
    if value == "stopped":
        return "stopped"
    if value == "expired":
        return "expired"
    raw_attributes = attributes or {}
    for key in (
        "terminal_reason",
        "terminalReason",
        "alarm_terminal_reason",
        "alarmTerminalReason",
        "termination_reason",
        "terminal_cause",
        "reason",
    ):
        reason = raw_attributes.get(key)
        if reason in {"expired", "stopped"}:
            return str(reason)
    return None


def calendar_windows(
    profile_id: str,
    alarms: Iterable[WakeLightAlarm],
    anchor: datetime,
    timezone: ZoneInfo,
    defaults: WakeLightDefaults,
) -> tuple[ScheduledOccurrence, ...]:
    """Enumerate bounded calendar intervals without catch-up or terminal filtering."""
    result: list[ScheduledOccurrence] = []
    for alarm in alarms:
        if not alarm.enabled:
            continue
        for candidate_date in _candidate_dates(alarm, anchor.astimezone(timezone)):
            if alarm.kind != ALARM_KIND_ONCE and candidate_date.weekday() not in {
                WEEKDAY_TO_PYTHON[day] for day in alarm.weekdays
            }:
                continue
            wake = resolve_wall_datetime(candidate_date, alarm.local_time, timezone).value.astimezone(UTC)
            result.append(ScheduledOccurrence(
                occurrence_id=occurrence_id(profile_id, alarm, wake),
                alarm_id=alarm.id,
                source=alarm.source,
                source_ref=alarm.source_ref,
                wake_at=wake,
                ramp_start_at=wake - timedelta(minutes=alarm.ramp_minutes),
                ramp_minutes=alarm.ramp_minutes,
                hold_minutes=defaults.post_wake_hold_minutes,
                source_schedule_id=alarm.source_schedule_ids.get(
                    WEEKDAYS[(candidate_date.weekday() + 1) % 7]
                ),
            ))
    return tuple(sorted(result, key=lambda item: (item.ramp_start_at, item.wake_at, item.occurrence_id)))


def unsupported_episode_ids(
    windows: Iterable[ScheduledOccurrence], now: datetime,
) -> tuple[str, ...]:
    """Reject connected execution windows that cannot fit the acquire-only budget."""
    start: datetime | None = None
    end: datetime | None = None
    ids: list[str] = []
    unsupported: list[str] = []
    for item in sorted(windows, key=lambda value: value.ramp_start_at):
        if item.wake_at + timedelta(seconds=DEFAULT_MISSED_ALARM_CATCHUP_SECONDS) < now:
            continue
        item_end = item.wake_at + timedelta(minutes=item.hold_minutes)
        if end is None or item.ramp_start_at > end:
            if start is not None and (end - max(start, now)).total_seconds() > MAX_EPISODE_SECONDS:
                unsupported.extend(ids)
            start, end, ids = item.ramp_start_at, item_end, [item.occurrence_id]
        else:
            end = max(end, item_end)
            ids.append(item.occurrence_id)
    if start is not None and end is not None and (end - max(start, now)).total_seconds() > MAX_EPISODE_SECONDS:
        unsupported.extend(ids)
    return tuple(unsupported)


def expand_relight_fence(
    state: ProfileState, windows: Iterable[ScheduledOccurrence],
    now: datetime | None = None,
) -> ProfileState:
    """Absorb newly discovered touching intervals before an old fence can expire."""
    cutoff = state.auto_relight_blocked_until or (
        state.last_cancellation.suppressed_until if state.last_cancellation else None
    )
    if cutoff is None:
        return state
    end = cutoff.astimezone(UTC)
    start = (
        state.auto_relight_blocked_from
        or (state.last_cancellation.at if state.last_cancellation else None)
        or end - timedelta(seconds=MAX_EPISODE_SECONDS)
    ).astimezone(UTC)
    intervals = list(windows)
    pending = list(intervals)
    component_ids: set[str] = set()
    changed = True
    while changed:
        changed = False
        remaining = []
        for item in pending:
            item_end = item.wake_at + timedelta(minutes=item.hold_minutes)
            if item.ramp_start_at <= end and item_end >= start:
                component_ids.add(item.occurrence_id)
                start = min(start, item.ramp_start_at)
                end = max(end, item_end)
                changed = True
            else:
                remaining.append(item)
        pending = remaining
    relevant_ids = component_ids if now is None else {
        item.occurrence_id for item in intervals
        if item.occurrence_id in component_ids
        and item.wake_at + timedelta(seconds=DEFAULT_MISSED_ALARM_CATCHUP_SECONDS) >= now
    }
    added = sorted(relevant_ids - set(state.terminal_occurrence_ids))
    if state.auto_relight_blocked_until is None and now is not None and end < now and not added:
        return state
    cancellation = state.last_cancellation
    if cancellation is not None:
        cancellation = replace(
            cancellation,
            occurrence_count=min(MAX_TERMINAL_OCCURRENCES, cancellation.occurrence_count + len(added)),
            occurrence_refs=tuple(dict.fromkeys((
                *cancellation.occurrence_refs,
                *(opaque_ref("occ", item) for item in added),
            )))[:MAX_CANCELLATION_OCCURRENCE_REFS],
            suppressed_until=end,
        )
    return replace(
        state.remember_terminal_occurrences(added),
        auto_relight_blocked_from=start,
        auto_relight_blocked_until=end,
        last_cancellation=cancellation,
    )


def terminal_once_alarm_ids(
    profile_id: str,
    alarms: Iterable[WakeLightAlarm],
    timezone: ZoneInfo,
    terminal_ids: Iterable[str],
    *,
    enabled_only: bool = True,
) -> set[str]:
    """Match the currently configured occurrence, never just a reused alarm ID."""
    terminal = set(terminal_ids)
    return {
        alarm.id
        for alarm in alarms
        if alarm.kind == ALARM_KIND_ONCE
        and (alarm.enabled or not enabled_only)
        and alarm.date is not None
        and occurrence_id(
            profile_id, alarm,
            resolve_wall_datetime(date.fromisoformat(alarm.date), alarm.local_time, timezone).value,
        ) in terminal
    }


def source_timestamp(value: Any, *, milliseconds: bool = False) -> datetime | None:
    """Parse the numeric Unix timestamps emitted by the Pod, without guessing units."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value / 1000 if milliseconds else value, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def source_event_matches(
    occurrence: ScheduledOccurrence,
    source_ref: str,
    attributes: Mapping[str, Any],
    now: datetime,
) -> bool:
    """Join an event to its source row and execution window, not its display time alone."""
    schedule_id = attributes.get("schedule_id")
    source_id = attributes.get("occurrence_id")
    scheduled = source_timestamp(attributes.get("scheduled_for"))
    published = source_timestamp(attributes.get("ts"), milliseconds=True)
    return bool(
        occurrence.source_ref == source_ref
        and occurrence.source_schedule_id is not None
        and not isinstance(schedule_id, bool)
        and isinstance(schedule_id, int)
        and schedule_id == occurrence.source_schedule_id
        and isinstance(source_id, str)
        and 0 < len(source_id) <= 128
        and source_id.strip() == source_id
        and scheduled is not None
        and published is not None
        and occurrence.wake_at <= now
        and occurrence.wake_at - timedelta(seconds=5) <= scheduled
        <= occurrence.wake_at + timedelta(seconds=DEFAULT_MISSED_ALARM_CATCHUP_SECONDS)
        and scheduled <= now + timedelta(seconds=5)
        and scheduled <= published <= now + timedelta(seconds=5)
    )
