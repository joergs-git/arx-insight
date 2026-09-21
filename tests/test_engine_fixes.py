"""v0.3.1 engine fixes: hidden sets, ROM-gated personal bests, detraining flag, temp-copy hygiene,
typed AI errors and a payload without free text or kg leaking into an lb payload."""
import json, os, re, tempfile, time, unittest
from datetime import date, datetime, timedelta

from tests import fixtures as fx
import arx_report as core
import arx_base as base

ROW, PRESS, SQUAT = 3, 23, 19


def report(rows, today, **extra):
    return core.build_report(fx.FakeDB(rows), fx.cfg(today, **extra))


class HiddenSets(unittest.TestCase):
    def test_hidden_working_set_is_excluded_and_reported(self):
        rows = [fx.make_set(1, ROW, datetime(2026, 9, 1, 10, 0)),
                fx.make_set(2, ROW, datetime(2026, 9, 3, 10, 0), hidden=True),
                fx.make_set(3, ROW, datetime(2026, 9, 5, 10, 0))]
        r = report(rows, "2026-09-06")
        self.assertEqual(r["sets_working"], 2)
        self.assertEqual(r["sets_excluded"], {"hidden": 1})
        self.assertNotIn("2026-09-03", r["training_days"])

    def test_already_excluded_set_keeps_its_specific_status(self):
        rows = [fx.make_set(1, ROW, datetime(2026, 9, 1, 10, 0), reps=2, hidden=True),   # a test set anyway
                fx.make_set(2, ROW, datetime(2026, 9, 3, 10, 0))]
        sets = core.load_sets(fx.FakeDB(rows), 1)
        self.assertEqual([s["status"] for s in sets], ["short", "working"])

    def test_hidden_set_is_not_turned_into_a_false_start(self):
        rows = [fx.make_set(1, ROW, datetime(2026, 9, 1, 10, 0), reps=6, hidden=True),
                fx.make_set(2, ROW, datetime(2026, 9, 1, 10, 3), reps=8)]      # restarted right after it
        sets = core.load_sets(fx.FakeDB(rows), 1)
        core.flag_false_starts(sets)
        self.assertEqual(sets[0]["status"], "hidden")


class RomGatedPersonalBests(unittest.TestCase):
    def rows(self, last_end_pos):
        """Three press days; the last one is the strongest. last_end_pos sets its range of motion."""
        return [fx.make_set(1, PRESS, datetime(2026, 9, 1, 10, 0), start_pos=10, end_pos=20, ecc=(200, 0.02)),
                fx.make_set(2, PRESS, datetime(2026, 9, 5, 10, 0), start_pos=10, end_pos=20, ecc=(210, 0.02)),
                fx.make_set(3, PRESS, datetime(2026, 9, 9, 10, 0), start_pos=10, end_pos=20, ecc=(215, 0.02)),
                fx.make_set(4, PRESS, datetime(2026, 9, 12, 10, 0), start_pos=10, end_pos=last_end_pos, ecc=(240, 0.02))]

    def test_record_on_a_comparable_day_counts(self):
        r = report(self.rows(20), "2026-09-13")
        e = r["exercises"][0]
        self.assertTrue(e["last_is_pb"])
        self.assertTrue(r["last_session"]["exercises"][0]["is_pb"])
        self.assertEqual(r["coach"]["milestones"]["pbs_last_14_days"], ["Horizontal Press"])

    def test_a_range_within_ten_percent_is_the_same_set_up(self):
        """v0.9.2 (owner): 3 cm on a 30 cm press is no reason to call two days incomparable - 10 % is the line."""
        for end_pos, comparable in ((20.8, True), (19.2, True), (21.2, False), (18.7, False)):     # +8 / -8 / +12 / -13 %
            r = report(self.rows(end_pos), "2026-09-13")
            e = r["exercises"][0]
            self.assertEqual((e["occ"][-1]["rom_valid"], e["last_is_pb"]), (comparable, comparable), end_pos)

    def test_record_on_a_shorter_range_does_not_count(self):
        r = report(self.rows(16), "2026-09-13")            # 40 % shorter ROM -> not comparable
        e = r["exercises"][0]
        self.assertFalse(e["occ"][-1]["rom_valid"])
        self.assertFalse(e["last_is_pb"])
        self.assertFalse(r["last_session"]["exercises"][0]["is_pb"])
        self.assertEqual(r["coach"]["milestones"]["pbs_last_14_days"], [])
        self.assertLess(e["pb_comparable"], e["pb"])       # the all-time best stays visible, but is not "the PB"

    def test_record_older_than_the_window_is_history(self):
        r = report(self.rows(20), "2026-09-26")            # exactly 14 days after the last day
        self.assertEqual(r["coach"]["milestones"]["pbs_last_14_days"], [])


