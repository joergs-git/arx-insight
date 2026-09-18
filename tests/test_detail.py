"""arx_detail: phase orientation, time-weighted means on an irregular grid, effort v3, the low-force
cap and the cache - on generated sets whose true values are known."""
import os, unittest
from datetime import datetime

from tests import fixtures as fx
import arx_detail as detail
import arx_report as core
from arx_base import LB_TO_KG

ROW, SQUAT = 3, 19


STEP = 1.0     # kg: the generated curves JUMP at phase borders (real force is continuous); the
               # trapezoid sees a 50 ms ramp there - far below the 40+ kg between the two phases


def detail_of(row):
    raw = detail.decode_raw(row["SERIALIZEDDETAILEDDATA"], row["EVENTSTREAMDATA"], row["REPSCHEMEDATA"], row["REPSCHEME"])
    return detail.set_detail(raw, row["INTENSITY"], row["ELAPSEDSECONDS"])


class TimeWeighting(unittest.TestCase):
    def test_mean_ignores_how_densely_a_stretch_was_sampled(self):
        # force 100 for the first 5 s (sampled 50x), force 0 for the last 5 s (sampled 3x)
        t = [i * 0.1 for i in range(50)] + [5.0, 7.5, 10.0]
        y = [100.0] * 50 + [0.0, 0.0, 0.0]
        self.assertAlmostEqual(sum(y) / len(y), 94.3, places=1)               # the naive mean is badly off
        self.assertAlmostEqual(detail.tw_mean(t, y, 0.0, 10.0), 50.0, delta=1.0)
        self.assertAlmostEqual(detail.tw_mean(t, y, 6.0, 9.0), 0.0, places=6)
        self.assertIsNone(detail.tw_mean(t, y, 4.0, 4.0))

    def test_borders_are_interpolated(self):
        t, y = [0.0, 10.0], [0.0, 100.0]                                       # a ramp, two samples only
        self.assertAlmostEqual(detail.tw_mean(t, y, 2.0, 4.0), 30.0, places=6)
        self.assertAlmostEqual(detail.tw_max(t, y, 2.0, 4.0), 40.0, places=6)


class PhaseOrientation(unittest.TestCase):
    def test_encoder_rising_means_first_half_concentric(self):
        d = detail_of(fx.make_set(1, ROW, datetime(2026, 9, 1, 10), start_pos=8.4, end_pos=20.0,
                                  con=(150, 0.0), ecc=(240, 0.0)))
        self.assertEqual((d["method"], d["first_half"], d["n_reps"]), ("phases", "con", 8))
        self.assertAlmostEqual(d["con_top3_kg"], 150 * LB_TO_KG, delta=STEP)
        self.assertAlmostEqual(d["ecc_top3_kg"], 240 * LB_TO_KG, delta=STEP)
        self.assertAlmostEqual(d["ecc_con_ratio"], 1.6, delta=0.03)

    def test_encoder_falling_means_first_half_eccentric(self):
        d = detail_of(fx.make_set(1, SQUAT, datetime(2026, 9, 1, 10), start_pos=20.0, end_pos=14.0,
                                  con=(300, 0.0), ecc=(400, 0.0)))
        self.assertEqual(d["first_half"], "ecc")
        self.assertAlmostEqual(d["con_top3_kg"], 300 * LB_TO_KG, delta=STEP)      # NOT mixed up with the eccentric
        self.assertAlmostEqual(d["ecc_top3_kg"], 400 * LB_TO_KG, delta=STEP)

    def test_holds_tempo_and_time_under_tension(self):
        d = detail_of(fx.make_set(1, ROW, datetime(2026, 9, 1, 10), reps=6, sec_per_dir=5.0, pause_end=3.0,
                                  pause_start=0.0, hold=90.0))
        self.assertAlmostEqual(d["tut"]["con_s"], 30.0, delta=0.5)
        self.assertAlmostEqual(d["tut"]["ecc_s"], 30.0, delta=0.5)
        self.assertAlmostEqual(d["tut"]["hold_s"], 18.0, delta=0.5)             # 6 end holds, no start pause
        self.assertAlmostEqual(d["tempo"]["con_s"], 5.0, delta=0.1)
        self.assertAlmostEqual(d["hold_kg"], 90 * LB_TO_KG, delta=STEP)
        self.assertFalse(d["reps"][2]["ecc_carryover"])                          # a real pause before the way back
        self.assertTrue(d["reps"][2]["con_carryover"])                           # 0 s at the start position

    def test_unfinished_last_rep_counts_for_time_only(self):
        full = detail_of(fx.make_set(1, ROW, datetime(2026, 9, 1, 10), reps=8))
        row = fx.make_set(2, ROW, datetime(2026, 9, 1, 10), reps=8, unfinished_tail=True)
        cut = detail_of(row)
        self.assertEqual(cut["n_reps"], 8)
        self.assertIsNotNone(cut["tail"])
        self.assertGreater(cut["tut"]["con_s"], full["tut"]["con_s"])
        self.assertEqual(cut["inroad_v3"], full["inroad_v3"])                    # the stub must not move the effort


