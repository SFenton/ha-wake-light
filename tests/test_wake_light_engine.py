"""Pure safety and state-machine tests for Wake Light."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components"),
)

from wake_light.engine import (  # noqa: E402
    PreflightInputs,
    cancellation_decision,
    connected_episode,
    evaluate_preflight,
    mark_acquired,
    mark_dispatched,
    merge_occurrences,
    refresh_recovery_holds,
    release_leaf,
    restart_decision,
    run_view,
    start_run,
)
from wake_light.model import (  # noqa: E402
    ScheduledOccurrence,
    WakeLightDefaults,
    WakeLightProfile,
)


def profile() -> WakeLightProfile:
    return WakeLightProfile(
        profile_id="master-bedroom",
        name="Bedroom",
        root_light_entity_id="light.bedroom",
        target_light_entity_ids=(
            "light.bedroom_window_light",
            "light.bedroom_door_light",
            "light.left_nightstand_light",
            "light.right_nightstand_light",
        ),
        pbl_switch_entity_id="switch.bedroom_presence_allowed",
        vacation_entity_id="input_boolean.vacation",
        defaults=WakeLightDefaults(),
        blocker_entity_ids=("switch.adaptive_lighting_bedroom",),
        legacy_brightness_lifecycle_safe=True,
    )


def occurrence(
    occurrence_id: str,
    start: datetime,
    wake: datetime,
) -> ScheduledOccurrence:
    return ScheduledOccurrence(
        occurrence_id=occurrence_id,
        alarm_id=f"alarm-{occurrence_id}",
        source="native",
        source_ref=None,
        wake_at=wake,
        ramp_start_at=start,
        ramp_minutes=int((wake - start).total_seconds() / 60),
        hold_minutes=5,
    )


class WakeLightEngineTests(unittest.TestCase):
    def test_preflight_fails_closed_for_vacation_pbl_blockers_and_targets(self) -> None:
        base = PreflightInputs(
            integration_available=True,
            vacation_state="off",
            pbl_state="on",
            target_states={"light.one": "off"},
            blocker_states={"switch.adaptive": "off"},
            valid_schedule=True,
            legacy_brightness_lifecycle_safe=True,
        )
        self.assertTrue(evaluate_preflight(base).allowed)
        self.assertIn(
            "vacation_not_off",
            evaluate_preflight(replace(base, vacation_state="unknown")).blockers,
        )
        self.assertIn(
            "pbl_not_ready",
            evaluate_preflight(replace(base, pbl_state="unavailable")).blockers,
        )
        self.assertIn(
            "blocker_not_off",
            evaluate_preflight(
                replace(base, blocker_states={"switch.adaptive": "on"})
            ).blockers,
        )
        self.assertIn(
            "target_unavailable",
            evaluate_preflight(
                replace(base, target_states={"light.one": "unavailable"})
            ).blockers,
        )

    def test_linear_curve_starts_at_one_and_dispatches_final_at_deadline(self) -> None:
        start = datetime(2030, 1, 1, 6, 0, tzinfo=UTC)
        wake = start + timedelta(minutes=30)
        run = start_run(
            profile(),
            (occurrence("occ-1", start, wake),),
            now=start,
            observed_floor_pct=1,
        )
        at_start = run_view(run, start)
        halfway = run_view(run, start + timedelta(minutes=15))
        deadline = run_view(run, wake)
        self.assertEqual(at_start.desired_brightness_pct, 1)
        self.assertAlmostEqual(halfway.desired_brightness_pct, 50.5)
        self.assertEqual(deadline.desired_brightness_pct, 100)
        self.assertTrue(deadline.force_final_dispatch)

    def test_overlap_uses_max_brightness_and_never_restarts_or_dims(self) -> None:
        start = datetime(2030, 1, 1, 6, 0, tzinfo=UTC)
        first = occurrence("occ-1", start, start + timedelta(minutes=30))
        later = occurrence(
            "occ-2",
            start + timedelta(minutes=10),
            start + timedelta(minutes=40),
        )
        run = start_run(
            profile(),
            (first,),
            now=start,
            observed_floor_pct=1,
        )
        lease_id = run.lease_id
        run = merge_occurrences(run, (later,))
        self.assertEqual(run.lease_id, lease_id)
        run = mark_dispatched(
            run,
            now=start + timedelta(minutes=20),
            brightness_pct=80,
            target_entity_ids=run.target_entity_ids,
            pbl_outcome="dispatched",
            final_dispatch=False,
        )
        view = run_view(run, start + timedelta(minutes=21))
        self.assertGreaterEqual(view.desired_brightness_pct, 80)

    def test_connected_episode_expands_through_touching_alarm_chain(self) -> None:
        start = datetime(2030, 1, 1, 6, 0, tzinfo=UTC)
        first = occurrence("occ-0630", start, start + timedelta(minutes=30))
        second = occurrence(
            "occ-0700",
            start + timedelta(minutes=30),
            start + timedelta(minutes=60),
        )
        third = occurrence(
            "occ-0730",
            start + timedelta(minutes=60),
            start + timedelta(minutes=90),
        )
        separate = occurrence(
            "occ-0900",
            start + timedelta(minutes=150),
            start + timedelta(minutes=180),
        )
        run = start_run(
            profile(),
            (first,),
            now=start,
            observed_floor_pct=1,
        )

        component = connected_episode(
            run,
            (third, separate, second),
            now=start + timedelta(minutes=29),
        )

        self.assertEqual(
            component.occurrence_ids,
            ("occ-0630", "occ-0700", "occ-0730"),
        )
        self.assertEqual(component.end_at, start + timedelta(minutes=95))

    def test_zero_ramp_waits_until_deadline_then_requires_full_dispatch(self) -> None:
        wake = datetime(2030, 1, 1, 6, 30, tzinfo=UTC)
        run = start_run(
            profile(),
            (occurrence("occ-1", wake, wake),),
            now=wake,
            observed_floor_pct=1,
        )
        deadline = run_view(run, wake)
        self.assertEqual(deadline.desired_brightness_pct, 100)
        self.assertEqual(deadline.progress, 100)
        self.assertTrue(deadline.force_final_dispatch)

    def test_catchup_restarts_the_full_hold_and_requires_a_real_final_dispatch(
        self,
    ) -> None:
        start = datetime(2030, 1, 1, 6, 0, tzinfo=UTC)
        wake = start + timedelta(minutes=30)
        caught_up_at = wake + timedelta(minutes=6)
        run = start_run(
            profile(),
            (occurrence("occ-1", start, wake),),
            now=caught_up_at,
            observed_floor_pct=1,
        )
        self.assertEqual(
            run.occurrences[0].hold_until,
            caught_up_at + timedelta(minutes=5),
        )
        view = run_view(run, caught_up_at)
        self.assertEqual(view.desired_brightness_pct, 100)
        self.assertTrue(view.force_final_dispatch)

        not_final = mark_dispatched(
            run,
            now=caught_up_at,
            brightness_pct=1,
            target_entity_ids=run.target_entity_ids,
            pbl_outcome="dispatched",
            final_dispatch=False,
        )
        self.assertFalse(not_final.occurrences[0].final_dispatched)

        recovered = refresh_recovery_holds(not_final, caught_up_at)
        self.assertEqual(
            recovered.occurrences[0].hold_until,
            caught_up_at + timedelta(minutes=5),
        )

    def test_restart_catchup_manual_revoke_and_leaf_release(self) -> None:
        start = datetime(2030, 1, 1, 6, 0, tzinfo=UTC)
        wake = start + timedelta(minutes=30)
        run = start_run(
            profile(),
            (occurrence("occ-1", start, wake),),
            now=start,
            observed_floor_pct=1,
        )
        run = mark_acquired(
            run,
            generation=2,
            acquired_at=start,
            expires_at=start + timedelta(hours=1),
            outcome="acquired",
        )
        self.assertEqual(
            restart_decision(run, start + timedelta(minutes=10)).action,
            "resume",
        )
        self.assertEqual(
            restart_decision(run, wake + timedelta(minutes=11)).action,
            "missed",
        )
        revoke = cancellation_decision(run, "manual_group_off")
        self.assertFalse(revoke.release_required)
        self.assertFalse(revoke.turn_off_owned)
        self.assertTrue(revoke.user_initiated)
        self.assertTrue(
            cancellation_decision(
                run,
                "manual_brightness_control",
            ).user_initiated
        )

        reduced, outcome = release_leaf(
            run,
            "light.bedroom_window_light",
        )
        self.assertEqual(outcome, "updated")
        assert reduced is not None
        self.assertNotIn(
            "light.bedroom_window_light",
            reduced.target_entity_ids,
        )
        current = reduced
        for entity_id in tuple(current.target_entity_ids):
            current, _ = release_leaf(current, entity_id)
            if current is None:
                break
        self.assertIsNone(current)


if __name__ == "__main__":
    unittest.main()
