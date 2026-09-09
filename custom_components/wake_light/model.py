"""Pure data model for the Wake Light integration."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from .const import (
    ALARM_KIND_WEEKLY,
    ALARM_KINDS,
    ALARM_SOURCE_NATIVE,
    ALARM_SOURCE_SLEEPYPOD,
    ALARM_SOURCES,
    CONF_AREA_ID,
    CONF_BLOCKER_ENTITY_IDS,
    CONF_DEFAULT_POST_WAKE_HOLD_MINUTES,
    CONF_DEFAULT_RAMP_MINUTES,
    CONF_LEGACY_BRIGHTNESS_LIFECYCLE_SAFE,
    CONF_OCCUPANCY_ENTITY_ID,
    CONF_PBL_SWITCH_ENTITY_ID,
    CONF_PROFILE_ID,
    CONF_PROFILE_NAME,
    CONF_ROOT_LIGHT_ENTITY_ID,
    CONF_SLEEPYPOD_LEFT_STATE_ENTITY_ID,
    CONF_SLEEPYPOD_RIGHT_STATE_ENTITY_ID,
    CONF_SLEEPYPOD_SCHEDULE_ENTITY_ID,
    CONF_SLEEPYPOD_SOURCE_SIDES,
    CONF_TARGET_LIGHT_ENTITY_IDS,
    CONF_VACATION_ENTITY_ID,
    DEFAULT_POST_WAKE_HOLD_MINUTES,
    DEFAULT_RAMP_MINUTES,
    FAILURE_MANUAL_REVOKE,
    MAX_ACTIVE_OCCURRENCES,
    MAX_ALARM_LINKS,
    MAX_CANCELLATION_OCCURRENCE_REFS,
    MAX_FAILURES,
    MAX_NATIVE_ALARMS,
    MAX_POST_WAKE_HOLD_MINUTES,
    MAX_RAMP_MINUTES,
    MAX_REQUEST_HISTORY,
    MAX_TERMINAL_OCCURRENCES,
    MAX_TARGET_LIGHTS,
    MIN_POST_WAKE_HOLD_MINUTES,
    MIN_RAMP_MINUTES,
    OUTCOME_CANCELLED_BY_USER,
    SOURCE_REF_PREFIX,
    SOURCE_SIDE_LEFT,
    SOURCE_SIDE_RIGHT,
    SOURCE_SIDES,
    STORE_VERSION,
    WEEKDAYS,
)

_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_LOCAL_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")
_ALARM_LINK_KEY_RE = re.compile(
    r"^(?P<source_ref>[a-z0-9_]+:[a-z0-9_]+)#(?P<weekday>[a-z]+)#(?P<local_time>(?:[01]\d|2[0-3]):[0-5]\d)$"
)


def alarm_link_key(source_ref: str, weekday: str, local_time: str) -> str:
    """Return the stable per-source-alarm wake-light link key.

    The key is derived from the source side, the execution weekday and the local
    wake time so both Home Assistant and the dashboard can compute it without a
    source-owned row identifier.
    """
    return f"{source_ref}#{weekday}#{local_time}"


def parse_alarm_link_key(value: str) -> tuple[str, str, str]:
    """Split one link key, raising ValueError when malformed."""
    match = _ALARM_LINK_KEY_RE.fullmatch(value)
    if match is None or match.group("weekday") not in WEEKDAYS:
        raise ValueError("invalid_alarm_link_key")
    return (
        match.group("source_ref"),
        match.group("weekday"),
        match.group("local_time"),
    )


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Normalize an aware datetime to UTC."""
    if value.tzinfo is None:
        raise ValueError("datetime_must_be_aware")
    return value.astimezone(UTC)


def parse_datetime(value: Any) -> datetime | None:
    """Parse an aware ISO datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def iso_or_none(value: datetime | None) -> str | None:
    """Serialize an aware timestamp."""
    return value.isoformat() if value is not None else None


def opaque_ref(prefix: str, *parts: object, length: int = 24) -> str:
    """Build a bounded deterministic correlation reference."""
    digest = hashlib.sha256(
        "\x1f".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()
    return f"{prefix}-{digest[:length]}"


def request_key(request_id: str) -> str:
    """Hash a caller request ID before persistence or logging."""
    return opaque_ref("req", request_id)


def payload_fingerprint(payload: Mapping[str, Any]) -> str:
    """Hash one canonical command payload."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )
    return opaque_ref("payload", encoded, length=32)


def _as_string(value: Any, *, code: str, maximum: int = 128) -> str:
    if not isinstance(value, str):
        raise ValueError(code)
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(code)
    return normalized


def _entity_id(
    value: Any,
    *,
    code: str,
    domain: str | None = None,
    optional: bool = False,
) -> str | None:
    if optional and (value is None or value == ""):
        return None
    entity_id = _as_string(value, code=code)
    if not _ENTITY_ID_RE.fullmatch(entity_id):
        raise ValueError(code)
    if domain is not None and not entity_id.startswith(f"{domain}."):
        raise ValueError(code)
    return entity_id


def _entity_ids(
    value: Any,
    *,
    code: str,
    domain: str | None = None,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple, set)):
        raise ValueError(code)
    result = tuple(
        dict.fromkeys(
            _entity_id(item, code=code, domain=domain) for item in value
        )
    )
    return tuple(item for item in result if item is not None)


