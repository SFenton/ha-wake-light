from __future__ import annotations

from copy import deepcopy

from custom_components.wake_light.bed_schedule import (
    add_temporary_alarm_payload,
    bind_temporary_alarm_id,
    execution_weekday,
    remove_temporary_alarm_payload,
)


def _schedule(*alarms):
    empty = {day: {"alarms": []} for day in (
        "sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"
    )}
    empty["tuesday"] = {"alarms": list(alarms)}
    return {"left": deepcopy(empty), "right": deepcopy(empty)}


def _alarm(alarm_id: int, time: str):
    return {
        "id": alarm_id,
        "alarmTemperature": 82,
        "duration": 180,
        "enabled": True,
        "time": time,
        "vibrationIntensity": 100,
        "vibrationPattern": "rise",
    }


def test_execution_weekday_uses_selected_local_date():
    assert execution_weekday("2026-09-15") == "tuesday"


def test_add_preserves_existing_alarms_and_records_baseline_ids():
    attributes = _schedule(_alarm(41, "07:00"))
    payload, baseline = add_temporary_alarm_payload(
        attributes, "right", "tuesday", "14:30"
    )
    assert baseline == (41,)
    assert [item["time"] for item in payload["right"]["tuesday"]["alarms"]] == [
        "07:00", "14:30"
    ]
    assert "id" not in payload["right"]["tuesday"]["alarms"][0]
    assert payload["right"]["monday"] == {"alarms": []}


def test_add_is_inert_when_matching_alarm_already_exists():
    attributes = _schedule(_alarm(41, "14:30"))
    payload, baseline = add_temporary_alarm_payload(
        attributes, "right", "tuesday", "14:30"
    )
    assert payload is None
    assert baseline == (41,)


def test_binding_selects_only_new_matching_provider_id():
    attributes = _schedule(_alarm(41, "07:00"), _alarm(52, "14:30"))
    assert bind_temporary_alarm_id(
        attributes, "right", "tuesday", "14:30", (41,)
    ) == 52


def test_binding_fails_closed_when_new_identity_is_ambiguous():
    attributes = _schedule(_alarm(52, "14:30"), _alarm(53, "14:30"))
    assert bind_temporary_alarm_id(
        attributes, "right", "tuesday", "14:30", ()
    ) is None


def test_remove_deletes_only_bound_temporary_alarm():
    attributes = _schedule(
        _alarm(41, "07:00"), _alarm(52, "14:30"), _alarm(60, "18:00")
    )
    payload = remove_temporary_alarm_payload(
        attributes, "right", "tuesday", "14:30", (41,), 52
    )
    assert [item["time"] for item in payload["right"]["tuesday"]["alarms"]] == [
        "07:00", "18:00"
    ]


def test_remove_never_deletes_a_baseline_alarm():
    attributes = _schedule(_alarm(41, "14:30"))
    assert remove_temporary_alarm_payload(
        attributes, "right", "tuesday", "14:30", (41,), 41
    ) is None
