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
v0.4.0: the profile becomes a short goal interview (outcome, measurable target, time budget,
time-vs-effort profile, experience) plus focus per body region, grip aids per exercise and an
OPTIONAL body log; the plan ledger (plans.json) remembers what was recommended; one shared
database snapshot per 30 s instead of a copy per request; settings files are written atomically.
v0.5.0: the AI coach runs as a background job (/api/coach/*), remembers its boards and answers
follow-up questions in a chat (/api/chat/*); /api/report never calls the API.
v0.4.1: one-click update (POST /api/update, this machine only - see arx_update.py); the version
check repeats every few hours, so an app that runs for days still learns about a new release.
v0.7.0: profile field excluded_exercises - exercises the athlete does not do on the ARX, each with a
reason from a fixed vocabulary (clean_profile); the planner, the findings and the coach read it.
v0.6.1: phone access is ON by default (owner's decision; one click switches it off and that is
kept), starts in the background, and no click waits for Windows any more (arx_lan.system_info).
v0.6.0: phone access - a second listener in the local network (arx_lan) that always wants
a token from a QR code (arx_access: trainer link, one athlete link per person). Every route sits in
ONE table with the least role that may call it; API key, update, shutdown and the links themselves
stay PC-only. Security headers on every answer; the rendered report can be downloaded as one
script-free HTML file.
"""
from __future__ import annotations
import os, re, sys, json, time, html, shutil, socket, secrets, argparse, threading, subprocess, webbrowser
from datetime import date, timedelta
from typing import NamedTuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import urllib.request
import arx_report as core   # reuse the read-only engine
import arx_update as updater  # one-click update: download, verify, unpack, run the installer (v0.4.1)
import arx_ai as ai           # the AI coach: structured board, memory, background jobs, chat (v0.5.0)
import arx_access as access   # who may do what: trainer / athlete links, limits, download tickets (v0.6.0)
import arx_lan as lan         # the phone listener in the local network (v0.6.0; on by default since v0.6.1)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(core.data_dir(), "config.json")   # stable, survives re-download
GOALS = os.path.join(core.data_dir(), "goals.json")     # per-user: {user_id: {...}}
WEB = os.path.join(HERE, "web", "index.html")

CHECKIN_KEEP_DAYS = 60        # daily check-ins older than this are pruned from goals.json
CHECKIN_FIELDS = ("sleep", "energy", "soreness", "rhr", "pain", "note")
CHECKIN_CHOICES = {"sleep": ("poor", "ok", "good"), "energy": ("low", "ok", "high")}
SORENESS_LEVELS = ("none", "mild", "strong")
BODY_PARTS = ("shoulder", "elbow", "wrist", "knee", "hip", "lower_back", "neck")     # pain today / restrictions


def clean_checkin(data: dict) -> dict:
    """The answers of one check-in, reduced to the known vocabulary (since v0.6.0 they may come from
    a phone): unknown values are dropped, never stored. Only fields that were sent are returned."""
    entry = {}
    for k, allowed in CHECKIN_CHOICES.items():
        if k in data:
            entry[k] = data[k] if data[k] in allowed else None
    if "soreness" in data:
        sore = data.get("soreness") if isinstance(data.get("soreness"), dict) else {}
        entry["soreness"] = {r: l for r, l in sore.items() if r in core.SORENESS_REGIONS and l in SORENESS_LEVELS}
    if "pain" in data:
        entry["pain"] = [p for p in (data.get("pain") if isinstance(data.get("pain"), list) else []) if p in BODY_PARTS]
    if "note" in data:
        entry["note"] = str(data.get("note") or "")[:300] or None       # stays on this machine (never part of the AI payload)
    if "rhr" in data:
        try:
            rhr = int(data["rhr"]) if data.get("rhr") not in ("", None) else None
        except (TypeError, ValueError):
            rhr = None
        entry["rhr"] = rhr if rhr is not None and 25 <= rhr <= 220 else None
    return entry

# ---- request safety -----------------------------------------------------------------------------
# Every /api call must carry this header. A web page from another origin cannot add a custom header
# without a CORS preflight, and this server never grants one - so no foreign page can read data,
# write settings or trigger a billed AI call. Value "local" on this machine; on the phone listener
# (v0.6.0) it carries the access token. /api/bootstrap stays open ON THIS MACHINE: it holds no
# personal data and older app versions probe it to find a running instance.
TOKEN_HEADER = "X-ARX-Token"              # (which routes are open is part of the route table below: perm "open")
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

STATE = {"db": None, "catalog": {}, "version": "0.0.0", "update": {}, "server": None, "secret": "", "update_checked": 0.0, "port": None}
LAN = None                      # arx_lan.LanManager - the phone listener, created in main()
UPDATE_JOB = updater.UpdateJob()
UPDATE_RECHECK_S = 6 * 3600     # a kiosk PC runs for days: look for a new version now and then


def refresh_update_info(force: bool = False) -> None:
    """Re-run the (3 s, best-effort) version check in the background when the last one is old."""
    if os.environ.get("ARX_NO_UPDATE_CHECK") and not force:
        return
    if not force and time.time() - STATE.get("update_checked", 0.0) < UPDATE_RECHECK_S:
        return
    STATE["update_checked"] = time.time()

    def run():
        STATE["update"] = check_update(STATE["version"])
    threading.Thread(target=run, daemon=True).start()
UPDATE_CHECKED = threading.Event()   # set once the background update check has finished


# ---- small JSON file helpers -------------------------------------------------
def read_json(path, default):
    try:
        # utf-8-sig tolerates a BOM (Windows PowerShell writes config.json with one)
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


JSON_LOCK = threading.RLock()   # settings / goals / ledger: read-modify-write is one step


def write_json(path, data):
    core.write_json_atomic(path, data)             # temp file + rename: never half a file


def update_json(path, change):
    """Read-modify-write under the lock: change(data) edits the dict in place (two requests at the
    same moment - a check-in and a profile save - used to be able to lose one of the writes)."""
    with JSON_LOCK:
        data = read_json(path, {})
        result = change(data)
        write_json(path, data)
    return result


# ---- database queries --------------------------------------------------------
def search_users(query: str) -> list[dict]:
    """Substring search on first/last name, like the ARX picker."""
    with core.shared_connection(STATE["db"]) as con:
        cur = con.cursor()
        cur.execute('''select id, trim(firstname), trim(lastname), gender, birthdate, createdate
                       from "User" where deleted is false order by lastname, firstname''')
        rows = cur.fetchall()
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


# ---- profile (goal interview), body log --------------------------------------------------------------------
PLANS = os.path.join(core.data_dir(), "plans.json")    # plan ledger: {user_id: [entries]} (see arx_plan)
FOCUS_LEVELS = ("more", "normal", "less", "off")
REGION_KEYS = ("legs", "back", "chest", "shoulders", "arms")
OUTCOMES = ("strength", "muscle", "body_composition", "health", "performance", "maintain")
EXPERIENCE = ("new", "some", "experienced")
AID_KINDS = ("hooks", "straps")
TARGET_KINDS = ("force", "body_weight", "waist")
BODY_LIMITS = {"weight_kg": (20, 300), "arm_cm": (10, 80), "chest_cm": (40, 200), "waist_cm": (30, 250),
               "thigh_cm": (20, 120), "fat_pct": (2, 70)}
BODY_KEEP = 400                 # entries kept per person
PROFILE_KEYS = ("focus_regions", "session_minutes", "commitment", "outcome", "experience", "target", "aids", "structure",
                "next_groups", "excluded_exercises")


def _iso_day(value, earliest: str = "2000-01-01", latest: date | None = None) -> str | None:
    try:
        day = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    if day.isoformat() < earliest or (latest and day > latest):
        return None
    return day.isoformat()


def clean_profile(data: dict, catalog: dict, today: date) -> dict:
    """The goal-interview fields of a POST /api/goal body, validated. A key that is present but
    empty / invalid comes back as None = "remove it" (back to the engine's own choice); keys that
    are absent are not touched. Nothing here is free text - it may reach the AI later."""
    out = {}
    if "focus_regions" in data:
        fr = data.get("focus_regions") or {}
        out["focus_regions"] = {r: l for r, l in fr.items() if r in REGION_KEYS and l in FOCUS_LEVELS and l != "normal"} or None
    if "session_minutes" in data:
        try:
            m = int(data["session_minutes"])
            out["session_minutes"] = m if 10 <= m <= 90 else None
        except (TypeError, ValueError):
            out["session_minutes"] = None                                # "auto"
    if "commitment" in data:
        out["commitment"] = data["commitment"] if data["commitment"] in core.planner.COMMITMENT else None
    if "structure" in data:                        # how sessions are built: auto | full_body | split ("auto" = not stored)
        out["structure"] = data["structure"] if data["structure"] in ("full_body", "split") else None
    if "outcome" in data:
        out["outcome"] = data["outcome"] if data["outcome"] in OUTCOMES else None
    if "experience" in data:
        out["experience"] = data["experience"] if data["experience"] in EXPERIENCE else None
    if "target" in data:
        t, names = data.get("target") or {}, {m.get("name") for m in catalog.values()}
        try:
            value = float(t.get("value"))
        except (TypeError, ValueError):
            value = None
        ok = t.get("kind") in TARGET_KINDS and value is not None and 0 < value < 2000 \
            and (t["kind"] != "force" or t.get("exercise") in names)
        out["target"] = ({"kind": t["kind"], "exercise": t.get("exercise") if t["kind"] == "force" else None, "value": round(value, 1),
                          "date": _iso_day(t.get("date"), today.isoformat()), "set_on": today.isoformat()} if ok else None)
    if "aids" in data:
        aids = {}
        for code, a in (data.get("aids") or {}).items():
            allowed = (catalog.get(str(code)) or {}).get("aids") or []
            kinds = [k for k in (a or {}).get("aids") or [] if k in AID_KINDS and k in allowed]
            if kinds:
                aids[str(code)] = {"aids": kinds[:1], "since": _iso_day((a or {}).get("since"), latest=today) or today.isoformat()}
        out["aids"] = aids or None
    if "excluded_exercises" in data:               # exercises the athlete does not do on the ARX (v0.7.0): {code: elsewhere | unwanted}
        raw = data.get("excluded_exercises") if isinstance(data.get("excluded_exercises"), dict) else {}
        off = {str(code): reason for code, reason in raw.items()
               if str(code) in catalog and isinstance(reason, str) and reason in core.planner.EXCLUDE_REASONS}
        out["excluded_exercises"] = off or None
    return out


def clean_body_entry(data: dict, today: date) -> tuple[str | None, dict]:
    """(date, values) of one body-log entry: every value optional, each within a sane range;
    no valid value at all = delete that day's entry."""
    day = _iso_day(data.get("date") or today.isoformat(), latest=today + timedelta(days=1))
    values = {}
    for k, (lo, hi) in BODY_LIMITS.items():
        try:
            v = float(str(data.get(k)).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if lo <= v <= hi:
            values[k] = round(v, 1)
    return day, values


# ---- report -------------------------------------------------------------------------------------------------
REPORT_CACHE: dict = {}         # key -> report (without AI fields); a handful of entries
REPORT_CACHE_KEEP = 6


def _report_key(con, cfg: dict) -> str:
    """Everything a report depends on: the person's recorded sets (count / newest id / newest
    change), every setting that feeds the engine, today and the app version."""
    import hashlib
    cur = con.cursor()
    cur.execute('select count(*), max(id), max(lastupdate) from "ExerciseSet" where user_id = ?', (cfg["user_id"],))
    sig = [str(x) for x in (cur.fetchone() or ())]
    basis = {k: v for k, v in cfg.items() if k not in ("_catalog", "anthropic_api_key")}
    blob = json.dumps([sig, basis, STATE.get("version"), core._today({}).isoformat()], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def report_cfg(user_id: int) -> tuple[dict, dict]:
    """(cfg, user info) - everything the engine and the coach need to know about one person."""
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
    cfg["focus"] = urec.get("focus", {})                   # group -> more/normal/less/off (pre-0.4 profiles)
    cfg["approach"] = urec.get("approach", "auto")         # full | split | auto
    for k in PROFILE_KEYS:                                 # goal interview, focus per region, grip aids
        if urec.get(k) is not None:
            cfg[k] = urec[k]
    cfg["body_log"] = urec.get("body_log") or []           # optional; [] = nothing is shown anywhere
    cfg["_plan_ledger"] = (read_json(PLANS, {}) or {}).get(str(user_id), [])
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
    return cfg, info


def make_report(user_id: int, cfg_info: tuple | None = None) -> dict:
    """The report of one person. Never calls the AI (see coach_state) - so it is fast and free."""
    cfg, info = cfg_info or report_cfg(user_id)
    with core.shared_connection(STATE["db"]) as con:
        key = _report_key(con, cfg)
        report = REPORT_CACHE.get(key)
        if report is None:
            report = core.build_report(con, cfg)
            report["created"] = info["created"]
            report["user"] = info
            REPORT_CACHE[key] = report
            for old in list(REPORT_CACHE)[:-REPORT_CACHE_KEEP]:
                REPORT_CACHE.pop(old, None)
    remember_plan(user_id, report)
    return report


def remember_plan(user_id: int, report: dict) -> None:
    """Plan ledger: store the recommendation when it differs from the last one stored, so the next
    report can say what was done with it (plan_vs_actual) - and, later, the coach stays consistent."""
    try:
        today = date.fromisoformat(report["today"])

        def change(ledger):
            entries, changed = core.planner.update_ledger(ledger.get(str(user_id), []), report.get("plan"), today)
            if changed:
                ledger[str(user_id)] = entries
            return changed
        with JSON_LOCK:
            ledger = read_json(PLANS, {})
            if change(ledger):
                write_json(PLANS, ledger)
    except Exception as e:                         # the ledger must never cost the athlete the report
        print(f"plan ledger not updated: {type(e).__name__}: {e}", file=sys.stderr)


# ---- AI coach: board as a background job, memory, chat -------------------------------------------------
BOARDS, JOBS, CHATS = ai.BoardStore(), ai.JobManager(), ai.ChatManager()


def coach_context(user_id: int) -> dict:
    """Report + payload + key for one person: what a board depends on and what the chat talks about."""
    cfg, info = report_cfg(user_id)
    report = make_report(user_id, (cfg, info))
    earlier = [r for r in BOARDS.all(user_id)]
    model, effort = ai.model_of(cfg), ai.effort_of(cfg, "ai_effort_board", ai.BOARD_EFFORT_DEFAULT)
    payload = ai.build_payload(report, cfg, None)
    key = ai.payload_key(payload, model, effort)
    previous = ai.memory_of([r for r in earlier if r.get("key") != key])
    return {"cfg": cfg, "info": info, "report": report, "key": key, "previous": previous}


def coach_state(user_id: int, start: bool = False, allow=None) -> dict:
    """{state: no_key | none | running | done | error | limit, record?, error?}. start=True begins the
    job when there is no board for this data state yet (two devices asking get the same job).
    allow() is asked right before a NEW billed job starts (the daily allowance of an athlete link)."""
    ctx = coach_context(user_id)
    cfg, key = ctx["cfg"], ctx["key"]
    if not ai.has_key(cfg):
        return {"state": "no_key"}
    rec = BOARDS.get(user_id, key)
    if rec:
        return {"state": "done", "record": rec}
    job = JOBS.status(user_id, key)
    if job and job["state"] == "running":
        return {"state": "running", "since": round(time.time() - job["started"])}
    if job and job["state"] == "error" and not start:
        return {"state": "error", "error": job["error"]}
    if not start:
        return {"state": "none"}
    if BOARDS.made_today(user_id) >= ai.BOARDS_PER_DAY:
        return {"state": "limit", "error": {"code": "daily_limit", "message": f"{ai.BOARDS_PER_DAY} boards a day"}}
    if allow is not None and not allow():
        return {"state": "limit", "error": {"code": "link_limit", "message": f"{access.ATHLETE_BOARDS_PER_DAY} boards a day on this link"}}

    def work():
        BOARDS.put(user_id, ai.make_board(ctx["report"], cfg, ctx["previous"]))
    JOBS.start(user_id, key, work)
    return {"state": "running", "since": 0}


# ---- what an athlete link gets to see, and the report as a file -------------------------------------------------
def first_name(name: str | None) -> str:
    return (str(name or "").split() or [""])[0]


def own_view(report: dict) -> dict:
    """The report for the person's own phone: the first name is enough there, and the birth date
    has no business on the Wi-Fi. A shallow copy - the cached report keeps everything."""
    out = dict(report)
    out["athlete_alias"] = first_name(report.get("athlete_alias"))
    user = report.get("user") or {}
    out["user"] = {"id": user.get("id"), "name": first_name(user.get("name")), "created": user.get("created")}
    return out


_PAGE_CSS: dict = {}


def page_css() -> str:
    """The <style> blocks of the app page (read once per file version) - the snapshot wears the same design."""
    try:
        stamp = os.path.getmtime(WEB)
        if _PAGE_CSS.get("stamp") != stamp:
            with open(WEB, encoding="utf-8") as f:
                _PAGE_CSS.update(stamp=stamp, css="\n".join(re.findall(r"<style[^>]*>(.*?)</style>", f.read(), flags=re.S)))
    except OSError:
        return ""
    return _PAGE_CSS.get("css", "")


def snapshot_document(markup: str, title: str, language=None) -> str:
    """One static HTML file from the report markup the page sent. Nothing in it can run: script
    elements are cut out AND the document's own policy forbids every script, handler and request
    (a file opened from the phone's Files app has no server headers to rely on)."""
    markup = re.sub(r"<script\b.*?</script\s*>", "", markup, flags=re.S | re.I)
    markup = re.sub(r"<(iframe|object|embed|link|meta|base|form)\b[^>]*>", "", markup, flags=re.I)
    lang = "de" if language == "de" else "en"
    head = html.escape(title or "ARX Insight")
    hide = (".seg,.banner .btn,.chapnav,#chatFab,#chat,.coach .btn,button,input,select,textarea{display:none!important}"
            ".screen{display:block!important}body{padding-bottom:24px}.snaphead{max-width:1180px;margin:0 auto;padding:18px 22px 0;"
            "font-family:Oswald,'Arial Narrow',sans-serif;font-size:1.3rem;letter-spacing:.04em;text-transform:uppercase}")
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP_SNAPSHOT}">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">'
            f'<title>{head}</title><style>{page_css()}{hide}</style></head><body>'
            f'<div class="snaphead">{head}</div><div class="screen active" id="report"><main class="wrap" id="r_body">{markup}</main></div>'
            f'</body></html>')


# ---- access: who is asking, and may they? (v0.6.0) ----------------------------------------------------------
# The route table IS the security model. Every route names the least role that may call it:
#   open     no token (only /api/bootstrap on the loopback listener - older versions probe it)
#   athlete  a phone with an athlete link, and everyone above; own_user routes are pinned to that
#            link's own user_id (any other id is answered 403, whether it exists or not)
#   trainer  a phone with the trainer link, and this machine
#   local    this machine only, whatever token a phone shows: API key, update, shutdown, phone
#            access and its links, the firewall helper
# Trust comes from the LISTENER that accepted the connection (loopback = this machine, LAN = token
# required), never from a header a client could set. See arx_access / arx_lan.
ACCESS, BAD_TOKENS, TICKETS, SEEN = access.AccessStore(), access.BadTokens(), access.Tickets(), access.Seen()
MAX_SNAPSHOT = 6 * 1024 * 1024                    # a rendered report with its charts, sent back as a download
MAX_DRAIN = 16 * 1024 * 1024                      # an oversized body up to this size is read and dropped before the 413
CSP_PAGE = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
CSP_SNAPSHOT = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"      # a downloaded report runs no script at all
STATIC = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
          "/vendor/qrcode.js": (os.path.join("vendor", "qrcode.js"), "application/javascript")}


class Route(NamedTuple):
    perm: str                       # open | athlete | trainer | local (arx_access.RANK)
    own_user: bool                  # the request names a person (user_id)
    max_body: int                   # POST body limit
    fn: object


ROUTES: dict = {}


def route(method: str, path: str, perm: str, own_user: bool = False, max_body: int = MAX_BODY):
    def deco(fn):
        ROUTES[(method, path)] = Route(perm, own_user, max_body, fn)
        return fn
    return deco


def q1(q: dict, key: str, default=None):
    """First value of a query-string parameter."""
    return (q.get(key) or [default])[0]


class Handler(BaseHTTPRequestHandler):
    timeout = 60                          # a connection that sends or reads nothing for a minute is closed

    def log_message(self, *a):            # keep the console quiet
        pass

    @property
    def on_lan(self) -> bool:
        """Did the phone listener accept this connection? (set on the server object by make_lan_server)"""
        return getattr(self.server, "kind", "local") == "lan"

    def _send(self, obj, code=200, ctype="application/json", headers=None):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if any(t in ctype for t in ("json", "html", "javascript")) else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-cache" if "javascript" in ctype else "no-store")
            if "html" in ctype and not (headers or {}).get("Content-Security-Policy"):
                self.send_header("Content-Security-Policy", CSP_PAGE)
                self.send_header("X-Frame-Options", "DENY")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, OSError):
            # the browser navigated away / closed the tab mid-response (common while
            # the multi-second AI request is in flight) - harmless, ignore quietly
            pass

    # -- request checks (see TOKEN_HEADER / LOCAL_HOSTS / MAX_BODY above) ---------------------
    def _loopback(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _host_ok(self) -> bool:
        """Host allow-list per listener: stops DNS-rebinding pages (a foreign site that resolves to
        this machine / this LAN address cannot use the app through the visitor's browser)."""
        host = (self.headers.get("Host") or "").strip().lower()
        allowed = getattr(self.server, "allowed_hosts", LOCAL_HOSTS)
        name = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.rsplit(":", 1)[0]
        if (host or self.on_lan) and name not in allowed:       # browsers always send it; the phone listener insists
            self._send({"error": "misdirected", "detail": "unknown Host"}, code=421)
            return False
        return True

    def _who(self, path: str):
        """The caller's identity, or None after having answered 401 / 403 / 429."""
        token = self.headers.get(TOKEN_HEADER)
        if not self.on_lan:
            if not self._loopback():                           # the local listener is bound to 127.0.0.1 - belt and braces
                self._send({"error": "forbidden"}, code=403)
                return None
            if not token:
                self._send({"error": "forbidden", "detail": f"missing {TOKEN_HEADER} header"}, code=403)
                return None
            return access.LOCAL
        addr = self.client_address[0]
        if BAD_TOKENS.blocked(addr):
            self._send({"error": "too_many_attempts"}, code=429)
            return None
        who = ACCESS.identify(token)
        if who is None:                                        # unknown, expired, revoked: all answered alike
            if token:                                          # a WRONG token counts; a page opened without one does not
                BAD_TOKENS.fail(addr)
            self._send({"error": "token_invalid"}, code=401)
            return None
        SEEN.touch(addr, who, self.headers.get("User-Agent"))
        return who

    def _read_json(self, limit: int = MAX_BODY) -> dict | None:
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
        if length < 0 or length > limit:
            # read (and drop) a moderately oversized body first: a browser that is still sending when the
            # connection closes reports a network error instead of showing our answer
            left = length if 0 < length <= MAX_DRAIN else 0
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
            self._send({"error": "bad_length"}, code=413 if length > limit else 411)
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
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        """Host -> route -> identity -> permission -> body -> own user -> handler. One path for
        every request, so no route can forget a check."""
        u = urlparse(self.path)
        if not self._host_ok():
            return
        if method == "GET" and u.path in STATIC:               # the page itself and the vendored QR encoder: no data
            return self._static(u.path)
        if method == "GET" and u.path.startswith("/dl/"):      # a one-time ticket is its own key
            return self._download(u.path[4:])
        r = ROUTES.get((method, u.path))
        if r is None:
            return self._send({"error": "not found"}, code=404)
        who = access.LOCAL if (r.perm == "open" and not self.on_lan) else self._who(u.path)
        if who is None:
            return
        if not who.allows(r.perm):
            return self._send({"error": "forbidden", "detail": "loopback_only" if r.perm == "local" else "not_allowed"}, code=403)
        if method == "POST":
            args = self._read_json(r.max_body)
            if args is None:
                return
        else:
            args = parse_qs(u.query)
        uid = None
        if r.own_user:
            uid = self._uid(args.get("user_id") if method == "POST" else q1(args, "user_id"))
            if uid is None:
                return self._send({"error": "bad_user_id"}, code=400)
            if who.role == "athlete" and uid != who.user_id:   # the same answer for every other id, existing or not
                return self._send({"error": "forbidden", "detail": "not_allowed"}, code=403)
        try:
            r.fn(self, args, who, uid)
        except Exception as e:                                 # never a traceback page, never a hang
            print(f"{method} {u.path} failed: {type(e).__name__}: {e}", file=sys.stderr)
            detail = str(e)[:300] if who.role == "local" else ""        # paths and the like stay on this machine
            self._send({"error": type(e).__name__, "detail": detail}, code=500)

    def _static(self, path: str):
        rel, ctype = STATIC[path]
        try:
            with open(os.path.join(HERE, "web", rel), "rb") as f:
                return self._send(f.read(), ctype=ctype)
        except OSError:
            return self._send({"error": "not found"}, code=404)

    def _download(self, ticket: str):
        item = TICKETS.redeem(ticket)
        if not item:
            return self._send({"error": "not found"}, code=404)
        body, filename, ctype = item
        self._send(body, ctype=ctype, headers={"Content-Disposition": f'attachment; filename="{filename}"',
                                               "Content-Security-Policy": CSP_SNAPSHOT + "; sandbox"})


# ---- routes: everyone ------------------------------------------------------------------------------------------
@route("GET", "/api/bootstrap", "open")
def r_bootstrap(h, q, who, uid):
    # Our own page waits a moment for the update check (so the banner is there on the first
    # paint); a probe by another instance - "?probe=1", or an older version without the
    # header - is answered at once, otherwise it would take us for a foreign program.
    local = who.role == "local"
    if local and h.headers.get(TOKEN_HEADER) and "probe" not in q:
        UPDATE_CHECKED.wait(2.5)
    cfg = read_json(CONFIG, {})
    if local:
        refresh_update_info()
    out = {
        "configured": bool(cfg.get("anthropic_api_key") or cfg.get("units") or cfg.get("language")) or not local,
        "language": cfg.get("language", "en"),      # US-first defaults
        "units": cfg.get("units", "imperial"),
        "sessions_per_week": cfg.get("sessions_per_week", 2),
        "has_key": bool(cfg.get("anthropic_api_key")) or bool(os.environ.get("ARX_AI_FAKE")),
        "ai": {"models": [list(m) for m in ai.MODELS], "model": ai.model_of(cfg), "efforts": list(ai.EFFORTS),
               "effort_board": ai.effort_of(cfg, "ai_effort_board", ai.BOARD_EFFORT_DEFAULT),
               "effort_chat": ai.effort_of(cfg, "ai_effort_chat", ai.CHAT_EFFORT_DEFAULT),
               "auto": bool(cfg.get("ai_auto", True)), "fake": bool(os.environ.get("ARX_AI_FAKE"))},
        "ai_share_profile": bool(cfg.get("ai_share_profile", True)),   # age band + sex (never a name)
        "ai_share_body": bool(cfg.get("ai_share_body", False)),        # relative body changes only
        "catalog": STATE["catalog"],
        "version": STATE["version"],
        "update": STATE["update"] if who.role != "athlete" else {},
        "access": {"role": who.role},               # what this device may do (the page hides the rest)
    }
    if local:
        out.update({
            "self_update": os.name == "nt" or bool(os.environ.get("ARX_UPDATE_ANYWHERE")),   # one-click update possible here
            "taskbar_pinned": cfg.get("taskbar_pinned"),    # set by the Windows installer; False -> one-time hint
            "pid": os.getpid(),                             # lets a newer instance verify whom it replaces
            "phone": {"enabled": bool(ACCESS.lan().get("enabled")) and not os.environ.get("ARX_NO_PHONE"),
                      "running": bool(LAN and LAN.status()["running"])},
        })
    elif who.role == "athlete":
        out["access"].update({"user_id": who.user_id, "name": first_name(user_info(who.user_id).get("name")), "expires": who.expires,
                              "chat": who.chat, "ai_left": {k: ACCESS.left(who.user_id, k) for k in ("boards", "questions")}})
    h._send(out)


@route("GET", "/api/science", "athlete")
def r_science(h, q, who, uid):                          # the evidence behind the planner's defaults
    h._send(read_json(os.path.join(HERE, "science.json"), {"topics": {}}))


# ---- routes: one person (an athlete link reaches only its own) ---------------------------------------------------
@route("GET", "/api/goal", "athlete", own_user=True)
def r_goal(h, q, who, uid):
    h._send(read_json(GOALS, {}).get(str(uid), {}))     # {} => not defined yet


@route("GET", "/api/checkin", "athlete", own_user=True)
def r_checkin(h, q, who, uid):                          # today's (or a given day's) check-in
    day = _iso_day(q1(q, "date", core._today({}).isoformat())) or core._today({}).isoformat()
    h._send(checkin_payload(read_json(GOALS, {}).get(str(uid), {}), day))


@route("GET", "/api/report", "athlete", own_user=True)
def r_report(h, q, who, uid):
    report = make_report(uid)                           # never calls the AI (see /api/coach/*)
    h._send(own_view(report) if who.role == "athlete" else report)


@route("GET", "/api/coach/status", "athlete", own_user=True)
def r_coach_status(h, q, who, uid):                     # board for the current data state: none | running | done | error
    h._send(coach_state(uid))


@route("GET", "/api/chat/poll", "athlete", own_user=True)
def r_chat_poll(h, q, who, uid):                        # incremental answer text of one turn (survives a locked phone)
    try:
        start = max(0, int(q1(q, "from", "0")))
    except ValueError:
        start = 0
    h._send(CHATS.poll(uid, q1(q, "turn", ""), start))


@route("GET", "/api/chat/history", "athlete", own_user=True)
def r_chat_history(h, q, who, uid):
    st = coach_state(uid)                               # the chat belongs to the delivered board
    if st["state"] != "done":
        return h._send({"turns": [], "left": 0, "board": False})
    out = dict(CHATS.history(uid, st["record"]["key"]), board=True,
               suggested=(st["record"].get("board") or {}).get("suggested_questions", []))
    if who.role == "athlete":                           # the link's own, smaller allowance
        out["left"] = min(out["left"], ACCESS.left(uid, "questions")) if who.chat else 0
        out["chat_off"] = not who.chat
    h._send(out)


@route("POST", "/api/goal", "athlete", own_user=True)
def p_goal(h, data, who, uid):                          # per-user profile / goal interview
    profile = clean_profile(data, STATE["catalog"], core._today({}))
    try:                                                # the planner divides by it - keep it a sane number
        spw = max(1, min(7, int(data.get("sessions_per_week", 2))))
    except (TypeError, ValueError):
        spw = 2
    goal = {k: max(0.0, min(1.0, float(v))) for k, v in (data.get("goal") or {}).items()
            if k in ("muscle", "strength", "conditioning") and isinstance(v, (int, float)) and not isinstance(v, bool)}

    def change(goals):
        rec = goals.get(str(uid), {})
        rec["goal"] = goal
        rec["sessions_per_week"] = spw
        if isinstance(data.get("focus"), dict):                    # group -> more/normal/less/off (pre-0.4)
            rec["focus"] = {str(g)[:12]: l for g, l in data["focus"].items() if l in FOCUS_LEVELS}
        if data.get("approach") in ("full", "split", "auto"): rec["approach"] = data["approach"]
        if "language" in data:                                     # "" clears -> device default
            if data["language"] in ("en", "de"): rec["language"] = data["language"]
            else: rec.pop("language", None)
        for opt, (lo, hi) in (("height_cm", (80, 250)), ("weight_kg", BODY_LIMITS["weight_kg"])):   # optional profile fields
            try:
                if opt in data and data[opt] not in ("", None) and lo <= float(data[opt]) <= hi:
                    rec[opt] = round(float(data[opt]), 1)
            except (TypeError, ValueError):
                pass
        if isinstance(data.get("notes"), str) and data["notes"].strip():
            rec["notes"] = data["notes"].strip()[:500]            # free text: stays on this machine
        for k, v in profile.items():                               # None = back to the engine's choice
            if v is None: rec.pop(k, None)
            else: rec[k] = v
        goals[str(uid)] = rec                      # keep any restrictions
    update_json(GOALS, change)
    h._send({"ok": True})


@route("POST", "/api/coach/start", "athlete", own_user=True)
def p_coach_start(h, data, who, uid):                   # begin the board job (billed call) - idempotent per data state
    allow = (lambda: ACCESS.use(uid, "boards")) if who.role == "athlete" else None
    h._send(coach_state(uid, start=True, allow=allow))


@route("POST", "/api/chat/send", "athlete", own_user=True)
def p_chat_send(h, data, who, uid):                     # one question to the coach about THIS report
    if who.role == "athlete":
        if not who.chat:                                # a minor's link: the trainer switches the chat on
            return h._send({"error": "chat_off", "detail": "the chat is switched off for this link"}, code=403)
        if ACCESS.left(uid, "questions") <= 0:
            return h._send({"error": "link_limit", "detail": f"{access.ATHLETE_QUESTIONS_PER_DAY} questions a day on this link"}, code=429)
    st = coach_state(uid)
    if st["state"] != "done":
        return h._send({"error": "no_board", "detail": "the coach board comes first"}, code=409)
    ctx = coach_context(uid)
    payload_text = ai.dumps_payload(ai.build_payload(ctx["report"], ctx["cfg"], ctx["previous"]))
    try:
        out = CHATS.send(uid, data.get("text"), data.get("client_msg_id"), cfg=ctx["cfg"], payload_text=payload_text,
                         board_record=st["record"], names=[ctx["info"].get("name") or ""])
    except ai.AIError as err:
        return h._send({"error": err.code, "detail": err.message}, code=429 if "limit" in err.code else 400)
    if who.role == "athlete" and out.get("new"):
        ACCESS.use(uid, "questions")
    h._send(dict(out, ok=True))


@route("POST", "/api/next_groups", "athlete", own_user=True)
def p_next_groups(h, data, who, uid):                   # "next session only these movement groups" - used up by the next training
    known = {m.get("group") for m in STATE["catalog"].values()} - {None, "?"}
    groups = sorted({g for g in (data.get("groups") or []) if g in known})

    def change(goals):
        rec = goals.get(str(uid), {})
        if groups: rec["next_groups"] = {"groups": groups, "set_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        else: rec.pop("next_groups", None)
        goals[str(uid)] = rec
    update_json(GOALS, change)
    h._send({"ok": True, "groups": groups})


@route("POST", "/api/body", "athlete", own_user=True)
def p_body(h, data, who, uid):                          # OPTIONAL body log: one entry per day, any subset of values
    day, values = clean_body_entry(data, core._today({}))
    if day is None:
        return h._send({"error": "bad_date"}, code=400)

    def change(goals):
        rec = goals.get(str(uid), {})
        log = [e for e in rec.get("body_log") or [] if e.get("date") != day]
        if values:
            log.append(dict(values, date=day))
        rec["body_log"] = sorted(log, key=lambda e: e["date"])[-BODY_KEEP:]
        goals[str(uid)] = rec
        return len(rec["body_log"])
    h._send({"ok": True, "date": day, "saved": bool(values), "entries": update_json(GOALS, change)})


@route("POST", "/api/restrictions", "athlete", own_user=True)
def p_restrictions(h, data, who, uid):                  # injury / limitation screen
    levels = {k: v for k, v in (data.get("restrictions") or {}).items() if k in BODY_PARTS and v in ("ok", "careful", "avoid")}

    def change(goals):
        rec = goals.get(str(uid), {})
        rec["restrictions"] = levels                    # {part: ok|careful|avoid}
        goals[str(uid)] = rec
    update_json(GOALS, change)
    h._send({"ok": True})


@route("POST", "/api/checkin", "athlete", own_user=True)
def p_checkin(h, data, who, uid):                       # daily check-in screen
    # The date comes from the client, the clock is ours: accept today +-1 day only (midnight,
    # a late entry) and prune relative to the SERVER's today - a far-future date used to
    # delete the whole check-in history.
    today = core._today({})
    try:
        day = date.fromisoformat(str(data.get("date") or today.isoformat()))
    except ValueError:
        return h._send({"error": "bad_date"}, code=400)
    if abs((day - today).days) > 1:
        return h._send({"error": "bad_date", "detail": "check-ins are for today"}, code=400)
    entry = clean_checkin(data)

    def change(goals):
        rec = goals.get(str(uid), {})
        cis = rec.setdefault("checkins", {})
        cis[day.isoformat()] = entry
        cutoff = (today - timedelta(days=CHECKIN_KEEP_DAYS)).isoformat()
        for d in [d for d in cis if d < cutoff]:      # keep the file small
            del cis[d]
        goals[str(uid)] = rec
    update_json(GOALS, change)
    h._send({"ok": True, "date": day.isoformat()})


@route("POST", "/api/export/snapshot", "athlete", own_user=True, max_body=MAX_SNAPSHOT)
def p_snapshot(h, data, who, uid):
    """The rendered report as ONE static file: the page sends its report markup, the server wraps it
    with the app's own CSS and a policy that forbids every script, and hands back a one-time
    download URL (a script-made download does not reach the Files app on an iPhone reliably).
    The file name carries the date, never a name."""
    markup = data.get("html")
    if not isinstance(markup, str) or not markup.strip():
        return h._send({"error": "bad_request"}, code=400)
    doc = snapshot_document(markup, str(data.get("title") or "")[:80], data.get("language"))
    ticket = TICKETS.issue(doc.encode("utf-8"), f"arx-report-{core._today({}).isoformat()}.html", "text/html")
    h._send({"ok": True, "url": f"/dl/{ticket}", "seconds": access.TICKET_TTL_S})


# ---- routes: trainer -----------------------------------------------------------------------------------------------
@route("GET", "/api/users", "trainer")
def r_users(h, q, who, uid):
    rows = search_users(q1(q, "q", ""))
    if who.role != "local":                             # over the network: the year is enough to tell two people apart
        rows = [dict(u, birthdate=(u.get("birthdate") or "")[:4] or None) for u in rows]
    h._send(rows)


@route("POST", "/api/config", "trainer")
def p_config(h, data, who, uid):                        # global setup screen
    if data.get("anthropic_api_key") and who.role != "local":
        return h._send({"error": "forbidden", "detail": "loopback_only"}, code=403)    # the key is typed at the PC, never sent through the Wi-Fi

    def change(cfg):
        if data.get("language") in ("en", "de"):
            cfg["language"] = data["language"]
        if data.get("units") in ("imperial", "metric"):
            cfg["units"] = data["units"]
        if "sessions_per_week" in data and data["sessions_per_week"] != "":
            try:
                cfg["sessions_per_week"] = max(1, min(7, int(data["sessions_per_week"])))
            except (TypeError, ValueError):
                pass
        if isinstance(data.get("anthropic_api_key"), str) and data["anthropic_api_key"].strip():
            cfg["anthropic_api_key"] = data["anthropic_api_key"].strip()[:300]
        if data.get("model") in dict(ai.MODELS):               # only models the coach is written for
            cfg["model"] = data["model"]
        for k in ("ai_effort_board", "ai_effort_chat"):
            if data.get(k) in ai.EFFORTS:
                cfg[k] = data[k]
        if "ai_auto" in data:
            cfg["ai_auto"] = bool(data["ai_auto"])
        for k in ("ai_share_profile", "ai_share_body"):        # what the AI may see (name-free either way)
            if k in data:
                cfg[k] = bool(data[k])
    update_json(CONFIG, change)
    h._send({"ok": True})


# ---- routes: this machine only ---------------------------------------------------------------------------------------
@route("GET", "/api/update/status", "local")
def r_update_status(h, q, who, uid):                    # progress of a one-click update
    h._send(UPDATE_JOB.status())


@route("POST", "/api/update", "local")
def p_update(h, data, who, uid):                        # one-click update: download, verify, unpack, run the installer
    # code is only ever fetched after a click on THIS machine - never from a phone or another PC
    started = UPDATE_JOB.start(core.data_dir(), STATE["version"], HERE)
    h._send({"ok": True, "started": started, **UPDATE_JOB.status()})


@route("POST", "/api/shutdown", "local")
def p_shutdown(h, data, who, uid):                      # a newer instance asks us to quit
    # only from this machine and only with the secret from instance.json - a web page or
    # another device must never be able to stop the app
    if not h._loopback() or not secrets.compare_digest(str(data.get("secret") or ""), STATE["secret"] or "-"):
        return h._send({"error": "forbidden"}, code=403)
    srv = STATE.get("server")
    if srv:
        threading.Thread(target=srv.shutdown, daemon=True).start()
    h._send({"ok": True})


@route("GET", "/api/lan/status", "local")
def r_lan_status(h, q, who, uid):
    """State of the phone listener for the Phone dialog - answered at once. full=1 also asks Windows
    for the adapters, the network category and the firewall rule: ONE PowerShell call that can take
    many seconds on a slow PC, so the dialog sends it in the background AFTER it is usable."""
    info = lan.system_info(fresh=True) if q1(q, "full") else lan.system_info(wait=False)
    out = dict(LAN.status() if LAN else {"running": False, "error": "unavailable"}, enabled=bool(ACCESS.lan().get("enabled")),
               seen=SEEN.list(), windows=lan.IS_WINDOWS, adapters=info["adapters"], firewall_rule=info["firewall_rule"],
               details=bool(q1(q, "full")) or info["known"] or not lan.IS_WINDOWS)
    out["network"] = next((a.get("network") for a in info["adapters"] if a["ip"] == out.get("ip")), None)
    h._send(out)


@route("POST", "/api/lan", "local")
def p_lan(h, data, who, uid):                           # switch phone access on / off, choose the adapter
    if LAN is None:
        return h._send({"error": "unavailable"}, code=409)
    choice = str(data.get("ip") or ACCESS.lan().get("ip") or "auto")
    if choice != "auto" and not (lan.private_ipv4(choice) and lan.has_address(choice)):
        return h._send({"error": "bad_address", "detail": "not a private address of this machine"}, code=400)
    if data.get("enabled"):
        ACCESS.set_lan(True, choice)
        ACCESS.trainer_token()                          # exists from the first switch-on
        st = LAN.start(choice)
    else:
        ACCESS.set_lan(False)
        st = LAN.stop()
    h._send(dict(st, enabled=bool(data.get("enabled"))))


@route("POST", "/api/access/link", "local", own_user=False)
def p_link(h, data, who, uid):
    """The access links behind the QR codes: {kind: trainer|athlete, action: show|renew|rotate|revoke|chat}.
    Only ever at the PC - whoever holds a phone cannot mint, extend or read a link."""
    kind, action = data.get("kind"), data.get("action") or "show"
    base = (LAN.status().get("url") if LAN else None)
    link = lambda token: f"{base}#t={token}" if (base and token) else None
    if kind == "trainer":
        token = ACCESS.trainer_token(rotate=action == "rotate")
        return h._send({"kind": "trainer", "url": link(token), "running": bool(base)})
    if kind != "athlete":
        return h._send({"error": "bad_request"}, code=400)
    person = h._uid(data.get("user_id"))
    if person is None:
        return h._send({"error": "bad_user_id"}, code=400)
    if action == "revoke":
        ACCESS.revoke_athlete(person)
        return h._send({"kind": "athlete", "user_id": person, "url": None, "revoked": True, "running": bool(base)})
    with core.shared_connection(STATE["db"]) as con:
        age = core.user_profile(con, person, core._today({})).get("age")
    minor = age is not None and age < 18
    if action == "renew":
        rec = ACCESS.renew_athlete(person) or ACCESS.athlete(person, create=True, minor=minor)
    elif action == "chat":
        rec = ACCESS.set_chat(person, bool(data.get("chat"))) or ACCESS.athlete(person, create=True, minor=minor)
    else:
        rec = ACCESS.athlete(person, create=True, minor=minor, rotate=action == "rotate")
    h._send({"kind": "athlete", "user_id": person, "url": None if rec["expired"] else link(rec["token"]), "running": bool(base),
             "expires": rec["expires"], "expired": rec["expired"], "chat": rec["chat"], "minor": minor, "usage": rec["usage"],
             "limits": {"boards": access.ATHLETE_BOARDS_PER_DAY, "questions": access.ATHLETE_QUESTIONS_PER_DAY, "days": access.ATHLETE_DAYS}})


@route("POST", "/api/firewall", "local")
def p_firewall(h, data, who, uid):                      # Windows: add (or remove) our inbound rule - UAC prompt on the PC's screen
    port = STATE.get("port") or 8765                    # the rule covers this port and the next ones the listener may take
    h._send({"started": lan.firewall_helper(port, remove=bool(data.get("remove"))), "windows": lan.IS_WINDOWS})


class Server(ThreadingHTTPServer):
    daemon_threads = True                    # don't let worker threads block shutdown
    # http.server sets SO_REUSEADDR. On Windows that option lets a SECOND process bind a port that
    # is already listening - bind() never fails there, so two app instances ran side by side and
    # the takeover logic in main() was never reached. Windows gets an exclusive bind instead;
    # elsewhere SO_REUSEADDR only skips the TIME_WAIT delay, which is what we want.
    allow_reuse_address = (os.name != "nt")
    kind = "local"                           # "lan" on the phone listener (see make_lan_server) - decides about trust
    allowed_hosts = LOCAL_HOSTS              # Host allow-list of THIS listener

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


def make_lan_server(ip: str, port: int) -> Server:
    """The phone listener: same handler, bound to ONE private address, marked as 'lan' - every
    request that arrives here needs a token, and its Host header must be exactly this address."""
    if not lan.private_ipv4(ip):
        raise OSError("not a private address")
    srv = Server((ip, port), Handler)
    srv.kind, srv.allowed_hosts = "lan", (ip,)
    return srv


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
    STATE["server"], STATE["port"] = srv, port         # so /api/shutdown can stop us
    write_instance(port)
    global LAN
    LAN = lan.LanManager(make_lan_server, port)

    def start_phone():                                 # in the background: nothing here may delay the app itself
        saved = ACCESS.lan()                           # ON unless the owner switched it off (v0.6.1)
        if not saved.get("enabled") or os.environ.get("ARX_NO_PHONE"):
            return
        try:
            ACCESS.trainer_token()                     # the code behind the trainer QR exists from the first start
            st = LAN.start(saved.get("ip") or "auto")
            print(f"Phone access is ON: {st['url']}  (start screen -> Phone: QR code, or switch it off)" if st["running"]
                  else f"Phone access is on, but there is no local network address yet ({st['error']}) - it starts by itself when there is one.")
        except Exception as e:
            print(f"phone access not started: {type(e).__name__}: {e}", file=sys.stderr)
    threading.Thread(target=start_phone, daemon=True).start()

    removed = core.sweep_stale_copies()                # DB copies of crashed runs hold private data
    if removed:
        print(f"Removed {removed} leftover temporary database copies.")
    def update_check():                                # one-off, 3 s cap - in the background, so the
        try:                                           # server answers (and can be probed) at once
            if not os.environ.get("ARX_NO_UPDATE_CHECK"):
                STATE["update_checked"] = time.time()      # bootstrap repeats the check every few hours
                STATE["update"] = check_update(STATE["version"])
        finally:
            UPDATE_CHECKED.set()
    threading.Thread(target=update_check, daemon=True).start()

    url = f"http://localhost:{port}"
    print(f"ARX Insight {STATE['version']} running at {url}  (close this window to stop)")
    if not args.no_browser and not os.environ.get("ARX_NO_BROWSER"):   # (a one-click update reloads the open page instead)
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
    finally:
        LAN.stop()
        srv.server_close()                             # free the port at once for a successor
        core.drop_snapshots()                          # no copy of the database stays behind
        mark_stopped()


if __name__ == "__main__":
    main()
