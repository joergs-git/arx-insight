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
import os, sys, json, time, argparse, threading, webbrowser
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import urllib.request
import arx_report as core   # reuse the read-only engine

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(core.data_dir(), "config.json")   # stable, survives re-download
GOALS = os.path.join(core.data_dir(), "goals.json")     # per-user: {user_id: {...}}
WEB = os.path.join(HERE, "web", "index.html")

CHECKIN_KEEP_DAYS = 60        # daily check-ins older than this are pruned from goals.json
CHECKIN_FIELDS = ("sleep", "energy", "soreness", "rhr", "pain", "note")

REPO_URL = "https://github.com/joergs-git/arx-insight"
RAW_VERSION_URL = "https://raw.githubusercontent.com/joergs-git/arx-insight/main/VERSION"

def app_version() -> str:
    try:
        with open(os.path.join(HERE, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "0.0.0"

def check_update(current: str) -> dict:
    """Best-effort: is a newer VERSION on GitHub? Never blocks for long."""
    try:
        with urllib.request.urlopen(RAW_VERSION_URL, timeout=3) as r:
            latest = r.read().decode("utf-8").strip()
        def parts(v): return [int(x) for x in v.split(".") if x.isdigit()]
        newer = parts(latest) > parts(current)
        return {"latest": latest, "update_available": newer, "url": REPO_URL}
    except Exception:
        return {"latest": current, "update_available": False, "url": REPO_URL}

STATE = {"db": None, "catalog": {}, "version": "0.0.0", "update": {}, "server": None}


# ---- small JSON file helpers -------------------------------------------------
def read_json(path, default):
    try:
        # utf-8-sig tolerates a BOM (Windows PowerShell writes config.json with one)
        with open(path, encoding="utf-8-sig") as f:
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


def checkin_payload(urec: dict, day: str) -> dict:
    """Today's check-in of one person plus the earlier resting-HR values (for
    the baseline). Stored per user in goals.json under 'checkins' {date: {...}}."""
    cis = urec.get("checkins") or {}
    ci = cis.get(day)
    history = [{"date": d, "rhr": v.get("rhr")} for d, v in sorted(cis.items())
               if d < day and v.get("rhr")]
    scored = core._readiness({"date": day}, history)     # only for the baseline figure
    return {"date": day, "checkin": ci, "history": history,
            "rhr_baseline": scored["rhr_baseline"] if scored else None}


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
    cfg["focus"] = urec.get("focus", {})                   # group -> more/normal/less/off
    cfg["approach"] = urec.get("approach", "auto")         # full | split | auto
    # today's check-in (sleep, energy, soreness, resting HR, pain) + RHR history
    today = core._today({}).isoformat()
    ck = checkin_payload(urec, today)
    cfg["checkin"] = dict(ck["checkin"], date=today) if ck["checkin"] else None
    # earlier check-ins in full (RHR baseline + readiness history for the deload rule)
    cfg["checkin_history"] = [dict(v, date=d) for d, v in sorted((urec.get("checkins") or {}).items())
                              if d < today]
    # per-user language override wins for the AI narrative (falls back to device default)
    cfg["language"] = urec.get("language") or cfg.get("language", "en")
    cfg["_catalog"] = STATE["catalog"]
    con, tmp = core.open_readonly(STATE["db"])
    try:
        report = core.build_report(con, cfg)
        report["created"] = info["created"]
        report["user"] = info
        if with_ai:
            report["ai_narrative"] = cached_narrative(report, cfg)
    finally:
        con.close()
        import shutil; shutil.rmtree(tmp, ignore_errors=True)
    return report


# ---- AI narrative cache -------------------------------------------------------
# The coach board is re-requested on every report load; without a cache every
# visit (a reload, a language switch back and forth) would bill a new call.
# The key covers everything the answer depends on: person, day, the recorded
# sets, today's check-in, restrictions, goal, focus, approach, units, language,
# model and effort. New training data or a new check-in -> a new answer.
AI_CACHE = os.path.join(core.data_dir(), ".ai_cache.json")
AI_CACHE_KEEP = 40            # most recent answers kept


def _ai_cache_key(report: dict, cfg: dict) -> str:
    import hashlib
    basis = {
        "user": cfg.get("user_id"), "today": report.get("today"),
        "sets": [report.get("sets_total"), report.get("sets_working")],
        "last_day": (report.get("training_days") or [None])[-1],
        "checkin": report.get("checkin"), "restrictions": report.get("restrictions"),
        "goal": report.get("goal"), "focus": report.get("focus"), "approach": report.get("approach"),
        "units": cfg.get("units"), "language": cfg.get("language"),
        "model": cfg.get("model"), "effort": cfg.get("ai_effort"),
        "version": STATE.get("version"),
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]


def cached_narrative(report: dict, cfg: dict) -> str | None:
    key = _ai_cache_key(report, cfg)
    cache = read_json(AI_CACHE, {})
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("text"):
        return hit["text"]
    text = core.ai_narrative(report, cfg)
    if text:
        cache[key] = {"text": text, "stored": time.strftime("%Y-%m-%dT%H:%M:%S"), "user": cfg.get("user_id")}
        if len(cache) > AI_CACHE_KEEP:                      # drop the oldest entries
            for k in sorted(cache, key=lambda k: cache[k].get("stored", ""))[:len(cache) - AI_CACHE_KEEP]:
                del cache[k]
        try:
            write_json(AI_CACHE, cache)
        except Exception:
            pass
    return text


# ---- HTTP handler ------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):            # keep the console quiet
        pass

    def _send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, OSError):
            # the browser navigated away / closed the tab mid-response (common while
            # the multi-second AI request is in flight) - harmless, ignore quietly
            pass

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
                "version": STATE["version"],
                "update": STATE["update"],
            })
        if u.path == "/api/users":
            return self._send(search_users(q.get("q", [""])[0]))
        if u.path == "/api/goal":
            goals = read_json(GOALS, {})
            uid = q.get("user_id", ["0"])[0]
            return self._send(goals.get(uid, {}))          # {} => not defined yet
        if u.path == "/api/checkin":                       # today's (or a given day's) check-in
            goals = read_json(GOALS, {})
            uid = q.get("user_id", ["0"])[0]
            day = q.get("date", [core._today({}).isoformat()])[0]
            return self._send(checkin_payload(goals.get(uid, {}), day))
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
            for k in ("language", "units", "sessions_per_week", "model", "ai_effort", "anthropic_api_key"):
                if k in data and data[k] != "":
                    cfg[k] = data[k]
            write_json(CONFIG, cfg)
            return self._send({"ok": True})
        if u.path == "/api/goal":                          # per-user profile / goal screen
            goals = read_json(GOALS, {})
            rec = goals.get(str(data["user_id"]), {})
            rec["goal"] = data.get("goal", {})
            rec["sessions_per_week"] = data.get("sessions_per_week", 2)
            if "focus" in data: rec["focus"] = data["focus"]          # group -> more/normal/less/off
            if "approach" in data: rec["approach"] = data["approach"] # full | split | auto
            if "language" in data:                                     # "" clears -> device default
                if data["language"]: rec["language"] = data["language"]
                else: rec.pop("language", None)
            for opt in ("height_cm", "weight_kg", "notes"):            # optional profile fields
                if opt in data and data[opt] not in ("", None):
                    rec[opt] = data[opt]
            goals[str(data["user_id"])] = rec              # keep any restrictions
            write_json(GOALS, goals)
            return self._send({"ok": True})
        if u.path == "/api/shutdown":                      # a newer instance asks us to quit
            srv = STATE.get("server")
            if srv:
                threading.Thread(target=srv.shutdown, daemon=True).start()
            return self._send({"ok": True})
        if u.path == "/api/restrictions":                  # injury / limitation screen
            goals = read_json(GOALS, {})
            rec = goals.get(str(data["user_id"]), {})
            rec["restrictions"] = data.get("restrictions", {})   # {part: ok|careful|avoid}
            goals[str(data["user_id"])] = rec
            write_json(GOALS, goals)
            return self._send({"ok": True})
        if u.path == "/api/checkin":                       # daily check-in screen
            goals = read_json(GOALS, {})
            rec = goals.get(str(data["user_id"]), {})
            day = data.get("date") or core._today({}).isoformat()
            entry = {k: data[k] for k in CHECKIN_FIELDS if k in data}
            try:
                entry["rhr"] = int(entry["rhr"]) if entry.get("rhr") not in ("", None) else None
            except (TypeError, ValueError):
                entry["rhr"] = None
            cis = rec.setdefault("checkins", {})
            cis[day] = entry
            cutoff = (date.fromisoformat(day) - timedelta(days=CHECKIN_KEEP_DAYS)).isoformat()
            for d in [d for d in cis if d < cutoff]:      # keep the file small
                del cis[d]
            goals[str(data["user_id"])] = rec
            write_json(GOALS, goals)
            return self._send({"ok": True, "date": day})
        return self._send({"error": "not found"}, code=404)


