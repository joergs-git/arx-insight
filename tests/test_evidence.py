"""arx_evidence + the comparable / familiarisation logic: context of a set within its visit, grip
aids, priors and shrinkage, measured order and repeat effects, and learning days that must not
count as progress or as hard loads."""
import unittest
from datetime import datetime

from tests import fixtures as fx
import arx_evidence as ev
import arx_report as core

ROW, PULLDOWN, DEADLIFT, CURL, PRESS = 3, 4, 10, 11, 23


def built(rows, today, **extra):
    return core.build_report(fx.FakeDB(rows), fx.cfg(today, **extra))


def work_sets(rows, **cfg_extra):
    """The working sets as the engine sees them (detail + context + comparable flags attached)."""
    cfg = fx.cfg("2026-10-01", **cfg_extra)
    sets = core.load_sets(fx.FakeDB(rows), 1)
    work = [s for s in sets if s["working"]]
    import arx_detail as detail
    for s in work:
        meta = fx.CATALOG.get(str(s["exercise"]), {})
        s["name"], s["group"] = meta.get("name"), meta.get("group")
        core.attach_detail(s, detail.get_detail(fx.FakeDB(rows), s, {}))
        s["limiters_eff"] = ev.effective_limiters(meta, s["exercise"], s["date"], cfg.get("aids"))
    core.cap_low_force(work)
    ev.annotate_context(work, fx.CATALOG, cfg.get("aids"))
    core._exercise_series(work, fx.CATALOG, {})
    return work


class Context(unittest.TestCase):
    def test_fresh_preloaded_repeat_and_a_new_visit(self):
        d = lambda h, m: datetime(2026, 9, 1, h, m)
        rows = [fx.make_set(1, ROW, d(10, 0)), fx.make_set(2, PRESS, d(10, 6), start_pos=10, end_pos=20),
                fx.make_set(3, PULLDOWN, d(10, 12), start_pos=27, end_pos=12),
                fx.make_set(4, ROW, d(10, 18)), fx.make_set(5, ROW, d(14, 0))]          # hours later = new visit
        w = work_sets(rows)
        self.assertEqual([s["context"] for s in w], ["fresh", "fresh", "preloaded", "repeat", "fresh"])
        self.assertEqual([s["visit"] for s in w], [1, 1, 1, 1, 2])
        self.assertEqual([s["position"] for s in w], [1, 2, 3, 4, 1])
        by = w[2]["preloaded_by"][0]
        self.assertEqual((by["exercise"], by["muscles"]), ("Row", ["elbow_flexors", "grip", "lats", "upper_back"]))
        self.assertAlmostEqual(by["minutes"], 12 - w[0]["seconds"] / 60, delta=0.1)       # end of Row -> start of Pull Down
        self.assertGreater(w[2]["preload"]["lats"], w[2]["preload"]["grip"])             # target counts full, limiter half

    def test_hooks_take_the_grip_out_from_the_day_they_are_used(self):
        d = lambda day, m: datetime(2026, 9, day, 10, m)
        rows = [fx.make_set(1, DEADLIFT, d(1, 0), start_pos=20, end_pos=9), fx.make_set(2, ROW, d(1, 6)),
                fx.make_set(3, DEADLIFT, d(8, 0), start_pos=20, end_pos=9), fx.make_set(4, ROW, d(8, 6))]
        w = work_sets(rows, aids={"10": {"aids": ["hooks"], "since": "2026-09-05"}})
        self.assertEqual((w[1]["context"], w[1]["preloaded_by"][0]["muscles"]), ("preloaded", ["grip"]))
        self.assertEqual(w[3]["context"], "fresh")                                       # the only shared muscle is gone
        self.assertEqual(ev.effective_limiters(fx.CATALOG["10"], 10, "2026-09-08", {"10": {"aids": ["hooks"]}}), ["lower_back"])


