"""Pure scheduling tests for the Wake Light integration."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components"),
)

from wake_light.model import (  # noqa: E402
    SourceSnapshot,
    WakeLightAlarm,
    WakeLightDefaults,
)
from wake_light.scheduler import (  # noqa: E402
    parse_sleepypod_side,
    refresh_source_snapshot,
    normalize_source_lifecycle,
    normalize_source_terminal_reason,
    resolve_alarm_occurrence,
    resolve_wall_datetime,
    sleepypod_wake_day,
    source_freshness_seconds,
    stale_once_alarm_ids,
)


class WakeLightSchedulingTests(unittest.TestCase):
    """Resolve native and read-only source alarms deterministically."""

    def setUp(self) -> None:
        self.timezone = ZoneInfo("America/Los_Angeles")
        self.defaults = WakeLightDefaults()

    def test_weekly_and_once_occurrence_resolution(self) -> None:
        weekly = WakeLightAlarm(
            id="weekday",
            label="Weekday Wake",
            kind="weekly",
            local_time="06:30",
            ramp_minutes=30,
            weekdays=("monday",),
        )
        now = datetime(2030, 1, 7, 5, 0, tzinfo=self.timezone)
        occurrence = resolve_alarm_occurrence(
            "master-bedroom",
            weekly,
            now,
            self.timezone,
            self.defaults,
        )
        self.assertIsNotNone(occurrence)
        assert occurrence is not None
        self.assertEqual(
            occurrence.wake_at.astimezone(self.timezone),
            datetime(2030, 1, 7, 6, 30, tzinfo=self.timezone),
        )
        self.assertEqual(
            occurrence.ramp_start_at.astimezone(self.timezone).time(),
            datetime(2030, 1, 7, 6, 0).time(),
        )

        once = WakeLightAlarm(
            id="flight",
            label="Flight",
            kind="once",
            local_time="04:45",
            ramp_minutes=20,
            date="2030-01-08",
        )
        once_occurrence = resolve_alarm_occurrence(
            "master-bedroom",
            once,
            now,
            self.timezone,
            self.defaults,
        )
        self.assertIsNotNone(once_occurrence)
        assert once_occurrence is not None
        self.assertEqual(
            once_occurrence.wake_at.astimezone(self.timezone).date(),
            date(2030, 1, 8),
        )

    def test_dst_gap_shifts_forward_and_fold_uses_first_physical_instant(self) -> None:
        gap = resolve_wall_datetime(
            date(2024, 3, 10),
            "02:30",
            self.timezone,
        )
        self.assertEqual(gap.policy, "gap_shift_forward")
        self.assertEqual(gap.shifted_minutes, 30)
        self.assertEqual(gap.value.hour, 3)
        self.assertEqual(gap.value.minute, 0)

        first = resolve_wall_datetime(
            date(2024, 11, 3),
            "01:30",
            self.timezone,
        )
        second = resolve_wall_datetime(
            date(2024, 11, 3),
            "01:30",
            self.timezone,
            fold_policy="second",
        )
        self.assertTrue(first.ambiguous)
        self.assertEqual(first.policy, "fold_first")
        self.assertLess(first.value.astimezone(UTC), second.value.astimezone(UTC))
        self.assertEqual(
            (
                second.value.astimezone(UTC)
                - first.value.astimezone(UTC)
            ).total_seconds(),
            3600,
        )

    def test_sleepypod_execution_day_does_not_depend_on_power(self) -> None:
        overnight = {"power": {"off": "09:00"}}
        same_day = {"power": {"off": "22:00"}}
        self.assertEqual(
            sleepypod_wake_day("saturday", overnight),
            "saturday",
        )
        self.assertEqual(
            sleepypod_wake_day("saturday", same_day),
            "saturday",
        )

        attributes = {
            "right": {
                "saturday": {
                    "power": {"off": "09:00"},
                    "alarms": [
                        {"enabled": True, "time": "06:30"},
                        {"enabled": False, "time": "07:15"},
                    ],
                }
            }
        }
        alarms = parse_sleepypod_side(attributes, "right", self.defaults)
        self.assertEqual(len(alarms), 2)
        self.assertEqual(alarms[0].weekdays, ("saturday",))
        self.assertTrue(alarms[0].enabled)
        self.assertFalse(alarms[1].enabled)
        self.assertEqual(alarms[0].source_ref, "sleepypod:right")

    def test_source_unavailability_retains_last_known_records_and_freshness(self) -> None:
        now = datetime(2030, 1, 1, tzinfo=UTC)
        attributes = {
            "left": {
                "monday": {
                    "power": {"off": "09:00"},
                    "alarms": [{"enabled": True, "time": "06:30"}],
                }
            }
        }
        available = refresh_source_snapshot(
            SourceSnapshot(),
            "sleepypod:left",
            attributes=attributes,
            available=True,
            now=now,
            defaults=self.defaults,
        )
        unavailable = refresh_source_snapshot(
            available,
            "sleepypod:left",
            attributes=None,
            available=False,
            now=now + timedelta(minutes=3),
            defaults=self.defaults,
        )
        self.assertFalse(unavailable.available)
        self.assertEqual(unavailable.alarms, available.alarms)
        self.assertEqual(unavailable.failure_code, "source_unavailable")
        self.assertEqual(
            source_freshness_seconds(
                unavailable,
                now + timedelta(minutes=3),
            ),
            180,
        )
        self.assertEqual(normalize_source_lifecycle("ringing"), "ringing")
        self.assertEqual(normalize_source_lifecycle("snoozed"), "snoozed")
        self.assertEqual(normalize_source_lifecycle("stopped"), "stopped")
        self.assertEqual(normalize_source_lifecycle("unknown"), "unavailable")
        self.assertEqual(
            normalize_source_terminal_reason(
                "idle",
                {"terminal_reason": "expired"},
            ),
            "expired",
        )
        self.assertEqual(
            normalize_source_terminal_reason(
                "idle",
                {"terminal_reason": "stopped"},
            ),
            "stopped",
        )
        self.assertIsNone(normalize_source_terminal_reason("idle", {}))

    def test_sleepypod_alarm_identity_survives_enabled_state_changes(self) -> None:
        enabled_attributes = {
            "right": {
                "monday": {
                    "power": {"off": "09:00"},
                    "alarms": [{"enabled": True, "time": "06:30"}],
                }
            }
        }
        disabled_attributes = {
            "right": {
                "monday": {
                    "power": {"off": "09:00"},
                    "alarms": [{"enabled": False, "time": "06:30"}],
                }
            }
        }
        enabled = parse_sleepypod_side(
            enabled_attributes,
            "right",
            self.defaults,
        )
        disabled = parse_sleepypod_side(
            disabled_attributes,
            "right",
            self.defaults,
        )
        self.assertEqual(enabled[0].id, disabled[0].id)
        self.assertTrue(enabled[0].enabled)
        self.assertFalse(disabled[0].enabled)

    def test_sleepypod_repeated_times_aggregate_into_one_weekly_alarm(self) -> None:
        attributes = {
            "right": {
                "sunday": {
                    "power": {"off": "09:00"},
                    "alarms": [{"enabled": True, "time": "06:30"}],
                },
                "monday": {
                    "power": {"off": "09:00"},
                    "alarms": [
                        {"enabled": True, "time": "06:30"},
                        {"enabled": True, "time": "07:00"},
                    ],
                },
            }
        }
        alarms = parse_sleepypod_side(
            attributes,
            "right",
            self.defaults,
        )
        self.assertEqual(len(alarms), 2)
        six_thirty = next(
            alarm for alarm in alarms if alarm.local_time == "06:30"
        )
        self.assertEqual(
            six_thirty.weekdays,
            ("sunday", "monday"),
        )

    def test_sleepypod_same_time_alarm_slots_are_not_coalesced(self) -> None:
        attributes = {
            "right": {
                "monday": {
                    "power": {"off": "09:00"},
                    "alarms": [
                        {"enabled": True, "time": "06:30"},
                        {"enabled": True, "time": "06:30"},
                    ],
                }
            }
        }

        alarms = parse_sleepypod_side(
            attributes,
            "right",
            self.defaults,
        )

        self.assertEqual(len(alarms), 2)
        self.assertEqual(
            [alarm.local_time for alarm in alarms],
            ["06:30", "06:30"],
        )
        self.assertNotEqual(alarms[0].id, alarms[1].id)

    def test_stale_once_alarm_is_not_deleted_but_is_identified_for_disable(self) -> None:
        alarm = WakeLightAlarm(
            id="stale",
            label="Stale",
            kind="once",
            local_time="06:00",
            ramp_minutes=30,
            date="2030-01-01",
        )
        stale = stale_once_alarm_ids(
            (alarm,),
            datetime(2030, 1, 1, 6, 11, tzinfo=self.timezone),
            self.timezone,
        )
        self.assertEqual(stale, ("stale",))

        occurrence = resolve_alarm_occurrence(
            "master-bedroom",
            alarm,
            datetime(2030, 1, 1, 5, 30, tzinfo=self.timezone),
            self.timezone,
            self.defaults,
        )
        assert occurrence is not None
        cancelled = stale_once_alarm_ids(
            (alarm,),
            datetime(2030, 1, 1, 6, 11, tzinfo=self.timezone),
            self.timezone,
            profile_id="master-bedroom",
            terminal_occurrence_ids=(occurrence.occurrence_id,),
        )
        self.assertEqual(cancelled, ())


if __name__ == "__main__":
    unittest.main()
