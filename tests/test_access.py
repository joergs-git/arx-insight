"""v0.6.0 phone access: the route table is the security model - so it is tested as a table. Two
listeners run side by side (loopback = this machine, "lan" = a token is required); the matrix walks
every route as nobody / athlete / trainer / this machine. Then: an athlete link reaches only its own
person, PC-only stays PC-only whatever token a phone shows, links expire / are revoked / replaced,
the daily AI allowance and the minors' chat switch hold, the report download cannot run a script,
and only private network addresses ever get a listener. No database needed (report and coach faked)."""
import json, os, tempfile, threading, time, types, unittest, unittest.mock, urllib.request
from datetime import datetime, timedelta

import tests                                   # noqa: F401  (points ARX_DATA_DIR at a temp folder first)
from tests.test_app_safety import call, JSON
from tests.test_coach import make
import arx_access as access
import arx_ai as ai
import arx_app as app
import arx_lan as lan
import arx_report as core

LOCAL = {app.TOKEN_HEADER: "local"}
REPORT = {"athlete_alias": "Anna Example", "user": {"id": 1, "name": "Anna Example", "gender": "f", "birthdate": "1990-02-03", "created": "2025-01-01"},
          "today": "2026-09-18", "plan": None}


class TwoListeners(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.STATE.update(version="0.6.0", secret="s3cret", catalog={})
        app.UPDATE_CHECKED.set()
        cls.servers = []
        for kind in ("local", "lan"):
            srv = app.Server(("127.0.0.1", 0), app.Handler)
            if kind == "lan":                                       # what make_lan_server does, on the loopback address for the test
                srv.kind, srv.allowed_hosts = "lan", ("127.0.0.1",)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            cls.servers.append(srv)
        cls.port, cls.lport = (s.server_address[1] for s in cls.servers)

    @classmethod
    def tearDownClass(cls):
        for srv in cls.servers:
            srv.shutdown()
            srv.server_close()

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self._orig = (app.ACCESS, app.BAD_TOKENS, app.make_report, app.search_users, app.user_info, app.LAN)
        app.ACCESS, app.BAD_TOKENS = access.AccessStore(os.path.join(tmp, "access.json")), access.BadTokens()
        app.make_report = lambda uid, cfg_info=None: dict(REPORT)
        app.search_users = lambda q: [{"id": 1, "name": "Anna Example", "gender": "f", "birthdate": "1990-02-03", "created": "2025-01-01"}]
        app.user_info = lambda uid: {"id": uid, "name": "Anna Example", "created": None}
        app.write_json(app.GOALS, {})
        self.trainer = {app.TOKEN_HEADER: app.ACCESS.trainer_token()}
        self.athlete = {app.TOKEN_HEADER: app.ACCESS.athlete(1, create=True)["token"]}
        self.addCleanup(self._restore)

    def _restore(self):
        app.ACCESS, app.BAD_TOKENS, app.make_report, app.search_users, app.user_info, app.LAN = self._orig

    def lan_call(self, path, who=None, **kw):
        return call(self.lport, path, headers={**(who or {}), **(JSON if kw.get("method") == "POST" else {})}, **kw)


class RouteTable(TwoListeners):
    PC_ONLY = {("POST", "/api/update"), ("GET", "/api/update/status"), ("POST", "/api/update/check"), ("POST", "/api/shutdown"), ("POST", "/api/lan"),
               ("GET", "/api/lan/status"), ("POST", "/api/access/link"), ("POST", "/api/firewall"), ("GET", "/api/export")}

    def test_the_table_is_complete_and_conservative(self):
        for key, r in app.ROUTES.items():
            self.assertIn(r.perm, access.RANK, key)
        self.assertEqual({k for k, r in app.ROUTES.items() if r.perm == "local"}, self.PC_ONLY)
        self.assertEqual([k for k, r in app.ROUTES.items() if r.perm == "open"], [("GET", "/api/bootstrap")])
        # whatever an athlete link may call is about ONE person - except the public science base
        loose = {k for k, r in app.ROUTES.items() if r.perm == "athlete" and not r.own_user}
        self.assertEqual(loose, {("GET", "/api/science")})

    def test_every_route_as_nobody_athlete_trainer(self):
        for (method, path), r in sorted(app.ROUTES.items()):
            body = {"user_id": 2} if method == "POST" else None
            url = path + ("?user_id=2" if method == "GET" else "")
            kw = {"method": method, "body": body}
            self.assertEqual(self.lan_call(url, None, **kw)[0], 401, (method, path))                 # no token: nothing, not even bootstrap
            status = self.lan_call(url, self.athlete, **kw)[0]                                      # athlete of person 1 asks about person 2
            if r.perm in ("trainer", "local") or r.own_user:
                self.assertEqual(status, 403, (method, path))
            status, out = self.lan_call(url, self.trainer, **kw)
            if r.perm == "local":
                self.assertEqual((status, out["detail"]), (403, "loopback_only"), (method, path))
            else:
                self.assertNotIn(status, (401, 403), (method, path))

    def test_an_athlete_link_reaches_only_its_own_person(self):
        status, rep = self.lan_call("/api/report?user_id=1", self.athlete)
        self.assertEqual(status, 200)
        self.assertEqual((rep["athlete_alias"], rep["user"]), ("Anna", {"id": 1, "name": "Anna", "created": "2025-01-01"}))   # no surname, no birth date
        self.assertEqual(self.lan_call("/api/report?user_id=2", self.athlete)[0], 403)
        self.assertEqual(self.lan_call("/api/report?user_id=999999", self.athlete)[0], 403)         # the same answer for an id that does not exist
        self.assertEqual(self.lan_call("/api/report", self.athlete)[0], 400)
        self.assertEqual(self.lan_call("/api/checkin", self.athlete, method="POST",
                                       body={"user_id": 1, "date": core._today({}).isoformat(), "sleep": "ok"})[0], 200)
        self.assertEqual(self.lan_call("/api/checkin", self.athlete, method="POST", body={"user_id": 2, "sleep": "ok"})[0], 403)
        self.assertEqual(list(app.read_json(app.GOALS, {})), ["1"])
        status, boot = self.lan_call("/api/bootstrap", self.athlete)
        self.assertEqual((status, boot["access"]["role"], boot["access"]["user_id"], boot["access"]["name"]), (200, "athlete", 1, "Anna"))
        for key in ("pid", "self_update", "phone", "taskbar_pinned"):
            self.assertNotIn(key, boot)
        self.assertEqual(boot["update"], {})

    def test_the_trainer_link_sees_people_but_never_the_pc_only_parts(self):
        status, rows = self.lan_call("/api/users?q=", self.trainer)
        self.assertEqual((status, rows[0]["birthdate"]), (200, "1990"))                             # the year is enough on the Wi-Fi
        self.assertEqual(call(self.port, "/api/users?q=", headers=LOCAL)[1][0]["birthdate"], "1990-02-03")
        self.assertEqual(self.lan_call("/api/report?user_id=1", self.trainer)[1]["athlete_alias"], "Anna Example")
        status, out = self.lan_call("/api/config", self.trainer, method="POST", body={"anthropic_api_key": "sk-ant-JOHNDOE"})
        self.assertEqual((status, out["detail"]), (403, "loopback_only"))
        self.assertNotIn("anthropic_api_key", app.read_json(app.CONFIG, {}))
        self.assertEqual(self.lan_call("/api/config", self.trainer, method="POST", body={"language": "de"})[0], 200)
        self.assertEqual(app.read_json(app.CONFIG, {}).get("language"), "de")
        self.assertEqual(self.lan_call("/api/shutdown", self.trainer, method="POST", body={"secret": "s3cret"})[0], 403)   # even with the secret
        self.assertEqual(self.lan_call("/api/bootstrap", self.trainer)[1]["access"], {"role": "trainer"})

    def test_this_machine_keeps_full_rights_and_the_open_probe(self):
        self.assertEqual(call(self.port, "/api/bootstrap")[1]["access"], {"role": "local"})
        self.assertIn("phone", call(self.port, "/api/bootstrap")[1])
        self.assertEqual(call(self.port, "/api/lan/status", headers=LOCAL)[0], 200)
        self.assertEqual(call(self.port, "/api/report?user_id=2", headers=self.athlete)[0], 200)    # on loopback any header value means "local"


class Links(TwoListeners):
    def test_expired_revoked_and_replaced_links_stop_working(self):
        self.assertEqual(self.lan_call("/api/goal?user_id=1", self.athlete)[0], 200)
        data = app.read_json(app.ACCESS.path, {})
        data["athletes"]["1"]["expires"] = (core._today({}) - timedelta(days=1)).isoformat()
        app.write_json(app.ACCESS.path, data)
        self.assertEqual(self.lan_call("/api/goal?user_id=1", self.athlete)[0], 401)
        self.assertTrue(app.ACCESS.athlete(1)["expired"])
        app.ACCESS.renew_athlete(1)                                                                 # the same code works again
        self.assertEqual(self.lan_call("/api/goal?user_id=1", self.athlete)[0], 200)
        app.ACCESS.revoke_athlete(1)
        self.assertEqual(self.lan_call("/api/goal?user_id=1", self.athlete)[0], 401)
        app.ACCESS.trainer_token(rotate=True)
        self.assertEqual(self.lan_call("/api/users?q=", self.trainer)[0], 401)

    def test_wrong_tokens_are_slowed_down(self):
        for _ in range(access.BAD_TOKEN_MAX):
            self.assertEqual(self.lan_call("/api/bootstrap", {app.TOKEN_HEADER: "A-guess"})[0], 401)
        self.assertEqual(self.lan_call("/api/bootstrap", {app.TOKEN_HEADER: "A-guess"})[0], 429)
        self.assertEqual(self.lan_call("/api/bootstrap", self.trainer)[0], 429)                    # the address is blocked, whatever it shows now
        self.assertEqual(call(self.port, "/api/bootstrap")[0], 200)                                 # this machine is never locked out

    def test_links_are_made_at_the_pc_only(self):
        body = {"kind": "trainer", "action": "show"}
        self.assertEqual(self.lan_call("/api/access/link", self.trainer, method="POST", body=body)[0], 403)
        hdr = {**LOCAL, **JSON}
        out = call(self.port, "/api/access/link", method="POST", body=body, headers=hdr)[1]
        self.assertEqual((out["url"], out["running"]), (None, False))                               # phone access is off: no address to point at
        app.LAN = types.SimpleNamespace(status=lambda: {"url": "http://192.168.1.20:8765/", "running": True, "port": 8765})
        out = call(self.port, "/api/access/link", method="POST", body=body, headers=hdr)[1]
        self.assertEqual(out["url"], "http://192.168.1.20:8765/#t=" + self.trainer[app.TOKEN_HEADER])   # fragment: never sent to a server
        orig = (core.shared_connection, core.user_profile)
        self.addCleanup(lambda: (setattr(core, "shared_connection", orig[0]), setattr(core, "user_profile", orig[1])))
        import contextlib
        core.shared_connection = lambda path: contextlib.nullcontext(None)
        core.user_profile = lambda con, uid, today=None: {"age": 15}
        kid = call(self.port, "/api/access/link", method="POST", body={"kind": "athlete", "user_id": 5}, headers=hdr)[1]
        self.assertEqual((kid["minor"], kid["chat"], kid["expired"]), (True, False, False))         # a minor's link starts without the chat
        self.assertTrue(kid["url"].startswith("http://192.168.1.20:8765/#t=A-"))
        on = call(self.port, "/api/access/link", method="POST", body={"kind": "athlete", "user_id": 5, "action": "chat", "chat": True}, headers=hdr)[1]
        self.assertTrue(on["chat"])
        new = call(self.port, "/api/access/link", method="POST", body={"kind": "athlete", "user_id": 5, "action": "rotate"}, headers=hdr)[1]
        self.assertNotEqual(new["url"], kid["url"])
        self.assertTrue(new["chat"])                                                                # the switch survives a replaced code
        gone = call(self.port, "/api/access/link", method="POST", body={"kind": "athlete", "user_id": 5, "action": "revoke"}, headers=hdr)[1]
        self.assertTrue(gone["revoked"])
        self.assertIsNone(app.ACCESS.athlete(5))


class Hardening(TwoListeners):
    def test_host_allow_list_per_listener(self):
        self.assertEqual(call(self.lport, "/", headers={"Host": "evil.example"})[0], 421)
        self.assertEqual(call(self.lport, "/api/bootstrap", headers={**self.trainer, "Host": f"localhost:{self.lport}"})[0], 421)   # only ITS address
        self.assertEqual(call(self.lport, "/api/bootstrap", headers={**self.trainer, "Host": f"127.0.0.1:{self.lport}"})[0], 200)

    def test_security_headers(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.lport}/", timeout=5) as r:      # a phone: never inside a foreign site
            hd = r.headers
            self.assertIn("frame-ancestors 'none'", hd["Content-Security-Policy"])
            self.assertIn("connect-src 'self'", hd["Content-Security-Policy"])
            self.assertEqual((hd["X-Content-Type-Options"], hd["Referrer-Policy"], hd["X-Frame-Options"], hd["Cache-Control"]),
                             ("nosniff", "no-referrer", "DENY", "no-store"))
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=5) as r:       # this machine: arx-free may frame the page (v0.21.0)
            hd = r.headers
            self.assertIn("frame-ancestors 'self' http://127.0.0.1:* http://localhost:*", hd["Content-Security-Policy"])
            self.assertNotIn("'none'", hd["Content-Security-Policy"].split("frame-ancestors")[1])
            self.assertIn("connect-src 'self'", hd["Content-Security-Policy"])
            self.assertIsNone(hd["X-Frame-Options"])                                            # would only mislead next to the allowing CSP
            self.assertEqual((hd["X-Content-Type-Options"], hd["Referrer-Policy"], hd["Cache-Control"]), ("nosniff", "no-referrer", "no-store"))

    def test_the_history_for_arx_free_is_answered_on_this_machine_only(self):
        # v0.22.0, contract arx-export-2: the same gzip'd NDJSON the export tool writes, only the sets after `since`
        import contextlib, gzip
        from tests.fixtures import make_set
        from tests.test_export_for_arx_free import FakeConnection
        first = make_set(10, 10, datetime(2026, 9, 13, 18, 0), reps=1)
        second = make_set(11, 10, datetime(2026, 9, 20, 18, 0), reps=1)
        for row in (first, second):
            row.update(PROTOCOLPARAMETER=1, NOTES=None, RESTTIMER=None, RESTTIMERUSED=None, COMPARISONSET_ID=None)
        users = [(1, "Anna", "Example", "f", None, None), (2, "Ben", "Muster", None, None, None)]
        orig = core.shared_connection
        core.shared_connection = lambda path: contextlib.nullcontext(FakeConnection(users, [second, first]))
        self.addCleanup(setattr, core, "shared_connection", orig)

        def fetch(query, headers=LOCAL, port=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port or self.port}/api/export{query}", headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.headers, [json.loads(line) for line in gzip.decompress(r.read()).decode("utf-8").splitlines()]
        hd, lines = fetch("?since=2026-09-13T18:00:00")
        self.assertEqual((hd["Content-Type"], hd["X-ARX-Export"]), ("application/gzip", "athletes=2; sets=1; skipped=0"))
        self.assertRegex(hd["Content-Disposition"], r'attachment; filename="arx-export-\d{8}-\d{6}\.ndjson\.gz"')
        self.assertEqual([line["kind"] for line in lines], ["header", "athlete", "athlete", "set", "footer"])
        self.assertEqual((lines[0]["format"], lines[0]["since"]), ("arx-export-1", "2026-09-13T18:00:00"))     # the line format is v1's
        self.assertEqual((lines[3]["source_set_id"], lines[-1]), ("11", {"kind": "footer", "athletes": 2, "sets": 1}))
        hd, lines = fetch("")                                                                                # without since: everything
        self.assertEqual(([line["source_set_id"] for line in lines if line["kind"] == "set"], lines[0]["since"]), (["10", "11"], None))
        self.assertEqual(call(self.port, "/api/export?since=yesterday", headers=LOCAL), (400, {"error": "bad_since", "detail": "since = a started_at of the export, like 2026-09-13T18:04:11"}))
        self.assertEqual(self.lan_call("/api/export", self.trainer)[1]["detail"], "loopback_only")            # never through the Wi-Fi
        self.assertEqual([n for n in os.listdir(core.data_dir()) if n.startswith(app.EXPORT_PREFIX)], [])    # nothing with names stays behind

    def test_whether_a_start_opens_the_browser_is_this_pcs_setting(self):
        # v0.21.0: on the kiosk arx-free fronts, both tools start at logon and only ONE may open the kiosk browser
        self.addCleanup(app.update_json, app.CONFIG, lambda cfg: cfg.pop("open_browser", None))
        self.assertTrue(app.browser_wanted())
        self.assertFalse(app.browser_wanted(no_browser_flag=True))
        with unittest.mock.patch.dict(os.environ, {"ARX_NO_BROWSER": "1"}):                     # the one-click update reloads the open page
            self.assertFalse(app.browser_wanted())
        self.assertEqual(self.lan_call("/api/config", self.trainer, method="POST", body={"open_browser": False})[0], 200)
        self.assertTrue(app.browser_wanted())                                                     # a phone cannot switch it
        self.assertEqual(call(self.port, "/api/config", method="POST", body={"open_browser": False}, headers={**LOCAL, **JSON})[0], 200)
        self.assertFalse(app.browser_wanted())
        self.assertFalse(call(self.port, "/api/bootstrap", headers=LOCAL)[1]["open_browser"])
        self.assertEqual(call(self.port, "/api/config", method="POST", body={"open_browser": True}, headers={**LOCAL, **JSON})[0], 200)
        self.assertTrue(app.browser_wanted())
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/vendor/qrcode.js", timeout=5) as r:
            self.assertIn("javascript", r.headers["Content-Type"])
            self.assertIn(b"Kazuhiko Arase", r.read(400))                                           # the MIT notice stays with the file
        self.assertEqual(call(self.port, "/vendor/../arx_app.py")[0], 404)                         # a fixed whitelist, no path games
        self.assertEqual(call(self.port, "/config.json")[0], 404)

    def test_an_error_tells_a_phone_nothing_about_this_machine(self):
        def boom(uid, cfg_info=None):
            raise RuntimeError("cannot open C:\\Users\\someone\\DB.FDB4")
        app.make_report = boom
        status, out = self.lan_call("/api/report?user_id=1", self.athlete)
        self.assertEqual((status, out), (500, {"error": "RuntimeError", "detail": ""}))
        self.assertIn("DB.FDB4", call(self.port, "/api/report?user_id=1", headers=LOCAL)[1]["detail"])

    def test_the_report_download_cannot_run_anything(self):
        markup = '<section><h2>Report</h2><script>alert(1)</script><p onclick="steal()">ok</p><iframe src="http://x"></iframe></section>'
        status, out = self.lan_call("/api/export/snapshot", self.athlete, method="POST",
                                    body={"user_id": 1, "html": markup, "title": "Anna <b>x</b> 2026-09-18", "language": "de"})
        self.assertEqual(status, 200)
        req = urllib.request.Request(f"http://127.0.0.1:{self.lport}{out['url']}")                  # the ticket needs no token - and works once
        with urllib.request.urlopen(req, timeout=5) as r:
            doc, hd = r.read().decode("utf-8"), r.headers
        self.assertIn("attachment", hd["Content-Disposition"])
        self.assertRegex(hd["Content-Disposition"], r'filename="arx-report-\d{4}-\d\d-\d\d\.html"')  # a date, never a name
        self.assertIn("sandbox", hd["Content-Security-Policy"])
        self.assertIn("default-src 'none'", doc.split("</head>")[0])                                # the file forbids scripts by itself
        self.assertNotIn("<script", doc.lower())
        self.assertNotIn("<iframe", doc.lower())
        self.assertIn("Anna &lt;b&gt;x&lt;/b&gt;", doc)
        self.assertIn("<h2>Report</h2>", doc)
        self.assertEqual(call(self.lport, out["url"])[0], 404)                                      # used up
        self.assertEqual(self.lan_call("/api/export/snapshot", self.athlete, method="POST", body={"user_id": 2, "html": markup})[0], 403)
        big = {"user_id": 1, "html": "x" * (app.MAX_SNAPSHOT + 10)}
        self.assertEqual(self.lan_call("/api/export/snapshot", self.athlete, method="POST", body=big)[0], 413)

    def test_what_a_phone_sends_is_stored_only_in_the_known_vocabulary(self):
        today = core._today({}).isoformat()
        junk = {"user_id": 1, "date": today, "sleep": "<script>", "energy": "high", "soreness": {"legs": "strong", "brain": "mild", "back": {"x": 1}},
                "pain": ["knee", "soul", {"a": 1}], "rhr": "9999", "note": "n" * 5000, "extra": {"deep": ["x"] * 100}}
        self.assertEqual(self.lan_call("/api/checkin", self.athlete, method="POST", body=junk)[0], 200)
        stored = app.read_json(app.GOALS, {})["1"]["checkins"][today]
        self.assertEqual({k: stored[k] for k in ("sleep", "energy", "soreness", "pain", "rhr")},
                         {"sleep": None, "energy": "high", "soreness": {"legs": "strong"}, "pain": ["knee"], "rhr": None})
        self.assertEqual(len(stored["note"]), 300)
        self.assertNotIn("extra", stored)
        # "minutes I have today" (v0.8.1): one of the offered windows as a number - anything else means "no limit"
        for sent, kept in ((20, 20), (90, 90), (25, None), ("20", None), (True, None), (-5, None), ({"a": 1}, None), (None, None)):
            self.assertEqual(self.lan_call("/api/checkin", self.athlete, method="POST", body={"user_id": 1, "date": today, "minutes": sent})[0], 200)
            self.assertEqual(app.read_json(app.GOALS, {})["1"]["checkins"][today].get("minutes"), kept, sent)
        self.assertNotIn("minutes", app.clean_checkin({"sleep": "ok"}))
        # single exercises today (v0.8.9): codes of the app's catalog with "careful" | "injury", nothing else
        app.STATE["catalog"] = {"23": {"name": "Horizontal Press"}, "3": {"name": "Row"}, "10": {"name": "Dead Lift"}, "4": {"name": "Pull Down"}}
        try:
            body = {"user_id": 1, "date": today, "exercises": {"23": "injury", "3": "careful", "3000": "injury", "10": "hurts", "4": ["injury"]}}
            self.assertEqual(self.lan_call("/api/checkin", self.athlete, method="POST", body=body)[0], 200)
            self.assertEqual(app.read_json(app.GOALS, {})["1"]["checkins"][today].get("exercises"), {"23": "injury", "3": "careful"})
            self.assertEqual(app.clean_checkin({"exercises": "all"})["exercises"], {})
            self.assertEqual(app.clean_checkin({"exercises": {"23": "injury"}})["exercises"], {"23": "injury"})
            self.assertNotIn("exercises", app.clean_checkin({"sleep": "ok"}))
        finally:
            app.STATE["catalog"] = {}
        body = {"user_id": 1, "goal": {"muscle": 0.6, "strength": 0.4}, "focus": {"Push": "more", "Pull": {"x": 1}}, "approach": "everything",
                "height_cm": "tall", "weight_kg": 82.5, "notes": "x" * 9000, "language": "xx"}
        self.assertEqual(self.lan_call("/api/goal", self.athlete, method="POST", body=body)[0], 200)
        rec = app.read_json(app.GOALS, {})["1"]
        self.assertEqual((rec["focus"], rec.get("approach"), rec.get("height_cm"), rec["weight_kg"], len(rec["notes"]), rec.get("language")),
                         ({"Push": "more"}, None, None, 82.5, 500, None))
        self.assertEqual(self.lan_call("/api/restrictions", self.athlete, method="POST",
                                       body={"user_id": 1, "restrictions": {"knee": "avoid", "ego": "careful", "hip": "maybe"}})[0], 200)
        self.assertEqual(app.read_json(app.GOALS, {})["1"]["restrictions"], {"knee": "avoid"})
        # v0.9.0: the same endpoint takes the lasting choices per exercise; a field that is not sent stays as it is
        app.STATE["catalog"] = {"23": {"name": "Horizontal Press"}, "5": {"name": "Overhead Press"}}
        try:
            body = {"user_id": 1, "excluded_exercises": {"5": "injury", "23": "careful", "999": "injury", "5x": "elsewhere", "23x": 7}}
            self.assertEqual(self.lan_call("/api/restrictions", self.athlete, method="POST", body=body)[0], 200)
            rec = app.read_json(app.GOALS, {})["1"]
            self.assertEqual((rec["excluded_exercises"], rec["restrictions"]), ({"5": "injury", "23": "careful"}, {"knee": "avoid"}))
            body = {"user_id": 1, "restrictions": {}, "excluded_exercises": {"5": "injury"}}     # the page's migration call
            self.assertEqual(self.lan_call("/api/restrictions", self.athlete, method="POST", body=body)[0], 200)
            rec = app.read_json(app.GOALS, {})["1"]
            self.assertEqual((rec["excluded_exercises"], rec["restrictions"]), ({"5": "injury"}, {}))
            status, ck = self.lan_call("/api/checkin?user_id=1", self.athlete)[:2]
            self.assertEqual(status, 200)
            self.assertEqual((ck["excluded_exercises"], ck["restrictions"]), ({"5": "injury"}, {}))
        finally:
            app.STATE["catalog"] = {}

    def test_only_private_addresses_get_a_listener(self):
        for ip in ("8.8.8.8", "127.0.0.1", "0.0.0.0", "169.254.1.1"):
            with self.assertRaises(OSError):
                app.make_lan_server(ip, 0)
        self.assertTrue(lan.private_ipv4("192.168.1.20") and lan.private_ipv4("10.1.2.3") and lan.private_ipv4("172.16.0.9"))
        self.assertFalse(lan.private_ipv4("172.32.0.1") or lan.private_ipv4("fe80::1") or lan.private_ipv4("example.org"))
        status, out = call(self.port, "/api/lan", method="POST", body={"enabled": True, "ip": "8.8.8.8"}, headers={**LOCAL, **JSON})
        self.assertIn(status, (400, 409))                                                          # refused before anything is bound

    def test_adapter_rows_from_windows_are_filtered(self):
        raw = ('[{"ip":"192.168.1.20","name":"WLAN","description":"Intel(R) Wi-Fi 6","gateway":true,"network":"Private"},'
               '{"ip":"172.28.0.1","name":"vEthernet (WSL)","description":"Hyper-V Virtual Ethernet Adapter","gateway":false,"network":""},'
               '{"ip":"10.8.0.2","name":"Work","description":"WireGuard Tunnel","gateway":false,"network":"Public"},'
               '{"ip":"84.12.1.9","name":"Ethernet","description":"Realtek","gateway":true,"network":"Public"}]')
        self.assertEqual(lan.parse_adapters(raw), [{"ip": "192.168.1.20", "name": "WLAN", "gateway": True, "network": "Private"}])
        self.assertEqual(lan.parse_adapters("not json"), [])


