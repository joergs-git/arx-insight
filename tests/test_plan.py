"""arx_plan: the ONE plan - when, what, order, targets. The rules a plan must never break (rest,
restrictions, means before the target), the things it should get right (no twins, a clean
measurement per session, aids, commitment profiles, guards) - and that it explains itself."""
import json, os, random, unittest
from datetime import date, datetime, timedelta

from tests import fixtures as fx
import arx_plan as planner
import arx_report as core

ROW, PULLDOWN, DEADLIFT, CURL, SQUAT, PRESS, DECLINE, PRESSDOWN = 3, 4, 10, 11, 19, 23, 26, 12
CATALOG = dict(fx.CATALOG)
CATALOG.update({
    "26": {"name": "Decline Press", "group": "Push", "kind": "compound", "targets": ["chest"],
           "limiters": ["triceps", "shoulders"], "joints": ["shoulder", "elbow", "wrist"]},
    "12": {"name": "Triceps Pressdown", "group": "Push", "kind": "isolation", "targets": ["triceps"], "limiters": [],
           "joints": ["elbow", "wrist"]},
    "lib:Overhead Press": {"name": "Overhead Press", "group": "Push", "kind": "compound", "targets": ["shoulders"],
                           "limiters": ["triceps"], "joints": ["shoulder", "elbow", "wrist", "neck"], "library": True},
})
CATALOG["10"] = dict(CATALOG["10"], aids=["hooks", "straps"])
CATALOG["4"] = dict(CATALOG["4"], aids=["hooks", "straps"])
POS = {ROW: (8.4, 20.0), PULLDOWN: (20.0, 8.4), DEADLIFT: (20.0, 8.4), CURL: (8.4, 20.0), SQUAT: (20.0, 8.4),
       PRESS: (10.0, 20.0), DECLINE: (10.0, 20.0), PRESSDOWN: (8.4, 20.0)}


def session(day: datetime, exercises, factor=1.0, first_id=1, decline=0.05, gap_min=6):
    """One visit: the exercises in order, one set each, gap_min minutes from start to start."""
    rows = []
    for i, ex in enumerate(exercises):
        a, b = POS[ex]
        rows.append(fx.make_set(first_id + i, ex, day + timedelta(minutes=gap_min * i), start_pos=a, end_pos=b,
                                con=(150 * factor, decline), ecc=(240 * factor, decline)))
    return rows


def history(exercises, days=(1, 4, 8, 11, 15), factors=None, **kw):
    rows = []
    for n, d in enumerate(days):
        rows += session(datetime(2026, 9, d, 10), exercises, (factors or [1.0] * len(days))[n], first_id=100 * (n + 1), **kw)
    return rows


def report(rows, today, users=None, **extra):
    extra.setdefault("_catalog", CATALOG)
    return core.build_report(fx.FakeDB(rows, users), fx.cfg(today, **extra))


def names(sess):
    return [it["name"] for it in sess["exercises"]]


class When(unittest.TestCase):
    def test_trained_today_means_tomorrow_at_the_earliest(self):
        r = report(history([ROW, PRESS, SQUAT], days=(1, 4, 8, 11, 15)), "2026-09-15")
        plan = r["plan"]
        self.assertGreater(plan["next_session"]["date"], "2026-09-15")
        self.assertFalse(plan["today"]["train_today"])
        self.assertIn("date_trained_today", [w["code"] for w in plan["next_session"]["why_this_date"]])
        self.assertIsNone(plan["today_session"])                        # nothing more today

    def test_muscles_are_ready_on_the_planned_date(self):
        r = report(history([ROW, PRESS, SQUAT, CURL]), "2026-09-16")
        for d in r["plan"]["week_strip"]:
            if d["session"]:
                self.assertNotIn("recovering", [d["readiness_by_region"][x] for x in d["session"]["regions"]], d)
        dates = [w["date"] for w in r["plan"]["week_plan"]]
        self.assertEqual(dates, sorted(set(dates)))
        self.assertGreaterEqual(len(dates), 2)                          # the week goes on after the next session

    def test_cadence_follows_the_weekly_target(self):
        rows = history([ROW, PRESS, SQUAT])
        two = report(rows, "2026-09-16", sessions_per_week=2)["plan"]["week_plan"]
        one = report(rows, "2026-09-16", sessions_per_week=1)["plan"]["week_plan"]
        self.assertGreater(len(two), len(one))

    def test_a_poor_checkin_moves_the_session_and_says_so(self):
        checkin = {"date": "2026-09-19", "sleep": "poor", "energy": "low", "soreness": {}}
        plan = report(history([ROW, PRESS, SQUAT]), "2026-09-19", checkin=checkin)["plan"]
        self.assertTrue(plan["today"]["rest_today"])
        self.assertGreater(plan["next_session"]["date"], "2026-09-19")
        self.assertIn("date_checkin_rest", [w["code"] for w in plan["next_session"]["why_this_date"]])

    def test_a_moderate_checkin_caps_effort_and_steps_today(self):
        checkin = {"date": "2026-09-19", "sleep": "poor", "energy": "ok", "soreness": {}}   # 40 of 75 -> moderate
        plan = report(history([ROW, PRESS, SQUAT]), "2026-09-19", checkin=checkin)["plan"]
        sess = plan["next_session"] if plan["today"]["train_today"] else plan["today_session"]
        self.assertEqual(sess["date"], "2026-09-19")
        self.assertIn("checkin", sess["effort_caps"])
        self.assertTrue(all(it["effort_target"]["label"] != "deep" and it["step_pct"] == 0 for it in sess["exercises"]))

    def test_an_ordinary_day_is_a_full_training_day(self):
        """Owner (v0.8.4): sleep ok, energy ok, nothing sore is what people tick when nothing is wrong - the middle
        answers must not make the session lighter. Only a clearly worse signal does - and then the plan says that
        it was the check-in, not the clock."""
        rows, day = history([ROW, PRESS, SQUAT, CURL, DEADLIFT, PRESSDOWN]), "2026-09-20"
        free = report(rows, day, session_minutes=40)["plan"]["next_session"]
        ordinary = report(rows, day, session_minutes=40, checkin={"date": day, "sleep": "ok", "energy": "ok", "soreness": {}, "minutes": 90})
        self.assertEqual(ordinary["readiness"]["band"], "go_hard")
        self.assertEqual(names(ordinary["plan"]["next_session"]), names(free))            # the very same session, today
        self.assertNotIn("checkin_cut", ordinary["plan"]["next_session"])
        for lang in ("en", "de"):
            off = report(rows, day, session_minutes=40, language=lang,
                         checkin={"date": day, "sleep": "poor", "energy": "ok", "soreness": {}, "minutes": 90})
            self.assertEqual(off["readiness"]["band"], "moderate")
            plan = off["plan"]
            today = plan["next_session"] if plan["today"]["train_today"] else plan["today_session"]
            self.assertLess(len(today["exercises"]), len(free["exercises"]))              # lighter ...
            cut = today["checkin_cut"]                                                    # ... and it says why: not the 90 minutes
            self.assertEqual((cut["code"], cut["params"]["k"], cut["params"]["n"]), ("size_checkin", len(today["exercises"]), len(free["exercises"])))
            self.assertNotIn("{", cut["text"]["meaning"] + cut["text"]["action"])
            self.assertNotIn("time_window", today)                                        # 90 minutes were never the limit

    def test_an_athlete_without_history_gets_a_starter_session(self):
        plan = report([], "2026-09-18")["plan"]
        sess = plan["next_session"]
        self.assertEqual(sess["date"], "2026-09-18")
        self.assertGreaterEqual(len(sess["exercises"]), 3)
        self.assertTrue(all(it["new"] and it["effort_target"]["label"] == "submax" and it["target_peak_kg"] is None
                            for it in sess["exercises"]))