class DetrainingFlag(unittest.TestCase):
    def test_a_long_gap_in_the_past_is_history(self):
        rows = [fx.make_set(1, SQUAT, datetime(2026, 8, 1, 10, 0), start_pos=20, end_pos=14),
                fx.make_set(2, SQUAT, datetime(2026, 8, 20, 10, 0), start_pos=20, end_pos=14)]   # 19-day gap
        self.assertNotEqual(report(rows, "2026-08-22")["load"]["flag"], "detraining_risk")
        self.assertEqual(report(rows, "2026-09-05")["load"]["flag"], "detraining_risk")            # 16 days ago


class TempCopies(unittest.TestCase):
    def copies(self):
        root = tempfile.gettempdir()
        return {n for n in os.listdir(root) if n.startswith(core.TEMP_PREFIX)}

    def test_failed_open_leaves_no_copy_behind(self):
        before = self.copies()
        with self.assertRaises(Exception):
            core.open_readonly(os.path.join(tempfile.gettempdir(), "does-not-exist.fdb4"))
        self.assertEqual(self.copies(), before)

    def test_sweep_removes_only_stale_copies(self):
        old = tempfile.mkdtemp(prefix=core.TEMP_PREFIX)
        new = tempfile.mkdtemp(prefix=core.TEMP_PREFIX)
        try:
            past = time.time() - core.STALE_COPY_SECONDS - 60
            os.utime(old, (past, past))
            self.assertGreaterEqual(core.sweep_stale_copies(), 1)
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(new))
        finally:
            for d in (old, new):
                if os.path.exists(d):
                    os.rmdir(d)


class SharedSnapshot(unittest.TestCase):
    """The app's shared database copy: private data - it must go as soon as nobody needs it."""
    def _snap(self, age_s, users=0):
        d = tempfile.mkdtemp(prefix=core.TEMP_PREFIX)
        snap = {"src": "x", "sig": (1, 1), "dir": d, "path": os.path.join(d, "arx_copy.fdb"), "made": time.time() - age_s, "users": users}
        base._SNAPS.append(snap)
        return snap

    def setUp(self):
        base._SNAPS.clear()

    def tearDown(self):
        base.drop_snapshots()

    def test_only_the_fresh_newest_copy_survives_and_a_copy_in_use_is_never_removed(self):
        old_idle, old_busy, newest = self._snap(5), self._snap(5, users=1), self._snap(1)
        with base._SNAP_LOCK:
            base._purge_snapshots()
        self.assertEqual([os.path.isdir(x["dir"]) for x in (old_idle, old_busy, newest)], [False, True, True])
        newest["made"] -= base.SNAPSHOT_TTL_S + 1                # nobody uses it and its time is up
        old_busy["users"] = 0
        with base._SNAP_LOCK:
            base._purge_snapshots()
        self.assertEqual((base._SNAPS, os.path.isdir(newest["dir"]), os.path.isdir(old_busy["dir"])), ([], False, False))

    def test_shutdown_removes_everything(self):
        a, b = self._snap(1, users=1), self._snap(0)
        base.drop_snapshots()
        self.assertFalse(os.path.isdir(a["dir"]) or os.path.isdir(b["dir"]))

    def test_settings_are_written_atomically(self):
        path = os.path.join(tempfile.mkdtemp(), "goals.json")
        base.write_json_atomic(path, {"1": {"goal": {"muscle": 0.6}}})
        base.write_json_atomic(path, {"1": {"goal": {"muscle": 0.7}}})
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["1"]["goal"]["muscle"], 0.7)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["goals.json"])     # no temp file left behind