class Allowance(TwoListeners):
    """An athlete link spends the owner's API key: a few boards and questions a day, no chat for a
    minor until the trainer says so."""
    def setUp(self):
        super().setUp()
        report, cfg = make()
        tmp = tempfile.mkdtemp()
        self._more = (app.report_cfg, app.BOARDS, app.JOBS, app.CHATS)
        app.report_cfg = lambda uid: (dict(cfg, user_id=uid), {"name": "Anna Example", "created": None})
        app.make_report = lambda uid, cfg_info=None: report
        app.BOARDS, app.JOBS, app.CHATS = ai.BoardStore(os.path.join(tmp, "b.json")), ai.JobManager(), ai.ChatManager(os.path.join(tmp, "c.json"))
        os.environ["ARX_AI_FAKE"] = "ok"
        self.addCleanup(os.environ.pop, "ARX_AI_FAKE", None)
        self.addCleanup(lambda: (setattr(app, "report_cfg", self._more[0]), setattr(app, "BOARDS", self._more[1]),
                                 setattr(app, "JOBS", self._more[2]), setattr(app, "CHATS", self._more[3])))

    def board(self):
        out = self.lan_call("/api/coach/start", self.athlete, method="POST", body={"user_id": 1})[1]
        for _ in range(80):
            if out["state"] != "running":
                break
            time.sleep(0.05)
            out = self.lan_call("/api/coach/status?user_id=1", self.athlete)[1]
        return out

    def test_boards_and_questions_are_counted_once(self):
        self.assertEqual(self.board()["state"], "done")
        self.assertEqual(app.ACCESS.left(1, "boards"), access.ATHLETE_BOARDS_PER_DAY - 1)
        self.assertEqual(self.board()["state"], "done")                                             # the same data state: no new call,
        self.assertEqual(app.ACCESS.left(1, "boards"), access.ATHLETE_BOARDS_PER_DAY - 1)           # nothing counted
        ask = {"user_id": 1, "text": "Why this order?", "client_msg_id": "m1"}
        self.assertEqual(self.lan_call("/api/chat/send", self.athlete, method="POST", body=ask)[0], 200)
        self.assertEqual(self.lan_call("/api/chat/send", self.athlete, method="POST", body=ask)[0], 200)   # a retry of the same message
        self.assertEqual(app.ACCESS.left(1, "questions"), access.ATHLETE_QUESTIONS_PER_DAY - 1)
        hist = self.lan_call("/api/chat/history?user_id=1", self.athlete)[1]
        self.assertLessEqual(hist["left"], access.ATHLETE_QUESTIONS_PER_DAY - 1)
        self.assertFalse(hist["chat_off"])

    def test_a_used_up_allowance_says_so(self):
        data = app.read_json(app.ACCESS.path, {})
        data["athletes"]["1"]["usage"] = {"date": core._today({}).isoformat(), "boards": access.ATHLETE_BOARDS_PER_DAY,
                                          "questions": access.ATHLETE_QUESTIONS_PER_DAY}
        app.write_json(app.ACCESS.path, data)
        out = self.lan_call("/api/coach/start", self.athlete, method="POST", body={"user_id": 1})[1]
        self.assertEqual((out["state"], out["error"]["code"]), ("limit", "link_limit"))
        status, out = self.lan_call("/api/chat/send", self.athlete, method="POST", body={"user_id": 1, "text": "hi"})
        self.assertEqual((status, out["error"]), (429, "link_limit"))
        self.assertEqual(self.lan_call("/api/coach/start", self.trainer, method="POST", body={"user_id": 1})[1]["state"], "running")   # the trainer is not capped by it

    def test_a_minors_link_has_no_chat_until_the_trainer_allows_it(self):
        kid = {app.TOKEN_HEADER: app.ACCESS.athlete(3, create=True, minor=True)["token"]}
        status, out = self.lan_call("/api/chat/send", kid, method="POST", body={"user_id": 3, "text": "hi"})
        self.assertEqual((status, out["error"]), (403, "chat_off"))
        self.assertTrue(self.lan_call("/api/chat/history?user_id=3", kid)[1].get("chat_off", True))
        app.ACCESS.set_chat(3, True)
        self.assertEqual(self.lan_call("/api/chat/send", kid, method="POST", body={"user_id": 3, "text": "hi"})[0], 409)   # allowed now - the board comes first