class What(unittest.TestCase):
    def test_avoid_is_never_planned_and_careful_is_sub_maximal(self):
        rows = history([ROW, PRESS, SQUAT, DEADLIFT])
        plan = report(rows, "2026-09-18", restrictions={"knee": "avoid", "shoulder": "careful"})["plan"]
        planned = {n for w in plan["week_plan"] for n in w["exercises"]}
        self.assertFalse(planned & {"Belt Squat", "Dead Lift"})          # both load the knee
        careful = [it for it in plan["next_session"]["exercises"] if it["restriction"] == "careful"]
        self.assertTrue(careful)
        for it in careful:
            self.assertEqual((it["effort_target"]["label"], it["target_rule"], it["target_peak_kg"]), ("submax", "sub_max_careful", None))

    def test_twins_are_not_doubled_unless_the_region_is_a_focus(self):
        rows = history([PRESS, DECLINE, ROW, SQUAT, CURL])
        plan = report(rows, "2026-09-18")["plan"]
        chest = [n for n in names(plan["next_session"]) if n in ("Horizontal Press", "Decline Press")]
        self.assertEqual(len(chest), 1)
        other = ({"Horizontal Press", "Decline Press"} - set(chest)).pop()
        alt = next(a for a in plan["next_session"]["alternatives"] if a["name"] == other)
        self.assertEqual((alt["reason"], alt["with"]), ("same_muscles", chest[0]))
        more = report(rows, "2026-09-18", focus_regions={"chest": "more"}, session_minutes=40)["plan"]
        self.assertEqual(len([n for n in names(more["next_session"]) if "Press" in n and n != "Overhead Press"]), 2)

    def test_a_new_exercise_only_fills_a_free_slot_or_serves_a_focus(self):
        rows = history([ROW, PRESS, SQUAT, CURL, DEADLIFT])
        plan = report(rows, "2026-09-18", session_minutes=20)["plan"]
        self.assertNotIn("Overhead Press", names(plan["next_session"]))
        gap = next(a for a in plan["next_session"]["alternatives"] if a["name"] == "Overhead Press")
        self.assertEqual((gap["reason"], gap["new"]), ("closes_a_gap", True))
        focus = report(rows, "2026-09-18", session_minutes=20, focus_regions={"shoulders": "more"})["plan"]
        it = next(x for x in focus["next_session"]["exercises"] if x["name"] == "Overhead Press")
        self.assertEqual((it["new"], it["effort_target"]["label"], it["target_rule"]), (True, "submax", "new_exercise"))
        self.assertEqual(sum(1 for x in focus["next_session"]["exercises"] if x["new"]), 1)

    def test_focus_off_removes_a_region(self):
        plan = report(history([ROW, PRESS, SQUAT, DEADLIFT]), "2026-09-18", focus_regions={"legs": "off"})["plan"]
        self.assertFalse({n for w in plan["week_plan"] for n in w["exercises"]} & {"Belt Squat", "Dead Lift"})

    def test_old_push_pull_drive_focus_is_mapped(self):
        self.assertEqual(planner.focus_regions({"focus": {"Drive": "off", "Pull": "more"}}),
                         {"legs": "off", "back": "more", "chest": "normal", "shoulders": "normal", "arms": "more"})

    def test_three_and_more_sessions_a_week_become_a_rotating_split(self):
        rows = history([ROW, PRESS, SQUAT, CURL, DEADLIFT, PRESSDOWN, PULLDOWN])
        week = report(rows, "2026-09-18", sessions_per_week=4, session_minutes=25)["plan"]["week_plan"]
        self.assertGreaterEqual(len(week), 4)
        self.assertTrue(all(w["session_type"] == "split" for w in week))
        first, second = set(week[0]["exercises"]), set(week[1]["exercises"])
        self.assertFalse(first & second)                                 # other muscles the next time
        # a helper muscle stays with the exercises it helps in: no curl on the day before the rows
        for a, b in zip(week, week[1:]):
            if "Biceps Curl" in a["exercises"] and (date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days < 3:
                self.assertFalse({"Row", "Pull Down"} & set(b["exercises"]), (a, b))

    def test_the_athlete_may_choose_full_body_or_split(self):
        rows = history([ROW, PRESS, SQUAT, CURL, DEADLIFT, PRESSDOWN, PULLDOWN])
        split = report(rows, "2026-09-18", sessions_per_week=2, structure="split", session_minutes=25)["plan"]
        self.assertEqual(split["profile"]["structure"], "split")
        self.assertTrue(all(w["session_type"] == "split" for w in split["week_plan"]))
        groups = lambda w: {CATALOG[c]["group"] for c in CATALOG if CATALOG[c]["name"] in w["exercises"]}
        self.assertTrue(all(len(groups(w)) <= 2 for w in split["week_plan"]), split["week_plan"])
        full = report(rows, "2026-09-18", sessions_per_week=4, structure="full_body", session_minutes=25)["plan"]
        self.assertTrue(all(w["session_type"] == "full_body" for w in full["week_plan"]))
        self.assertEqual(full["cadence_note"]["code"], "cadence_limited")   # four full-body days a week: recovery says no
        self.assertEqual(planner.split_factor(2), 1)
        self.assertEqual(planner.split_factor(2, "split"), 2)
        self.assertEqual(planner.split_factor(6, "full_body"), 1)

    def test_next_session_only_these_groups_applies_once(self):
        rows = history([ROW, PRESS, SQUAT, CURL, DEADLIFT, PULLDOWN])                     # last sets: 2026-09-15 10:xx
        choice = {"groups": ["Pull"], "set_at": "2026-09-15 18:00:00"}
        plan = report(rows, "2026-09-16", next_groups=choice, session_minutes=40)["plan"]
        sess = plan["next_session"]
        self.assertEqual({it["group"] for it in sess["exercises"]}, {"Pull"})
        self.assertEqual(sess["why_this_date"][0]["code"], "date_manual_groups")
        self.assertEqual(plan["profile"]["next_groups"], ["Pull"])
        self.assertNotEqual({CATALOG[c]["group"] for c in CATALOG if CATALOG[c]["name"] in plan["week_plan"][1]["exercises"]}, {"Pull"})
        used_up = report(rows, "2026-09-16", next_groups=dict(choice, set_at="2026-09-15 09:00:00"), session_minutes=40)["plan"]
        self.assertIsNone(used_up["profile"]["next_groups"])                # a session came after the choice
        self.assertGreater(len({it["group"] for it in used_up["next_session"]["exercises"]}), 1)

    def test_one_session_a_week_is_always_full_body_with_a_big_exercise_per_group(self):
        rows = history([ROW, PRESS, SQUAT, CURL, DEADLIFT, PRESSDOWN, PULLDOWN], days=(1, 8, 15))
        plan = report(rows, "2026-09-18", sessions_per_week=1, structure="split", session_minutes=25)["plan"]
        sess = plan["next_session"]
        self.assertEqual(sess["session_type"], "full_body")               # a split once a week = every muscle every 2-3 weeks
        self.assertEqual(plan["structure_note"]["code"], "structure_split_too_rare")
        groups = {it["group"] for it in sess["exercises"] if it["kind"] == "compound"}
        self.assertEqual(groups, {"Push", "Pull", "Drive"})
        self.assertIn("sel_cover_group", [w["code"] for it in sess["exercises"] for w in it["why_selected"]])
        self.assertEqual(plan["dose_note"]["code"], "dose_one_session_growth")          # muscle goal, one session: said openly
        two = report(rows, "2026-09-18", sessions_per_week=2, structure="split", session_minutes=25)["plan"]
        self.assertIsNone(two["structure_note"])
        self.assertIsNone(two["dose_note"])

    def test_a_muscle_that_would_wait_too_long_is_flagged(self):
        rows = history([ROW, PRESS, SQUAT, CURL], days=(1, 8, 15))
        choice = {"groups": ["Push"], "set_at": "2026-09-15 18:00:00"}                   # only push next time, once a week
        plan = report(rows, "2026-09-18", sessions_per_week=1, next_groups=choice)["plan"]
        flagged = {n["region"]: n["days"] for n in plan["frequency_notes"]}
        self.assertTrue(flagged and all(d > planner.MAX_MUSCLE_GAP_DAYS for d in flagged.values()), flagged)
        self.assertIn("legs", flagged)                                    # the legs get nothing for far more than a week
        self.assertIn("back", flagged)
        self.assertNotIn("chest", flagged)
        fine = report(history([ROW, PRESS, SQUAT]), "2026-09-16", sessions_per_week=2)["plan"]
        self.assertEqual(fine["frequency_notes"], [])

    def test_the_week_plan_never_takes_a_worse_date_to_fill_the_horizon(self):
        week = report(history([ROW, PRESS, SQUAT]), "2026-09-16", sessions_per_week=1)["plan"]["week_plan"]
        gaps = [(date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days for a, b in zip(week, week[1:])]
        self.assertTrue(all(g >= 6 for g in gaps), gaps)

    def test_a_cadence_the_recovery_rules_cannot_deliver_is_said_openly(self):
        rows = history([ROW, PRESS, SQUAT])
        plan = report(rows, "2026-09-18", sessions_per_week=6, session_minutes=25)["plan"]
        self.assertEqual(plan["cadence_note"]["code"], "cadence_limited")
        self.assertLess(plan["cadence_note"]["params"]["possible"], 6)
        self.assertIsNone(report(rows, "2026-09-18", sessions_per_week=2)["plan"]["cadence_note"])


class Order(unittest.TestCase):
    def test_a_means_comes_before_the_target(self):
        rows = history([ROW, CURL, PRESS, PRESSDOWN, SQUAT])
        plan = report(rows, "2026-09-18", session_minutes=40)["plan"]
        order = names(plan["next_session"])
        self.assertLess(order.index("Row"), order.index("Biceps Curl"))
        self.assertLess(order.index("Horizontal Press"), order.index("Triceps Pressdown"))
        curl = next(it for it in plan["next_session"]["exercises"] if it["name"] == "Biceps Curl")
        self.assertEqual(curl["order_rules"][0]["code"], "order_means_first")
        self.assertFalse(planner.check_plan(plan["next_session"], *self._ctx(rows, "2026-09-18")))

    def _ctx(self, rows, today):
        r = report(rows, today, session_minutes=40)
        pool = planner.build_pool(r["exercises"], [], CATALOG, {}, date.fromisoformat(today), r["history"]["progress_factors"])
        return pool, planner.muscle_state(r["load"], [], CATALOG, {}), date.fromisoformat(today)

    def test_check_plan_names_what_is_wrong(self):
        rows = history([ROW, CURL, PRESS, SQUAT])
        r = report(rows, "2026-09-15", session_minutes=40)               # trained today: nothing is ready today
        pool, state, today = self._ctx(rows, "2026-09-15")
        bad = {"date": "2026-09-15", "exercises": [
            {"name": "Biceps Curl", "effort_target": {"label": "deep"}, "muscles": {}},
            {"name": "Row", "effort_target": {"label": "deep"}, "muscles": {}},
            {"name": "Leg Swing", "effort_target": {"label": "deep"}, "muscles": {}}]}
        codes = {p["code"] for p in planner.check_plan(bad, pool, state, today)}
        self.assertEqual(codes, {"muscle_not_ready", "target_before_its_means", "unknown_exercise"})

    def test_the_benchmark_is_done_before_anything_that_loads_its_muscles(self):
        # Row always came after Pull Down: never measured fresh -> it gets the clean slot now
        rows = history([PULLDOWN, ROW, PRESS, SQUAT])
        plan = report(rows, "2026-09-18", focus_regions={"back": "more"}, session_minutes=40)["plan"]
        sess = plan["next_session"]
        self.assertEqual(sess["benchmark"], "Row")
        bench = next(it for it in sess["exercises"] if it["benchmark"])
        self.assertEqual((bench["planned_context"], bench["evidence"], bench["target_rule"]), ("fresh", [], "retest_fresh"))
        self.assertIn("sel_benchmark_never", [w["code"] for w in bench["why_selected"]])
        nxt = plan["week_plan"][1]
        self.assertNotEqual(nxt["benchmark"], "Row")                     # the rotation moves on

    def test_the_order_does_not_depend_on_the_input_order(self):
        rows = history([ROW, CURL, PRESS, PRESSDOWN, SQUAT, DEADLIFT])
        r = report(rows, "2026-09-18", session_minutes=45)
        today = date(2026, 9, 18)
        base = None
        for seed in range(4):
            ex = list(r["exercises"])
            random.Random(seed).shuffle(ex)
            pool = planner.build_pool(ex, [], CATALOG, {}, today, r["history"]["progress_factors"])
            state = planner.muscle_state(r["load"], [], CATALOG, {})
            sel = planner.select_session(today + timedelta(days=3), pool, state, {}, planner.focus_regions({}), 2, 5, today)
            seq, _rows, meta = planner.best_order(sel["chosen"], r["evidence"])
            got = ([c["name"] for c in seq], meta["benchmark"])
            base = base or got
            self.assertEqual(got, base)

    def test_a_measured_order_effect_outranks_the_prior(self):
        a = {"name": "A", "kind": "compound", "new": False, "muscles": {"lats": "target"}, "base_score": 1.0, "set_seconds": 100,
             "bench_rank": None}
        b = dict(a, name="B")
        ev = {"pair_effects": [{"id": "order:B|after:A", "before": "A", "then": "B", "n": 4, "loss_pct": 25.0, "confidence": "low"}]}
        seq, rows, _ = planner.best_order([a, b], ev)                    # prior both ways: 8 %; measured A->B: 25 %
        self.assertEqual([c["name"] for c in seq], ["B", "A"])
        self.assertEqual(rows[1]["evidence"][0]["source"], "prior")
        seq, rows, _ = planner.best_order([a, b], {})
        self.assertEqual([c["name"] for c in seq], ["A", "B"])           # nothing measured: a tie, natural order

    def test_no_rest_effect_is_assumed(self):
        self.assertIsNone(planner._rest_model({"rest_effect": {"status": "not_detectable", "loss_share_change_per_min": None}}))
        self.assertEqual(planner._rest_model({"rest_effect": {"status": "detected", "loss_share_change_per_min": -0.05,
                                                              "reference_minutes": 6.0}}), (-0.05, 6.0))


class How(unittest.TestCase):
    def test_a_target_after_a_shared_muscle_exercise_allows_for_the_loss(self):
        rows = history([ROW, SQUAT, PRESS]) + session(datetime(2026, 9, 2, 10), [PULLDOWN], first_id=900)
        plan = report(rows, "2026-09-18", focus_regions={"back": "more"}, session_minutes=40)["plan"]
        ex = {it["name"]: it for it in plan["next_session"]["exercises"]}
        self.assertEqual(plan["next_session"]["benchmark"], "Pull Down")
        row = ex["Row"]
        self.assertEqual(row["base_context"], "fresh")
        self.assertGreater(row["shared_loss_pct"], 0)
        self.assertAlmostEqual(row["target_peak_kg"], row["base_kg"] * (1 - row["shared_loss_pct"] / 100), delta=0.11)
        self.assertEqual(row["context_note"]["code"], "plan_context_adjusted")

    def test_commitment_profiles(self):
        rows = history([ROW, PRESS, SQUAT, CURL], factors=[1.0, 1.04, 1.08, 1.12, 1.16], decline=0.06)
        got = {}
        for key in planner.COMMITMENT:
            plan = report(rows, "2026-09-19", commitment=key, session_minutes=30)["plan"]
            self.assertTrue(plan["profile"]["commitment_chosen"])
            got[key] = plan["next_session"]
        first = lambda s: s["exercises"][0]
        self.assertEqual(first(got["min_time_max_effort"])["effort_target"]["label"], "deep")
        self.assertEqual(first(got["more_time_less_brutal"])["effort_target"]["label"], "moderate")
        self.assertEqual(first(got["more_time_less_brutal"])["sets"], 2)
        self.assertEqual(first(got["min_time_max_effort"])["sets"], 1)
        self.assertGreater(got["more_time_less_brutal"]["est_minutes"], got["min_time_max_effort"]["est_minutes"])
        self.assertIn("step", {it["target_rule"] for it in got["min_time_max_effort"]["exercises"]})
        self.assertTrue(all(it["step_pct"] == 0 for it in got["maintain"]["exercises"]))
        self.assertLessEqual(len(got["maintain"]["exercises"]), len(got["balanced"]["exercises"]))
        opts = {o["key"]: o for o in report(rows, "2026-09-19")["plan"]["profile"]["commitment_options"]}
        self.assertGreater(opts["more_time_less_brutal"]["minutes_per_week"], opts["min_time_max_effort"]["minutes_per_week"])
        self.assertLessEqual(opts["maintain"]["minutes_per_week"], opts["balanced"]["minutes_per_week"])
        self.assertFalse(opts["maintain"]["progression"])

    def test_an_unfinished_set_means_effort_before_force(self):
        rows = history([ROW, PRESS, SQUAT], factors=[1.0, 1.04, 1.08, 1.12, 1.16], decline=0.01)   # no real fatigue
        sess = report(rows, "2026-09-19")["plan"]["next_session"]
        rules = {it["name"]: it["target_rule"] for it in sess["exercises"] if not it["benchmark"]}
        self.assertTrue(rules and set(rules.values()) == {"hold_reach_effort"}, rules)
        self.assertTrue(all(it["step_pct"] == 0 for it in sess["exercises"]))

    def test_minors_are_capped_and_supervised(self):
        rows = history([ROW, PRESS, SQUAT, CURL, DEADLIFT], factors=[1.0, 1.04, 1.08, 1.12, 1.16], decline=0.06)
        plan = report(rows, "2026-09-19", users={1: ("m", datetime(2011, 5, 1))}, session_minutes=45)["plan"]
        sess = plan["next_session"]
        self.assertTrue(plan["profile"]["supervision"])
        self.assertEqual(sess["guard"]["code"], "guard_youth")
        self.assertLessEqual(len(sess["exercises"]), planner.MINOR_MAX_EXERCISES)
        self.assertTrue(all(it["effort_target"]["label"] != "deep" and it["step_pct"] == 0 and it["sets"] == 1
                            for it in sess["exercises"]))

    def test_age_alone_adds_no_rest_day_but_one_session_a_week_gets_a_word(self):
        self.assertEqual(planner.rest_days(3, "70+"), planner.rest_days(3, "18-29"))          # science.json: older_adults
        rows = history([ROW, PRESS, SQUAT])
        older = {1: ("f", datetime(1960, 5, 1))}
        one = report(rows, "2026-09-18", users=older, sessions_per_week=1)["plan"]["next_session"]
        two = report(rows, "2026-09-18", users=older, sessions_per_week=2)["plan"]["next_session"]
        self.assertEqual(one["guard"]["code"], "guard_older_dose")
        self.assertNotIn("guard", two)

    def test_an_exercise_not_done_for_months_restarts_gently(self):
        rows = history([ROW, PRESS, SQUAT]) + session(datetime(2026, 1, 10, 10), [CURL], first_id=900)
        sess = report(rows, "2026-09-18", session_minutes=45)["plan"]["next_session"]
        curl = next(it for it in sess["exercises"] if it["name"] == "Biceps Curl")
        self.assertEqual((curl["target_rule"], curl["target_peak_kg"], curl["step_pct"]), ("return_after_break", None, 0.0))
        self.assertNotEqual(curl["effort_target"]["label"], "deep")


class Science(unittest.TestCase):
    """Every planner default that rests on sport science names an entry of science.json - and the
    entry is referenced and reviewed."""
    def setUp(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "science.json"), encoding="utf-8") as f:
            self.science = json.load(f)

    def test_every_default_cites_an_entry_with_references(self):
        self.assertGreaterEqual(len(planner.SCIENCE), 15)
        for const, topic in planner.SCIENCE.items():
            self.assertTrue(hasattr(planner, const.split(".")[0]) or const.split(".")[0] == "REQUIRED_REST", const)
            entry = self.science["topics"].get(topic)
            self.assertIsNotNone(entry, f"{const} cites unknown topic {topic}")
            self.assertTrue(entry["references"] and entry["reviewed"] and entry["applied_as"] and entry["practical_rule"], topic)

    def test_references_are_traceable(self):
        for topic, entry in self.science["topics"].items():
            self.assertIn(entry["evidence_level"], self.science["evidence_levels"], topic)
            for r in entry["references"]:
                self.assertTrue(r.get("doi") or r.get("pmid"), (topic, r.get("title")))
                self.assertTrue(r["authors"] and r["year"] and r["title"] and r["journal"], (topic, r))


class Limiters(unittest.TestCase):
    def setUp(self):
        self.rows = history([PULLDOWN, DEADLIFT, CURL, PRESS])

    def test_budget_counts_the_muscle_only_where_it_is_a_means(self):
        sess = report(self.rows, "2026-09-18", session_minutes=40)["plan"]["next_session"]
        grip = sess["limiter_budget"]["grip"]
        self.assertEqual({r["name"] for r in grip["by_exercise"]}, {"Pull Down", "Dead Lift", "Biceps Curl"})
        self.assertEqual({r["name"] for r in sess["limiter_budget"]["elbow_flexors"]["by_exercise"]}, {"Pull Down"})   # not the curl
        self.assertEqual(grip["status"], "ok")                           # the same three as in every session so far

    def test_an_aid_is_suggested_where_the_limiter_is_farthest_from_the_goal(self):
        sess = report(self.rows, "2026-09-18", session_minutes=40)["plan"]["next_session"]
        self.assertEqual([(h["exercise"], h["aid"]) for h in sess["aid_hints"]], [("Dead Lift", "hooks")])
        self.assertEqual(next(it for it in sess["exercises"] if it["name"] == "Dead Lift")["aid_hint"], "hooks")

    def test_an_aid_in_use_takes_the_grip_out(self):
        aids = {"10": {"aids": ["hooks"], "since": "2026-09-01"}}
        sess = report(self.rows, "2026-09-18", session_minutes=40, aids=aids)["plan"]["next_session"]
        self.assertEqual({r["name"] for r in sess["limiter_budget"]["grip"]["by_exercise"]}, {"Pull Down", "Biceps Curl"})
        self.assertFalse(sess["aid_hints"])
        self.assertEqual(next(it for it in sess["exercises"] if it["name"] == "Dead Lift")["aid"], "hooks")


class Ledger(unittest.TestCase):
    def test_the_same_plan_is_stored_once_and_a_changed_one_replaces_todays(self):
        rows = history([ROW, PRESS, SQUAT])
        today = date(2026, 9, 18)
        plan = report(rows, "2026-09-18")["plan"]
        entries, changed = planner.update_ledger([], plan, today)
        self.assertTrue(changed)
        entries, changed = planner.update_ledger(entries, plan, today)
        self.assertFalse(changed)
        other = report(rows, "2026-09-18", focus_regions={"legs": "off"})["plan"]
        entries, changed = planner.update_ledger(entries, other, today)
        self.assertTrue(changed)
        self.assertEqual(len(entries), 1)                                # same day: the latest recommendation counts
        self.assertNotIn("Athlete", json.dumps(entries))

    def test_plan_vs_actual(self):
        rows = history([ROW, PRESS, SQUAT], days=(1, 4, 8, 11))
        plan = report(rows, "2026-09-12")["plan"]
        entries, _ = planner.update_ledger([], plan, date(2026, 9, 12))
        planned = entries[-1]["exercises"]
        done = [x for x in planned if x["name"] != "Horizontal Press"]
        codes = {"Row": ROW, "Belt Squat": SQUAT, "Horizontal Press": PRESS}
        day = datetime.fromisoformat(entries[-1]["date"] + "T10:00:00") + timedelta(days=1)   # a day later than planned
        rows2 = rows + session(day, [codes[x["name"]] for x in done] + [CURL], first_id=800)
        r = report(rows2, day.date().isoformat(), _plan_ledger=entries)
        pva = r["plan_vs_actual"]
        self.assertEqual((pva["date_delta_days"], pva["skipped"], pva["added"]), (1, ["Horizontal Press"], ["Biceps Curl"]))
        self.assertEqual(set(pva["done"]), {x["name"] for x in done})
        self.assertEqual(pva["interp"]["code"], "pva_partly")
        self.assertTrue(pva["interp"]["text"]["meaning"])
        self.assertIsNone(report(rows, "2026-09-12", _plan_ledger=entries)["plan_vs_actual"])   # no session after the plan yet


class Breaks(unittest.TestCase):
    """v0.5.1 - a gap is named as what it is: a short one holds the numbers, a long one sets a new
    starting point, and nothing is ever "caught up" (science.json: training_breaks)."""
    ROWS = None

    def setUp(self):
        if Breaks.ROWS is None:                    # progressing on every exercise: without a break this earns a step
            Breaks.ROWS = history([ROW, PRESS, SQUAT, CURL], factors=[1.0, 1.04, 1.08, 1.12, 1.16], decline=0.06)

    def plan(self, today, **extra):
        return report(self.ROWS, today, session_minutes=40, **extra)["plan"]

    def test_without_a_break_nothing_changes(self):
        plan = self.plan("2026-09-19")
        self.assertIsNone(plan["training_break"])
        codes = [w["code"] for w in plan["next_session"]["why_this_date"]]
        self.assertIn("date_cadence", codes)
        self.assertNotIn("date_after_break", codes)
        self.assertIn("step", {it["target_rule"] for it in plan["next_session"]["exercises"]})
        self.assertEqual(plan["decision_space"]["target_floor_pct"], {})

    def test_a_short_break_continues_the_plan_but_holds_the_numbers(self):
        plan = self.plan("2026-10-02")             # 17 days after the last session
        brk, sess = plan["training_break"], plan["next_session"]
        self.assertEqual((brk["tier"], brk["days"], brk["interp"]["code"]), ("short", 17, "break_short"))
        codes = [w["code"] for w in sess["why_this_date"]]
        self.assertIn("date_after_break", codes)
        self.assertNotIn("date_cadence", codes)     # the weekly rhythm is no argument after a break
        self.assertNotIn("date_overdue", codes)
        self.assertEqual(sess["date"], "2026-10-02")                    # everything is recovered: today
        self.assertEqual(sess["session_type"], "full_body")
        for it in (x for x in sess["exercises"] if not x["new"]):
            self.assertEqual((it["step_pct"], it["sets"]), (0.0, 1), it["name"])
            self.assertNotIn(it["target_rule"], ("step", "plateau_add_set", "rebase_after_break"), it["name"])
            self.assertEqual(it["effort_target"]["label"], "deep")      # the effort stays what the goal asks for
        self.assertIn("plan_hold_after_break", {it["interp"]["code"] for it in sess["exercises"]})
        later = [w for w in plan["week_plan"] if w["date"] > sess["date"]]
        self.assertTrue(later)                      # and the week goes on as usual

    def test_a_long_break_sets_a_new_starting_point_and_adds_nothing(self):
        plan = self.plan("2026-10-25")             # 40 days
        brk, sess, space = plan["training_break"], plan["next_session"], plan["decision_space"]
        self.assertEqual((brk["tier"], brk["weeks"], brk["interp"]["code"]), ("long", 6, "break_long"))
        self.assertEqual({it["target_rule"] for it in sess["exercises"]}, {"rebase_after_break"})
        self.assertFalse(any(it["new"] for it in sess["exercises"]))   # back to the known exercises first - nothing new on the comeback day
        for it in sess["exercises"]:
            self.assertEqual((it["target_peak_kg"], it["sets"], it["step_pct"]), (it["base_kg"], 1, 0.0))   # orientation, nothing extra
        self.assertEqual(set(space["target_floor_pct"]), set(names(sess)))
        # the coach may go down to the floor, never further up than usual
        rows = [{"exercise": it["name"], "sets": 1, "effort": "deep", "rest_before_min": it["rest_before_min"],
                 "target_kg": round(it["target_peak_kg"] * 0.88, 1)} for it in sess["exercises"]]
        self.assertEqual(planner.check_rows(sess["date"], rows, space), [])
        for factor in (0.80, 1.10):
            bad = [dict(r, target_kg=round(sess["exercises"][i]["target_peak_kg"] * factor, 1)) if i == 0 else r for i, r in enumerate(rows)]
            self.assertIn("target_out_of_bounds", [x["code"] for x in planner.check_rows(sess["date"], bad, space)], factor)

    def test_after_half_a_year_the_restart_is_gentle(self):
        plan = self.plan("2027-05-01")
        self.assertEqual(plan["training_break"]["tier"], "very_long")
        self.assertEqual({it["target_rule"] for it in plan["next_session"]["exercises"]}, {"return_after_break"})
        self.assertTrue(all(it["effort_target"]["label"] != "deep" for it in plan["next_session"]["exercises"]))

    def test_a_neglected_muscle_is_treated_like_a_break(self):
        rows = history([ROW, PRESS, SQUAT]) + session(datetime(2026, 8, 10, 10), [CURL], first_id=900)
        ex = {it["name"]: it for it in report(rows, "2026-09-18", session_minutes=45)["plan"]["next_session"]["exercises"]}
        # the curl's target (elbow flexors) had no DIRECT work for 39 days although the athlete trained all along
        self.assertEqual(ex["Biceps Curl"]["target_rule"], "rebase_after_break")
        self.assertNotEqual(ex["Row"]["target_rule"], "rebase_after_break")

    def test_the_rhythm_really_lived_decides_about_a_split(self):
        self.assertIsNone(planner.real_frequency(["2026-09-01", "2026-09-15"]))                   # too short a history
        weekly = [(date(2026, 8, 3) + timedelta(days=7 * k)).isoformat() for k in range(7)]
        self.assertEqual(planner.real_frequency(weekly), 1.0)
        self.assertEqual(planner.real_frequency(weekly + ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]), 2.0)
        self.assertEqual(planner.split_factor(4, "auto"), 2)
        self.assertEqual(planner.split_factor(4, "auto", 1.5), 1)       # a muscle would wait 9 days
        self.assertEqual(planner.split_factor(4, "auto", 2.0), 2)
        self.assertEqual(planner.split_factor(5, "auto", 2.0), 2)       # thirds need about three real sessions a week
        self.assertEqual(planner.split_factor(5, "auto", 3.0), 3)
        self.assertEqual(planner.split_factor(4, "split", 1.0), 2)      # an explicit wish is kept - and commented
        rows = []
        for n, day in enumerate(weekly):
            rows += session(datetime.fromisoformat(day + "T10:00:00"), [ROW, PRESS, SQUAT, CURL, DEADLIFT, PULLDOWN, PRESSDOWN],
                            first_id=100 * (n + 1))
        auto = report(rows, "2026-09-16", sessions_per_week=4, session_minutes=45)["plan"]
        self.assertEqual(auto["structure_note"]["code"], "structure_adapted_real")
        self.assertEqual((auto["profile"]["split_factor"], auto["profile"]["real_sessions_per_week"]), (1, 1.0))
        self.assertEqual(auto["next_session"]["session_type"], "full_body")
        wish = report(rows, "2026-09-16", sessions_per_week=4, session_minutes=45, structure="split")["plan"]
        self.assertEqual(wish["structure_note"]["code"], "structure_split_real_too_rare")
        self.assertEqual(wish["next_session"]["session_type"], "split")


