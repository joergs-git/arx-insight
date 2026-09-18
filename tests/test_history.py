"""arx_history: weekly / monthly windows with honest availability, progress factors with a status,
ranked findings - and the rule that every item explains itself in both languages."""
import json, os, re, unittest
from datetime import datetime, timedelta

from tests import fixtures as fx
import arx_history as hist
import arx_report as core

ROW, PRESS, SQUAT = 3, 23, 19


def series(exercise, start, factors, every=4, **kw):
    """One set of an exercise every `every` days; factors scale both phase forces (1.0 = 150 / 240 lb)."""
    base = dict(start_pos=8.4, end_pos=20.0)
    base.update(kw)
    return [fx.make_set(100 * exercise + i, exercise, start + timedelta(days=every * i),
                        con=(150 * f, 0.05), ecc=(240 * f, 0.05), **base) for i, f in enumerate(factors)]


def report(rows, today, **extra):
    return core.build_report(fx.FakeDB(rows), fx.cfg(today, **extra))


def progress_of(r, name):
    return next(p for p in r["history"]["progress_factors"]["exercises"] if p["name"] == name)


class Windows(unittest.TestCase):
    def setUp(self):
        self.rows = series(ROW, datetime(2026, 9, 1, 10), [1, 1, 1, 1, 1, 1], every=3)      # 1..16 Sep
        self.r = report(self.rows, "2026-09-18")

    def test_weekly_buckets_and_the_partial_current_week(self):
        weeks = self.r["history"]["windows"]["weeks"]
        self.assertEqual([w["label"] for w in weeks], ["W36", "W37", "W38"])                 # nothing before the first week
        self.assertEqual([w["training_days"] for w in weeks], [2, 3, 1])
        self.assertEqual([w["partial"] for w in weeks], [False, False, True])
        self.assertEqual([w["target_met"] for w in weeks], [True, True, False])
        self.assertEqual(sum(w["working_sets"] for w in weeks), 6)

    def test_hard_sets_per_region_count_limiters_half(self):
        w = self.r["history"]["windows"]["weeks"][1]                                         # three hard Row sets
        self.assertEqual(w["hard_sets"], 3)
        self.assertEqual(w["hard_sets_by_region"]["back"], 6.0)                              # two target muscles each
        self.assertEqual(w["hard_sets_by_region"]["grip"], 1.5)                              # limiter: half
        self.assertEqual(w["hard_sets_by_region"]["legs"], 0.0)

    def test_quarter_and_year_stay_locked_until_the_data_is_there(self):
        av = self.r["history"]["windows"]["availability"]
        self.assertTrue(av["w4"]["available"])
        self.assertEqual((av["quarter"]["available"], av["quarter"]["unlock_in_weeks"]), (False, 6))
        self.assertFalse(av["year"]["available"])
        long_rows = series(ROW, datetime(2026, 1, 5, 10), [1] * 40, every=7)
        av = report(long_rows, "2026-10-10")["history"]["windows"]["availability"]
        self.assertEqual((av["quarter"]["available"], av["year"]["available"]), (True, True))


class ProgressFactors(unittest.TestCase):
    def test_progressing_with_the_phases_reported_separately(self):
        rows = [fx.make_set(i + 1, ROW, datetime(2026, 8, 1, 10) + timedelta(days=6 * i),
                            con=(150 * (1 + 0.04 * i), 0.05), ecc=(240, 0.05)) for i in range(6)]   # only the concentric grows
        p = progress_of(report(rows, "2026-09-01"), "Row")                                   # 6 days over 30 days
        self.assertEqual((p["status"], p["reference_context"], p["n"]), ("progressing", "fresh", 6))
        self.assertGreater(p["change_con_pct"], 12)
        self.assertAlmostEqual(p["change_ecc_pct"], 0.0, delta=1.0)
        self.assertEqual(p["rate_quality"], "good")
        self.assertGreater(p["rate_pct_per_week"], 1.5)
        self.assertEqual(p["interp"]["code"], "progress_progressing")

    def test_plateau_regressing_and_too_little_data(self):
        start = datetime(2026, 8, 1, 10)
        self.assertEqual(progress_of(report(series(ROW, start, [1, 1.01, 0.99, 1, 1.01, 1], every=5), "2026-08-28"), "Row")["status"], "plateau")
        self.assertEqual(progress_of(report(series(ROW, start, [1, 1, 0.97, 0.93, 0.9, 0.9], every=5), "2026-08-28"), "Row")["status"], "regressing")
        two = progress_of(report(series(ROW, start, [1, 1.2], every=5), "2026-08-10"), "Row")
        self.assertEqual((two["status"], two["interp"]["code"]), ("insufficient", "progress_insufficient"))
        self.assertIsNone(two["rate_pct_per_week"])                                          # never a %/week from two points
        one = progress_of(report(series(ROW, start, [1]), "2026-08-02"), "Row")
        self.assertEqual((one["status"], one["interp"]["code"]), ("not_comparable", "progress_single_day"))

    def test_a_lower_value_measured_preloaded_is_not_a_regression(self):
        start = datetime(2026, 8, 1, 10)
        rows = series(ROW, start, [1, 1, 1, 1], every=5)
        last = start + timedelta(days=20)
        rows += [fx.make_set(900, 4, last, start_pos=27, end_pos=12),                        # Pull Down first ...
                 fx.make_set(901, ROW, last + timedelta(minutes=6), con=(120, 0.05), ecc=(192, 0.05))]   # ... then a weaker Row
        p = progress_of(report(rows, "2026-08-22"), "Row")
        self.assertIn(p["status"], ("stable", "plateau"))
        self.assertLess(p["latest_context_drop_pct"], -15)
        self.assertTrue(p["interp"]["code"].endswith("_context"))

    def test_overall_index_needs_three_exercises(self):
        start = datetime(2026, 8, 1, 10)
        r = report(series(ROW, start, [1, 1.1, 1.1], every=6), "2026-08-20")
        self.assertIsNone(r["history"]["progress_factors"]["overall"]["strength_index"])
        rows = (series(ROW, start, [1, 1.1, 1.1], every=6) + series(PRESS, start + timedelta(hours=1), [1, 1.1, 1.1], every=6, start_pos=10, end_pos=20)
                + series(SQUAT, start + timedelta(hours=2), [1, 1.1, 1.1], every=6, start_pos=20, end_pos=14))
        overall = report(rows, "2026-08-20")["history"]["progress_factors"]["overall"]
        self.assertAlmostEqual(overall["strength_index"], 1.10, delta=0.02)
        self.assertEqual(overall["exercises_in_index"], 3)


