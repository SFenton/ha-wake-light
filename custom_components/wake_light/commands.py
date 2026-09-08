"""Pure implementation of the versioned wake_light.command contract."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from .const import (
    ALARM_SOURCE_NATIVE,
    COMMAND_OPERATIONS,
    MAX_NATIVE_ALARMS,
    OP_BIND_SOURCE,
    OP_CANCEL_OCCURRENCE,
    OP_DELETE_ALARM,
    OP_DISMISS,
    OP_END_EPISODE,
    OP_UPDATE_DEFAULTS,
    OP_UPSERT_ALARM,
    RAMP_MINUTE_OPTIONS,
    OUTCOME_ACCEPTED,
    OUTCOME_CANCELLED_BY_USER,
    OUTCOME_INVALID_REQUEST,
    OUTCOME_NO_ACTIVE_OCCURRENCE,
    OUTCOME_NO_CHANGE,
    OUTCOME_NOT_FOUND,
    OUTCOME_READ_ONLY_SOURCE,
    OUTCOME_REQUEST_ID_CONFLICT,
    OUTCOME_REVISION_CONFLICT,
)
from .engine import remove_occurrence
from .model import (
    ActiveRun,
    ProfileState,
    WakeLightAlarm,
    WakeLightProfile,
    payload_fingerprint,
    opaque_ref,
    request_key,
    utc_now,
)
from .scheduler import calendar_windows, expand_relight_fence, runnable_alarms, terminal_once_alarm_ids, unsupported_episode_ids

_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}$")
_BASE_FIELDS = {
    "profile_id",
    "expected_revision",
    "request_id",
    "operation",
}
_OPERATION_FIELDS = {
    OP_UPSERT_ALARM: {"alarm"},
    OP_DELETE_ALARM: {"alarm_id"},
    OP_UPDATE_DEFAULTS: {"defaults"},
    OP_BIND_SOURCE: {"source_ref", "enabled"},
    OP_DISMISS: {"occurrence_id", "episode_ref"},
    OP_CANCEL_OCCURRENCE: {"occurrence_id", "episode_ref"},
    OP_END_EPISODE: {"episode_ref"},
}
_ALARM_FIELDS = {
    "date",
    "enabled",
    "id",
    "kind",
    "label",
    "local_time",
    "ramp_minutes",
    "revision",
    "source",
    "source_label",
    "source_ref",
    "weekdays",
}
_DEFAULT_FIELDS = {
    "post_wake_hold_minutes",
    "ramp_minutes",
}


@dataclass(frozen=True)
class CommandEffect:
    """Runtime side effect requested by a pure command mutation."""

    kind: str
    previous_run: ActiveRun | None = None
    occurrence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandDecision:
    """Result of validating and applying one command."""

    state: ProfileState
    response: Mapping[str, Any]
    effect: CommandEffect | None = None


def _command_payload(command: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in command.items()
        if key != "request_id"
    }


def _response(
    state: ProfileState,
    request_id: str,
    outcome: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "profile_id": state.profile_id,
        "revision": state.revision,
        "request_id": request_id,
        "request_ref": request_key(request_id),
        **extra,
    }


def _remember(
    state: ProfileState,
    request_id: str,
    payload: Mapping[str, Any],
    response: Mapping[str, Any],
) -> ProfileState:
    stored_response = {
        key: value for key, value in response.items() if key != "request_id"
    }
    return state.remember_request(
        request_id,
        payload,
        stored_response,
        at=utc_now(),
    )


def _invalid(
    state: ProfileState,
    request_id: str,
    payload: Mapping[str, Any],
    code: str,
) -> CommandDecision:
    response = _response(
        state,
        request_id,
        OUTCOME_INVALID_REQUEST,
        error=code,
    )
    return CommandDecision(
        _remember(state, request_id, payload, response),
        response,
    )


def apply_command(
    state: ProfileState,
    profile: WakeLightProfile,
    command: Mapping[str, Any],
    *,
    now: datetime | None = None,
    timezone: ZoneInfo | None = None,
) -> CommandDecision:
    """Apply one idempotent expected-revision command without HA dependencies."""
    effective_now = now or utc_now()
    request_id = command.get("request_id")
    if not isinstance(request_id, str) or not _OPAQUE_ID_RE.fullmatch(request_id):
        response = {
            "outcome": OUTCOME_INVALID_REQUEST,
            "profile_id": state.profile_id,
            "revision": state.revision,
            "error": "invalid_request_id",
        }
        return CommandDecision(state, response)
    payload = _command_payload(command)
    existing_request = state.request_record(request_id)
    if existing_request is not None:
        if existing_request.payload_hash == payload_fingerprint(payload):
            return CommandDecision(
                state,
                {
                    **existing_request.response,
                    "request_id": request_id,
                    "idempotent": True,
                },
            )
        return CommandDecision(
            state,
            _response(
                state,
                request_id,
                OUTCOME_REQUEST_ID_CONFLICT,
            ),
        )

    if command.get("profile_id") != profile.profile_id:
        return _invalid(
            state,
            request_id,
            payload,
            "unknown_profile",
        )
    operation = command.get("operation")
    if operation not in COMMAND_OPERATIONS:
        return _invalid(
            state,
            request_id,
            payload,
            "unsupported_operation",
        )
    unexpected = set(command) - _BASE_FIELDS - _OPERATION_FIELDS[operation]
    if unexpected:
        return _invalid(
            state,
            request_id,
            payload,
            "unexpected_fields",
        )
    expected_revision = command.get("expected_revision")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
    ):
        return _invalid(
            state,
            request_id,
            payload,
            "invalid_expected_revision",
        )
    episode_ref = command.get("episode_ref")
    active_operation = operation in {
        OP_DISMISS, OP_CANCEL_OCCURRENCE, OP_END_EPISODE,
    }
    matching_episode = bool(
        active_operation
        and state.active_run is not None
        and episode_ref == opaque_ref("episode", state.active_run.lease_id)
    )
    if episode_ref is not None and not matching_episode:
        response = _response(state, request_id, OUTCOME_NO_ACTIVE_OCCURRENCE)
        return CommandDecision(_remember(state, request_id, payload, response), response)
    if expected_revision != state.revision and not matching_episode:
        response = _response(
            state,
            request_id,
            OUTCOME_REVISION_CONFLICT,
            expected_revision=expected_revision,
            current_revision=state.revision,
        )
        return CommandDecision(
            _remember(state, request_id, payload, response),
            response,
        )

    next_state = state
    effect: CommandEffect | None = None
    changed = False
    error: str | None = None
    state_outcome: str | None = None

    if operation == OP_UPSERT_ALARM:
        raw_alarm = command.get("alarm")
        if not isinstance(raw_alarm, Mapping):
            return _invalid(
                state,
                request_id,
                payload,
                "invalid_alarm",
            )
        if set(raw_alarm) - _ALARM_FIELDS:
            return _invalid(
                state,
                request_id,
                payload,
                "unexpected_alarm_fields",
            )
        try:
            alarm = WakeLightAlarm.from_dict(
                raw_alarm,
                state.defaults,
                require_native=True,
            )
        except ValueError as err:
            code = str(err)
            if code == "read_only_source":
                response = _response(
                    state,
                    request_id,
                    OUTCOME_READ_ONLY_SOURCE,
                )
                return CommandDecision(
                    _remember(state, request_id, payload, response),
                    response,
                )
            return _invalid(state, request_id, payload, code)
        if alarm.ramp_minutes not in RAMP_MINUTE_OPTIONS:
            return _invalid(state, request_id, payload, "unsupported_ramp_minutes")
        current = next(
            (item for item in state.alarms if item.id == alarm.id),
            None,
        )
        if alarm.id in terminal_once_alarm_ids(
            profile.profile_id, (alarm,), timezone or ZoneInfo("UTC"),
            state.terminal_occurrence_ids,
        ):
            return _invalid(state, request_id, payload, "occurrence_already_ended")
        if current is not None and alarm.revision != current.revision:
            response = _response(
                state,
                request_id,
                OUTCOME_REVISION_CONFLICT,
                conflict="alarm_revision",
                current_alarm_revision=current.revision,
            )
            return CommandDecision(
                _remember(state, request_id, payload, response),
                response,
            )
        if current is None and alarm.revision != 0:
            return _invalid(
                state,
                request_id,
                payload,
                "new_alarm_revision_must_be_zero",
            )
        if current is None and len(state.alarms) >= MAX_NATIVE_ALARMS:
            return _invalid(
                state,
                request_id,
                payload,
                "alarm_limit_reached",
            )
        if current is not None and alarm.with_revision(current.revision) == current:
            error = OUTCOME_NO_CHANGE
        else:
            saved = alarm.with_revision(
                (current.revision if current else 0) + 1
            )
            alarms = tuple(
                saved if item.id == saved.id else item
                for item in state.alarms
            )
            if current is None:
                alarms = (*alarms, saved)
            next_state = replace(state, alarms=alarms)
            changed = True

    elif operation == OP_DELETE_ALARM:
        alarm_id = command.get("alarm_id")
        if not isinstance(alarm_id, str) or not _OPAQUE_ID_RE.fullmatch(alarm_id):
            return _invalid(
                state,
                request_id,
                payload,
                "invalid_alarm_id",
            )
        source_alarm = next(
            (
                item
                for item in state.public_alarms()
                if item.id == alarm_id and item.source != ALARM_SOURCE_NATIVE
            ),
            None,
        )
        if source_alarm is not None:
            response = _response(
                state,
                request_id,
                OUTCOME_READ_ONLY_SOURCE,
            )
            return CommandDecision(
                _remember(state, request_id, payload, response),
                response,
            )
        alarms = tuple(item for item in state.alarms if item.id != alarm_id)
        if len(alarms) == len(state.alarms):
            error = OUTCOME_NOT_FOUND
        else:
            next_state = replace(state, alarms=alarms)
            changed = True

    elif operation == OP_UPDATE_DEFAULTS:
        defaults = command.get("defaults")
        if not isinstance(defaults, Mapping):
            return _invalid(
                state,
                request_id,
                payload,
                "invalid_defaults",
            )
        if set(defaults) - _DEFAULT_FIELDS:
            return _invalid(
                state,
                request_id,
                payload,
                "unexpected_default_fields",
            )
        try:
            parsed = state.defaults.from_dict(defaults, state.defaults)
        except ValueError as err:
            return _invalid(state, request_id, payload, str(err))
        if parsed.ramp_minutes not in RAMP_MINUTE_OPTIONS:
            return _invalid(state, request_id, payload, "unsupported_ramp_minutes")
        if parsed == state.defaults:
            error = OUTCOME_NO_CHANGE
        else:
            next_state = replace(state, defaults=parsed)
            changed = True

    elif operation == OP_BIND_SOURCE:
        source_ref = command.get("source_ref")
        enabled = command.get("enabled")
        if source_ref not in profile.source_refs:
            return _invalid(
                state,
                request_id,
                payload,
                "source_not_configured",
            )
        if not isinstance(enabled, bool):
            return _invalid(
                state,
                request_id,
                payload,
                "invalid_enabled",
            )
        if state.source_bindings.get(str(source_ref)) == enabled:
            error = OUTCOME_NO_CHANGE
        else:
            bindings = dict(state.source_bindings)
            bindings[str(source_ref)] = enabled
            next_state = replace(state, source_bindings=bindings)
            changed = True
            if not enabled and state.active_run is not None:
                previous_run = state.active_run
                removed = tuple(
                    item.schedule.occurrence_id
                    for item in previous_run.occurrences
                    if item.schedule.source_ref == source_ref
                )
                remaining = tuple(
                    item
                    for item in previous_run.occurrences
                    if item.schedule.source_ref != source_ref
                )
                if removed:
                    next_state = replace(
                        next_state,
                        active_run=(
                            replace(previous_run, occurrences=remaining)
                            if remaining
                            else None
                        ),
                    )
                    next_state = next_state.remember_terminal_occurrences(
                        removed
                    )
                    effect = CommandEffect(
                        (
                            "update_lease"
                            if remaining
                            else "release_cancelled"
                        ),
                        previous_run=previous_run,
                        occurrence_ids=removed,
                    )

    elif operation in {OP_DISMISS, OP_CANCEL_OCCURRENCE}:
        occurrence_id = command.get("occurrence_id")
        if not isinstance(occurrence_id, str):
            return _invalid(
                state,
                request_id,
                payload,
                "invalid_occurrence_id",
            )
        if state.active_run is None:
            error = OUTCOME_NO_ACTIVE_OCCURRENCE
        else:
            updated_run, outcome = remove_occurrence(
                state.active_run,
                occurrence_id,
            )
            if outcome != OUTCOME_ACCEPTED:
                error = outcome
            else:
                next_state = replace(state, active_run=updated_run)
                next_state = next_state.remember_terminal_occurrences(
                    (occurrence_id,)
                )
                changed = True
                effect = CommandEffect(
                    (
                        "release_cancelled"
                        if updated_run is None
                        else "update_lease"
                    ),
                    previous_run=state.active_run,
                    occurrence_ids=(occurrence_id,),
                )

    elif operation == OP_END_EPISODE:
        if state.active_run is None:
            error = OUTCOME_NO_ACTIVE_OCCURRENCE
        else:
            previous_run = state.active_run
            next_state = replace(state, active_run=None)
            next_state = next_state.remember_terminal_occurrences(
                previous_run.occurrence_ids
            )
            changed = True
            state_outcome = OUTCOME_CANCELLED_BY_USER
            effect = CommandEffect(
                "end_episode",
                previous_run=previous_run,
                occurrence_ids=previous_run.occurrence_ids,
            )

    validates_schedule = operation in {OP_UPSERT_ALARM, OP_UPDATE_DEFAULTS} or (
        operation == OP_BIND_SOURCE and command.get("enabled") is True
    )
    if changed and operation == OP_UPSERT_ALARM and alarm.kind == "once" and alarm.enabled:
        anchor = next_state.auto_relight_blocked_from or (
            next_state.last_cancellation.at if next_state.last_cancellation else effective_now
        )
        fenced = expand_relight_fence(next_state, calendar_windows(
            profile.profile_id, runnable_alarms(next_state), anchor,
            timezone or ZoneInfo("UTC"), next_state.defaults,
        ), effective_now)
        if alarm.id in terminal_once_alarm_ids(
            profile.profile_id, (alarm,), timezone or ZoneInfo("UTC"), fenced.terminal_occurrence_ids,
        ):
            return _invalid(state, request_id, payload, "occurrence_already_ended")
    if changed and validates_schedule and unsupported_episode_ids(
        calendar_windows(
            profile.profile_id, runnable_alarms(next_state), effective_now,
            timezone or ZoneInfo("UTC"), next_state.defaults,
        ), effective_now,
    ):
        return _invalid(state, request_id, payload, "episode_duration_exceeded")
    outcome = error or (OUTCOME_ACCEPTED if changed else OUTCOME_NO_CHANGE)
    if changed:
        next_state = replace(
            next_state,
            revision=state.revision + 1,
            last_outcome=state_outcome or outcome,
        )
    response = _response(next_state, request_id, outcome)
    next_state = _remember(next_state, request_id, payload, response)
    return CommandDecision(next_state, response, effect)