# v0.7.0 - a bigger repertoire: Overhead Press and High Pull are mapped exercises the athlete really does
OHP, HIGHPULL, CALF, LATERAL = 5, 41, 14, 77
POS.update({OHP: (10.0, 20.0), HIGHPULL: (8.4, 20.0), CALF: (8.4, 12.0)})
BIG = {k: v for k, v in CATALOG.items() if k != "lib:Overhead Press"}
BIG.update({
    "5": {"name": "Overhead Press", "group": "Push", "kind": "compound", "targets": ["shoulders"], "limiters": ["triceps"],
          "joints": ["shoulder", "elbow", "wrist", "neck"]},
    "41": {"name": "High Pull", "group": "Pull", "kind": "compound", "targets": ["upper_back", "shoulders"], "limiters": ["grip"],
           "joints": ["shoulder", "elbow", "wrist", "lower_back", "neck"]},
    "14": {"name": "Calf Raise", "group": "Drive", "kind": "isolation", "targets": ["calves"], "limiters": [], "joints": ["knee"]},
})
CODE = {m["name"]: int(c) for c, m in BIG.items()}


def roll_forward(rows, start: str, weeks: int, **extra):
    """Let the planner run: every proposed session is performed as planned, the next report is made
    the day after. -> [(date, [exercise names], plan)]"""
    rows, today, end, out, sid = list(rows), date.fromisoformat(start), date.fromisoformat(start) + timedelta(days=7 * weeks), [], 5000
    while today < end:
        plan = report(rows, today.isoformat(), _catalog=BIG, **extra)["plan"]
        sess = plan["next_session"]
        if not sess or date.fromisoformat(sess["date"]) >= end:
            break
        day = date.fromisoformat(sess["date"])
        rows += session(datetime(day.year, day.month, day.day, 10), [CODE[n] for n in names(sess)], first_id=sid)
        sid += 20
        out.append((day, names(sess), plan))
        today = day + timedelta(days=1)
    return out


