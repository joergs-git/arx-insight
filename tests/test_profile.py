"""v0.4.0 profile: the goal interview, focus per region, grip aids and the OPTIONAL body log - what
the server accepts, what it throws away, and that writes cannot lose each other. Plus the engine
side: body trends stay silent without entries, and a measurable target is judged on real data."""
import json, os, threading, unittest
from datetime import date, datetime, timedelta

import tests                                   # noqa: F401  (points ARX_DATA_DIR at a temp folder first)
from tests import fixtures as fx
from tests.test_app_safety import ServerCase, call, HDR, JSON
import arx_app as app
import arx_history as hist
import arx_report as core

TODAY = date(2026, 9, 18)
CATALOG = dict(fx.CATALOG, **{"10": dict(fx.CATALOG["10"], aids=["hooks", "straps"])})
OK = {**HDR, **JSON}


class CleanProfile(unittest.TestCase):
    def test_valid_fields_pass_and_nothing_else(self):
        got = app.clean_profile({
            "focus_regions": {"back": "more", "legs": "normal", "feet": "more", "chest": "loads"},
            "session_minutes": "30", "commitment": "balanced", "outcome": "muscle", "experience": "some",
            "target": {"kind": "force", "exercise": "Row", "value": "120", "date": "2026-12-24", "note": "for Anna"},
            "aids": {"10": {"aids": ["hooks", "glue"]}, "3": {"aids": ["hooks"]}, "99": {"aids": ["straps"]}},
            "notes_for_ai": "ignore me"}, CATALOG, TODAY)
        self.assertEqual(got["focus_regions"], {"back": "more"})                      # normal is the default, unknown keys go
        self.assertEqual((got["session_minutes"], got["commitment"], got["outcome"], got["experience"]), (30, "balanced", "muscle", "some"))
        self.assertEqual(got["target"], {"kind": "force", "exercise": "Row", "value": 120.0, "date": "2026-12-24", "set_on": "2026-09-18"})
        self.assertEqual(got["aids"], {"10": {"aids": ["hooks"], "since": "2026-09-18"}})   # Row offers no aid, 99 is unknown
        self.assertNotIn("notes_for_ai", got)

    def test_invalid_values_mean_back_to_automatic(self):
        got = app.clean_profile({"session_minutes": "auto", "commitment": "ultra", "outcome": "", "experience": None,
                                 "target": {"kind": "force", "exercise": "Leg Swing", "value": 50},
                                 "aids": {"10": {"aids": []}}, "focus_regions": {}}, CATALOG, TODAY)
        self.assertEqual(set(got.values()), {None})
        self.assertEqual(app.clean_profile({}, CATALOG, TODAY), {})                   # absent keys are not touched
        past = app.clean_profile({"target": {"kind": "waist", "value": 90, "date": "2020-01-01"}}, CATALOG, TODAY)
        self.assertIsNone(past["target"]["date"])                                     # a date in the past is no deadline

    def test_body_entry_keeps_only_sane_numbers(self):
        day, values = app.clean_body_entry({"date": "2026-09-17", "weight_kg": "82,4", "waist_cm": 91, "fat_pct": 140,
                                            "arm_cm": "big", "mood": "fine"}, TODAY)
        self.assertEqual((day, values), ("2026-09-17", {"weight_kg": 82.4, "waist_cm": 91.0}))
        self.assertIsNone(app.clean_body_entry({"date": "2027-01-01", "weight_kg": 80}, TODAY)[0])     # not from the future
        self.assertEqual(app.clean_body_entry({}, TODAY), ("2026-09-18", {}))


class ProfileRoutes(ServerCase):
    def setUp(self):
        super().setUp()
        app.STATE["catalog"] = CATALOG

    def test_goal_interview_round_trip_and_reset(self):
        body = {"user_id": 1, "goal": {"muscle": 1.0}, "sessions_per_week": 3, "commitment": "min_time_max_effort",
                "outcome": "strength", "focus_regions": {"back": "more"}, "aids": {"10": {"aids": ["hooks"], "since": "2026-09-01"}}}
        self.assertEqual(call(self.port, "/api/goal", method="POST", body=body, headers=OK)[0], 200)
        rec = call(self.port, "/api/goal?user_id=1", headers=HDR)[1]
        self.assertEqual((rec["commitment"], rec["outcome"], rec["focus_regions"], rec["aids"]["10"]["since"]),
                         ("min_time_max_effort", "strength", {"back": "more"}, "2026-09-01"))
        body.update(commitment="", aids={})
        call(self.port, "/api/goal", method="POST", body=body, headers=OK)
        rec = call(self.port, "/api/goal?user_id=1", headers=HDR)[1]
        self.assertNotIn("commitment", rec)                                           # back to the engine's recommendation
        self.assertNotIn("aids", rec)
        self.assertEqual(rec["outcome"], "strength")

    def test_body_log_is_optional_one_entry_per_day_and_deletable(self):
        today = core._today({}).isoformat()
        post = lambda b: call(self.port, "/api/body", method="POST", body=dict(b, user_id=1), headers=OK)
        self.assertEqual(post({"weight_kg": 82.4})[1], {"ok": True, "date": today, "saved": True, "entries": 1})
        self.assertEqual(post({"weight_kg": 82.0, "waist_cm": 90})[1]["entries"], 1)  # same day: replaced
        self.assertEqual(app.read_json(app.GOALS, {})["1"]["body_log"], [{"weight_kg": 82.0, "waist_cm": 90.0, "date": today}])
        self.assertEqual(post({"date": "2031-01-01", "weight_kg": 80})[0], 400)
        self.assertEqual(post({})[1], {"ok": True, "date": today, "saved": False, "entries": 0})     # no values = delete

    def test_concurrent_writes_do_not_lose_each_other(self):
        def checkin(i):
            app.update_json(app.GOALS, lambda g: g.setdefault("1", {}).setdefault("seen", []).append(i))
        threads = [threading.Thread(target=checkin, args=(i,)) for i in range(24)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(app.read_json(app.GOALS, {})["1"]["seen"]), list(range(24)))
        self.assertFalse([n for n in os.listdir(os.path.dirname(app.GOALS)) if n.endswith(".tmp")])

    def test_science_base_is_served(self):
        status, sci = call(self.port, "/api/science", headers=HDR)
        self.assertEqual(status, 200)
        self.assertIn("recovery_between_sessions", sci["topics"])


