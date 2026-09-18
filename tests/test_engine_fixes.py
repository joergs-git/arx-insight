"""v0.3.1 engine fixes: hidden sets, ROM-gated personal bests, detraining flag, temp-copy hygiene,
typed AI errors and a payload without free text or kg leaking into an lb payload."""
import os, re, tempfile, time, unittest
from datetime import datetime

from tests import fixtures as fx
import arx_report as core

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

    def test_no_key_means_no_call_and_no_error(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.assertIsNone(core.ai_narrative({}, {}))


class Payload(unittest.TestCase):
    def setUp(self):
        rows = [fx.make_set(1, ROW, datetime(2026, 9, 1, 10, 0)),
                fx.make_set(2, PRESS, datetime(2026, 9, 1, 10, 6), start_pos=10, end_pos=20),
                fx.make_set(3, ROW, datetime(2026, 9, 4, 10, 0)),
                fx.make_set(4, PRESS, datetime(2026, 9, 4, 10, 6), start_pos=10, end_pos=20)]
        checkin = {"date": "2026-09-08", "sleep": "ok", "energy": "high", "soreness": {}, "note": "ask Anna about it"}
        self.cfg = fx.cfg("2026-09-08", units="imperial", checkin=checkin, restrictions={"shoulder": "careful"})
        self.report = core.build_report(fx.FakeDB(rows), self.cfg)
        self.payload = core.ai_summary(self.report, self.cfg)

    def test_free_text_note_never_leaves_the_machine(self):
        self.assertEqual(self.report["readiness"]["note"], "ask Anna about it")   # kept locally
        self.assertNotIn("note", self.payload["checkin_today"])
        self.assertNotIn("Anna", str(self.payload))

    def test_no_kg_values_in_an_lb_payload(self):
        self.assertTrue(self.payload["session_plan"])
        for item in self.payload["session_plan"]:
            self.assertNotIn("last", item)
            self.assertIn("last_day_best", item)
        kg_best = {i["name"]: i["last"] for i in self.report["session_plan"]}
        for item in self.payload["session_plan"]:
            if kg_best[item["name"]] is None:            # an exercise suggested as new has no best yet
                self.assertIsNone(item["last_day_best"])
                continue
            self.assertAlmostEqual(item["last_day_best"], kg_best[item["name"]] * 2.20462, delta=0.06)
        leaks = re.findall(r"'(\w+_(?:kg|cm))'", str(self.payload))
        self.assertFalse(leaks, leaks)                   # no metric key anywhere in an imperial payload
        self.assertEqual(self.payload["totals"]["work_unit"], "lb*s")
        self.assertAlmostEqual(self.payload["totals"]["total_work_impulse"],
                               self.report["totals"]["total_work_impulse"] * 2.20462, delta=0.06)
        for check in self.payload["restriction_checks"]:
            self.assertFalse([k for k in check if k.endswith("_kg")], check)


if __name__ == "__main__":
    unittest.main()