class WeeklyStimulus(unittest.TestCase):
    """The big slot of a movement group goes to the exercise that keeps the muscles at a weekly
    stimulus - with Overhead Press / High Pull in the repertoire 'a compound of the group' is not
    enough any more (found by simulation after all 17 exercises were mapped)."""
    REPERTOIRE = [ROW, PRESS, SQUAT, OHP, HIGHPULL, CURL, PRESSDOWN, CALF]

    def test_once_a_week_no_big_muscle_waits_two_weeks(self):
        rows = []
        for n, d in enumerate((1, 8, 15)):                                 # three weeks in which everything was trained
            rows += session(datetime(2026, 9, d, 10), self.REPERTOIRE, first_id=100 * (n + 1))
        log = roll_forward(rows, "2026-09-16", 5, sessions_per_week=1, session_minutes=30)
        self.assertGreaterEqual(len(log), 4)
        for _, ns, plan in log:
            direct = {m for n in ns for m in BIG[str(CODE[n])]["targets"]}
            reached = direct | {m for n in ns for m in BIG[str(CODE[n])]["limiters"]}
            self.assertLessEqual({"chest", "lats", "upper_back", "quads", "glutes", "calves"}, direct, ns)   # every week, directly
            self.assertIn("shoulders", reached, ns)                        # at least as a helper of the press
            self.assertEqual(plan["frequency_notes"], [], ns)              # and the plan has nothing to warn about
        self.assertGreater(len({tuple(sorted(ns)) for _, ns, _ in log}), 1)                # the free slot still rotates
        said = [w for _, _, plan in log for it in plan["next_session"]["exercises"] for w in it["why_selected"] if w["code"] == "sel_urgent"]
        self.assertTrue(said)                                              # the athlete is told why it is this exercise
        for w in said:
            self.assertNotIn("_", w["text"]["meaning"] + w["text"]["action"])
            self.assertNotIn("{", w["text"]["meaning"] + w["text"]["action"])

    def test_nothing_urgent_means_the_old_choice(self):
        rows = history([ROW, PRESS, SQUAT, CURL, DEADLIFT, PRESSDOWN, PULLDOWN])          # twice a week, everything in rhythm
        sess = report(rows, "2026-09-16", sessions_per_week=2, session_minutes=25)["plan"]["next_session"]
        self.assertFalse([w for it in sess["exercises"] for w in it["why_selected"] if w["code"] == "sel_urgent"])
        cands = planner.score_candidates  # noqa: F841  (the candidates carry the urgency they were judged on)
        state = {"chest": {"ready_on": None, "last_target": "2026-09-15", "last_stim": "2026-09-15", "sore": None}}
        pool = [{"code": "23", "name": "Horizontal Press", "group": "Push", "kind": "compound", "muscles": {"chest": "target"},
                 "regions": ["chest"], "new": False, "library": False, "series": None, "progress": None, "restriction": "ok",
                 "aids_possible": [], "aid": None, "set_seconds": 100, "impulse": 0, "last_fresh": None, "n_days": 3}]
        focus = planner.focus_regions({})
        soon = planner.score_candidates(pool, state, date(2026, 9, 18), focus, 2, {"language": "en"})
        late = planner.score_candidates(pool, state, date(2026, 9, 21), focus, 2, {"language": "en"})
        self.assertEqual((soon[0]["urgent"], late[0]["urgent"]), ([], ["chest"]))         # 3 + 3.5 days is fine, 6 + 3.5 is not