class PriorsAndShrinkage(unittest.TestCase):
    def test_priors_follow_the_catalog_overlap(self):
        m = lambda code: ev.set_muscles({"exercise": code, "date": "2026-09-01"}, fx.CATALOG)
        self.assertEqual(ev.pair_prior(m(CURL), m(ROW)), 10.0 + 4.0)      # curl's target is the row's limiter + shared grip
        self.assertEqual(ev.pair_prior(m(DEADLIFT), m(ROW)), 4.0)         # only the grip, a limiter for both
        self.assertEqual(ev.pair_prior(m(PRESS), m(ROW)), 0.0)
        self.assertEqual(ev.pair_prior(m(PULLDOWN), m(ROW)), ev.PRIOR_CAP)

    def test_observations_pull_the_estimate_as_n_grows(self):
        self.assertEqual(ev.shrink([], 12.0), 12.0)
        self.assertAlmostEqual(ev.shrink([30.0], 12.0), 16.5)
        self.assertAlmostEqual(ev.shrink([30.0] * 12, 12.0), 26.4)
        self.assertEqual([ev.confidence(n) for n in (1, 3, 6, 12)], ["anecdotal", "low", "medium", "high"])
        self.assertEqual(ev.confidence(8, agree=0.5), "low")


class MeasuredEffects(unittest.TestCase):
    def rows(self):
        d = lambda day, m: datetime(2026, 9, day, 10, m)
        strong, weak = dict(con=(150, 0.02), ecc=(240, 0.02)), dict(con=(120, 0.02), ecc=(192, 0.02))   # weak = 80 %
        pd = dict(start_pos=27, end_pos=12)
        return [fx.make_set(1, ROW, d(1, 0), **strong), fx.make_set(2, ROW, d(3, 0), **strong),
                fx.make_set(3, ROW, d(5, 0), **strong),
                fx.make_set(4, PULLDOWN, d(8, 0), **pd), fx.make_set(5, ROW, d(8, 6), **weak),     # Row AFTER Pull Down
                fx.make_set(6, ROW, d(11, 0), **strong), fx.make_set(7, ROW, d(11, 8), **weak)]    # a second Row set

    def test_order_effect_is_measured_against_the_fresh_baseline(self):
        e = built(self.rows(), "2026-09-12")["evidence"]
        pair = next(p for p in e["pair_effects"] if p["then"] == "Row")
        self.assertEqual((pair["before"], pair["n"], pair["confidence"]), ("Pull Down", 1, "anecdotal"))
        self.assertAlmostEqual(pair["observed_loss_pct"], 20.0, delta=1.0)
        self.assertEqual(pair["observations"][0]["baseline"], "two_sided")
        self.assertAlmostEqual(pair["loss_pct"], (20.0 + 3 * 20.0) / 4, delta=0.4)       # prior 20 (capped), K = 3
        self.assertEqual(pair["id"], "order:Row|after:Pull Down")

    def test_a_second_set_is_a_repeat_effect_not_an_order_effect(self):
        e = built(self.rows(), "2026-09-12")["evidence"]
        rep = next(r for r in e["repeat_effects"] if r["exercise"] == "Row")
        self.assertEqual(rep["n"], 1)
        self.assertAlmostEqual(rep["observed_loss_pct"], 20.0, delta=1.0)
        self.assertFalse([p for p in e["pair_effects"] if p["then"] == "Row" and p["before"] == "Row"])

    def test_never_fresh_lists_exercises_without_a_clean_measurement(self):
        d = lambda m: datetime(2026, 9, 1, 10, m)
        rows = [fx.make_set(1, ROW, d(0)), fx.make_set(2, PULLDOWN, d(6), start_pos=27, end_pos=12)]
        self.assertEqual(built(rows, "2026-09-02")["evidence"]["never_fresh"], ["Pull Down"])