class EffortV3(unittest.TestCase):
    def test_a_rising_set_reads_zero(self):
        e = detail.effort_v3([50, 55, 60, 64, 66, 70, 72, 75], [90, 95, 99, 104, 108, 110, 115, 118])
        self.assertEqual((e["inroad_v3"], e["effort"]), (0, "submax"))
        self.assertGreater(e["pacing_deficit_pct"], 15)                          # the early reps were held back

    def test_concentric_collapse_with_a_held_eccentric_still_shows(self):
        e = detail.effort_v3([100, 100, 98, 80, 65, 60, 58, 55], [160, 160, 158, 157, 156, 155, 154, 153])
        self.assertGreaterEqual(e["fatigue_con_pct"], 40)
        self.assertLess(e["fatigue_ecc_pct"], 6)
        self.assertEqual(e["effort"], "deep")

    def test_shifting_work_between_the_phases_is_not_fatigue(self):
        e = detail.effort_v3([100, 100, 100, 100, 80, 80, 80, 80], [150, 150, 150, 150, 180, 180, 180, 180])
        self.assertEqual(e["inroad_v3"], 0)

    def test_one_abandoned_last_rep_does_not_make_a_set_deep(self):
        e = detail.effort_v3([100] * 7 + [40], [160] * 7 + [70])
        self.assertLess(e["inroad_v3"], detail.INROAD_MODERATE)

    def test_thresholds_and_too_few_reps(self):
        self.assertEqual(detail.effort_v3([100, 98, 96, 94, 90, 86, 84, 82], [160] * 8)["effort"], "submax")
        self.assertEqual(detail.effort_v3([100, 90, 80], [160, 150, 140])["effort"], "unknown")

    def test_generated_set_matches_its_recipe(self):
        # concentric loses 6 % of the start value per rep, eccentric 2 %: clearly a hard set
        d = detail_of(fx.make_set(1, ROW, datetime(2026, 9, 1, 10), con=(150, 0.06), ecc=(240, 0.02)))
        self.assertAlmostEqual(d["fatigue_con_pct"], 36, delta=3)
        self.assertAlmostEqual(d["fatigue_ecc_pct"], 12, delta=2)
        self.assertEqual(d["effort"], "deep")
        self.assertEqual(d["best_rep"], 1)


class StaticSets(unittest.TestCase):
    def test_isometric_set_is_read_as_time_slices(self):
        d = detail_of(fx.make_static_set(1, SQUAT, datetime(2026, 9, 1, 10), seconds=60, start_force=300, end_force=180))
        self.assertEqual(d["method"], "static")
        self.assertEqual(len(d["static_slices_kg"]), detail.STATIC_SLICES)
        self.assertEqual(d["effort"], "deep")
        self.assertAlmostEqual(d["tut"]["hold_s"], 60.0, delta=0.2)


class ReportIntegration(unittest.TestCase):
    def test_a_first_day_far_below_later_force_is_no_hard_load(self):
        rows = [fx.make_set(1, ROW, datetime(2026, 9, 1, 10), con=(60, 0.06), ecc=(90, 0.05)),      # learning the machine
                fx.make_set(2, ROW, datetime(2026, 9, 3, 10), con=(150, 0.06), ecc=(240, 0.05)),
                fx.make_set(3, ROW, datetime(2026, 9, 6, 10), con=(155, 0.06), ecc=(245, 0.05))]
        sets = core.load_sets(fx.FakeDB(rows), 1)
        work = [s for s in sets if s["working"]]
        for s in work:
            core.attach_detail(s, detail.get_detail(fx.FakeDB(rows), s, {}))
        core.cap_low_force(work)
        self.assertEqual([s["effort"] for s in work], ["submax", "deep", "deep"])
        self.assertEqual((work[0]["effort_capped"], work[0]["effort_uncapped"]), ("low_force", "deep"))
        self.assertIsNone(work[1]["effort_capped"])

    def test_report_carries_effort_v3_and_the_legacy_figure(self):
        rows = [fx.make_set(1, ROW, datetime(2026, 9, 1, 10), con=(150, 0.06), ecc=(240, 0.01)),
                fx.make_set(2, ROW, datetime(2026, 9, 4, 10), con=(150, 0.06), ecc=(240, 0.01))]
        r = core.build_report(fx.FakeDB(rows), fx.cfg("2026-09-05"))
        x = r["last_session"]["exercises"][0]
        self.assertEqual(x["effort"], "deep")                  # concentric fatigue ~36 %
        self.assertLess(x["inroad_legacy"], 10)                # the eccentric spike barely moved: v2 saw nothing
        self.assertEqual(len(x["rep_con"]), 8)
        self.assertEqual(x["inroad_pct"], x["inroad"])         # card and recovery use the same figure
        self.assertEqual(r["load"]["recovery"]["muscles"]["upper_back"]["last_effort"], "deep")


class Cache(unittest.TestCase):
    def test_key_holds_the_date_and_the_cache_survives_a_round_trip(self):
        self.assertEqual(detail.cache_key(7, "2026-09-01 10:00:00"), "7@2026-09-01 10:00:00")
        self.assertNotEqual(detail.cache_key(7, "2026-09-01 10:00:00"), detail.cache_key(7, "2026-09-02 10:00:00"))
        row = fx.make_set(7, ROW, datetime(2026, 9, 1, 10))
        s = {"id": 7, "date": "2026-09-01 10:00:00", "seconds": row["ELAPSEDSECONDS"]}
        cache = {}
        d = detail.get_detail(fx.FakeDB([row]), s, cache)
        detail.save_detail_cache(cache)
        self.assertEqual(detail.load_detail_cache()[detail.cache_key(7, s["date"])]["inroad_v3"], d["inroad_v3"])
        self.assertIs(detail.get_detail(None, s, cache), d)    # a hit needs no database at all
        os.remove(detail.DETAIL_CACHE)


if __name__ == "__main__":
    unittest.main()