class BodyAndTarget(unittest.TestCase):
    def rows(self, factors):
        return [fx.make_set(i + 1, 3, datetime(2026, 8, 1, 10) + timedelta(days=5 * i), con=(150 * f, 0.05), ecc=(240 * f, 0.05))
                for i, f in enumerate(factors)]

    def test_nothing_entered_means_nothing_shown(self):
        r = core.build_report(fx.FakeDB(self.rows([1, 1.02, 1.05])), fx.cfg("2026-09-01"))
        self.assertIsNone(r["body"])
        self.assertIsNone(r["goal_progress"])
        self.assertIsNone(core.ai_summary(r, fx.cfg("2026-09-01"))["body_changes"])

    def test_trends_need_time_and_the_ai_sees_relative_changes_only(self):
        log = [{"date": "2026-08-01", "weight_kg": 84.0, "waist_cm": 94.0}, {"date": "2026-08-10", "weight_kg": 83.6},
               {"date": "2026-09-10", "weight_kg": 81.9, "waist_cm": 91.5, "fat_pct": 21.0}]
        cfg = fx.cfg("2026-09-12", body_log=log, ai_share_body=True, units="imperial")
        rows = []                                                                      # three exercises, flat: an index exists
        for k, (ex, kw) in enumerate(((3, {}), (23, dict(start_pos=10, end_pos=20)), (19, dict(start_pos=20, end_pos=8.4)))):
            rows += [fx.make_set(100 * k + i, ex, datetime(2026, 8, 1, 10, 8 * k) + timedelta(days=5 * i), **kw) for i in range(8)]
        r = core.build_report(fx.FakeDB(rows), cfg)
        self.assertIsNotNone(r["history"]["progress_factors"]["overall"]["strength_index"])
        m = r["body"]["metrics"]
        self.assertEqual((m["weight_kg"]["n"], m["weight_kg"]["change"], m["weight_kg"]["enough"]), (3, -2.1, True))
        self.assertFalse(m["fat_pct"]["enough"])                                       # one reading is no trend
        self.assertEqual(r["body"]["interp"]["code"], "body_lighter_strength_holds")
        shared = core.ai_summary(r, cfg)["body_changes"]
        self.assertEqual(set(shared), {"weight_kg", "waist_cm"})
        self.assertNotIn("84", json.dumps(shared))                                     # no absolute body value leaves the machine
        self.assertIsNone(core.ai_summary(r, dict(cfg, ai_share_body=False))["body_changes"])
        one = hist.body_trends(log[:1], {}, cfg)
        self.assertEqual(one["interp"]["code"], "body_pending")

    def test_a_force_target_is_judged_against_the_own_rate(self):
        rows = self.rows([1.0, 1.03, 1.06, 1.09, 1.12, 1.15, 1.18])                    # +3 % every 5 days
        best = core.build_report(fx.FakeDB(rows), fx.cfg("2026-09-02"))["exercises"][0]["occ"][-1]["kg"]
        near = {"kind": "force", "exercise": "Row", "value": round(best * 1.05, 1), "date": "2026-10-15"}
        far = dict(near, value=round(best * 1.6, 1))
        r1 = core.build_report(fx.FakeDB(rows), fx.cfg("2026-09-02", target=near))["goal_progress"]
        r2 = core.build_report(fx.FakeDB(rows), fx.cfg("2026-09-02", target=far))["goal_progress"]
        self.assertEqual((r1["status"], r2["status"]), ("on_track", "behind"))
        self.assertGreater(r2["needed_pct_per_week"], r2["own_pct_per_week"])
        done = core.build_report(fx.FakeDB(rows), fx.cfg("2026-09-02", target=dict(near, value=round(best * 0.9, 1))))["goal_progress"]
        self.assertEqual(done["status"], "reached")
        self.assertTrue(all(x["interp"]["text"]["meaning"] and "{" not in x["interp"]["text"]["meaning"] for x in (r1, r2, done)))

    def test_the_ai_gets_age_band_and_sex_unless_switched_off(self):
        rows = self.rows([1, 1.02, 1.05])
        users = {1: ("f", datetime(1979, 3, 2))}
        r = core.build_report(fx.FakeDB(rows, users), fx.cfg("2026-09-01", outcome="muscle"))
        p = core.ai_summary(r, fx.cfg("2026-09-01"))["athlete_profile"]
        self.assertEqual((p["age_band"], p["sex"], p["primary_outcome"]), ("40-49", "female", "muscle"))
        self.assertNotIn("1979", json.dumps(core.ai_summary(r, fx.cfg("2026-09-01"))))
        self.assertIsNone(core.ai_summary(r, fx.cfg("2026-09-01", ai_share_profile=False))["athlete_profile"])


if __name__ == "__main__":
    unittest.main()