class SessionSize(unittest.TestCase):
    """v0.8.0 (owner): how many exercises a session holds is the athlete's TIME decision - not a magic number,
    not a rule of the coach. Alternating unrelated muscles costs no result (science.json: paired_sets)."""
    EIGHT = [ROW, PRESS, SQUAT, OHP, HIGHPULL, CURL, PRESSDOWN, CALF, DEADLIFT]

    def rows(self, gap_min=6):
        out = []
        for n, d in enumerate((1, 4, 8, 11, 15)):
            out += session(datetime(2026, 9, d, 10), self.EIGHT, first_id=100 * (n + 1), gap_min=gap_min)
        return out

    def test_more_time_buys_more_exercises_up_to_the_cap(self):
        rows = self.rows()
        small = report(rows, "2026-09-19", _catalog=BIG, session_minutes=30)["plan"]
        big = report(rows, "2026-09-19", _catalog=BIG, session_minutes=90)["plan"]
        self.assertEqual(planner.SESSION_MAX_EX, 8)
        self.assertEqual(big["profile"]["exercises_per_session"], 8)
        self.assertGreater(len(big["next_session"]["exercises"]), len(small["next_session"]["exercises"]))
        self.assertGreaterEqual(len(big["next_session"]["exercises"]), 7)
        by = big["profile"]["size_by_minutes"]
        self.assertEqual(list(by), [str(m) for m in planner.MINUTES_OPTIONS])             # what every budget buys, for the profile
        sizes = list(by.values())
        self.assertEqual(sizes, sorted(sizes))
        self.assertEqual((by["90"], small["profile"]["size_by_minutes"]["30"]), (8, small["profile"]["exercises_per_session"]))
        auto = report(rows, "2026-09-19", _catalog=BIG)["plan"]["profile"]
        self.assertEqual((auto["auto_size"], auto["session_minutes"]), (auto["exercises_per_session"], auto["auto_minutes"]))
        # a big session may go one deeper per body region (like a split day), a normal one may not
        legs = lambda plan: [it["name"] for it in plan["next_session"]["exercises"] if "legs" in it["regions"]]
        self.assertLessEqual(len(legs(small)), planner.MAX_PER_REGION)
        self.assertEqual(len(legs(big)), planner.MAX_PER_REGION + 1)

    def test_the_coach_gets_the_price_of_one_more_exercise_not_a_wall(self):
        plan = report(self.rows(), "2026-09-19", _catalog=BIG, session_minutes=30)["plan"]
        space, sess = plan["decision_space"], plan["next_session"]
        self.assertEqual(space["bounds"]["rows_max"], planner.SESSION_MAX_EX)
        self.assertEqual(space["rows_in_time_budget"], len(sess["exercises"]))
        self.assertGreater(space["minutes_per_extra_exercise"], 1)
        # the proposal plus TWO more trainable exercises is a valid plan (it used to be "proposal + 1")
        day = next(d for d in space["dates"] if d["date"] == sess["date"])
        extra = [c["name"] for c in day["candidates"] if c["name"] not in names(sess) and c["status"] == "ready" and not c["new"]
                 and c["restriction"] == "ok"][:2]
        self.assertEqual(len(extra), 2)
        rows = [{"exercise": it["name"], "sets": it["sets"], "target_kg": it["target_peak_kg"], "effort": it["effort_target"]["label"],
                 "rest_before_min": it["rest_before_min"]} for it in sess["exercises"]]
        rows += [{"exercise": n, "sets": 1, "target_kg": 0, "effort": "submax", "rest_before_min": 3} for n in extra]
        self.assertNotIn("row_count", [p["code"] for p in planner.check_rows(sess["date"], rows, space)])
        young = report(self.rows(), "2026-09-19", {1: ("Male", datetime(2011, 5, 1))}, _catalog=BIG, session_minutes=60)["plan"]
        self.assertEqual(young["decision_space"]["bounds"]["rows_max"], planner.MINOR_MAX_EXERCISES)   # the youth guard stays a wall

    def test_training_in_turns_is_not_wasted_set_up_time(self):
        rows = self.rows(gap_min=10)                                       # about eight minutes between two exercises
        alone = report(rows, "2026-09-19", _catalog=BIG, session_minutes=45)["plan"]
        self.assertEqual(alone["next_session"]["time_note"]["code"], "time_transitions")   # alone: set up faster, save time
        for lang in ("en", "de"):
            turns = report(rows, "2026-09-19", _catalog=BIG, session_minutes=45, partner=True, language=lang)["plan"]
            note = turns["next_session"]["time_note"]
            self.assertEqual(note["code"], "time_partner")
            self.assertLess(note["params"]["alone"], note["params"]["total"])
            self.assertNotIn("{", note["text"]["meaning"] + note["text"]["action"])
            self.assertTrue(turns["profile"]["partner"])
            self.assertEqual(names(turns["next_session"]), names(alone["next_session"]))   # the plan itself does not change