class Listener(unittest.TestCase):
    """arx_lan.LanManager: start / stop, next port when one is taken, honest state when there is no
    private address. The address is faked (the loopback one) - no real network is touched."""
    def setUp(self):
        self._pick = lan.pick_ip
        self.addCleanup(lambda: setattr(lan, "pick_ip", self._pick))

        def make(ip, port):
            srv = app.Server((ip, port), app.Handler)
            srv.kind, srv.allowed_hosts = "lan", (ip,)
            return srv
        self.make = make

    def test_start_stop_and_the_next_port(self):
        lan.pick_ip = lambda choice: "127.0.0.1"
        blocker = app.Server(("127.0.0.1", 0), app.Handler)             # something already sits on the first port
        self.addCleanup(blocker.server_close)
        first = blocker.server_address[1]
        mgr = lan.LanManager(self.make, first)
        self.addCleanup(mgr.stop)
        st = mgr.start("auto")
        self.assertTrue(st["running"])
        self.assertGreater(st["port"], first)
        self.assertEqual(st["url"], f"http://127.0.0.1:{st['port']}/")
        self.assertEqual(call(st["port"], "/api/bootstrap")[0], 401)      # it IS the phone listener: a token or nothing
        st = mgr.stop()
        self.assertEqual((st["running"], st["url"]), (False, None))

    def test_no_private_address_is_said_not_worked_around(self):
        lan.pick_ip = lambda choice: None
        mgr = lan.LanManager(self.make, 1)
        st = mgr.start("auto")
        self.assertEqual((st["running"], st["error"]), (False, "no_private_address"))
        mgr.stop()

    def test_the_elevated_firewall_command_survives_odd_paths(self):
        cmd = lan.firewall_command(r"C:\Users\O'Neil Smith\app\windows\firewall.ps1", 8765, False, [r"C:\Program Files\Python312\python.exe"])
        self.assertIn("-Verb RunAs", cmd)
        self.assertNotIn("Hidden", cmd)                                      # what runs elevated shows its window (v0.8.3)
        self.assertIn("O''Neil Smith", cmd)                                  # a quote inside a PowerShell literal is doubled
        self.assertIn("'-Port','8765'", cmd)
        self.assertNotIn("-Remove", cmd)
        self.assertIn("'-Remove'", lan.firewall_command("x.ps1", 8765, True))
        # v0.8.5: the line travels as plain, readable text - possible because it contains no double quote at all
        self.assertNotIn('"', cmd)
        self.assertIn("[char]34", cmd)                                       # the quotes a path with spaces needs, made at run time
        self.assertEqual(lan.ps_command(cmd, interactive=True), ["powershell", "-NoProfile", "-Command", cmd])
        self.assertEqual(lan.ps_command("Get-Date")[:3], ["powershell", "-NoProfile", "-NonInteractive"])
        self.assertNotIn('"', lan._PS_STATE)
        with self.assertRaises(ValueError):
            lan.ps_command('Write-Host "quoted"')

    def test_nothing_in_the_release_looks_like_malware_without_a_reason(self):
        """Windows security judged the release ZIP as a trojan (2026-09). Patterns that malware uses and we do not
        need stay out: commands in encoded form, hidden windows for elevated commands, programs pinning
        themselves to the taskbar, switching protection off. (The needles are put together here so that this
        very file does not contain them.)"""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        needles = ["-Encoded" + "Command", "-enc" + " ", "FromBase64" + "String", "-WindowStyle" + " Hidden", ".Do" + "It()",
                   "Set-MpPre" + "ference", "Add-MpPre" + "ference", "DisableRealtime" + "Monitoring", "Invoke-Ex" + "pression", "| i" + "ex"]
        shipped = [os.path.join(here, n) for n in os.listdir(here) if n.endswith((".py", ".bat", ".ps1"))]
        shipped += [os.path.join(here, "windows", n) for n in os.listdir(os.path.join(here, "windows"))]
        self.assertGreater(len(shipped), 12)
        for path in shipped:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            for needle in needles:
                self.assertNotIn(needle.lower(), text.lower(), f"{os.path.basename(path)}: {needle}")