def _bounded_number(
    value: Any,
    *,
    code: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise ValueError(code)
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(code)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as err:
        raise ValueError(code) from err
    if parsed < minimum or parsed > maximum:
        raise ValueError(code)
    return parsed


def _local_time(value: Any) -> str:
    parsed = _as_string(value, code="invalid_local_time", maximum=5)
    if not _LOCAL_TIME_RE.fullmatch(parsed):
        raise ValueError("invalid_local_time")
    return parsed


@dataclass(frozen=True)
class WakeLightDefaults:
    """Per-profile default alarm behavior."""

    ramp_minutes: int = DEFAULT_RAMP_MINUTES
    post_wake_hold_minutes: int = DEFAULT_POST_WAKE_HOLD_MINUTES

    def __post_init__(self) -> None:
        _bounded_number(
            self.ramp_minutes,
            code="invalid_ramp_minutes",
            minimum=MIN_RAMP_MINUTES,
            maximum=MAX_RAMP_MINUTES,
        )
        _bounded_number(
            self.post_wake_hold_minutes,
            code="invalid_post_wake_hold_minutes",
            minimum=MIN_POST_WAKE_HOLD_MINUTES,
            maximum=MAX_POST_WAKE_HOLD_MINUTES,
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any] | None,
        fallback: WakeLightDefaults | None = None,
    ) -> WakeLightDefaults:
        """Parse defaults from persisted or service data."""
        base = fallback or cls()
        raw = value or {}
        return cls(
            ramp_minutes=_bounded_number(
                raw.get("ramp_minutes", base.ramp_minutes),
                code="invalid_ramp_minutes",
                minimum=MIN_RAMP_MINUTES,
                maximum=MAX_RAMP_MINUTES,
            ),
            post_wake_hold_minutes=_bounded_number(
                raw.get(
                    "post_wake_hold_minutes",
                    base.post_wake_hold_minutes,
                ),
                code="invalid_post_wake_hold_minutes",
                minimum=MIN_POST_WAKE_HOLD_MINUTES,
                maximum=MAX_POST_WAKE_HOLD_MINUTES,
            ),
        )

    def to_dict(self) -> dict[str, int]:
        """Serialize the public/store defaults contract."""
        return {
            "ramp_minutes": self.ramp_minutes,
            "post_wake_hold_minutes": self.post_wake_hold_minutes,
        }