class Server(ThreadingHTTPServer):
    daemon_threads = True                    # don't let worker threads block shutdown
    def handle_error(self, request, client_address):
        # a browser closing the tab mid-response raises ConnectionAborted/Reset -
        # that's normal and not worth a scary traceback; only log real errors
        if issubclass(sys.exc_info()[0] or Exception, (ConnectionError, OSError)):
            return
        super().handle_error(request, client_address)


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
    STATE["version"] = app_version()
    STATE["update"] = check_update(STATE["version"])   # one-off, 3 s cap

    # Single instance with update-takeover:
    #  * same version already running  -> just open the browser (stray double-click)
    #  * a DIFFERENT/older version running (e.g. right after an update) -> tell it to
    #    quit and take over the port, so the old process never lingers
    #  * some other program on the port -> try the next port
    def ours_version(port):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/bootstrap", timeout=1.5) as r:
                d = json.loads(r.read())
                return d.get("version", "?") if "catalog" in d else None
        except Exception:
            return None

    def try_bind(port):
        try:
            return Server(("127.0.0.1", port), Handler)
        except OSError:
            return None

    srv, port = None, args.port
    for p in range(args.port, args.port + 6):
        s = try_bind(p)
        if s:
            srv, port = s, p; break
        ver = ours_version(p)
        if ver is None:
            continue                                   # not us -> next port
        if ver == STATE["version"]:
            print(f"ARX Insight is already running at http://localhost:{p} - opening it.")
            if not args.no_browser:
                webbrowser.open(f"http://localhost:{p}")
            return
        print(f"Replacing a running ARX Insight (v{ver}) on port {p} ...")
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{p}/api/shutdown", method="POST"), timeout=2).read()
        except Exception:
            pass
        for _ in range(24):                            # wait up to ~6 s for the port to free
            time.sleep(0.25)
            s = try_bind(p)
            if s:
                srv, port = s, p; break
        if srv:
            break
        if not args.no_browser:                        # couldn't take over -> open the old one
            webbrowser.open(f"http://localhost:{p}")
        return
    if srv is None:
        sys.exit("Could not find a free port for ARX Insight.")
    STATE["server"] = srv                              # so /api/shutdown can stop us

    url = f"http://localhost:{port}"
    print(f"ARX Insight {STATE['version']} running at {url}  (close this window to stop)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
