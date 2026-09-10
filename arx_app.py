#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARX Insight - local touch app.

A tiny, dependency-light local web app for the machine where the ARX database
lives. It runs a stdlib HTTP server and serves a single-page, touch-first UI.

App flow (as specified):
  start
   -> no global config yet?         -> SETUP screen (units, language, key, ...)
   -> otherwise                     -> SEARCH screen (centered name search)
        pick a user
         -> user has no goal yet?   -> GOAL screen (sliders), saved permanently
         -> otherwise               -> REPORT screen (real name + created date)
              Exit                  -> back to SEARCH

Real names are shown because this runs locally on the owner's machine. The API
key stays in config.json (git-ignored) and is only used server-side.

Run:  python arx_app.py --db "<path to DB.FDB4>"   then open http://localhost:8765
Public domain / CC0. Not medical advice.
"""
from __future__ import annotations
import os, sys, json, argparse, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import arx_report as core   # reuse the read-only engine

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
GOALS = os.path.join(HERE, "goals.json")          # per-user goals: {user_id: {...}}
WEB = os.path.join(HERE, "web", "index.html")

STATE = {"db": None, "catalog": {}}


# ---- small JSON file helpers -------------------------------------------------
def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---- database queries --------------------------------------------------------
def search_users(query: str) -> list[dict]:
    """Substring search on first/last name, like the ARX picker."""
    con, tmp = core.open_readonly(STATE["db"])
    try:
        cur = con.cursor()
        cur.execute('''select id, trim(firstname), trim(lastname), gender, birthdate, createdate
                       from "User" where deleted is false order by lastname, firstname''')
        rows = cur.fetchall()
    finally:
        con.close()
        import shutil; shutil.rmtree(tmp, ignore_errors=True)
    q = (query or "").strip().lower()
    out = []
    for uid, fn, ln, gender, dob, created in rows:
        full = f"{fn} {ln}".strip()
        if q and q not in full.lower():
            continue
        out.append({"id": uid, "name": full, "gender": (gender or "").strip(),
                    "birthdate": str(dob)[:10] if dob else None,
                    "created": str(created)[:10] if created else None})
    return out


def user_info(user_id: int) -> dict:
    for u in search_users(""):
        if u["id"] == user_id:
            return u
    return {"id": user_id, "name": f"User {user_id}", "created": None}


def make_report(user_id: int, with_ai: bool) -> dict:
    cfg = read_json(CONFIG, {})
    goals = read_json(GOALS, {})
    info = user_info(user_id)
    cfg = dict(cfg)
    cfg["user_id"] = user_id
    cfg["alias"] = info["name"]                       # REAL name, local app
    urec = goals.get(str(user_id), {})
    cfg["goal"] = urec.get("goal", cfg.get("goal", {}))
    cfg["sessions_per_week"] = urec.get("sessions_per_week", cfg.get("sessions_per_week"))
    cfg["restrictions"] = urec.get("restrictions", {})     # body part -> ok/careful/avoid
    cfg["_catalog"] = STATE["catalog"]
    con, tmp = core.open_readonly(STATE["db"])
    try:
        report = core.build_report(con, cfg)
        report["created"] = info["created"]
        report["user"] = info
        if with_ai:
            report["ai_narrative"] = core.ai_narrative(report, cfg)
    finally:
        con.close()
        import shutil; shutil.rmtree(tmp, ignore_errors=True)
    return report


# ---- HTTP handler ------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):            # keep the console quiet
        pass

    def _send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            with open(WEB, "rb") as f:
                return self._send(f.read(), ctype="text/html")
        if u.path == "/api/bootstrap":
            cfg = read_json(CONFIG, {})
            return self._send({
                "configured": bool(cfg.get("anthropic_api_key") or cfg.get("units") or cfg.get("language")),
                "language": cfg.get("language", "en"),      # US-first defaults
                "units": cfg.get("units", "imperial"),
                "sessions_per_week": cfg.get("sessions_per_week", 2),
                "has_key": bool(cfg.get("anthropic_api_key")),
                "catalog": STATE["catalog"],
            })
        if u.path == "/api/users":
            return self._send(search_users(q.get("q", [""])[0]))
        if u.path == "/api/goal":
            goals = read_json(GOALS, {})
            uid = q.get("user_id", ["0"])[0]
            return self._send(goals.get(uid, {}))          # {} => not defined yet
        if u.path == "/api/report":
            uid = int(q.get("user_id", ["0"])[0])
            ai = q.get("ai", ["0"])[0] == "1"
            try:
                return self._send(make_report(uid, ai))
            except Exception as e:
                return self._send({"error": type(e).__name__, "detail": str(e)[:300]}, code=500)
        return self._send({"error": "not found"}, code=404)

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        if u.path == "/api/config":                        # global setup screen
            cfg = read_json(CONFIG, {})
            for k in ("language", "units", "sessions_per_week", "model", "anthropic_api_key"):
                if k in data and data[k] != "":
                    cfg[k] = data[k]
            write_json(CONFIG, cfg)
            return self._send({"ok": True})
        if u.path == "/api/goal":                          # per-user goal screen
            goals = read_json(GOALS, {})
            rec = goals.get(str(data["user_id"]), {})
            rec["goal"] = data.get("goal", {})
            rec["sessions_per_week"] = data.get("sessions_per_week", 2)
            goals[str(data["user_id"])] = rec              # keep any restrictions
            write_json(GOALS, goals)
            return self._send({"ok": True})
        if u.path == "/api/restrictions":                  # injury / limitation screen
            goals = read_json(GOALS, {})
            rec = goals.get(str(data["user_id"]), {})
            rec["restrictions"] = data.get("restrictions", {})   # {part: ok|careful|avoid}
            goals[str(data["user_id"])] = rec
            write_json(GOALS, goals)
            return self._send({"ok": True})
        return self._send({"error": "not found"}, code=404)


def main():
    ap = argparse.ArgumentParser(description="ARX Insight - local touch app")
    ap.add_argument("--db", default=os.environ.get("ARX_DB"), help="Path to DB.FDB4")
    ap.add_argument("--catalog", default=os.path.join(HERE, "exercises.json"))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    db = args.db or read_json(CONFIG, {}).get("db")   # installer stores the path in config.json
    if not db:
        sys.exit("No database path. Use --db, set ARX_DB, or add 'db' to config.json.")
    STATE["db"] = db
    STATE["catalog"] = core.load_catalog(args.catalog)

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"ARX Insight running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
