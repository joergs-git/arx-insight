"""v0.3.1 request safety of the local server: required header, Host allow-list, JSON-only POST,
check-in date validation (a future date used to wipe the history), protected shutdown, and an AI
failure that is reported instead of breaking the report. No database is needed for these routes."""
import json, threading, unittest, urllib.error, urllib.request
from datetime import timedelta

import tests                                   # noqa: F401  (points ARX_DATA_DIR at a temp folder first)
import arx_app as app
import arx_report as core

HDR = {app.TOKEN_HEADER: "local"}
JSON = {"Content-Type": "application/json"}


def call(port, path, *, method="GET", body=None, headers=None):
    """-> (status, parsed JSON or None)"""
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except ValueError:
            return e.code, None


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.STATE.update(version="0.3.1", secret="s3cret", catalog={})
        app.UPDATE_CHECKED.set()
        cls.srv = app.Server(("127.0.0.1", 0), app.Handler)
        app.STATE["server"] = cls.srv
        cls.port = cls.srv.server_address[1]
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        app.write_json(app.GOALS, {})


class RequestGuards(ServerCase):
    def test_api_needs_the_header_but_bootstrap_stays_open(self):
        self.assertEqual(call(self.port, "/api/goal?user_id=1")[0], 403)
        self.assertEqual(call(self.port, "/api/goal?user_id=1", headers=HDR)[0], 200)
        status, boot = call(self.port, "/api/bootstrap")            # older versions probe it without a header
        self.assertEqual(status, 200)
        self.assertIn("catalog", boot)
        self.assertEqual(boot["version"], "0.3.1")
        self.assertIsInstance(boot["pid"], int)

    def test_foreign_host_header_is_rejected(self):
        self.assertEqual(call(self.port, "/api/bootstrap", headers={"Host": "evil.example:8765"})[0], 421)
        self.assertEqual(call(self.port, "/api/bootstrap", headers={"Host": f"localhost:{self.port}"})[0], 200)

    def test_post_must_be_a_small_json_object(self):
        ok = {**HDR, **JSON}
        self.assertEqual(call(self.port, "/api/restrictions", method="POST", body=b"user_id=1",
                              headers={**HDR, "Content-Type": "text/plain"})[0], 415)
        self.assertEqual(call(self.port, "/api/restrictions", method="POST", body=b"{not json", headers=ok)[0], 400)
        self.assertEqual(call(self.port, "/api/restrictions", method="POST", body=b"[1,2]", headers=ok)[0], 400)
        self.assertEqual(call(self.port, "/api/restrictions", method="POST", body={"restrictions": {}}, headers=ok)[0], 400)
        self.assertEqual(call(self.port, "/api/restrictions", method="POST",
                              body={"user_id": 1, "pad": "x" * (app.MAX_BODY + 10)}, headers=ok)[0], 413)
        self.assertEqual(call(self.port, "/api/restrictions", method="POST",
                              body={"user_id": 1, "restrictions": {"knee": "careful"}}, headers=ok)[0], 200)
        self.assertEqual(app.read_json(app.GOALS, {})["1"]["restrictions"], {"knee": "careful"})

    def test_report_needs_a_numeric_user(self):
        self.assertEqual(call(self.port, "/api/report?user_id=abc", headers=HDR)[0], 400)


class CheckinDates(ServerCase):
    def post(self, body):
        return call(self.port, "/api/checkin", method="POST", body=body, headers={**HDR, **JSON})

    def test_a_future_date_cannot_wipe_the_history(self):
        today = core._today({})
        old = (today - timedelta(days=5)).isoformat()
        app.write_json(app.GOALS, {"1": {"checkins": {old: {"sleep": "good", "rhr": 55}}}})
        status, _ = self.post({"user_id": 1, "date": "2099-01-01", "sleep": "ok"})
        self.assertEqual(status, 400)
        self.assertEqual(self.post({"user_id": 1, "date": "yesterday"})[0], 400)
        self.assertEqual(list(app.read_json(app.GOALS, {})["1"]["checkins"]), [old])     # untouched

    def test_today_is_stored_and_only_really_old_entries_are_pruned(self):
        today = core._today({})
        keep = (today - timedelta(days=app.CHECKIN_KEEP_DAYS - 1)).isoformat()
        drop = (today - timedelta(days=app.CHECKIN_KEEP_DAYS + 1)).isoformat()
        app.write_json(app.GOALS, {"1": {"checkins": {keep: {"rhr": 55}, drop: {"rhr": 56}}}})
        status, res = self.post({"user_id": 1, "date": today.isoformat(), "sleep": "ok", "rhr": "58"})
        self.assertEqual((status, res["date"]), (200, today.isoformat()))
        stored = app.read_json(app.GOALS, {})["1"]["checkins"]
        self.assertEqual(sorted(stored), sorted([keep, today.isoformat()]))
        self.assertEqual(stored[today.isoformat()]["rhr"], 58)


class Shutdown(unittest.TestCase):
    def test_only_the_secret_holder_may_stop_the_app(self):
        app.STATE.update(version="0.3.1", secret="s3cret", catalog={})
        app.UPDATE_CHECKED.set()
        srv = app.Server(("127.0.0.1", 0), app.Handler)
        app.STATE["server"] = srv
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            hdr = {**HDR, **JSON}
            self.assertEqual(call(port, "/api/shutdown", method="POST", body={}, headers=hdr)[0], 403)
            self.assertEqual(call(port, "/api/shutdown", method="POST", body={"secret": "wrong"}, headers=hdr)[0], 403)
            self.assertEqual(call(port, "/api/shutdown", method="POST", body={"secret": "s3cret"}, headers=JSON)[0], 403)  # no header
            self.assertTrue(t.is_alive())
            self.assertTrue(app.request_shutdown(port, "s3cret"))
            t.join(timeout=5)
            self.assertFalse(t.is_alive())
        finally:
            srv.server_close()


class StartDecision(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(app.vparts("0.3.1"), [0, 3, 1])
        self.assertEqual(app.start_decision("0.3.1", "0.3.1"), "reuse")      # stray double-click
        self.assertEqual(app.start_decision("0.4.0", "0.3.1"), "reuse")      # old shortcut: never downgrade
        self.assertEqual(app.start_decision("0.3.0", "0.3.1"), "replace")    # the usual update
        self.assertEqual(app.start_decision("0.9.0", "0.10.0"), "replace")   # numeric, not alphabetical


class AiFailure(unittest.TestCase):
    def test_failure_is_reported_and_never_cached(self):
        report = {"today": "2026-09-18", "sets_total": 1, "sets_working": 1, "training_days": ["2026-09-18"]}
        original = core.ai_narrative
        calls = []
        try:
            def boom(r, c):
                calls.append(1)
                raise core.AIError("rate_limit", "slow down")
            core.ai_narrative = boom
            self.assertEqual(app.cached_narrative(report, {"user_id": 1}), (None, {"code": "rate_limit", "message": "slow down"}))
            core.ai_narrative = lambda r, c: (calls.append(1), "## 1. Last session")[1]
            self.assertEqual(app.cached_narrative(report, {"user_id": 1}), ("## 1. Last session", None))
            self.assertEqual(app.cached_narrative(report, {"user_id": 1}), ("## 1. Last session", None))
            self.assertEqual(len(calls), 2)                  # failure not cached, success cached
        finally:
            core.ai_narrative = original


if __name__ == "__main__":
    unittest.main()