@dataclass(frozen=True)
class WakeLightAlarm:
    """One native or read-only source alarm."""

    id: str
    label: str
    kind: str
    local_time: str
    ramp_minutes: int
    revision: int = 0
    enabled: bool = True
    date: str | None = None
    weekdays: tuple[str, ...] = ()
    source: str = ALARM_SOURCE_NATIVE
    source_ref: str | None = None
    source_label: str | None = None
    source_schedule_ids: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _OPAQUE_ID_RE.fullmatch(self.id):
            raise ValueError("invalid_alarm_id")
        _as_string(self.label, code="invalid_alarm_label", maximum=80)
        if self.kind not in ALARM_KINDS:
            raise ValueError("invalid_alarm_kind")
        _local_time(self.local_time)
        _bounded_number(
            self.ramp_minutes,
            code="invalid_ramp_minutes",
            minimum=MIN_RAMP_MINUTES,
            maximum=MAX_RAMP_MINUTES,
        )
        if self.revision < 0:
            raise ValueError("invalid_alarm_revision")
        if self.source not in ALARM_SOURCES:
            raise ValueError("invalid_alarm_source")
        if self.kind == ALARM_KIND_WEEKLY:
            if not self.weekdays or any(day not in WEEKDAYS for day in self.weekdays):
                raise ValueError("invalid_alarm_weekdays")
            if self.date is not None:
                raise ValueError("weekly_alarm_has_date")
        else:
            if self.weekdays:
                raise ValueError("once_alarm_has_weekdays")
            if self.date is None:
                raise ValueError("once_alarm_missing_date")
            try:
                date.fromisoformat(self.date)
            except ValueError as err:
                raise ValueError("invalid_alarm_date") from err
        if self.source == ALARM_SOURCE_SLEEPYPOD and not self.source_ref:
            raise ValueError("source_alarm_missing_ref")
        if any(
            day not in WEEKDAYS
            or isinstance(schedule_id, bool)
            or not isinstance(schedule_id, int)
            or schedule_id < 1
            for day, schedule_id in self.source_schedule_ids.items()
        ):
            raise ValueError("invalid_source_schedule_ids")

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        defaults: WakeLightDefaults,
        *,
        require_native: bool = False,
    ) -> WakeLightAlarm:
        """Parse an alarm from Store or command data."""
        alarm_id = _as_string(value.get("id"), code="invalid_alarm_id")
        if not _OPAQUE_ID_RE.fullmatch(alarm_id):
            raise ValueError("invalid_alarm_id")
        label = _as_string(
            value.get("label"),
            code="invalid_alarm_label",
            maximum=80,
        )
        kind = value.get("kind")
        if kind not in ALARM_KINDS:
            raise ValueError("invalid_alarm_kind")
        source = value.get("source", ALARM_SOURCE_NATIVE)
        if source not in ALARM_SOURCES:
            raise ValueError("invalid_alarm_source")
        if require_native and source != ALARM_SOURCE_NATIVE:
            raise ValueError("read_only_source")

        raw_weekdays = value.get("weekdays", [])
        if not isinstance(raw_weekdays, (list, tuple)):
            raise ValueError("invalid_alarm_weekdays")
        if any(
            not isinstance(day, str) or day not in WEEKDAYS
            for day in raw_weekdays
        ):
            raise ValueError("invalid_alarm_weekdays")
        weekdays = tuple(
            day for day in WEEKDAYS if day in dict.fromkeys(raw_weekdays)
        )
        raw_date = value.get("date")
        alarm_date = raw_date.strip() if isinstance(raw_date, str) else None
        source_ref = value.get("source_ref")
        source_label = value.get("source_label")
        return cls(
            id=alarm_id,
            label=label,
            kind=kind,
            local_time=_local_time(value.get("local_time")),
            ramp_minutes=_bounded_number(
                value.get("ramp_minutes", defaults.ramp_minutes),
                code="invalid_ramp_minutes",
                minimum=MIN_RAMP_MINUTES,
                maximum=MAX_RAMP_MINUTES,
            ),
            revision=_bounded_number(
                value.get("revision", 0),
                code="invalid_alarm_revision",
                minimum=0,
                maximum=2_147_483_647,
            ),
            enabled=value.get("enabled") is not False,
            date=alarm_date or None,
            weekdays=weekdays,
            source=source,
            source_ref=(
                _as_string(
                    source_ref,
                    code="invalid_source_ref",
                    maximum=64,
                )
                if source_ref
                else None
            ),
            source_label=(
                _as_string(
                    source_label,
                    code="invalid_source_label",
                    maximum=80,
                )
                if source_label
                else None
            ),
            source_schedule_ids=(
                dict(value["source_schedule_ids"])
                if isinstance(value.get("source_schedule_ids"), Mapping)
                else {}
            ),
        )

    def with_revision(self, revision: int) -> WakeLightAlarm:
        """Return a copy with a new per-alarm revision."""
        return replace(self, revision=revision)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the exact React alarm contract."""
        return {
            "date": self.date,
            "enabled": self.enabled,
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "local_time": self.local_time,
            "ramp_minutes": self.ramp_minutes,
            "revision": self.revision,
            "source": self.source,
            "source_label": self.source_label,
            "source_ref": self.source_ref,
            "weekdays": list(self.weekdays),
            **(
                {"source_schedule_ids": dict(self.source_schedule_ids)}
                if self.source == ALARM_SOURCE_SLEEPYPOD
                else {}
            ),
        }


@dataclass(frozen=True)
class ScheduledOccurrence:
    """One resolved alarm occurrence."""

    occurrence_id: str
    alarm_id: str
    source: str
    source_ref: str | None
    wake_at: datetime
    ramp_start_at: datetime
    ramp_minutes: int
    hold_minutes: int
    source_schedule_id: int | None = None

    def __post_init__(self) -> None:
        if not _OPAQUE_ID_RE.fullmatch(self.occurrence_id):
            raise ValueError("invalid_occurrence_id")
        if not _OPAQUE_ID_RE.fullmatch(self.alarm_id):
            raise ValueError("invalid_alarm_id")
        if self.source not in ALARM_SOURCES:
            raise ValueError("invalid_alarm_source")
        if self.source == ALARM_SOURCE_SLEEPYPOD and not self.source_ref:
            raise ValueError("source_occurrence_missing_ref")
        as_utc(self.wake_at)
        as_utc(self.ramp_start_at)
        if self.ramp_start_at > self.wake_at:
            raise ValueError("invalid_occurrence_window")
        if self.source_schedule_id is not None and (
            isinstance(self.source_schedule_id, bool)
            or not isinstance(self.source_schedule_id, int)
            or self.source_schedule_id < 1
        ):
            raise ValueError("invalid_source_schedule_id")

    def to_dict(self) -> dict[str, Any]:
        """Serialize an occurrence for active-state recovery."""
        return {
            "occurrence_id": self.occurrence_id,
            "alarm_id": self.alarm_id,
            "source": self.source,
            "source_ref": self.source_ref,
            "wake_at": self.wake_at.isoformat(),
            "ramp_start_at": self.ramp_start_at.isoformat(),
            "ramp_minutes": self.ramp_minutes,
            "hold_minutes": self.hold_minutes,
            "source_schedule_id": self.source_schedule_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ScheduledOccurrence:
        """Restore an occurrence."""
        wake_at = parse_datetime(value.get("wake_at"))
        ramp_start_at = parse_datetime(value.get("ramp_start_at"))
        if wake_at is None or ramp_start_at is None:
            raise ValueError("invalid_occurrence_timestamp")
        return cls(
            occurrence_id=_as_string(
                value.get("occurrence_id"),
                code="invalid_occurrence_id",
            ),
            alarm_id=_as_string(
                value.get("alarm_id"),
                code="invalid_alarm_id",
            ),
            source=_as_string(
                value.get("source"),
                code="invalid_alarm_source",
            ),
            source_ref=(
                _as_string(
                    value.get("source_ref"),
                    code="invalid_source_ref",
                    maximum=64,
                )
                if value.get("source_ref")
                else None
            ),
            wake_at=wake_at,
            ramp_start_at=ramp_start_at,
            ramp_minutes=_bounded_number(
                value.get("ramp_minutes"),
                code="invalid_ramp_minutes",
                minimum=MIN_RAMP_MINUTES,
                maximum=MAX_RAMP_MINUTES,
            ),
            hold_minutes=_bounded_number(
                value.get("hold_minutes"),
                code="invalid_post_wake_hold_minutes",
                minimum=MIN_POST_WAKE_HOLD_MINUTES,
                maximum=MAX_POST_WAKE_HOLD_MINUTES,
            ),
            source_schedule_id=value.get("source_schedule_id"),
        )


@dataclass(frozen=True)
class ActiveOccurrence:
    """Runtime state for one occurrence in a shared room run."""

    schedule: ScheduledOccurrence
    snoozed_until: datetime | None = None
    hold_until: datetime | None = None
    held_brightness_pct: float | None = None
    final_dispatched: bool = False
    source_occurrence_id: str | None = None
    source_snoozed_until: datetime | None = None
    source_updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize active occurrence state."""
        return {
            "schedule": self.schedule.to_dict(),
            "snoozed_until": iso_or_none(self.snoozed_until),
            "hold_until": iso_or_none(self.hold_until),
            "held_brightness_pct": self.held_brightness_pct,
            "final_dispatched": self.final_dispatched,
            "source_occurrence_id": self.source_occurrence_id,
            "source_snoozed_until": iso_or_none(self.source_snoozed_until),
            "source_updated_at": iso_or_none(self.source_updated_at),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ActiveOccurrence:
        """Restore active occurrence state."""
        raw_schedule = value.get("schedule")
        if not isinstance(raw_schedule, Mapping):
            raise ValueError("invalid_active_occurrence")
        return cls(
            schedule=ScheduledOccurrence.from_dict(raw_schedule),
            snoozed_until=parse_datetime(value.get("snoozed_until")),
            hold_until=parse_datetime(value.get("hold_until")),
            held_brightness_pct=(
                float(value["held_brightness_pct"])
                if value.get("held_brightness_pct") is not None
                else None
            ),
            final_dispatched=value.get("final_dispatched") is True,
            source_occurrence_id=(
                value["source_occurrence_id"]
                if isinstance(value.get("source_occurrence_id"), str)
                and _OPAQUE_ID_RE.fullmatch(value["source_occurrence_id"])
                else None
            ),
            source_snoozed_until=parse_datetime(value.get("source_snoozed_until")),
            source_updated_at=parse_datetime(value.get("source_updated_at")),
        )


@dataclass(frozen=True)
class ActiveRun:
    """One root-scoped run and its PBL lease state."""

    lease_id: str
    controller_id: str
    occurrences: tuple[ActiveOccurrence, ...]
    target_entity_ids: tuple[str, ...]
    wake_owned_target_ids: tuple[str, ...] = ()
    released_target_ids: tuple[str, ...] = ()
    generation: int | None = None
    lease_acquired_at: datetime | None = None
    lease_expires_at: datetime | None = None
    observed_floor_pct: float = 1.0
    last_brightness_pct: float = 0.0
    last_command_at: datetime | None = None
    next_deadline: datetime | None = None
    cumulative_snooze_seconds: int = 0
    command_sequence: int = 0
    last_pbl_outcome: str | None = None
    recovery_decision: str | None = None

    def __post_init__(self) -> None:
        if not _OPAQUE_ID_RE.fullmatch(self.lease_id):
            raise ValueError("invalid_lease_id")
        if not _OPAQUE_ID_RE.fullmatch(self.controller_id):
            raise ValueError("invalid_controller_id")
        if not self.occurrences or len(self.occurrences) > MAX_ACTIVE_OCCURRENCES:
            raise ValueError("invalid_active_occurrences")
        if not self.target_entity_ids:
            raise ValueError("invalid_active_targets")
        if len(self.target_entity_ids) > MAX_TARGET_LIGHTS:
            raise ValueError("invalid_active_targets")
        if not set(self.wake_owned_target_ids).issubset(
            self.target_entity_ids
        ):
            raise ValueError("invalid_wake_owned_targets")
        if set(self.released_target_ids).intersection(self.target_entity_ids):
            raise ValueError("invalid_released_targets")
        if self.generation is not None and self.generation < 1:
            raise ValueError("invalid_lease_generation")

    @property
    def occurrence_ids(self) -> tuple[str, ...]:
        """Return active occurrence IDs."""
        return tuple(item.schedule.occurrence_id for item in self.occurrences)

    def to_dict(self) -> dict[str, Any]:
        """Serialize restart-critical active state."""
        return {
            "lease_id": self.lease_id,
            "controller_id": self.controller_id,
            "occurrences": [item.to_dict() for item in self.occurrences],
            "target_entity_ids": list(self.target_entity_ids),
            "wake_owned_target_ids": list(self.wake_owned_target_ids),
            "released_target_ids": list(self.released_target_ids),
            "generation": self.generation,
            "lease_acquired_at": iso_or_none(self.lease_acquired_at),
            "lease_expires_at": iso_or_none(self.lease_expires_at),
            "observed_floor_pct": self.observed_floor_pct,
            "last_brightness_pct": self.last_brightness_pct,
            "last_command_at": iso_or_none(self.last_command_at),
            "next_deadline": iso_or_none(self.next_deadline),
            "cumulative_snooze_seconds": self.cumulative_snooze_seconds,
            "command_sequence": self.command_sequence,
            "last_pbl_outcome": self.last_pbl_outcome,
            "recovery_decision": self.recovery_decision,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ActiveRun:
        """Restore an active run."""
        raw_occurrences = value.get("occurrences")
        if not isinstance(raw_occurrences, list):
            raise ValueError("invalid_active_occurrences")
        return cls(
            lease_id=_as_string(value.get("lease_id"), code="invalid_lease_id"),
            controller_id=_as_string(
                value.get("controller_id"),
                code="invalid_controller_id",
            ),
            occurrences=tuple(
                ActiveOccurrence.from_dict(item)
                for item in raw_occurrences
                if isinstance(item, Mapping)
            ),
            target_entity_ids=_entity_ids(
                value.get("target_entity_ids"),
                code="invalid_active_targets",
                domain="light",
            ),
            wake_owned_target_ids=_entity_ids(
                value.get("wake_owned_target_ids"),
                code="invalid_active_targets",
                domain="light",
            ),
            released_target_ids=_entity_ids(
                value.get("released_target_ids"),
                code="invalid_active_targets",
                domain="light",
            ),
            generation=(
                _bounded_number(
                    value.get("generation"),
                    code="invalid_lease_generation",
                    minimum=1,
                    maximum=2_147_483_647,
                )
                if value.get("generation") is not None
                else None
            ),
            lease_acquired_at=parse_datetime(value.get("lease_acquired_at")),
            lease_expires_at=parse_datetime(value.get("lease_expires_at")),
            observed_floor_pct=float(value.get("observed_floor_pct", 1.0)),
            last_brightness_pct=float(value.get("last_brightness_pct", 0.0)),
            last_command_at=parse_datetime(value.get("last_command_at")),
            next_deadline=parse_datetime(value.get("next_deadline")),
            cumulative_snooze_seconds=max(
                0, int(value.get("cumulative_snooze_seconds", 0))
            ),
            command_sequence=max(0, int(value.get("command_sequence", 0))),
            last_pbl_outcome=(
                str(value["last_pbl_outcome"])
                if value.get("last_pbl_outcome")
                else None
            ),
            recovery_decision=(
                str(value["recovery_decision"])
                if value.get("recovery_decision")
                else None
            ),
        )


@dataclass(frozen=True)
class FailureRecord:
    """One bounded non-personal failure record."""

    code: str
    at: datetime
    request_ref: str | None = None
    occurrence_ref: str | None = None
    lease_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize a failure."""
        return {
            "code": self.code,
            "at": self.at.isoformat(),
            "request_ref": self.request_ref,
            "occurrence_ref": self.occurrence_ref,
            "lease_ref": self.lease_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FailureRecord:
        """Restore a failure."""
        at = parse_datetime(value.get("at")) or utc_now()
        return cls(
            code=_as_string(
                value.get("code"),
                code="invalid_failure_code",
                maximum=128,
            ),
            at=at,
            request_ref=(
                str(value["request_ref"]) if value.get("request_ref") else None
            ),
            occurrence_ref=(
                str(value["occurrence_ref"])
                if value.get("occurrence_ref")
                else None
            ),
            lease_ref=(
                str(value["lease_ref"]) if value.get("lease_ref") else None
            ),
        )


@dataclass(frozen=True)
class CancellationRecord:
    """Bounded, non-personal disclosure for the latest user cancellation."""

    at: datetime
    occurrence_count: int
    occurrence_refs: tuple[str, ...]
    suppressed_until: datetime
    outcome: str = OUTCOME_CANCELLED_BY_USER

    def __post_init__(self) -> None:
        as_utc(self.at)
        as_utc(self.suppressed_until)
        if self.occurrence_count < 0:
            raise ValueError("invalid_cancellation_count")
        if self.occurrence_count < len(self.occurrence_refs):
            raise ValueError("invalid_cancellation_count")
        if len(self.occurrence_refs) > MAX_CANCELLATION_OCCURRENCE_REFS:
            raise ValueError("too_many_cancellation_refs")
        if any(not _OPAQUE_ID_RE.fullmatch(item) for item in self.occurrence_refs):
            raise ValueError("invalid_cancellation_ref")
        if self.outcome != OUTCOME_CANCELLED_BY_USER:
            raise ValueError("invalid_cancellation_outcome")
        if self.suppressed_until < self.at:
            raise ValueError("invalid_cancellation_timestamp")

    def to_dict(self) -> dict[str, Any]:
        """Serialize public-safe cancellation telemetry."""
        return {
            "at": self.at.isoformat(),
            "occurrence_count": self.occurrence_count,
            "occurrence_refs": list(self.occurrence_refs),
            "outcome": self.outcome,
            "suppressed_until": self.suppressed_until.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CancellationRecord:
        """Restore one cancellation disclosure record."""
        at = parse_datetime(value.get("at"))
        suppressed_until = parse_datetime(value.get("suppressed_until"))
        if at is None or suppressed_until is None:
            raise ValueError("invalid_cancellation_timestamp")
        raw_refs = value.get("occurrence_refs")
        refs = (
            tuple(
                item
                for item in raw_refs[:MAX_CANCELLATION_OCCURRENCE_REFS]
                if isinstance(item, str) and _OPAQUE_ID_RE.fullmatch(item)
            )
            if isinstance(raw_refs, list)
            else ()
        )
        return cls(
            at=at,
            occurrence_count=max(0, int(value.get("occurrence_count", 0))),
            occurrence_refs=refs,
            outcome=_as_string(
                value.get("outcome", OUTCOME_CANCELLED_BY_USER),
                code="invalid_cancellation_outcome",
                maximum=64,
            ),
            suppressed_until=suppressed_until,
        )


@dataclass(frozen=True)
class IdempotencyRecord:
    """One bounded request result indexed by a hashed request ID."""

    request_key: str
    payload_hash: str
    response: Mapping[str, Any]
    at: datetime

    def to_dict(self) -> dict[str, Any]:
        """Serialize an idempotency record."""
        return {
            "request_key": self.request_key,
            "payload_hash": self.payload_hash,
            "response": {
                key: value
                for key, value in self.response.items()
                if key != "request_id"
            },
            "at": self.at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> IdempotencyRecord:
        """Restore an idempotency record."""
        raw_response = value.get("response")
        response = (
            {
                key: item
                for key, item in raw_response.items()
                if key != "request_id"
            }
            if isinstance(raw_response, Mapping)
            else {}
        )
        return cls(
            request_key=_as_string(
                value.get("request_key"),
                code="invalid_request_key",
            ),
            payload_hash=_as_string(
                value.get("payload_hash"),
                code="invalid_payload_hash",
            ),
            response=response,
            at=parse_datetime(value.get("at")) or utc_now(),
        )


@dataclass(frozen=True)
class SourceSnapshot:
    """Last known normalized alarms and freshness for one source side."""

    alarms: tuple[WakeLightAlarm, ...] = ()
    available: bool = False
    last_success_at: datetime | None = None
    last_observed_at: datetime | None = None
    failure_code: str | None = None
    lifecycle_state: str = "unknown"
    terminal_reason: str | None = None
    last_stopped_occurrence_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize source cache state."""
        return {
            "alarms": [alarm.to_dict() for alarm in self.alarms],
            "available": self.available,
            "last_success_at": iso_or_none(self.last_success_at),
            "last_observed_at": iso_or_none(self.last_observed_at),
            "failure_code": self.failure_code,
            "lifecycle_state": self.lifecycle_state,
            "terminal_reason": self.terminal_reason,
            "last_stopped_occurrence_id": self.last_stopped_occurrence_id,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        defaults: WakeLightDefaults,
    ) -> SourceSnapshot:
        """Restore source cache state."""
        raw_alarms = value.get("alarms")
        alarms: list[WakeLightAlarm] = []
        if isinstance(raw_alarms, list):
            for item in raw_alarms:
                if not isinstance(item, Mapping):
                    continue
                try:
                    alarms.append(WakeLightAlarm.from_dict(item, defaults))
                except ValueError:
                    continue
        return cls(
            alarms=tuple(alarms),
            available=value.get("available") is True,
            last_success_at=parse_datetime(value.get("last_success_at")),
            last_observed_at=parse_datetime(value.get("last_observed_at")),
            failure_code=(
                str(value["failure_code"]) if value.get("failure_code") else None
            ),
            lifecycle_state=str(value.get("lifecycle_state", "unknown")),
            terminal_reason=(
                str(value["terminal_reason"])
                if value.get("terminal_reason") in {"expired", "stopped"}
                else None
            ),
            last_stopped_occurrence_id=(
                str(value["last_stopped_occurrence_id"])[:128]
                if value.get("last_stopped_occurrence_id")
                else None
            ),
        )


@dataclass(frozen=True)
class WakeLightProfile:
    """Validated config-entry profile."""

    profile_id: str
    name: str
    root_light_entity_id: str
    target_light_entity_ids: tuple[str, ...]
    pbl_switch_entity_id: str
    vacation_entity_id: str
    defaults: WakeLightDefaults
    area_id: str | None = None
    occupancy_entity_id: str | None = None
    blocker_entity_ids: tuple[str, ...] = ()
    sleepypod_schedule_entity_id: str | None = None
    sleepypod_source_sides: tuple[str, ...] = ()
    source_state_entity_ids: Mapping[str, str] = field(default_factory=dict)
    legacy_brightness_lifecycle_safe: bool = False

    def __post_init__(self) -> None:
        if not _PROFILE_ID_RE.fullmatch(self.profile_id):
            raise ValueError("invalid_profile_id")
        _as_string(self.name, code="invalid_profile_name", maximum=80)
        if not self.target_light_entity_ids:
            raise ValueError("target_lights_required")
        if len(self.target_light_entity_ids) > MAX_TARGET_LIGHTS:
            raise ValueError("too_many_target_lights")
        if self.root_light_entity_id in self.target_light_entity_ids:
            raise ValueError("root_must_not_be_leaf_target")
        if self.pbl_switch_entity_id in self.blocker_entity_ids:
            raise ValueError("pbl_switch_must_not_be_blocker")
        if {
            self.root_light_entity_id,
            *self.target_light_entity_ids,
        }.intersection(self.blocker_entity_ids):
            raise ValueError("light_target_must_not_be_blocker")
        if any(side not in SOURCE_SIDES for side in self.sleepypod_source_sides):
            raise ValueError("invalid_source_side")
        if self.sleepypod_source_sides and not self.sleepypod_schedule_entity_id:
            raise ValueError("source_sensor_required")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> WakeLightProfile:
        """Parse config entry data/options into one profile."""
        profile_id = _as_string(
            value.get(CONF_PROFILE_ID),
            code="invalid_profile_id",
            maximum=64,
        )
        if not _PROFILE_ID_RE.fullmatch(profile_id):
            raise ValueError("invalid_profile_id")
        sides_raw = value.get(CONF_SLEEPYPOD_SOURCE_SIDES, [])
        if not isinstance(sides_raw, (list, tuple, set)):
            raise ValueError("invalid_source_side")
        sides = tuple(side for side in SOURCE_SIDES if side in set(sides_raw))
        state_entities: dict[str, str] = {}
        left_state = _entity_id(
            value.get(CONF_SLEEPYPOD_LEFT_STATE_ENTITY_ID),
            code="invalid_source_state_entity",
            optional=True,
        )
        right_state = _entity_id(
            value.get(CONF_SLEEPYPOD_RIGHT_STATE_ENTITY_ID),
            code="invalid_source_state_entity",
            optional=True,
        )
        if left_state:
            if SOURCE_SIDE_LEFT in sides:
                state_entities[f"{SOURCE_REF_PREFIX}{SOURCE_SIDE_LEFT}"] = left_state
        if right_state:
            if SOURCE_SIDE_RIGHT in sides:
                state_entities[f"{SOURCE_REF_PREFIX}{SOURCE_SIDE_RIGHT}"] = right_state
        return cls(
            profile_id=profile_id,
            name=_as_string(
                value.get(CONF_PROFILE_NAME),
                code="invalid_profile_name",
                maximum=80,
            ),
            area_id=(
                _as_string(
                    value.get(CONF_AREA_ID),
                    code="invalid_area_id",
                    maximum=128,
                )
                if value.get(CONF_AREA_ID)
                else None
            ),
            root_light_entity_id=_entity_id(
                value.get(CONF_ROOT_LIGHT_ENTITY_ID),
                code="invalid_root_light",
                domain="light",
            )
            or "",
            target_light_entity_ids=_entity_ids(
                value.get(CONF_TARGET_LIGHT_ENTITY_IDS),
                code="invalid_target_lights",
                domain="light",
            ),
            occupancy_entity_id=_entity_id(
                value.get(CONF_OCCUPANCY_ENTITY_ID),
                code="invalid_occupancy_entity",
                optional=True,
            ),
            pbl_switch_entity_id=_entity_id(
                value.get(CONF_PBL_SWITCH_ENTITY_ID),
                code="invalid_pbl_switch",
                domain="switch",
            )
            or "",
            vacation_entity_id=_entity_id(
                value.get(CONF_VACATION_ENTITY_ID),
                code="invalid_vacation_entity",
            )
            or "",
            blocker_entity_ids=_entity_ids(
                value.get(CONF_BLOCKER_ENTITY_IDS, []),
                code="invalid_blocker_entities",
            ),
            sleepypod_schedule_entity_id=_entity_id(
                value.get(CONF_SLEEPYPOD_SCHEDULE_ENTITY_ID),
                code="invalid_source_sensor",
                domain="sensor",
                optional=True,
            ),
            sleepypod_source_sides=sides,
            source_state_entity_ids=state_entities,
            defaults=WakeLightDefaults.from_dict(
                {
                    "ramp_minutes": value.get(
                        CONF_DEFAULT_RAMP_MINUTES,
                        DEFAULT_RAMP_MINUTES,
                    ),
                    "post_wake_hold_minutes": value.get(
                        CONF_DEFAULT_POST_WAKE_HOLD_MINUTES,
                        DEFAULT_POST_WAKE_HOLD_MINUTES,
                    ),
                }
            ),
            legacy_brightness_lifecycle_safe=(
                value.get(CONF_LEGACY_BRIGHTNESS_LIFECYCLE_SAFE) is True
            ),
        )

    @property
    def source_refs(self) -> tuple[str, ...]:
        """Return configured read-only source references."""
        return tuple(f"{SOURCE_REF_PREFIX}{side}" for side in self.sleepypod_source_sides)

    @property
    def controller_id(self) -> str:
        """Return the stable PBL controller ID for this profile."""
        return opaque_ref("wlctl", self.profile_id)


@dataclass(frozen=True)
class ProfileState:
    """Versioned persisted mutable profile state."""

    profile_id: str
    revision: int
    defaults: WakeLightDefaults
    alarms: tuple[WakeLightAlarm, ...] = ()
    alarm_links: Mapping[str, bool] = field(default_factory=dict)
    source_cache: Mapping[str, SourceSnapshot] = field(default_factory=dict)
    active_run: ActiveRun | None = None
    last_outcome: str | None = None
    last_pbl_outcome: str | None = None
    failures: tuple[FailureRecord, ...] = ()
    request_history: tuple[IdempotencyRecord, ...] = ()
    terminal_occurrence_ids: tuple[str, ...] = ()
    auto_relight_blocked_until: datetime | None = None
    auto_relight_blocked_from: datetime | None = None
    last_cancellation: CancellationRecord | None = None
    version: int = STORE_VERSION

    @classmethod
    def initial(cls, profile: WakeLightProfile) -> ProfileState:
        """Create empty state for a new config entry."""
        return cls(
            profile_id=profile.profile_id,
            revision=0,
            defaults=profile.defaults,
            alarm_links={},
            source_cache={
                source_ref: SourceSnapshot() for source_ref in profile.source_refs
            },
        )

    def with_failure(
        self,
        code: str,
        *,
        at: datetime | None = None,
        request_ref: str | None = None,
        occurrence_ref: str | None = None,
        lease_ref: str | None = None,
    ) -> ProfileState:
        """Append a bounded failure record."""
        record = FailureRecord(
            code=code,
            at=at or utc_now(),
            request_ref=request_ref,
            occurrence_ref=occurrence_ref,
            lease_ref=lease_ref,
        )
        failures = (*self.failures, record)[-MAX_FAILURES:]
        return replace(self, failures=failures, last_outcome=code)

    def remember_request(
        self,
        request_id: str,
        command_payload: Mapping[str, Any],
        response: Mapping[str, Any],
        *,
        at: datetime | None = None,
    ) -> ProfileState:
        """Persist a bounded idempotency result."""
        record = IdempotencyRecord(
            request_key=request_key(request_id),
            payload_hash=payload_fingerprint(command_payload),
            response=dict(response),
            at=at or utc_now(),
        )
        history = tuple(
            item
            for item in self.request_history
            if item.request_key != record.request_key
        )
        return replace(
            self,
            request_history=(*history, record)[-MAX_REQUEST_HISTORY:],
        )

    def request_record(self, request_id: str) -> IdempotencyRecord | None:
        """Look up an idempotency record without retaining the caller token."""
        key = request_key(request_id)
        return next(
            (item for item in reversed(self.request_history) if item.request_key == key),
            None,
        )

    def remember_terminal_occurrences(
        self,
        occurrence_ids: Iterable[str],
    ) -> ProfileState:
        """Retain a bounded set of completed/cancelled occurrence IDs."""
        values = list(self.terminal_occurrence_ids)
        for occurrence_id in occurrence_ids:
            if occurrence_id in values:
                values.remove(occurrence_id)
            values.append(occurrence_id)
        return replace(
            self,
            terminal_occurrence_ids=tuple(values[-MAX_TERMINAL_OCCURRENCES:]),
        )

    def remember_user_cancellation(
        self,
        occurrence_ids: Iterable[str],
        *,
        at: datetime,
        suppressed_until: datetime,
        suppressed_from: datetime | None = None,
    ) -> ProfileState:
        """Persist terminal IDs, a bounded fence, and public-safe disclosure."""
        unique_ids = tuple(dict.fromkeys(occurrence_ids))
        cutoff = as_utc(suppressed_until)
        if self.auto_relight_blocked_until is not None:
            cutoff = max(
                cutoff,
                self.auto_relight_blocked_until.astimezone(UTC),
            )
        state = self.remember_terminal_occurrences(unique_ids)
        return replace(
            state,
            active_run=None,
            auto_relight_blocked_until=cutoff,
            auto_relight_blocked_from=as_utc(suppressed_from or at),
            last_cancellation=CancellationRecord(
                at=as_utc(at),
                occurrence_count=len(unique_ids),
                occurrence_refs=tuple(
                    opaque_ref("occ", occurrence_id)
                    for occurrence_id in unique_ids[
                        :MAX_CANCELLATION_OCCURRENCE_REFS
                    ]
                ),
                suppressed_until=cutoff,
            ),
            last_outcome=OUTCOME_CANCELLED_BY_USER,
        )

    def clear_expired_relight_fence(self, now: datetime) -> ProfileState:
        """Clear the active fence after its inclusive cutoff has passed."""
        if (
            self.auto_relight_blocked_until is None
            or as_utc(now)
            <= self.auto_relight_blocked_until.astimezone(UTC)
        ):
            return self
        return replace(
            self,
            auto_relight_blocked_until=None,
            auto_relight_blocked_from=None,
        )

    def alarm_link_enabled(
        self,
        source_ref: str,
        weekday: str,
        local_time: str,
    ) -> bool:
        """Return whether one source alarm day drives this wake light.

        Links default to enabled; only explicit opt-outs are persisted.
        """
        key = alarm_link_key(source_ref, weekday, local_time)
        return self.alarm_links.get(key) is not False

    def linked_weekdays(self, alarm: WakeLightAlarm) -> tuple[str, ...]:
        """Return the weekdays of one source alarm that remain linked."""
        if (
            alarm.source == ALARM_SOURCE_NATIVE
            or not alarm.source_ref
            or alarm.kind != ALARM_KIND_WEEKLY
        ):
            return alarm.weekdays
        return tuple(
            day
            for day in alarm.weekdays
            if self.alarm_link_enabled(alarm.source_ref, day, alarm.local_time)
        )

    def source_alarm_linked(self, alarm: WakeLightAlarm) -> bool:
        """Return whether one alarm still drives this wake light at all."""
        if alarm.source == ALARM_SOURCE_NATIVE or not alarm.source_ref:
            return True
        if alarm.kind == ALARM_KIND_WEEKLY:
            return bool(self.linked_weekdays(alarm))
        if alarm.date is None:
            return True
        weekday = WEEKDAYS[(date.fromisoformat(alarm.date).weekday() + 1) % 7]
        return self.alarm_link_enabled(alarm.source_ref, weekday, alarm.local_time)

    def has_linked_alarms(self, source_ref: str) -> bool:
        """Return whether any cached alarm of one source still drives this light."""
        snapshot = self.source_cache.get(source_ref)
        if snapshot is None:
            return False
        return any(
            alarm.enabled and self.source_alarm_linked(alarm)
            for alarm in snapshot.alarms
        )

    def public_alarms(self) -> tuple[WakeLightAlarm, ...]:
        """Return native plus read-only source alarms."""
        alarms = list(self.alarms)
        for source_ref in self.source_cache:
            alarms.extend(
                replace(alarm, ramp_minutes=self.defaults.ramp_minutes)
                for alarm in self.source_cache.get(source_ref, SourceSnapshot()).alarms
            )
        return tuple(
            sorted(
                alarms,
                key=lambda item: (
                    item.source != ALARM_SOURCE_NATIVE,
                    item.label.casefold(),
                    item.id,
                ),
            )
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize Store data."""
        return {
            "version": self.version,
            "profile_id": self.profile_id,
            "revision": self.revision,
            "defaults": self.defaults.to_dict(),
            "alarms": [alarm.to_dict() for alarm in self.alarms],
            "alarm_links": dict(self.alarm_links),
            "source_cache": {
                key: value.to_dict() for key, value in self.source_cache.items()
            },
            "active_run": self.active_run.to_dict() if self.active_run else None,
            "last_outcome": self.last_outcome,
            "last_pbl_outcome": self.last_pbl_outcome,
            "failures": [failure.to_dict() for failure in self.failures],
            "request_history": [
                request.to_dict() for request in self.request_history
            ],
            "terminal_occurrence_ids": list(self.terminal_occurrence_ids),
            "auto_relight_blocked_until": iso_or_none(
                self.auto_relight_blocked_until
            ),
            "auto_relight_blocked_from": iso_or_none(
                self.auto_relight_blocked_from
            ),
            "last_cancellation": (
                self.last_cancellation.to_dict()
                if self.last_cancellation is not None
                else None
            ),
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        profile: WakeLightProfile,
    ) -> ProfileState:
        """Restore Store data, dropping invalid bounded records."""
        defaults = WakeLightDefaults.from_dict(
            value.get("defaults")
            if isinstance(value.get("defaults"), Mapping)
            else None,
            profile.defaults,
        )
        alarms: list[WakeLightAlarm] = []
        if isinstance(value.get("alarms"), list):
            for item in value["alarms"][:MAX_NATIVE_ALARMS]:
                if not isinstance(item, Mapping):
                    continue
                try:
                    alarm = WakeLightAlarm.from_dict(
                        item,
                        defaults,
                        require_native=True,
                    )
                except ValueError:
                    continue
                alarms.append(alarm)

        links: dict[str, bool] = {}
        raw_links = value.get("alarm_links")
        if isinstance(raw_links, Mapping):
            for key, enabled in raw_links.items():
                if len(links) >= MAX_ALARM_LINKS:
                    break
                if not isinstance(key, str) or enabled is not False:
                    continue
                try:
                    source_ref, _weekday, _local_time = parse_alarm_link_key(key)
                except ValueError:
                    continue
                if source_ref in profile.source_refs:
                    links[key] = False

        source_cache = {
            source_ref: SourceSnapshot() for source_ref in profile.source_refs
        }
        raw_source_cache = value.get("source_cache")
        if isinstance(raw_source_cache, Mapping):
            for source_ref in source_cache:
                raw_snapshot = raw_source_cache.get(source_ref)
                if isinstance(raw_snapshot, Mapping):
                    source_cache[source_ref] = SourceSnapshot.from_dict(
                        raw_snapshot,
                        defaults,
                    )

        active_run = None
        if isinstance(value.get("active_run"), Mapping):
            try:
                active_run = ActiveRun.from_dict(value["active_run"])
            except (TypeError, ValueError):
                active_run = None

        failures: list[FailureRecord] = []
        if isinstance(value.get("failures"), list):
            for item in value["failures"][-MAX_FAILURES:]:
                if not isinstance(item, Mapping):
                    continue
                try:
                    failure = FailureRecord.from_dict(item)
                except ValueError:
                    continue
                if failure.code == FAILURE_MANUAL_REVOKE:
                    continue
                failures.append(failure)

        requests: list[IdempotencyRecord] = []
        if isinstance(value.get("request_history"), list):
            for item in value["request_history"][-MAX_REQUEST_HISTORY:]:
                if not isinstance(item, Mapping):
                    continue
                try:
                    requests.append(IdempotencyRecord.from_dict(item))
                except ValueError:
                    continue

        terminal_ids: list[str] = []
        if isinstance(value.get("terminal_occurrence_ids"), list):
            terminal_ids = [
                item
                for item in value["terminal_occurrence_ids"][
                    -MAX_TERMINAL_OCCURRENCES:
                ]
                if isinstance(item, str) and _OPAQUE_ID_RE.fullmatch(item)
            ]

        last_cancellation = None
        if isinstance(value.get("last_cancellation"), Mapping):
            try:
                last_cancellation = CancellationRecord.from_dict(
                    value["last_cancellation"]
                )
            except (TypeError, ValueError):
                last_cancellation = None

        return cls(
            version=STORE_VERSION,
            profile_id=profile.profile_id,
            revision=max(0, int(value.get("revision", 0))),
            defaults=defaults,
            alarms=tuple(alarms),
            alarm_links=links,
            source_cache=source_cache,
            active_run=active_run,
            last_outcome=(
                str(value["last_outcome"]) if value.get("last_outcome") else None
            ),
            last_pbl_outcome=(
                str(value["last_pbl_outcome"])
                if value.get("last_pbl_outcome")
                else None
            ),
            failures=tuple(failures),
            request_history=tuple(requests),
            terminal_occurrence_ids=tuple(terminal_ids),
            auto_relight_blocked_until=parse_datetime(
                value.get("auto_relight_blocked_until")
            ),
            auto_relight_blocked_from=(
                parse_datetime(value.get("auto_relight_blocked_from"))
            ),
            last_cancellation=last_cancellation,
        )