class SplitRhythm(unittest.TestCase):
    """v0.8.6 (owner): three sessions a week means every other day - a rested group trains the day after a session
    of the OTHER group; it does not wait until a limited group is fresh, and the week does not fall short."""
    UPPER, LEGS = [PRESS, DECLINE, ROW, PULLDOWN, CURL, PRESSDOWN], [SQUAT, DEADLIFT]
    HOOKS = {"10": {"aids": ["hooks"], "since": "2026-09-01"}}             # the grip is no limiter of the dead lift

    def rows(self):
        out, sid = [], 100
        for d, ex in ((8, self.UPPER), (10, self.LEGS), (12, self.UPPER), (14, self.LEGS), (16, self.UPPER), (18, self.LEGS), (20, self.UPPER)):
            out += session(datetime(2026, 9, d, 10), ex, first_id=sid)
            sid += 20
        return out

    def test_a_rested_group_trains_the_day_after_the_other_groups_session(self):
        plan = report(self.rows(), "2026-09-21", sessions_per_week=3, session_minutes=60, aids=self.HOOKS)["plan"]
        sess = plan["next_session"]
        self.assertIn(sess["date"], ("2026-09-21", "2026-09-22"))             # legs were rested for three days: now, not in three days
        self.assertEqual({it["group"] for it in sess["exercises"]}, {"Drive"})
        dates = [date.fromisoformat(w["date"]) for w in plan["week_plan"]]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        self.assertGreaterEqual(len([d for d in dates if (d - dates[0]).days < 7]), 3)   # three in the first seven days
        self.assertTrue(all(g <= 3 for g in gaps), gaps)                        # every other day, never bunched at the end
        self.assertIsNone(plan["cadence_note"])
        groups = [set(CATALOG[c]["group"] for c in CATALOG if CATALOG[c]["name"] in w["exercises"]) for w in plan["week_plan"]]
        self.assertTrue(all(a != b for a, b in zip(groups, groups[1:])), groups)            # the split rotates

    def test_a_limited_group_does_not_delay_a_fresh_one(self):
        """The upper body was trained deep yesterday; the legs are fresh. A Drive+Pull day whose rows would only be
        'limited' (biceps not fresh) must not push the legs back - legs today, the rows fresh on their day."""
        plan = report(self.rows(), "2026-09-21", sessions_per_week=3, session_minutes=60, aids=self.HOOKS)["plan"]
        first, second = plan["week_plan"][0], plan["week_plan"][1]
        self.assertFalse({"Row", "Pull Down", "Biceps Curl"} & set(first["exercises"]))
        self.assertLessEqual((date.fromisoformat(second["date"]) - date.fromisoformat(first["date"])).days, 3)


class Soreness(unittest.TestCase):
    """Mild soreness takes the all-out set away for the day; strong soreness takes the exercise away."""
    def test_mild_is_a_sub_maximal_day_and_strong_is_a_day_off_for_that_muscle(self):
        rows, hooks = SplitRhythm().rows(), SplitRhythm.HOOKS
        free = report(rows, "2026-09-21", sessions_per_week=3, session_minutes=60, aids=hooks)["plan"]
        self.assertIn(free["next_session"]["date"], ("2026-09-21", "2026-09-22"))
        self.assertEqual([o["date"] for o in free["date_options"]][:1], ["2026-09-21"])   # today is a possible day
        mild = report(rows, "2026-09-21", sessions_per_week=3, session_minutes=60, aids=hooks,
                      checkin={"date": "2026-09-21", "sleep": "good", "energy": "high", "soreness": {"legs": "mild"}})["plan"]
        strong = report(rows, "2026-09-21", sessions_per_week=3, session_minutes=60, aids=hooks,
                        checkin={"date": "2026-09-21", "sleep": "good", "energy": "high", "soreness": {"legs": "strong"}})["plan"]
        # mild: the legs may work today, sub-maximal, and the plan says why - the full session is recommended later
        today = mild["next_session"] if mild["next_session"]["date"] == "2026-09-21" else mild["today_session"]
        self.assertIsNotNone(today)                                            # "training today anyway": yes, sub-maximal
        legs = [it for it in today["exercises"] if it["group"] == "Drive"]
        self.assertTrue(legs)
        for it in legs:
            self.assertEqual((it["status"], it["effort_target"]["label"], it["target_peak_kg"], it["interp"]["code"]), ("limited", "submax", None, "plan_submax_sore"))
            self.assertNotIn("_", it["interp"]["text"]["meaning"] + it["interp"]["text"]["action"])
        # strong: no leg exercise today at all
        self.assertFalse(strong["today"]["train_today"])
        blocked = strong["today_session"]
        self.assertTrue(blocked is None or not [it for it in blocked["exercises"] if it["group"] == "Drive"])
        self.assertNotEqual([o["date"] for o in mild["date_options"]][:1], [o["date"] for o in strong["date_options"]][:1])