class AiErrors(unittest.TestCase):
    def test_sdk_exceptions_map_to_codes(self):
        import anthropic
        for cls_name, code in (("AuthenticationError", "invalid_key"), ("RateLimitError", "rate_limit"),
                               ("APITimeoutError", "timeout"), ("APIConnectionError", "network"),
                               ("NotFoundError", "model_unavailable"), ("BadRequestError", "bad_request")):
            cls = getattr(anthropic, cls_name)
            exc = cls.__new__(cls)                          # no request/response objects needed
            self.assertEqual(core.classify_ai_error(exc).code, code, cls_name)
        self.assertEqual(core.classify_ai_error(ValueError("x")).code, "unknown")
        self.assertEqual(core.classify_ai_error(core.AIError("refusal")).code, "refusal")

    def test_the_api_message_and_billing_code_are_used(self):
        import anthropic
        exc = anthropic.BadRequestError.__new__(anthropic.BadRequestError)
        exc.body = {"type": "error", "error": {"type": "billing_error", "message": "Your credit balance is too low."}}
        err = core.classify_ai_error(exc)
        self.assertEqual((err.code, err.message), ("no_credit", "Your credit balance is too low."))

    def test_no_key_means_no_call(self):
        import arx_ai
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("ARX_AI_FAKE", None)
        self.assertFalse(arx_ai.has_key({}))
        with self.assertRaises(core.AIError) as ctx:
            arx_ai.make_client({})
        self.assertEqual(ctx.exception.code, "no_key")


class StaticSetsAndProtocols(unittest.TestCase):
    def test_an_isometric_hold_is_listed_under_its_own_name(self):
        rows = [fx.make_set(1, ROW, datetime(2026, 9, 1, 10, 0)),
                fx.make_static_set(2, ROW, datetime(2026, 9, 1, 10, 8), seconds=60),
                fx.make_static_set(3, ROW, datetime(2026, 9, 1, 10, 12), seconds=8)]      # a short test stays a test
        sets = core.load_sets(fx.FakeDB(rows), 1)
        # v0.14.0 (contract modes-1): a hold of real length is a working set with its own effort method (time slices)
        self.assertEqual([s["status"] for s in sets], ["working", "working", "short"])
        self.assertEqual([s["protocol_label"] for s in sets], ["reps", "static", "static"])
        self.assertEqual([(s["movement"], s["ending"]) for s in sets], [("dynamic", "reps"), ("static", "time"), ("static", "time")])   # the fixture's holds are timed
        report = core.build_report(fx.FakeDB(rows), fx.cfg("2026-09-02"))
        self.assertEqual(report["sets_excluded"], {"short": 1})
        self.assertEqual(report["sets_working"], 2)
        ex = report["exercises"][0]
        self.assertEqual(sorted(ex["modes_seen"]), ["dynamic/reps"])                     # the day's best set is the dynamic one, a hold never outranks it
        self.assertEqual([s.get("movement") for s in report["last_session"]["exercises"]], ["dynamic"])   # the day's best set carries the card

    def test_countdown_and_inroad_protocols_are_named(self):
        rows = [fx.make_set(1, ROW, datetime(2026, 9, 1, 10, 0), protocol=1), fx.make_set(2, ROW, datetime(2026, 9, 3, 10, 0), protocol=0),
                fx.make_set(3, ROW, datetime(2026, 9, 5, 10, 0), protocol=7)]
        sets = core.load_sets(fx.FakeDB(rows), 1)
        self.assertEqual([s["protocol_label"] for s in sets], ["countdown", "inroad", "unknown"])
        self.assertEqual([s["ending"] for s in sets], ["time", "inroad", "unknown"])      # an unknown code is never silently "reps"