class Page(unittest.TestCase):
    """The single-file page: a syntax error means a blank screen on every device."""
    HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_the_script_parses(self):
        import shutil, subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        check = ("const fs=require('fs');const h=fs.readFileSync(process.argv[1],'utf8');"
                 "const m=h.match(/<script>([\\s\\S]*)<\\/script>/);new Function(m[1]);")
        r = subprocess.run([node, "-e", check, os.path.join(self.HERE, "web", "index.html")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])

    def test_the_page_sends_the_code_in_the_header_and_never_keeps_it_in_the_address(self):
        with open(os.path.join(self.HERE, "web", "index.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertIn('history.replaceState(null,"",location.pathname+location.search)', page)   # the fragment leaves the address bar
        self.assertIn('const HDR={"X-ARX-Token":TOKEN}', page)
        self.assertNotIn("?t=", page)                                        # never as a query parameter (that would reach logs)


class DefaultOn(unittest.TestCase):
    """v0.6.1 (owner): phone access is ON until somebody switches it off - and no click waits for Windows."""
    def test_on_until_switched_off_and_only_the_switch_counts(self):
        store = access.AccessStore(os.path.join(tempfile.mkdtemp(), "access.json"))
        self.assertEqual(store.lan(), {"enabled": True, "ip": "auto", "chosen": False})
        store.athlete(1, create=True)                                       # the file is written for another reason ...
        self.assertTrue(store.lan()["enabled"])                             # ... and that does not freeze anything
        self.assertEqual(store.set_lan(False), {"enabled": False, "ip": "auto", "chosen": True})
        self.assertFalse(access.AccessStore(store.path).lan()["enabled"])   # a choice survives the restart
        self.assertTrue(store.set_lan(True, "192.168.1.20")["enabled"])
        app.write_json(store.path, {"lan": {"enabled": False, "ip": "auto"}, "athletes": {}})    # a v0.6.0 file: nobody ever chose
        self.assertTrue(store.lan()["enabled"])

    def test_nothing_waits_for_windows_but_who_asks_gets_the_answer(self):
        calls = []

        def slow(script):
            calls.append(time.time())
            time.sleep(0.6)
            return ('{"adapters":[{"ip":"192.168.1.20","name":"WLAN","description":"Wi-Fi","gateway":true,"network":"Public"},'
                    '{"ip":"10.8.0.2","name":"Work","description":"WireGuard Tunnel","gateway":true,"network":"Public"}],"rule":false}')
        saved = (lan.IS_WINDOWS, lan._powershell, dict(lan._STATE))
        self.addCleanup(lambda: (setattr(lan, "IS_WINDOWS", saved[0]), setattr(lan, "_powershell", saved[1]), lan._STATE.update(saved[2]), lan._STATE_DONE.set()))
        lan.IS_WINDOWS, lan._powershell = True, slow
        lan._STATE.update(at=0.0, adapters=[], firewall_rule=None, known=False, busy=False)
        t = time.time()
        first = lan.system_info(wait=False)                                 # the fast path: what is known now, the question goes out in the background
        lan.pick_ip("auto")
        self.assertLess(time.time() - t, 0.3)
        self.assertFalse(first["known"])
        full = lan.system_info(fresh=True)                                  # the dialog's background request JOINS that question instead of asking twice
        self.assertEqual(len(calls), 1)
        self.assertEqual((full["known"], full["firewall_rule"], [a["name"] for a in full["adapters"]]), (True, False, ["WLAN"]))
        self.assertEqual(full["adapters"][0]["network"], "Public")           # the dialog warns: Windows blocks phones on a public network
        self.assertEqual(lan.pick_ip("auto"), "192.168.1.20")               # never the VPN adapter, even when the default route is there

    def test_the_listener_comes_up_by_itself_when_the_network_does(self):
        saved = (lan.pick_ip, lan.WATCH_S)
        self.addCleanup(lambda: (setattr(lan, "pick_ip", saved[0]), setattr(lan, "WATCH_S", saved[1])))
        there = {"ip": None}
        lan.pick_ip, lan.WATCH_S = (lambda choice: there["ip"]), 0.05

        def make(ip, port):
            srv = app.Server((ip, port), app.Handler)
            srv.kind, srv.allowed_hosts = "lan", (ip,)
            return srv
        free = app.Server(("127.0.0.1", 0), app.Handler)
        port = free.server_address[1]
        free.server_close()
        mgr = lan.LanManager(make, port)
        self.addCleanup(mgr.stop)
        st = mgr.start("auto")                                              # the PC booted faster than its Wi-Fi
        self.assertEqual((st["running"], st["wanted"], st["error"]), (False, True, "no_private_address"))
        there["ip"] = "127.0.0.1"
        for _ in range(100):
            if mgr.status()["running"]:
                break
            time.sleep(0.05)
        self.assertTrue(mgr.status()["running"])
        self.assertFalse(mgr.stop()["wanted"])

    def test_the_status_answers_at_once_and_bootstrap_says_on(self):
        saved = (app.ACCESS, app.LAN)
        self.addCleanup(lambda: (setattr(app, "ACCESS", saved[0]), setattr(app, "LAN", saved[1])))
        app.ACCESS, app.LAN = access.AccessStore(os.path.join(tempfile.mkdtemp(), "access.json")), None
        app.STATE.update(version="0.6.1", secret="s3cret", catalog={})
        app.UPDATE_CHECKED.set()
        srv = app.Server(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        port = srv.server_address[1]
        self.assertTrue(call(port, "/api/bootstrap")[1]["phone"]["enabled"])
        t = time.time()
        status, st = call(port, "/api/lan/status", headers=LOCAL)
        self.assertLess(time.time() - t, 1.0)
        self.assertEqual((status, st["enabled"], st["running"]), (200, True, False))
        self.assertIn("details", st)


if __name__ == "__main__":
    unittest.main()