class Familiarisation(unittest.TestCase):
    def rows(self, first=0.5):
        lv = lambda f: dict(con=(150 * f, 0.05), ecc=(240 * f, 0.05))
        d = lambda day: datetime(2026, 9, day, 10, 0)
        return [fx.make_set(1, ROW, d(1), **lv(first)), fx.make_set(2, ROW, d(3), **lv(1.0)),
                fx.make_set(3, ROW, d(6), **lv(1.02)), fx.make_set(4, ROW, d(9), **lv(1.04))]

    def test_learning_day_is_no_progress_no_record_and_no_hard_load(self):
        r = built(self.rows(0.7), "2026-09-10")
        e = r["exercises"][0]
        self.assertEqual([o["fam"] for o in e["occ"]], [True, False, False, False])
        self.assertEqual([o["comparable"] for o in e["occ"]], [False, True, True, True])
        self.assertEqual((e["days_familiarisation"], e["trend_n"]), (1, 3))
        self.assertLess(e["trend_per_session"], 5)                  # ~ +2 kg per session, not the +30 kg learning jump
        self.assertEqual((e["occ"][0]["effort"], e["occ"][0]["effort_capped"]), ("submax", "familiarisation"))

    def test_a_later_dip_is_a_regression_not_learning(self):
        rows = self.rows(1.0) + [fx.make_set(5, ROW, datetime(2026, 9, 12, 10, 0), con=(100, 0.05), ecc=(160, 0.05)),
                                 fx.make_set(6, ROW, datetime(2026, 9, 15, 10, 0)), fx.make_set(7, ROW, datetime(2026, 9, 18, 10, 0))]
        e = built(rows, "2026-09-19")["exercises"][0]
        self.assertFalse(any(o["fam"] for o in e["occ"]))

    def test_one_strong_later_day_is_not_enough_to_call_a_day_learning(self):
        lv = lambda f: dict(con=(150 * f, 0.05), ecc=(240 * f, 0.05))
        rows = [fx.make_set(1, ROW, datetime(2026, 9, 1, 10), **lv(0.8)), fx.make_set(2, ROW, datetime(2026, 9, 4, 10), **lv(1.0))]
        e = built(rows, "2026-09-05")["exercises"][0]
        self.assertFalse(any(o["fam"] for o in e["occ"]))

    def test_other_tempo_or_protocol_is_not_comparable(self):
        d = lambda day: datetime(2026, 9, day, 10, 0)
        rows = [fx.make_set(1, ROW, d(1), sec_per_dir=8.0), fx.make_set(2, ROW, d(3), protocol=1),
                fx.make_set(3, ROW, d(5)), fx.make_set(4, ROW, d(7)), fx.make_set(5, ROW, d(9))]
        e = built(rows, "2026-09-10")["exercises"][0]
        self.assertEqual([o["settings_ok"] for o in e["occ"]], [False, False, True, True, True])
        self.assertEqual(e["days_excluded_other"], 2)
        self.assertAlmostEqual(e["tempo_s_reference"], 5.0, delta=0.1)


class RestEffect(unittest.TestCase):
    """More rest = less loss is only believed when the athlete's data really shows it."""
    def test_four_mixed_observations_prove_nothing(self):
        e = ev.rest_effect_from([(1.6, 15.2, 16.0), (8.2, 1.9, 4.0), (9.3, -0.6, 16.0), (14.2, 12.7, 16.0)])
        self.assertEqual((e["status"], e["loss_share_change_per_min"], e["n"]), ("not_detectable", None, 4))

    def test_noise_is_not_an_effect(self):
        import random
        rng = random.Random(7)
        obs = [(m, 16.0 * rng.uniform(0.6, 1.4), 16.0) for m in (1, 2, 3, 5, 6, 8, 10, 12, 15)]
        self.assertEqual(ev.rest_effect_from(obs)["status"], "not_detectable")

    def test_a_consistent_relation_is_detected_relative_to_each_pairs_prior(self):
        # two different pairs (priors 16 % and 4 %): the loss falls from 1.5x to 0.6x of the prior
        obs = [(m, p * (1.5 - 0.06 * m), p) for m, p in ((1, 16.0), (2, 4.0), (4, 16.0), (6, 4.0), (8, 16.0), (10, 4.0), (13, 16.0), (15, 4.0))]
        e = ev.rest_effect_from(obs)
        self.assertEqual(e["status"], "detected")
        self.assertAlmostEqual(e["loss_share_change_per_min"], -0.06, delta=0.005)
        self.assertLessEqual(e["p_shuffle"], 0.10)
        self.assertEqual(e["reference_minutes"], 7.0)


if __name__ == "__main__":
    unittest.main()