class TimeWindow(unittest.TestCase):
    """v0.8.1 (owner): the check-in may carry "minutes I have today" - an upper limit for a session planned for
    today. It never makes a plan longer; a plan that does not fit is cut in a sensible order."""
    SIX = [ROW, PRESS, SQUAT, CURL, DEADLIFT, PRESSDOWN]
    DAY = "2026-09-20"                                                   # the engine recommends training on this day

    def plan(self, minutes=None, day=None, **extra):
        day = day or self.DAY
        extra.setdefault("session_minutes", 40)
        if minutes is not None:
            extra["checkin"] = {"date": day, "minutes": minutes}
        return report(history(self.SIX), day, **extra)["plan"]

    def test_no_window_or_a_wide_one_changes_nothing(self):
        base = self.plan()
        self.assertEqual(base["next_session"]["date"], self.DAY)
        row = lambda s: [(it["name"], it["sets"], it["target_peak_kg"], it["rest_before_min"]) for it in s["exercises"]]
        for wide in (45, 90):                                               # more time is no reason to do more
            p = self.plan(wide)
            self.assertEqual(row(p["next_session"]), row(base["next_session"]))
            self.assertNotIn("time_window", p["next_session"])
            self.assertEqual(p["profile"]["window_minutes"], wide)
        self.assertIsNone(base["profile"]["window_minutes"])
        self.assertIsNone(self.plan(25)["profile"]["window_minutes"])       # not one of the offered windows: no limit

    def test_a_tight_window_keeps_what_matters_most(self):
        base = self.plan()["next_session"]
        for lang in ("en", "de"):
            p = self.plan(15, language=lang)
            sess, tw = p["next_session"], p["next_session"]["time_window"]
            self.assertLessEqual(sess["est_minutes"], 15)
            self.assertLess(len(sess["exercises"]), len(base["exercises"]))
            self.assertGreaterEqual(len(sess["exercises"]), planner.WINDOW_MIN_EX)
            self.assertLessEqual(set(names(sess)), set(names(base)))         # a cut of the normal plan, nothing else
            self.assertEqual({it["kind"] for it in sess["exercises"]}, {"compound"})      # the big exercises stay ...
            self.assertEqual({it["group"] for it in sess["exercises"]}, {"Push", "Pull", "Drive"})   # ... one per movement group
            self.assertEqual(sorted(tw["left_out"]), sorted(set(names(base)) - set(names(sess))))    # and what was left out is named
            self.assertEqual((tw["interp"]["code"], tw["size"], tw["normal_size"]), ("window_applied", len(sess["exercises"]), len(base["exercises"])))
            text = tw["interp"]["text"]["meaning"] + tw["interp"]["text"]["action"]
            self.assertNotIn("{", text)
            self.assertNotIn("_", text)
            # the coach cannot blow the window, and the check-in screen promised no more than the plan delivers
            self.assertLessEqual(p["decision_space"]["bounds"]["rows_max"], max(len(sess["exercises"]), p["profile"]["window_sizes"]["15"]))
            self.assertEqual(p["profile"]["window_sizes"]["15"], len(sess["exercises"]))
        # what was left out comes back by itself: the next session takes it up
        after = self.plan(15)["week_plan"][1]["exercises"]
        self.assertTrue(set(self.plan(15)["next_session"]["time_window"]["left_out"]) & set(after))

    def test_extra_sets_go_before_exercises(self):
        normal = self.plan(commitment="more_time_less_brutal", session_minutes=30)["next_session"]
        self.assertIn(2, [it["sets"] for it in normal["exercises"]])
        cut = self.plan(30, commitment="more_time_less_brutal", session_minutes=30)["next_session"]
        self.assertEqual({it["sets"] for it in cut["exercises"]}, {1})       # volume is what goes first - the hard set stays
        self.assertLessEqual(cut["est_minutes"], 30)
        self.assertGreaterEqual(len(cut["exercises"]), len(normal["exercises"]) - 1)

    def test_the_window_is_for_today_only(self):
        p = self.plan(15, day="2026-09-18")                                  # the engine recommends the 20th
        self.assertGreater(p["next_session"]["date"], "2026-09-18")
        self.assertNotIn("time_window", p["next_session"])                   # that day has its normal budget
        self.assertEqual(len(p["next_session"]["exercises"]), len(self.plan(day="2026-09-18")["next_session"]["exercises"]))
        today = p["today_session"]                                           # "training today anyway" is what the window cuts
        self.assertLessEqual(today["est_minutes"], 15)
        self.assertEqual(today["time_window"]["interp"]["code"], "window_applied")