class Modes(unittest.TestCase):
    """v0.14.0 (contract modes-1): a set's mode is movement x ending (+ phase); only the same mode is compared; timed
    sets progress by Output at the same duration; the machine's own inroad scale rides along."""
    def test_timed_sets_progress_by_output_and_are_kept_apart_from_rep_sets(self):
        rows = [fx.make_set(1, ROW, datetime(2026, 9, 1, 10, 0), protocol=1, con=(150, 0.03)),
                fx.make_set(2, ROW, datetime(2026, 9, 4, 10, 0), protocol=1, con=(160, 0.03)),
                fx.make_set(3, ROW, datetime(2026, 9, 7, 10, 0), protocol=3, con=(170, 0.03))]
        r = core.build_report(fx.FakeDB(rows), fx.cfg("2026-09-08"))
        e = r["exercises"][0]
        self.assertEqual([o["ending"] for o in e["occ"]], ["time", "time", "reps"])
        self.assertEqual(len(e["output_series"]), 2)
        self.assertIsNotNone(e["output_delta_pct"]); self.assertGreater(e["output_delta_pct"], 0)     # more force in the same time
        self.assertEqual(sorted(e["modes_seen"]), ["dynamic/reps", "dynamic/time"])
        self.assertFalse(e["occ"][-1]["settings_ok"])                          # the rep set does not join the timed reference
        for o in e["occ"]:
            self.assertIsNotNone(o["inroad_machine"])                          # the machine's scale rides along
        last = r["last_session"]["exercises"][0]
        self.assertEqual((last["movement"], last["ending"], last["phase"]), ("dynamic", "reps", "both"))

    def test_a_single_phase_set_is_recognised_and_gets_no_fatigue_judgement(self):
        rows = [fx.make_set(1, ROW, datetime(2026, 9, 1, 10, 0), con=(150, 0.03), ecc=(240, 0.03)),
                fx.make_set(2, ROW, datetime(2026, 9, 4, 10, 0), con=(1.0, 0.0), ecc=(240, 0.03))]      # negative-only: no concentric work
        r = core.build_report(fx.FakeDB(rows), fx.cfg("2026-09-05"))
        by = {x["date"]: x for x in r["exercises"][0]["occ"]}
        self.assertEqual((by["2026-09-01"]["phase"], by["2026-09-04"]["phase"]), ("both", "negative"))
        self.assertIsNone(by["2026-09-04"]["inroad"])
        self.assertIn("dynamic/reps/negative", r["exercises"][0]["modes_seen"])
        last = r["last_session"]["exercises"][0]
        self.assertEqual((last["phase"], last["inroad"], last["effort"]), ("negative", None, "unknown"))

    def test_a_hold_loads_the_muscles_and_compares_only_with_holds_at_the_same_position(self):
        rows = [fx.make_static_set(1, ROW, datetime(2026, 9, 1, 10, 0), seconds=60, start_force=300, end_force=180),
                fx.make_static_set(2, ROW, datetime(2026, 9, 4, 10, 0), seconds=60, start_force=310, end_force=200)]
        r = core.build_report(fx.FakeDB(rows), fx.cfg("2026-09-05"))
        e = r["exercises"][0]
        self.assertEqual([o["movement"] for o in e["occ"]], ["static", "static"])
        self.assertTrue(all(o["rom_valid"] and o["settings_ok"] for o in e["occ"]))       # same position: comparable holds
        self.assertEqual(r["sets_working"], 2)
        self.assertIn("lats", r["load"]["recovery"]["muscles"])                            # the hold loaded its muscles
        self.assertEqual(r["load"]["recovery"]["muscles"]["lats"]["ready_on"] > "2026-09-04", True)


class Adherence(unittest.TestCase):
    def test_the_streak_counts_every_week_back_to_the_first_session(self):
        first = date(2026, 6, 1)                                         # a Monday, 12 full weeks of 2 sessions
        days = sorted((first + timedelta(weeks=w, days=d)).isoformat() for w in range(12) for d in (0, 3))
        a = core._adherence(days, 2, first + timedelta(weeks=12, days=1))
        self.assertEqual(a["streak_weeks"], 12)
        self.assertEqual(len(a["weeks"]), 4)                             # the board still lists four
        days.remove((first + timedelta(weeks=8, days=3)).isoformat())    # one week missed the target
        self.assertEqual(core._adherence(days, 2, first + timedelta(weeks=12, days=1))["streak_weeks"], 3)


if __name__ == "__main__":
    unittest.main()