class Findings(unittest.TestCase):
    def test_ranked_limited_per_exercise_and_self_explaining(self):
        start = datetime(2026, 8, 1, 10)
        rows = series(ROW, start, [1, 1.02, 1.04, 1.2], every=4)                             # a comparable record at the end
        rows += series(PRESS, start + timedelta(hours=1), [1, 1], every=4, start_pos=10, end_pos=20)    # chest: then nothing for weeks
        rows += [fx.make_set(990, PRESS, start + timedelta(days=8, hours=1), start_pos=10, end_pos=16)]  # ... and a shorter range
        r = report(rows, "2026-08-14")
        f = r["history"]["findings"]
        types = [(x["type"], x["exercise"] or x["muscle"]) for x in f["all"]]
        self.assertIn(("pb", "Row"), types)
        self.assertIn(("rom_drift", "Horizontal Press"), types)
        self.assertLessEqual(len(f["top"]), hist.FINDINGS_TOP)
        for name in {x["exercise"] for x in f["all"] if x["exercise"]}:
            self.assertLessEqual(sum(1 for x in f["all"] if x["exercise"] == name), hist.FINDINGS_PER_EXERCISE)
        scores = [x["score"] for x in f["all"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        late = report(rows, "2026-09-05")["history"]["findings"]["all"]
        self.assertIn(("neglected_muscle", "chest"), [(x["type"], x["muscle"]) for x in late])


class Sentences(unittest.TestCase):
    def test_every_code_used_by_the_engine_has_both_languages(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "meanings.json"), encoding="utf-8") as fh:
            table = json.load(fh)
        with open(hist.__file__, encoding="utf-8") as fh:
            src = fh.read()
        codes = set(re.findall(r'"(finding_[a-z_]+|progress_(?!factors)[a-z_]+|overall_index[a-z_]*|time_efficiency[a-z_]*)"', src))
        codes |= {f"progress_{s}" for s in ("familiarisation", "not_comparable", "insufficient", "progressing", "regressing",
                                            "regressing_context", "plateau", "stable", "progressing_context", "stable_context", "plateau_context")}
        codes |= {f"finding_{t}" for t in ("rom_drift", "pb", "order_effect", "repeat_effect", "never_fresh", "unsteady_force", "hold_not_held",
                                           "ecc_con_shift", "underload", "short_intra_session_rest", "too_many_sets", "false_start_pattern",
                                           "effort_target_missed", "missed_weekly_target", "neglected_muscle", "context_drop")}
        for code in sorted(codes):
            for part in ("meaning", "action"):
                for lang in ("en", "de"):
                    self.assertTrue(table.get(code, {}).get(part, {}).get(lang), f"{code}.{part}.{lang} is missing")

    def test_no_placeholder_survives_and_units_follow_the_athlete(self):
        start = datetime(2026, 8, 1, 10)
        rows = series(ROW, start, [1, 1.02, 1.04, 1.2], every=4) + [fx.make_set(990, PRESS, start, start_pos=10, end_pos=20),
                                                                    fx.make_set(991, PRESS, start + timedelta(days=4), start_pos=10, end_pos=16)]
        for lang, units, unit in (("en", "imperial", " lb"), ("de", "metric", " kg")):
            r = report(rows, "2026-08-14", language=lang, units=units)
            items = [p["interp"] for p in r["history"]["progress_factors"]["exercises"]] + [x["interp"] for x in r["history"]["findings"]["all"]]
            items += [r["history"]["progress_factors"]["overall"]["interp"], r["history"]["time_efficiency"]["interp"]]
            for it in items:
                for part in ("meaning", "action"):
                    self.assertTrue(it["text"][part], it["code"])
                    self.assertNotIn("{", it["text"][part], it["code"])
            pb = next(x for x in r["history"]["findings"]["all"] if x["type"] == "pb")
            self.assertIn(unit, pb["interp"]["text"]["meaning"])


if __name__ == "__main__":
    unittest.main()
