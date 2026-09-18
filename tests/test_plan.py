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
        checkin = {"date": "2026-09-19", "sleep": "ok", "energy": "ok", "soreness": {}}     # 30 of 50 -> moderate
        plan = report(history([ROW, PRESS, SQUAT]), "2026-09-19", checkin=checkin)["plan"]
        sess = plan["next_session"] if plan["today"]["train_today"] else plan["today_session"]
        self.assertEqual(sess["date"], "2026-09-19")
        self.assertIn("checkin", sess["effort_caps"])
        self.assertTrue(all(it["effort_target"]["label"] != "deep" and it["step_pct"] == 0 for it in sess["exercises"]))

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


class SelfExplaining(unittest.TestCase):
    def test_every_plan_item_has_both_sentences_in_both_languages(self):
        rows = history([PULLDOWN, ROW, DEADLIFT, CURL, PRESS, PRESSDOWN, SQUAT], factors=[1.0, 1.04, 1.08, 1.12, 1.16], decline=0.06)
        for lang, units in (("en", "imperial"), ("de", "metric")):
            for extra in ({}, {"sessions_per_week": 4}, {"checkin": {"date": "2026-09-19", "sleep": "ok", "energy": "ok", "soreness": {}}}):
                plan = report(rows, "2026-09-19", language=lang, units=units, session_minutes=40, **extra)["plan"]
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
                                                                     "today_train", "today_rest", "pva_followed", "pva_partly", "pva_other"}
        self.assertGreater(len(codes), 30)
        self.assertFalse(sorted(c for c in codes if c not in meanings))


if __name__ == "__main__":
    unittest.main()