class Excluded(unittest.TestCase):
    """Exercises the athlete does not do on the ARX (profile: excluded_exercises)."""
    def test_a_switched_off_exercise_does_not_exist_for_the_planner(self):
        rows = history([ROW, PRESS, SQUAT, CURL, DEADLIFT])
        base = report(rows, "2026-09-18", session_minutes=40)["plan"]
        self.assertIn("Biceps Curl", {n for w in base["week_plan"] for n in w["exercises"]})
        self.assertIn("Overhead Press", names(base["next_session"]) + [a["name"] for a in base["next_session"]["alternatives"]])   # for the shoulders
        self.assertIsNone(base["excluded"])
        plan = report(rows, "2026-09-18", session_minutes=40,
                      excluded_exercises={"11": "unwanted", "lib:Overhead Press": "elsewhere", "999": "unwanted", "3": "because"})["plan"]
        everywhere = ({n for w in plan["week_plan"] for n in w["exercises"]} | {a["name"] for a in plan["next_session"]["alternatives"]}
                      | {c["name"] for d in plan["decision_space"]["dates"] for c in d["candidates"]} | set(plan["decision_space"]["muscles"]))
        self.assertFalse(everywhere & {"Biceps Curl", "Overhead Press"})
        self.assertIn("Row", everywhere)                                   # an unknown reason switches nothing off
        ex = plan["excluded"]
        self.assertEqual([(x["name"], x["reason"]) for x in ex["exercises"]], [("Biceps Curl", "unwanted"), ("Overhead Press", "elsewhere")])
        self.assertEqual(ex["external_muscles"], ["shoulders"])
        self.assertEqual([n["code"] for n in ex["notes"]], ["excluded_elsewhere", "excluded_unwanted"])
        # the coach can not bring it back: not a candidate on any date
        problems = planner.check_rows(plan["next_session"]["date"], [{"exercise": "Biceps Curl", "sets": 1, "target_kg": 0, "effort": "submax",
                                                                      "rest_before_min": 2}], plan["decision_space"])
        self.assertIn("not_trainable_that_day", [p["code"] for p in problems])

    def test_trained_elsewhere_means_covered_there(self):
        catalog = dict(CATALOG, **{"77": {"name": "Lateral Raise", "group": "Push", "kind": "isolation", "targets": ["shoulders"],
                                          "limiters": [], "joints": ["shoulder"]}})
        rows = history([ROW, PRESS, SQUAT, DEADLIFT]) + session(datetime(2026, 8, 20, 10), [CURL], first_id=900)   # the curl: four weeks ago
        gap = lambda plan: {a["name"] for a in plan["next_session"]["alternatives"] if a["reason"] == "closes_a_gap"}
        neglected = lambda r: {f["muscle"] for f in r["history"]["findings"]["all"] if f["type"] == "neglected_muscle"}
        plain = report(rows, "2026-09-18", session_minutes=20, _catalog=catalog)
        self.assertIn("elbow_flexors", neglected(plain))
        self.assertTrue(gap(plain["plan"]) & {"Overhead Press", "Lateral Raise"})
        away = report(rows, "2026-09-18", session_minutes=20, _catalog=catalog,
                      excluded_exercises={"11": "elsewhere", "lib:Overhead Press": "elsewhere"})
        self.assertNotIn("elbow_flexors", neglected(away))                  # curls are done at home: not neglected
        self.assertFalse(gap(away["plan"]) & {"Overhead Press", "Lateral Raise"})      # shoulders are trained elsewhere: nothing new for them
        self.assertFalse([n for n in away["plan"]["frequency_notes"] if n["region"] in ("arms", "shoulders")])
        unwanted = report(rows, "2026-09-18", session_minutes=20, _catalog=catalog, excluded_exercises={"lib:Overhead Press": "unwanted"})
        self.assertIn("Lateral Raise", gap(unwanted["plan"]))               # not wanted: the muscles stay the plan's business
        self.assertIn("elbow_flexors", neglected(unwanted))

    def test_a_switched_off_exercise_leaves_no_trace_in_the_plan(self):
        """Owner's rule (v0.7.1): what is switched off has no significance whatsoever. The reference is a
        catalog that never contained the exercise: 'not for me' must give the very same report; 'elsewhere'
        may only ever REMOVE a suggestion (its muscles count as covered), never add one."""
        rows = history([ROW, PRESS, SQUAT, CURL, DEADLIFT])
        without = {c: m for c, m in BIG.items() if c not in ("41", "14")}               # no High Pull, no Calf Raise

        def clean(r):
            r = json.loads(json.dumps(r, default=str))
            r.pop("generated", None)
            r["plan"].pop("excluded", None)
            return r
        ref = clean(report(rows, "2026-09-18", session_minutes=30, _catalog=without))
        off = clean(report(rows, "2026-09-18", session_minutes=30, _catalog=BIG, excluded_exercises={"41": "unwanted", "14": "unwanted"}))
        self.assertEqual(off, ref)
        away = clean(report(rows, "2026-09-18", session_minutes=30, _catalog=BIG, excluded_exercises={"41": "elsewhere", "14": "elsewhere"}))
        offered = lambda r: [{c["name"] for c in d["candidates"]} for d in r["plan"]["decision_space"]["dates"]]
        self.assertEqual([d["date"] for d in away["plan"]["decision_space"]["dates"]], [d["date"] for d in ref["plan"]["decision_space"]["dates"]])
        for a, b in zip(offered(away), offered(ref)):
            self.assertLessEqual(a, b)                                     # upper back + shoulders elsewhere: fewer ideas, never more
        self.assertLessEqual({a["name"] for a in away["plan"]["next_session"]["alternatives"]},
                             {a["name"] for a in ref["plan"]["next_session"]["alternatives"]})
        self.assertNotIn("Overhead Press", {n for s_ in offered(away) for n in s_})          # the shoulders are trained elsewhere

    def test_an_exercise_with_a_history_goes_quiet_when_it_is_switched_off(self):
        rows = history([ROW, PRESS, SQUAT, CURL], factors=[1.0, 0.97, 0.94, 0.91, 0.88]) \
            + session(datetime(2026, 8, 25, 10), [PRESSDOWN], first_id=900)             # triceps: only long ago
        plain = report(rows, "2026-09-16")
        about = lambda r, name: [f["type"] for f in r["history"]["findings"]["all"] if f.get("exercise") == name]
        self.assertTrue(about(plain, "Biceps Curl"))                       # falling numbers are worth a finding ...
        self.assertIn("Biceps Curl", [x["name"] for x in plain["load"]["recovery"]["exercises"]])
        for reason in planner.EXCLUDE_REASONS:
            r = report(rows, "2026-09-16", excluded_exercises={"11": reason, "12": reason})
            self.assertEqual(about(r, "Biceps Curl"), [], reason)          # ... until the athlete switches it off
            quiet = {"Biceps Curl", "Triceps Pressdown"}
            rec = r["load"]["recovery"]
            self.assertFalse(quiet & {x["name"] for x in rec["exercises"]})
            self.assertFalse(quiet & (set(rec["ready_today"]) | {x["name"] for x in rec["limited_today"]} | {x["name"] for x in rec["not_ready"]}))
            self.assertFalse(quiet & {x["name"] for x in r["coach"]["exercises"]})
            self.assertFalse(quiet & {x["name"] for x in (r["coach"].get("deload") or {}).get("declining", [])})
            self.assertFalse(quiet & set(r["evidence"].get("never_fresh") or []))
            neglected = {f["muscle"] for f in r["history"]["findings"]["all"] if f["type"] == "neglected_muscle"}
            self.assertFalse(neglected & {"elbow_flexors", "triceps"}, reason)    # no exercise is left for them: nobody's business
            self.assertIn("elbow_flexors", rec["muscles"])                  # the body did the curls yesterday: recovery still counts them
            kept = {e["name"]: e["excluded"] for e in r["exercises"]}
            self.assertEqual((kept["Biceps Curl"], kept["Row"]), (reason, None))   # the history itself stays - it is data

    def test_not_possible_for_health_reasons_rules_out_exactly_that_exercise(self):
        """Owner (v0.8.7): a shoulder problem forbids the overhead pressing, not every exercise that touches the
        joint. Reason "injury": planned like "not for me" (identical to a catalog without it), the muscles stay the
        plan's business, and doing it anyway shows up in the "limits respected?" mirror."""
        rows = history([ROW, PRESS, SQUAT, CURL, DEADLIFT])
        without = {c: m for c, m in BIG.items() if c != "5"}                       # no Overhead Press at all

        def clean(r):
            r = json.loads(json.dumps(r, default=str))
            r.pop("generated", None)
            r["plan"].pop("excluded", None)
            r.pop("restriction_checks", None)
            return r
        ref = clean(report(rows, "2026-09-18", session_minutes=30, _catalog=without))
        hurt = report(rows, "2026-09-18", session_minutes=30, _catalog=BIG, excluded_exercises={"5": "injury"})
        self.assertEqual(clean(hurt), ref)
        ex = hurt["plan"]["excluded"]
        self.assertEqual([(x["name"], x["reason"]) for x in ex["exercises"]], [("Overhead Press", "injury")])
        self.assertEqual(ex["external_muscles"], [])                             # the shoulders stay the plan's business
        self.assertEqual([n["code"] for n in ex["notes"]], ["excluded_injury"])
        for lang in ("en", "de"):
            n = report(rows, "2026-09-18", session_minutes=30, _catalog=BIG, language=lang, excluded_exercises={"5": "injury"})["plan"]["excluded"]["notes"][0]
            self.assertNotIn("{", n["text"]["meaning"] + n["text"]["action"])
        # the body part can go back to "ok": nothing else is eased, the shoulder-joint exercises train at full effort
        sess = hurt["plan"]["next_session"]
        self.assertIn("Horizontal Press", names(sess))
        press = next(it for it in sess["exercises"] if it["name"] == "Horizontal Press")
        self.assertEqual((press["restriction"], press["effort_target"]["label"]), ("ok", "deep"))
        # done anyway -> the mirror says so (like an avoided one), with no body-part restriction set at all
        done = rows + session(datetime(2026, 9, 17, 10), [OHP], first_id=900)
        mirror = report(done, "2026-09-18", session_minutes=30, _catalog=BIG, excluded_exercises={"5": "injury"})["restriction_checks"]
        self.assertEqual([(c["exercise"], c["issue"]) for c in mirror], [("Overhead Press", "trained_avoid")])
        self.assertEqual(report(done, "2026-09-18", session_minutes=30, _catalog=BIG, excluded_exercises={"5": "unwanted"})["restriction_checks"], [])

    def test_switching_off_known_exercises_is_no_reason_for_a_beginner_session(self):
        rows = history([ROW, PRESS, SQUAT, DEADLIFT])
        plan = report(rows, "2026-09-18", session_minutes=40, excluded_exercises={"19": "elsewhere", "10": "unwanted"})["plan"]
        sess = plan["next_session"]
        self.assertLessEqual(sum(1 for it in sess["exercises"] if it["new"]), 1)
        self.assertFalse({"Belt Squat", "Dead Lift"} & set(names(sess)))

    def test_everything_switched_off_is_said_plainly(self):
        rows = history([ROW, PRESS, SQUAT])
        plan = report(rows, "2026-09-18", excluded_exercises={"3": "unwanted", "23": "elsewhere", "19": "elsewhere"})["plan"]
        if plan["next_session"] is None:
            self.assertEqual(plan["today"]["interp"]["code"], "plan_all_excluded")
        else:                                                              # only never-performed exercises are left: nothing the athlete switched off
            self.assertFalse({"Row", "Horizontal Press", "Belt Squat"} & set(names(plan["next_session"])))
        self.assertEqual(len(plan["excluded"]["exercises"]), 3)


class SelfExplaining(unittest.TestCase):
    def test_every_plan_item_has_both_sentences_in_both_languages(self):
        rows = history([PULLDOWN, ROW, DEADLIFT, CURL, PRESS, PRESSDOWN, SQUAT], factors=[1.0, 1.04, 1.08, 1.12, 1.16], decline=0.06)
        for lang, units in (("en", "imperial"), ("de", "metric")):
            for extra in ({}, {"sessions_per_week": 4}, {"checkin": {"date": "2026-09-19", "sleep": "ok", "energy": "ok", "soreness": {}}},
                          {"_day": "2026-10-04"}, {"_day": "2026-11-20"}, {"_day": "2027-06-01"},           # after a short / long / very long break
                          {"excluded_exercises": {"11": "elsewhere", "12": "unwanted"}},                    # exercises switched off in the profile
                          {"_day": "2026-09-20", "checkin": {"date": "2026-09-20", "minutes": 15}}):        # a tight time window today
                extra = dict(extra)
                plan = report(rows, extra.pop("_day", "2026-09-19"), language=lang, units=units, session_minutes=40, **extra)["plan"]
                items = []

                def walk(node):
                    if isinstance(node, dict):
                        if "code" in node and "text" in node:
                            items.append(node)
                        for v in node.values():
                            walk(v)
                    elif isinstance(node, list):
                        for v in node:
                            walk(v)
                walk(plan)
                self.assertGreater(len(items), 10)
                for it in items:
                    self.assertTrue(it["text"]["meaning"] and it["text"]["action"], it["code"])
                    self.assertNotIn("{", it["text"]["meaning"] + it["text"]["action"], it["code"])
                    self.assertNotIn("_", it["text"]["meaning"] + it["text"]["action"], it["code"])   # no raw identifiers

    def test_every_meaning_code_used_by_the_planner_exists(self):
        import re
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "arx_plan.py"), encoding="utf-8") as f:
            src = f.read()
        with open(os.path.join(here, "meanings.json"), encoding="utf-8") as f:
            meanings = json.load(f)
        codes = set(re.findall(r'item[(]\s*"([a-z_]+)"', src))
        codes |= {f"commitment_{k}" for k in planner.COMMITMENT} | {"budget_ok", "budget_high", "budget_unknown",
                                                                     "today_train", "today_rest", "pva_followed", "pva_partly", "pva_other",
                                                                     "break_short", "break_long", "break_very_long"}
        self.assertGreater(len(codes), 30)
        self.assertFalse(sorted(c for c in codes if c not in meanings))


if __name__ == "__main__":
    unittest.main()
