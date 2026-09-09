"""Pure SleepyPod bed-alarm schedule transformations."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from .const import WEEKDAYS

_ALARM_FIELDS = (
    "alarmTemperature",
    "duration",
    "enabled",
    "time",
    "vibrationIntensity",
    "vibrationPattern",
)
_DEFAULT_ALARM = {
    "alarmTemperature": 82,
    "duration": 300,
    "enabled": True,
    "vibrationIntensity": 100,
    "vibrationPattern": "rise",
}


def execution_weekday(value: str) -> str:
    """Return the SleepyPod execution weekday for an ISO local date."""
    parsed = date.fromisoformat(value)
    return WEEKDAYS[(parsed.weekday() + 1) % 7]


def _day_alarms(
    attributes: Mapping[str, Any], side: str, weekday: str,
) -> tuple[Mapping[str, Any], ...]:
    side_value = attributes.get(side)
    if not isinstance(side_value, Mapping):
        return ()
    day_value = side_value.get(weekday)
    if not isinstance(day_value, Mapping):
        return ()
    alarms = day_value.get("alarms")
    if not isinstance(alarms, list):
        return ()
    return tuple(item for item in alarms if isinstance(item, Mapping))


def alarm_ids(
    attributes: Mapping[str, Any], side: str, weekday: str,
) -> tuple[int, ...]:
    """Return valid provider IDs currently present on one execution day."""
    return tuple(
        value
        for alarm in _day_alarms(attributes, side, weekday)
        if isinstance((value := alarm.get("id")), int)
        and not isinstance(value, bool)
        and value > 0
    )


def matching_alarm_ids(
    attributes: Mapping[str, Any], side: str, weekday: str, local_time: str,
) -> tuple[int, ...]:
    """Return provider IDs for alarms matching one side/day/time."""
    return tuple(
        value
        for alarm in _day_alarms(attributes, side, weekday)
        if alarm.get("time") == local_time
        and isinstance((value := alarm.get("id")), int)
        and not isinstance(value, bool)
        and value > 0
    )


def _provider_alarm(value: Mapping[str, Any]) -> dict[str, Any]:
    return {field: value[field] for field in _ALARM_FIELDS if field in value}


def _side_payload(
    attributes: Mapping[str, Any], side: str,
    replacement: Mapping[str, tuple[Mapping[str, Any], ...]],
) -> dict[str, Any]:
    return {
        side: {
            weekday: {
                "alarms": [
                    _provider_alarm(alarm)
                    for alarm in replacement.get(
                        weekday, _day_alarms(attributes, side, weekday)
                    )
                ]
            }
            for weekday in WEEKDAYS
        }
    }


def add_temporary_alarm_payload(
    attributes: Mapping[str, Any], side: str, weekday: str, local_time: str,
) -> tuple[dict[str, Any] | None, tuple[int, ...]]:
    """Build an add payload unless that side already has the requested alarm."""
    baseline = alarm_ids(attributes, side, weekday)
    if any(
        alarm.get("time") == local_time
        for alarm in _day_alarms(attributes, side, weekday)
    ):
        return None, baseline
    added = {**_DEFAULT_ALARM, "time": local_time}
    records = (*_day_alarms(attributes, side, weekday), added)
    return _side_payload(attributes, side, {weekday: records}), baseline


def bind_temporary_alarm_id(
    attributes: Mapping[str, Any], side: str, weekday: str, local_time: str,
    baseline_ids: tuple[int, ...],
) -> int | None:
    """Resolve exactly one newly-created provider alarm ID, failing closed on ambiguity."""
    candidates = tuple(
        alarm_id
        for alarm_id in matching_alarm_ids(attributes, side, weekday, local_time)
        if alarm_id not in set(baseline_ids)
    )
    return candidates[0] if len(candidates) == 1 else None


def remove_temporary_alarm_payload(
    attributes: Mapping[str, Any], side: str, weekday: str, local_time: str,
    baseline_ids: tuple[int, ...], schedule_id: int | None,
) -> dict[str, Any] | None:
    """Remove only the alarm proven to have been created by Wake Light."""
    target_id = schedule_id or bind_temporary_alarm_id(
        attributes, side, weekday, local_time, baseline_ids,
    )
    if target_id is None or target_id in set(baseline_ids):
        return None
    records = _day_alarms(attributes, side, weekday)
    retained = tuple(alarm for alarm in records if alarm.get("id") != target_id)
    if len(retained) == len(records):
        return None
    return _side_payload(attributes, side, {weekday: retained})
