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

v0.3.1: exactly ONE app process (exclusive bind on Windows + instance file + takeover of an older
version), every /api call must carry the X-ARX-Token header (no cross-site requests), POST bodies
are validated, a future check-in date can no longer wipe the history, and an AI failure is reported
as such instead of breaking the report.
"""
from __future__ import annotations
import os, sys, json, time, shutil, socket, secrets, argparse, threading, subprocess, webbrowser
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

# ---- request safety -----------------------------------------------------------------------------
# Every /api call must carry this header. A web page from another origin cannot add a custom header
# without a CORS preflight, and this server never grants one - so no foreign page can read data,
# write settings or trigger a billed AI call. Value "local" on this machine (from v0.6.0 the phone's
# access token travels in the same header). /api/bootstrap stays open: it holds no personal data and
# older app versions probe it to find a running instance.
TOKEN_HEADER = "X-ARX-Token"
OPEN_PATHS = ("/api/bootstrap",)
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")   # Host allow-list: stops DNS-rebinding pages
MAX_BODY = 64 * 1024                              # POST bodies are small JSON objects

# ---- single instance ------------------------------------------------------------------------------
# Who is running: {pid, port, version, folder, secret, started}. Written after a successful bind and
# marked as stopped (pid/port None) on a clean exit - the file itself stays, it also tells the next
# start that a version with the exclusive bind has run here before. A newer version reads the secret
# from here to ask this one to quit.
INSTANCE = os.path.join(core.data_dir(), "instance.json")

REPO_URL = "https://github.com/joergs-git/arx-insight"
RAW_VERSION_URL = "https://raw.githubusercontent.com/joergs-git/arx-insight/main/VERSION"

def app_version() -> str:
    try:
        with open(os.path.join(HERE, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "0.0.0"

def vparts(v: str) -> list[int]:
    """'0.3.1' -> [0, 3, 1] for version comparisons (non-numeric parts are ignored)."""
    return [int(x) for x in str(v or "").split(".") if x.isdigit()]


def check_update(current: str) -> dict:
    """Best-effort: is a newer VERSION on GitHub? Never blocks for long."""
    try:
        with urllib.request.urlopen(RAW_VERSION_URL, timeout=3) as r:
            latest = r.read().decode("utf-8").strip()
        newer = vparts(latest) > vparts(current)
        return {"latest": latest, "update_available": newer, "url": REPO_URL}
    except Exception:
        return {"latest": current, "update_available": False, "url": REPO_URL}

STATE = {"db": None, "catalog": {}, "version": "0.0.0", "update": {}, "server": None, "secret": ""}
UPDATE_CHECKED = threading.Event()   # set once the background update check has finished


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
        shutil.rmtree(tmp, ignore_errors=True)
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
            # a failed AI call must never cost the athlete the report: text or a typed error
            report["ai_narrative"], report["ai_error"] = cached_narrative(report, cfg)
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)
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


def cached_narrative(report: dict, cfg: dict) -> tuple[str | None, dict | None]:
    """(text, error). Only successful answers are cached; a failure comes back as
    {code, message} (see core.classify_ai_error) so the UI can explain it and offer a retry."""
    key = _ai_cache_key(report, cfg)
    cache = read_json(AI_CACHE, {})
    hit = cache.get(key)
    if isinstance(hit, dict) and hit.get("text"):
        return hit["text"], None
    try:
        text = core.ai_narrative(report, cfg)
    except Exception as exc:                       # AIError or anything unexpected inside the SDK
        return None, core.classify_ai_error(exc).info()
    if text:
        cache[key] = {"text": text, "stored": time.strftime("%Y-%m-%dT%H:%M:%S"), "user": cfg.get("user_id")}
        if len(cache) > AI_CACHE_KEEP:                      # drop the oldest entries
            for k in sorted(cache, key=lambda k: cache[k].get("stored", ""))[:len(cache) - AI_CACHE_KEEP]:
                del cache[k]
        try:
            write_json(AI_CACHE, cache)
        except Exception:
            pass
    return text, None


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
            self.send_header("X-Content-Type-Options", "nosniff")
            if "json" in ctype:
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, OSError):
            # the browser navigated away / closed the tab mid-response (common while
            # the multi-second AI request is in flight) - harmless, ignore quietly
            pass

    # -- request checks (see TOKEN_HEADER / LOCAL_HOSTS / MAX_BODY above) ---------------------
    def _loopback(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _guard(self, path: str) -> bool:
        """Checks that run before any handler. Returns False after having answered."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host:                                           # browsers always send it
            name = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0]
            if name not in LOCAL_HOSTS:
                self._send({"error": "misdirected", "detail": "unknown Host"}, code=421)
                return False
        if path.startswith("/api/") and path not in OPEN_PATHS and not self.headers.get(TOKEN_HEADER):
            self._send({"error": "forbidden", "detail": f"missing {TOKEN_HEADER} header"}, code=403)
            return False
        return True

    def _read_json(self) -> dict | None:
        """Parse a POST body. Anything but a small JSON object is answered with 4xx (-> None):
        a form or text/plain post is what a foreign web page could send without a preflight."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send({"error": "unsupported_media_type"}, code=415)
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._send({"error": "bad_length"}, code=413 if length > MAX_BODY else 411)
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            data = None
        if not isinstance(data, dict):
            self._send({"error": "bad_json"}, code=400)
            return None
        return data

    @staticmethod
    def _uid(value) -> int | None:
        """A user id from a query string or a JSON body (None when it is not a number)."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def do_GET(self):
        u = urlparse(self.path)
        if not self._guard(u.path):
            return
        try:
            self._get(u, parse_qs(u.query))
        except Exception as e:                             # never a traceback page, never a hang
            print(f"GET {u.path} failed: {type(e).__name__}: {e}", file=sys.stderr)
            self._send({"error": type(e).__name__, "detail": str(e)[:300]}, code=500)

    def _get(self, u, q):
        if u.path in ("/", "/index.html"):
            with open(WEB, "rb") as f:
                return self._send(f.read(), ctype="text/html")
        if u.path == "/api/bootstrap":
            # Our own page waits a moment for the update check (so the banner is there on the first
            # paint); a probe by another instance - "?probe=1", or an older version without the
            # header - is answered at once, otherwise it would take us for a foreign program.
            if self.headers.get(TOKEN_HEADER) and "probe" not in q:
                UPDATE_CHECKED.wait(2.5)
            cfg = read_json(CONFIG, {})
            return self._send({
                "configured": bool(cfg.get("anthropic_api_key") or cfg.get("units") or cfg.get("language")),
                "language": cfg.get("language", "en"),      # US-first defaults
                "units": cfg.get("units", "imperial"),
                "sessions_per_week": cfg.get("sessions_per_week", 2),
                "has_key": bool(cfg.get("anthropic_api_key")),
                "catalog": STATE["catalog"],
                "version": STATE["version"],
                "pid": os.getpid(),                         # lets a newer instance verify whom it replaces
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
            uid = self._uid(q.get("user_id", [None])[0])
            if uid is None:
                return self._send({"error": "bad_user_id"}, code=400)
            ai = q.get("ai", ["0"])[0] == "1"
            return self._send(make_report(uid, ai))
        return self._send({"error": "not found"}, code=404)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._guard(u.path):
            return
        data = self._read_json()
        if data is None:
            return
        try:
            self._post(u, data)
        except Exception as e:
            print(f"POST {u.path} failed: {type(e).__name__}: {e}", file=sys.stderr)
            self._send({"error": type(e).__name__, "detail": str(e)[:300]}, code=500)

    def _post(self, u, data):
        if u.path == "/api/shutdown":                      # a newer instance asks us to quit
            # only from this machine and only with the secret from instance.json - a web page or
            # another device must never be able to stop the app
            if not self._loopback() or not secrets.compare_digest(str(data.get("secret") or ""), STATE["secret"] or "-"):
                return self._send({"error": "forbidden"}, code=403)
            srv = STATE.get("server")
            if srv:
                threading.Thread(target=srv.shutdown, daemon=True).start()
            return self._send({"ok": True})
        if u.path == "/api/config":                        # global setup screen
            cfg = read_json(CONFIG, {})
            for k in ("language", "units", "sessions_per_week", "model", "ai_effort", "anthropic_api_key"):
                if k in data and data[k] != "":
                    cfg[k] = data[k]
            write_json(CONFIG, cfg)
            return self._send({"ok": True})
        # everything below belongs to one person
        uid = self._uid(data.get("user_id"))
        if uid is None:
            return self._send({"error": "bad_user_id"}, code=400)
        if u.path == "/api/goal":                          # per-user profile / goal screen
            goals = read_json(GOALS, {})
            rec = goals.get(str(uid), {})
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
            goals[str(uid)] = rec                          # keep any restrictions
            write_json(GOALS, goals)
            return self._send({"ok": True})
        if u.path == "/api/restrictions":                  # injury / limitation screen
            goals = read_json(GOALS, {})
            rec = goals.get(str(uid), {})
            rec["restrictions"] = data.get("restrictions", {})   # {part: ok|careful|avoid}
            goals[str(uid)] = rec
            write_json(GOALS, goals)
            return self._send({"ok": True})
        if u.path == "/api/checkin":                       # daily check-in screen
            # The date comes from the client, the clock is ours: accept today +-1 day only (midnight,
            # a late entry) and prune relative to the SERVER's today - a far-future date used to
            # delete the whole check-in history.
            today = core._today({})
            try:
                day = date.fromisoformat(str(data.get("date") or today.isoformat()))
            except ValueError:
                return self._send({"error": "bad_date"}, code=400)
            if abs((day - today).days) > 1:
                return self._send({"error": "bad_date", "detail": "check-ins are for today"}, code=400)
            goals = read_json(GOALS, {})
            rec = goals.get(str(uid), {})
            entry = {k: data[k] for k in CHECKIN_FIELDS if k in data}
            try:
                entry["rhr"] = int(entry["rhr"]) if entry.get("rhr") not in ("", None) else None
            except (TypeError, ValueError):
                entry["rhr"] = None
            cis = rec.setdefault("checkins", {})
            cis[day.isoformat()] = entry
            cutoff = (today - timedelta(days=CHECKIN_KEEP_DAYS)).isoformat()
            for d in [d for d in cis if d < cutoff]:      # keep the file small
                del cis[d]
            goals[str(uid)] = rec
            write_json(GOALS, goals)
            return self._send({"ok": True, "date": day.isoformat()})
        return self._send({"error": "not found"}, code=404)


class Server(ThreadingHTTPServer):
    daemon_threads = True                    # don't let worker threads block shutdown
    # http.server sets SO_REUSEADDR. On Windows that option lets a SECOND process bind a port that
    # is already listening - bind() never fails there, so two app instances ran side by side and
    # the takeover logic in main() was never reached. Windows gets an exclusive bind instead;
    # elsewhere SO_REUSEADDR only skips the TIME_WAIT delay, which is what we want.
    allow_reuse_address = (os.name != "nt")

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        # a browser closing the tab mid-response raises ConnectionAborted/Reset -
        # that's normal and not worth a scary traceback; only log real errors
        if issubclass(sys.exc_info()[0] or Exception, (ConnectionError, OSError)):
            return
        super().handle_error(request, client_address)


# ---- single instance: probe, take over, register ---------------------------------------------------
def probe(port: int, timeout: float = 1.5) -> dict | None:
    """Is one of OUR servers listening on this port? -> its bootstrap info (version, pid) or None."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/bootstrap?probe=1", headers={TOKEN_HEADER: "local"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
        return d if isinstance(d, dict) and "catalog" in d else None
    except Exception:
        return None


def start_decision(running_version: str, mine: str) -> str:
    """'reuse' the running app when it is the same or a newer version (a stray double-click, or an
    old Desktop shortcut after an update - never a silent downgrade), 'replace' an older one."""
    return "reuse" if vparts(running_version) >= vparts(mine) else "replace"


def request_shutdown(port: int, secret: str) -> bool:
    """Ask a running instance to quit. Versions before 0.3.1 ignore the extra fields."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/shutdown", method="POST",
            data=json.dumps({"secret": secret or ""}).encode("utf-8"),
            headers={"Content-Type": "application/json", TOKEN_HEADER: "local"})
        urllib.request.urlopen(req, timeout=2).read()
        return True
    except Exception:
        return False


def kill_process(pid: int) -> None:
    """Last resort when an older instance does not react to the shutdown request."""
    try:
        if os.name == "nt":                                # /T = with child processes, /F = force
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass


def write_instance(port: int) -> None:
    try:
        write_json(INSTANCE, {"pid": os.getpid(), "port": port, "version": STATE["version"], "folder": HERE,
                              "secret": STATE["secret"], "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
    except Exception:
        pass


def mark_stopped() -> None:
    """Clean exit: mark OUR entry in instance.json as stopped (a successor may already have
    written its own entry - that one must stay untouched)."""
    try:
        if read_json(INSTANCE, {}).get("pid") == os.getpid():
            write_json(INSTANCE, {"pid": None, "port": None, "version": STATE["version"], "folder": HERE,
                                  "stopped": time.strftime("%Y-%m-%dT%H:%M:%S")})
    except Exception:
        pass


def set_console_title(title: str) -> None:
    """Windows: name the console window, so nobody wonders what that black window is."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


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
    STATE["secret"] = secrets.token_urlsafe(16)
    set_console_title(f"ARX Insight {STATE['version']} - close this window to stop")

    def open_browser(port):
        if not args.no_browser:
            webbrowser.open(f"http://localhost:{port}")

    def try_bind(port):
        try:
            return Server(("127.0.0.1", port), Handler)
        except OSError:
            return None

    # Exactly ONE app process:
    #  * the same or a NEWER version is already running -> say so, open it, quit (a stray
    #    double-click, or an old Desktop shortcut after an update - never a silent downgrade)
    #  * an OLDER version is running (the usual case right after an update) -> ask it to quit, wait
    #    for the port, kill it as a last resort; its console window closes with it
    #  * some other program owns the port -> try the next port
    # instance.json names the port of a running instance directly; otherwise a port is probed when
    # it cannot be bound. On the very first start of a version with the exclusive bind on Windows
    # (no instance.json yet) the default port is probed BEFORE binding: an instance older than
    # 0.3.1 may hold it non-exclusively, and the probe does not depend on how Windows treats that.
    inst = read_json(INSTANCE, {})
    ports = list(range(args.port, args.port + 6))
    known = inst.get("port") if (isinstance(inst.get("port"), int) and inst.get("pid")) else None
    if known and known not in ports:
        ports.insert(0, known)

    srv, port = None, None
    for p in ports:
        running = probe(p) if (p == known or (os.name == "nt" and p == args.port and not inst)) else None
        s = None if running else try_bind(p)
        if s is None and running is None:
            running = probe(p)
            if running is None:
                continue                                   # not us -> next port
        if running is None:
            srv, port = s, p
            break
        ver = running.get("version", "?")
        if start_decision(ver, STATE["version"]) == "reuse":
            print(f"ARX Insight {ver} is already running at http://localhost:{p} - opening it.")
            print("Only one ARX Insight runs at a time. This window closes in a moment.")
            open_browser(p)
            time.sleep(2.5)
            return
        print(f"Replacing the running ARX Insight {ver} on port {p} with {STATE['version']} ...")
        request_shutdown(p, inst.get("secret") if inst.get("port") == p else "")
        for i in range(24):                                # wait up to ~6 s for the port to free
            time.sleep(0.25)
            s = try_bind(p)
            if s:
                break
        if s is None and inst.get("port") == p and inst.get("pid") and running.get("pid") == inst.get("pid"):
            print("The old instance does not react - closing it.")
            kill_process(int(inst["pid"]))
            for i in range(20):
                time.sleep(0.25)
                s = try_bind(p)
                if s:
                    break
        if s:
            srv, port = s, p
            break
        print(f"Could not take over port {p}. Close the old ARX Insight window and start again.")
        open_browser(p)                                    # couldn't take over -> open the old one
        time.sleep(4)
        return
    if srv is None:
        sys.exit("Could not find a free port for ARX Insight.")
    STATE["server"] = srv                              # so /api/shutdown can stop us
    write_instance(port)

    removed = core.sweep_stale_copies()                # DB copies of crashed runs hold private data
    if removed:
        print(f"Removed {removed} leftover temporary database copies.")
    def update_check():                                # one-off, 3 s cap - in the background, so the
        try:                                           # server answers (and can be probed) at once
            if not os.environ.get("ARX_NO_UPDATE_CHECK"):
                STATE["update"] = check_update(STATE["version"])
        finally:
            UPDATE_CHECKED.set()
    threading.Thread(target=update_check, daemon=True).start()

    url = f"http://localhost:{port}"
    print(f"ARX Insight {STATE['version']} running at {url}  (close this window to stop)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    finally:
        srv.server_close()                             # free the port at once for a successor
        mark_stopped()


if __name__ == "__main__":
    main()
