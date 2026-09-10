#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARX Insight  -  read-only analyzer for the ARX training database.

The ARX Windows app stores every set in a local Firebird database
(Resources/DB.FDB4). This tool opens a COPY of that file read-only, turns the
raw records into meaningful, sport-science-oriented metrics (in kg and cm),
and writes a compact JSON that the HTML report renders. Optionally it asks a
Claude model (using the user's own API key) to phrase the findings and a
session suggestion in plain language.

Nothing is ever written back to the ARX database. We copy the file first and
open the copy, so a running ARX app is never disturbed.

Public domain / CC0. No warranty. Not medical advice.
"""

from __future__ import annotations
import os, sys, json, gzip, shutil, tempfile, argparse, statistics as st
from datetime import datetime


def data_dir() -> str:
    """Stable per-user location for settings, goals and caches.

    Kept OUTSIDE the program folder so re-downloading the app (a fresh ZIP)
    never overwrites the user's settings, goals, venv or Firebird client.
    Override with the ARX_DATA_DIR environment variable (the Windows launcher
    sets it to %LOCALAPPDATA%\\ARXInsight)."""
    d = os.environ.get("ARX_DATA_DIR")
    if not d:
        if os.name == "nt":
            d = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ARXInsight")
        else:
            d = os.path.join(os.path.expanduser("~"), ".arx-insight")
    os.makedirs(d, exist_ok=True)
    return d

# --- unit conversions (ARX stores imperial internally) -----------------------
LB_TO_KG = 0.45359237          # pounds  -> kilograms
IN_TO_CM = 2.54                # inches  -> centimeters

# --- a "real" working set must clear this noise filter ------------------------
MIN_SECONDS = 30.0             # shorter = false start / repositioning
MIN_REPS = 2                   # fewer   = aborted attempt


# =============================================================================
# Firebird access
# =============================================================================
def locate_fbclient() -> str | None:
    """Find the Firebird client library.

    Priority: explicit env var, then the library that ships with the ARX app on
    Windows (embedded mode), then a couple of common install locations. On the
    machine running ARX this library is always present, because ARX itself uses
    Firebird embedded.
    """
    env = os.environ.get("ARX_FBCLIENT")
    if env and os.path.exists(env):
        return env
    candidates = [
        # Windows: next to the ARX executable (embedded fbclient)
        r"C:\Program Files\WindowsApps",  # searched below
        r"C:\Program Files\Firebird\Firebird_5_0\fbclient.dll",
        r"C:\Program Files\Firebird\Firebird_4_0\fbclient.dll",
        # macOS / Linux
        "/Library/Frameworks/Firebird.framework/Firebird",
        "/opt/firebird/lib/libfbclient.so",
        "/usr/lib/libfbclient.so",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # Windows: hunt for fbclient.dll inside the installed ARX package
    wa = r"C:\Program Files\WindowsApps"
    if os.path.isdir(wa):
        for root, _dirs, files in os.walk(wa):
            if "Arx" in root and "fbclient.dll" in files:
                return os.path.join(root, "fbclient.dll")
    return None


def open_readonly(db_path: str):
    """Return (connection, tempdir). Opens a COPY so the original is untouched."""
    from firebird.driver import connect, driver_config  # imported late on purpose

    lib = locate_fbclient()
    if lib:
        driver_config.fb_client_library.value = lib
    tmp = tempfile.mkdtemp(prefix="arx_ro_")
    copy = os.path.join(tmp, "arx_copy.fdb")
    shutil.copy2(db_path, copy)
    con = connect(copy, user="SYSDBA")   # embedded: no password required
    return con, tmp


def blob_bytes(v) -> bytes:
    """Normalize a Firebird blob value to bytes (text blobs come back as str)."""
    if v is None:
        return b""
    if hasattr(v, "read"):
        v = v.read()
    return v.encode("latin1") if isinstance(v, str) else bytes(v)


# =============================================================================
# Metrics
# =============================================================================
def load_sets(con, user_id: int) -> list[dict]:
    """Read all non-deleted sets for one user and derive per-set metrics."""
    cur = con.cursor()
    cur.execute(
        '''select id, exercisedate, "SESSION", exercise, protocol, maxload,
                  concentricmax, eccentricmax, intensity, elapsedseconds,
                  repschemedata, eventstreamdata
           from "ExerciseSet"
           where user_id = ? and deleted is false
           order by exercisedate''',
        (user_id,),
    )
    cols = [d[0] for d in cur.description]
    out = []
    for row in cur.fetchall():
        r = dict(zip(cols, row))
        rsd = blob_bytes(r.pop("REPSCHEMEDATA"))
        ev = blob_bytes(r.pop("EVENTSTREAMDATA"))

        reps = 0
        try:
            events = json.loads(ev.decode("latin1"))
            reps = sum(1 for e in events if e.get("Type") == "EndRep")
        except Exception:
            pass

        rom_cm = None
        try:
            cfg = json.loads(rsd.decode("latin1"))
            rom_cm = round(abs(cfg.get("StartPosition", 0) - cfg.get("EndPosition", 0)) * IN_TO_CM, 1)
        except Exception:
            pass

        sec = float(r["ELAPSEDSECONDS"] or 0)
        c = float(r["CONCENTRICMAX"] or 0)
        e = float(r["ECCENTRICMAX"] or 0)
        mean_force = float(r["INTENSITY"] or 0)   # INTENSITY == time-averaged force (lb)
        working = sec >= MIN_SECONDS and reps >= MIN_REPS and c > 0 and e > 0

        out.append({
            "id": r["ID"],
            "date": str(r["EXERCISEDATE"]),
            "session": r["SESSION"],
            "exercise": r["EXERCISE"],
            "protocol": r["PROTOCOL"],
            "reps": reps,
            "rom_cm": rom_cm,
            "max_kg": round(float(r["MAXLOAD"] or 0) * LB_TO_KG, 1),
            "concentric_kg": round(c * LB_TO_KG, 1),
            "eccentric_kg": round(e * LB_TO_KG, 1),
            "mean_force_kg": round(mean_force * LB_TO_KG, 1),
            "seconds": round(sec, 1),
            # impulse = average force x time-under-load: a volume/work proxy that
            # is meaningful on an adaptive-resistance machine (no fixed "weight").
            "impulse_kg_s": round(mean_force * LB_TO_KG * sec),
            "working": working,
        })
    return out


def decode_force_curve(con, set_id: int) -> dict:
    """Decode the full force/position/speed curve for one set.

    SERIALIZEDDETAILEDDATA is gzip-compressed UTF-16 JSON: an array of ~20 Hz
    samples with Value (force lb), EncoderValue (position inch) and SpeedValue.
    We also segment per rep (via BeginRep/EndRep events) to compute 'inroad' -
    the force decline across reps that signals how deeply the set fatigued.
    """
    cur = con.cursor()
    cur.execute('select serializeddetaileddata, eventstreamdata from "ExerciseSet" where id = ?', (set_id,))
    det_raw, ev_raw = cur.fetchone()
    samples = json.loads(gzip.decompress(blob_bytes(det_raw)).decode("utf-16"))
    events = json.loads(blob_bytes(ev_raw).decode("latin1"))

    def ts(s):
        try:
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return None

    t0 = ts(samples[0]["Time"])
    curve = [{
        "t": round(ts(s["Time"]) - t0, 2),
        "force_kg": round(s["Value"] * LB_TO_KG, 1),
        "pos_cm": round((s.get("EncoderValue") or 0) * IN_TO_CM, 1),
    } for s in samples]

    begins = [ts(e["Time"]) - t0 for e in events if e["Type"] == "BeginRep"]
    ends = [ts(e["Time"]) - t0 for e in events if e["Type"] == "EndRep"]
    rep_peaks = []
    for a, b in zip(begins, ends):
        seg = [p["force_kg"] for p in curve if a <= p["t"] <= b]
        if seg:
            rep_peaks.append(round(max(seg), 1))

    inroad_pct = None
    if len(rep_peaks) >= 2 and max(rep_peaks) > 0:
        inroad_pct = round((max(rep_peaks) - rep_peaks[-1]) / max(rep_peaks) * 100)

    return {"curve": curve, "rep_peaks": rep_peaks, "inroad_pct": inroad_pct}


# --- exercise catalog: code -> {name, group (Push/Pull/Drive), kind} ----------
def load_catalog(path: str) -> dict:
    """Load the ARX Omni exercise catalog (see exercises.json)."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return {k: v for k, v in raw.items() if not k.startswith("_")}
    except Exception:
        return {}


def _linfit(xs, ys):
    """Least-squares slope/intercept; slope is progress per session occurrence."""
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs) or 1e-9
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return b, my - b * mx


def _exercise_series(work: list[dict], catalog: dict) -> list[dict]:
    """Per-exercise progress, aggregated to the BEST set per training day.

    Important: an athlete often does several sets of the same exercise in one
    session; the later sets are weaker because the muscles are already fatigued.
    That within-session decline is expected (it is the 'inroad' story), NOT a
    regression. So cross-day progress compares the *best set of each day*, and we
    record how many sets / sessions that day contributed so the fatigue context
    is preserved (and handed to the AI). Trend and a diminishing-returns forecast
    (slope decays 15%/step) are computed on those daily bests."""
    by_ex = {}
    for s in work:
        by_ex.setdefault(s["exercise"], []).append(s)
    d0 = min(datetime.fromisoformat(s["date"][:10]).toordinal() for s in work) if work else 0

    out = []
    for ex, ss in by_ex.items():
        meta = catalog.get(str(ex), {})
        # aggregate to daily best
        by_day = {}
        for s in ss:
            day = s["date"][:10]
            d = by_day.setdefault(day, {"kg": 0, "sets": 0, "sessions": set()})
            d["kg"] = max(d["kg"], s["max_kg"])
            d["sets"] += 1
            d["sessions"].add(s["session"])
        days_sorted = sorted(by_day)
        occ = [{"i": i,
                "day": datetime.fromisoformat(day).toordinal() - d0,
                "date": day, "kg": by_day[day]["kg"],
                "sets": by_day[day]["sets"],           # sets of this exercise that day
                "sessions": len(by_day[day]["sessions"])}
               for i, day in enumerate(days_sorted)]
        slope, _ = _linfit([o["i"] for o in occ], [o["kg"] for o in occ])
        cur, step, fc = occ[-1]["kg"], slope, []
        for _ in range(6):
            step *= 0.85
            cur += step
            fc.append(round(cur, 1))
        out.append({
            "ex": ex,
            "name": meta.get("name", f"Übung {ex}"),
            "group": meta.get("group", "?"),
            "kind": meta.get("kind", "?"),
            "pb": max(o["kg"] for o in occ),
            "n": len(occ),                              # number of training DAYS
            "total_sets": len(ss),                      # all sets across all days
            "multi_set_days": sum(1 for o in occ if o["sets"] > 1),
            "first": occ[0]["kg"], "last": occ[-1]["kg"],  # first/last DAY best
            "trend_per_session": round(slope, 1),
            "occ": occ, "forecast": fc,
        })
    out.sort(key=lambda e: e["pb"], reverse=True)
    return out


def _whole_body(work: list[dict], exercises: list[dict]) -> dict:
    """Self-referenced whole-body index: each exercise as % of the athlete's own
    personal best, averaged per training day. Not a cross-person score - it only
    makes sense with broad muscle-group coverage, so we report coverage too."""
    pb = {e["ex"]: e["pb"] for e in exercises}
    total = len(pb)
    series = []
    for day in sorted({s["date"][:10] for s in work}):
        best = {}
        for s in (x for x in work if x["date"][:10] == day):
            best[s["exercise"]] = max(best.get(s["exercise"], 0), s["max_kg"])
        idx = st.mean([v / pb[e] * 100 for e, v in best.items()]) if best else 0
        series.append({"date": day[5:], "index": round(idx),
                       "coverage": len(best), "of": total})
    return {"series": series,
            "coverage_max": max((w["coverage"] for w in series), default=0),
            "total_exercises": total}


def _totals(work: list[dict], weekly_rate) -> dict:
    """Motivational totals: how much work and time the training added up to.

    'Time under load' is the actual working time (sum of set durations - the
    number that stays near the ~15 min ideal). 'Session wall-clock' includes the
    rest between sets. 'Work' is the summed impulse (mean force x time), a
    volume proxy. Per-week/month/year figures are projections from the current
    cadence, so they are labelled as such in the UI."""
    def tsec(s):
        try:
            return datetime.fromisoformat(s["date"]).timestamp()
        except Exception:
            return None

    tul_sec = sum(s["seconds"] for s in work)
    work_impulse = sum(s["impulse_kg_s"] for s in work)

    sessions = {}
    for s in work:
        sessions.setdefault(s["session"], []).append(s)
    walls = []
    for ss in sessions.values():
        starts = [tsec(s) for s in ss if tsec(s) is not None]
        ends = [tsec(s) + s["seconds"] for s in ss if tsec(s) is not None]
        if starts and ends:
            walls.append((max(ends) - min(starts)) / 60.0)
    n_sessions = len(sessions)
    avg_wall = round(st.mean(walls), 1) if walls else None
    avg_tul = round((tul_sec / n_sessions) / 60.0, 1) if n_sessions else None

    proj = {}
    if weekly_rate and avg_wall is not None:
        per_week_wall = weekly_rate * avg_wall
        per_week_tul = weekly_rate * (avg_tul or 0)
        proj = {
            "per_week_min": round(per_week_wall),
            "per_month_min": round(per_week_wall * 4.345),
            "per_year_h": round(per_week_wall * 52 / 60, 1),
            "per_week_tul_min": round(per_week_tul),
        }

    return {
        "sessions": n_sessions,
        "total_time_under_load_min": round(tul_sec / 60, 1),
        "total_wall_min": round(sum(walls), 1) if walls else None,
        "avg_session_wall_min": avg_wall,
        "avg_session_tul_min": avg_tul,
        "total_work_impulse": round(work_impulse),
        "projection": proj,
    }


def _load_analysis(work: list[dict]) -> dict:
    """Recovery / load signals from how training is spaced over time.

    Base idea of ARX-style high-intensity training: brief, all-out, and
    INFREQUENT (often ~1x/week per muscle group). Standard resistance-training
    recovery is ~48-72h per muscle group. So we look at the gaps between training
    days overall and per muscle group (Push/Pull/Drive), and the weekly rate.
    Too-frequent training with short rest points to overreaching / insufficient
    recovery; very long gaps point to detraining. The final judgement is left to
    the AI (with these numbers + guidance); we add only a conservative flag."""
    days = sorted({s["date"][:10] for s in work})
    ords = [datetime.fromisoformat(d).toordinal() for d in days]
    gaps = [b - a for a, b in zip(ords, ords[1:])]           # days between sessions
    span = (ords[-1] - ords[0]) if len(ords) > 1 else 0
    weekly_rate = round(len(days) / (span / 7), 1) if span else None
    last = ords[-1] if ords else 0
    sessions_last7 = sum(1 for o in ords if last - o < 7)

    # Effort-conditioned recovery: the 48-72h window only applies AFTER a session
    # that actually reached deep fatigue. A sub-maximal session needs far less
    # (~12-24h). So required rest is a function of the previous day's real effort.
    RANK = {"deep": 3, "moderate": 2, "submax": 1, "unknown": 0}
    REQUIRED_REST = {3: 3, 2: 2, 1: 1, 0: 2}   # days of rest a session of this effort needs

    per_group = {}
    insufficient_after_hard = 0
    for grp in ("Push", "Pull", "Drive"):
        # daily effort for this muscle group
        by_day = {}
        for s in (x for x in work if x.get("group") == grp):
            d = s["date"][:10]
            r = RANK.get(s.get("effort"), 0)
            cell = by_day.setdefault(d, {"rank": 0, "inroad": None})
            if r >= cell["rank"]:
                cell["rank"] = r
                cell["inroad"] = s.get("inroad")
        gdays = sorted(by_day)
        gords = [datetime.fromisoformat(d).toordinal() for d in gdays]
        # check each consecutive pair: was the gap enough for the PREVIOUS effort?
        events = []
        for (pd, po), (cd, co) in zip(zip(gdays, gords), zip(gdays[1:], gords[1:])):
            prev_rank = by_day[pd]["rank"]
            need = REQUIRED_REST[prev_rank]
            got = co - po
            if got < need and prev_rank >= 2:      # short rest after a hard session
                events.append({"after": pd, "effort": prev_rank, "rest_days": got, "needed": need})
                insufficient_after_hard += 1
        last_day = gdays[-1] if gdays else None
        rank_label = {3: "deep", 2: "moderate", 1: "submax", 0: "unknown"}
        last_rank = by_day[last_day]["rank"] if last_day else 0
        days_since = (datetime.now().toordinal() - gords[-1]) if gords else None   # vs today
        ready = (days_since is None) or (days_since >= REQUIRED_REST[last_rank])   # recovered enough
        per_group[grp] = {
            "days": len(gdays),
            "min_gap": min((b - a for a, b in zip(gords, gords[1:])), default=None),
            "hard_days": sum(1 for d in gdays if by_day[d]["rank"] >= 2),
            "submax_days": sum(1 for d in gdays if by_day[d]["rank"] == 1),
            "last_effort": rank_label[last_rank] if last_day else None,
            "last_inroad": by_day[last_day]["inroad"] if last_day else None,
            "days_since": days_since,
            "ready": ready,
            "insufficient_recovery": events,
        }

    # When should the next session start? Rest scales with the LAST day's real
    # effort (per the effort-conditioned rule above), taken across the groups
    # trained that day. Sub-maximal last day -> train again soon; deep -> wait.
    next_rest, next_earliest = None, None
    if days:
        last_day = days[-1]
        last_ranks = [RANK.get(s.get("effort"), 0) for s in work if s["date"][:10] == last_day]
        r = max(last_ranks) if last_ranks else 0
        next_rest = REQUIRED_REST[r]
        from datetime import timedelta
        next_earliest = (datetime.fromisoformat(last_day) + timedelta(days=next_rest)).date().isoformat()

    hard_total = sum(pg["hard_days"] for pg in per_group.values())
    submax_total = sum(pg["submax_days"] for pg in per_group.values())
    # Flag is now effort-conditioned, not frequency alone.
    if insufficient_after_hard >= 1:
        flag = "overload_risk"                 # short rest FOLLOWING genuinely hard sessions
    elif gaps and sorted(gaps)[len(gaps) // 2] > 10:
        flag = "detraining_risk"               # long gaps -> losing adaptation
    elif hard_total == 0 and len(days) >= 3:
        flag = "underload"                     # frequent but never truly maximal
    else:
        flag = "ok"

    return {
        "training_days": len(days),
        "gaps_days": gaps,
        "median_gap_days": sorted(gaps)[len(gaps) // 2] if gaps else None,
        "min_gap_days": min(gaps) if gaps else None,
        "sessions_last7": sessions_last7,
        "weekly_rate": weekly_rate,
        "hard_sessions": hard_total,
        "submax_sessions": submax_total,
        "insufficient_recovery_after_hard": insufficient_after_hard,
        "recommended_rest_days": next_rest,      # days to wait before the next session
        "next_earliest": next_earliest,          # earliest sensible next training date
        "per_group": per_group,
        "flag": flag,
    }


# Which exercises load which body part (for injury-aware planning).
BODYPART_EXERCISES = {
    "shoulder":   ["Incline Press", "Horizontal Press", "Decline Press", "Overhead Press",
                   "Pec Fly", "High Pull", "Shrugs"],
    "elbow":      ["Biceps Curl", "Triceps Pressdown", "Incline Press", "Horizontal Press",
                   "Decline Press", "Overhead Press", "Pull Down", "Row"],
    "wrist":      ["Biceps Curl", "Triceps Pressdown", "Incline Press", "Horizontal Press",
                   "Decline Press", "Overhead Press", "Pull Down", "Row", "High Pull"],
    "knee":       ["Belt Squat", "Calf Raise"],
    "hip":        ["Belt Squat", "Dead Lift", "Romanian Dead Lift"],
    "lower_back": ["Dead Lift", "Romanian Dead Lift", "Belt Squat", "Row", "High Pull", "Shrugs"],
    "neck":       ["Shrugs", "High Pull", "Overhead Press"],
}
_LEVEL_RANK = {"ok": 0, "careful": 1, "avoid": 2}


def exercise_restriction(name: str, restrictions: dict) -> str:
    """Worst restriction level that applies to an exercise via its body parts."""
    worst = "ok"
    for part, level in (restrictions or {}).items():
        if _LEVEL_RANK.get(level, 0) == 0:
            continue
        if name in BODYPART_EXERCISES.get(part, []):
            if _LEVEL_RANK[level] > _LEVEL_RANK[worst]:
                worst = level
    return worst


def _session_plan(exercises: list[dict], restrictions: dict,
                  focus: dict, approach: str, load: dict) -> list[dict]:
    """Rule-based ~15 min session plan (classic methodology).

    Honors:
      * restrictions: 'avoid' exercises are never programmed; 'careful' stay but
        with a gentle, sub-maximal target.
      * focus: per muscle group (Push/Pull/Drive) 'more' | 'normal' | 'less' |
        'off'. 'off' excludes the group entirely (e.g. an upper-body preference
        drops Drive / Belt Squat); 'more' comes first and may get an extra slot;
        'less' only fills leftover slots.
      * approach: 'full' (balanced full body every session), 'split' (feature the
        one most due & recovered region this session), or 'auto' (auto-regulate:
        skip groups that are not recovered yet, per the load model).
    The AI plan on top of this individualizes further from history."""
    focus = focus or {}
    approach = approach or "full"
    frank = {"more": 0, "normal": 1, "less": 2, "off": 3}
    fstate = lambda g: focus.get(g, "normal")
    ready = {g: (load.get("per_group", {}).get(g, {}) or {}).get("ready", True)
             for g in ("Push", "Pull", "Drive")}

    def restr(e): return exercise_restriction(e["name"], restrictions)
    avail = [e for e in exercises if restr(e) != "avoid" and fstate(e["group"]) != "off"]

    def mk(e):
        r = restr(e)
        return {"name": e["name"], "group": e["group"], "last": e["last"],
                "restriction": r, "target": "gentle" if r == "careful" else "max"}

    chosen = []
    if approach == "split":
        # feature one region: prefer emphasized, then recovered, then most overdue
        groups = [g for g in ("Drive", "Push", "Pull") if fstate(g) != "off"]
        def score(g):
            pg = load.get("per_group", {}).get(g, {}) or {}
            return (frank[fstate(g)], 0 if pg.get("ready", True) else 1, -(pg.get("days_since") or 0))
        groups.sort(key=score)
        feature = groups[0] if groups else None
        chosen = [e for e in avail if e["group"] == feature][:4]
    else:
        # 'full' and 'auto' both produce a classic, always-usable balanced plan
        # (never gated to empty). For 'auto' we put the most-recovered groups
        # first; the readiness/timing itself is shown separately and handled by
        # the AI. This way the standard plan is always visible - e.g. as a
        # preview of what to do when a rest day ends.
        used = set()
        def gkey(g):
            pg = load.get("per_group", {}).get(g, {}) or {}
            not_ready = 0 if pg.get("ready", True) else 1
            return (not_ready if approach == "auto" else 0, frank[fstate(g)])
        order = sorted(["Drive", "Push", "Pull"], key=gkey)
        for g in order:
            if fstate(g) == "off":
                continue
            cand = [e for e in avail if e["group"] == g and e["ex"] not in used]
            if cand:
                used.add(cand[0]["ex"]); chosen.append(cand[0])
        for e in avail:                       # fill up to 5, de-emphasized groups last
            if len(chosen) >= 5:
                break
            if e["ex"] in used or fstate(e["group"]) == "less":
                continue
            used.add(e["ex"]); chosen.append(e)

    return [mk(e) for e in chosen[:5]]


EFFORT_CACHE = os.path.join(data_dir(), ".effort_cache.json")


def set_effort(con, set_id: int, cache: dict) -> dict:
    """Per-set effort / true fatigue from the force curve.

    Approximates ARX 'inroad' without segmenting reps: compare the peak force of
    the first third of the set vs the last third. A set that reached deep fatigue
    declines toward the end (positive inroad); a warm-up / ramping / sub-maximal
    set ends at or above its early peak (rising, ~0 inroad). Cached by set id
    because a recorded set never changes."""
    key = str(set_id)
    if key in cache:
        return cache[key]
    result = {"inroad": None, "effort": "unknown"}
    try:
        cur = con.cursor()
        cur.execute('select serializeddetaileddata from "ExerciseSet" where id=?', (set_id,))
        raw = _blob_to_bytes(cur.fetchone()[0])
        samples = json.loads(gzip.decompress(raw).decode("utf-16"))
        f = [s["Value"] for s in samples if isinstance(s.get("Value"), (int, float))]
        if len(f) >= 20:
            peak = max(f)
            third = max(1, len(f) // 3)
            early_peak = max(f[:third])
            late_peak = max(f[-third:])
            inroad = round((peak - late_peak) / peak * 100) if peak else 0
            rising = late_peak >= early_peak * 0.98
            effort = "deep" if inroad >= 20 else ("moderate" if inroad >= 8 and not rising else "submax")
            result = {"inroad": inroad, "effort": effort}
    except Exception:
        pass
    cache[key] = result
    return result


def _blob_to_bytes(v) -> bytes:
    if v is None:
        return b""
    if hasattr(v, "read"):
        v = v.read()
    return v.encode("latin1") if isinstance(v, str) else bytes(v)


def build_report(con, cfg: dict) -> dict:
    """Assemble the full analysis payload for one athlete."""
    catalog = cfg.get("_catalog", {})
    sets = load_sets(con, cfg["user_id"])
    work = [s for s in sets if s["working"]]
    # effort per set (real fatigue reached), cached by set id
    cache = {}
    try:
        with open(EFFORT_CACHE, encoding="utf-8") as fh:
            cache = json.load(fh)
    except Exception:
        pass
    for s in work:                       # attach name/group + effort/inroad
        m = catalog.get(str(s["exercise"]), {})
        s["name"] = m.get("name", f"Übung {s['exercise']}")
        s["group"] = m.get("group", "?")
        e = set_effort(con, s["id"], cache)
        s["inroad"] = e["inroad"]
        s["effort"] = e["effort"]
    try:
        with open(EFFORT_CACHE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
    except Exception:
        pass

    exercises = _exercise_series(work, catalog)
    restrictions = cfg.get("restrictions", {}) or {}
    for e in exercises:                       # annotate each exercise with its restriction
        e["restriction"] = exercise_restriction(e["name"], restrictions)
    days = sorted({s["date"][:10] for s in work})

    featured = None
    if work:
        top = max(work, key=lambda s: s["max_kg"])
        featured = {"meta": top, **decode_force_curve(con, top["id"])}

    load = _load_analysis(work)

    # headline KPIs for the UI
    ce = [s["eccentric_kg"] / s["concentric_kg"] for s in work if s["concentric_kg"] > 0]
    kpi = {
        "top_force": round(max((s["max_kg"] for s in work), default=0), 1),
        "sessions": len({s["session"] for s in work}),
        "ce_ratio": round(st.mean(ce), 2) if ce else None,
        "total_impulse": sum(s["impulse_kg_s"] for s in work),
    }

    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "athlete_alias": cfg.get("alias", "Athlet"),
        "goal": cfg.get("goal", {}),
        "sessions_per_week": cfg.get("sessions_per_week"),
        "sets_total": len(sets),
        "sets_working": len(work),
        "training_days": days,
        "kpi": kpi,
        "exercises": exercises,
        "whole_body": _whole_body(work, exercises),
        "load": load,
        "totals": _totals(work, load.get("weekly_rate")),
        "restrictions": restrictions,
        "focus": cfg.get("focus", {}) or {},
        "approach": cfg.get("approach", "auto"),
        "session_plan": _session_plan(exercises, restrictions,
                                      cfg.get("focus", {}) or {},
                                      cfg.get("approach", "auto"), load),
        "featured": featured,
    }


# =============================================================================
# Optional AI narrative (uses the user's own Claude API key)
# =============================================================================
def ai_narrative(report: dict, cfg: dict) -> str | None:
    """Ask Claude to phrase findings + a session suggestion. Key stays local.

    The model never sees raw personal data - only the aggregated metrics - and
    it phrases, it does not compute: the numbers are already final.
    """
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        from anthropic import Anthropic
    except ImportError:
        return None

    client = Anthropic(api_key=key)
    model = cfg.get("model", "claude-opus-5")   # override in config if desired

    # Units: convert force/length so the model answers in the user's units.
    imp = cfg.get("units", "imperial") == "imperial"
    ff = 2.20462 if imp else 1.0
    lf = (1 / 2.54) if imp else 1.0
    FU, LU = ("lb", "in") if imp else ("kg", "cm")
    kg = lambda v: round(v * ff, 1) if v is not None else None

    # Hand the model a compact, name-free metrics summary (numbers are final;
    # the model only phrases them). Exercise names/groups come from the catalog.
    f = report.get("featured")
    summary = {
        "units": {"force": FU, "length": LU},
        "goal": report["goal"],
        "restrictions": report.get("restrictions", {}),   # body part -> ok/careful/avoid
        "focus": report.get("focus", {}),                 # group -> more/normal/less/off
        "approach": report.get("approach", "auto"),       # full | split | auto
        "sessions_per_week_target": report.get("sessions_per_week"),
        "training_days": report["training_days"],
        "exercises": [{
            "name": e["name"], "group": e["group"], "kind": e["kind"],
            "restriction": e.get("restriction", "ok"),
            "personal_best": kg(e["pb"]),
            "latest_day_best": kg(e["last"]),   # best set of the most recent training day
            "training_days": e["n"], "total_sets": e["total_sets"],
            "days_with_multiple_sets": e["multi_set_days"],
            "trend_per_day": kg(e["trend_per_session"]),
        } for e in report["exercises"]],
        "whole_body_index_per_day": report["whole_body"]["series"],
        "load_and_recovery": report["load"],
        "totals": report["totals"],
        "session_plan": report["session_plan"],
        "featured_set": {
            "exercise": f["meta"].get("name") if f else None,
            "group": f["meta"].get("group") if f else None,
            "peak": kg(f["meta"]["max_kg"]) if f else None,
            "rep_peaks": [kg(v) for v in f["rep_peaks"]] if f else None,
            "inroad_pct": f["inroad_pct"] if f else None,
        },
    }

    system = (
        "You are a strength-training analyst for users of the ARX adaptive "
        "resistance machine (amateurs training for muscle and strength). "
        "Resistance auto-adapts to the user's force, so progress is measured by "
        "produced force (kg) and by 'inroad' - the force decline across reps "
        "that shows the set reached deep fatigue. Rising per-rep peaks mean the "
        "early reps were sub-maximal. Exercises are grouped Push / Pull / Drive.\n"
        "CRITICAL - fatigue vs. regression: an athlete often performs SEVERAL "
        "sets of the same exercise within one session; later sets are naturally "
        "weaker because the muscles are already fatigued. This is expected and is "
        "NOT a regression or a deficit. The per-exercise numbers are already "
        "aggregated to the BEST set of each training day (latest_day_best_kg vs. "
        "personal_best_kg). Only compare best-of-day across days. When "
        "days_with_multiple_sets > 0, treat any within-day or later-set drop as "
        "fatigue, and never tell the user to 'work back up' to a value that is "
        "just a fatigued repeat set. Judge progress by the trend of daily bests "
        "and by whether sets reach real inroad.\n"
        "LOAD & RECOVERY (effort-conditioned - important): the 48-72h recovery "
        "window applies ONLY after a session that truly reached deep fatigue "
        "(high inroad, effort 'deep'). A sub-maximal session (effort 'submax', "
        "low/zero inroad, rising peaks) needs far less - ~12-24h - so training "
        "again the next day is NOT overtraining in that case. Use "
        "load_and_recovery: per muscle group it gives last_effort, last_inroad, "
        "hard_days vs submax_days, and insufficient_recovery events (short rest "
        "that followed a genuinely hard session). Only warn about OVERTRAINING "
        "when insufficient_recovery_after_hard > 0, i.e. short rest after real "
        "maximal effort. If the athlete trains often but the sessions are "
        "sub-maximal (flag 'underload', hard_sessions low), the problem is the "
        "opposite: not enough intensity - tell them to push to real inroad "
        "rather than to rest more. Also flag UNDERLOAD across days as a plateau "
        "or decline despite ample rest. Give a concrete recovery recommendation "
        "scaled to the last session's actual effort. Never give medical advice.\n"
        "SAFETY - restrictions (highest priority): the user may flag body parts "
        "as limited. For any exercise with restriction 'avoid', do NOT recommend "
        "it and do NOT encourage effort on that body part - state plainly that it "
        "is excluded because of the restriction. For 'careful' (ramping back "
        "up), recommend gradual, sub-maximal, controlled-range loading and "
        "technique focus - never tell the user to go to maximum or to full "
        "inroad on those. Avoiding aggravation always outranks progress. This is "
        "not medical or rehabilitation advice.\n"
        "FOCUS & APPROACH: honor the user's focus (per group 'more'/'less'/'off' "
        "- e.g. an upper-body preference means emphasize Push/Pull and drop or "
        "minimize Drive/legs like Belt Squat) and their chosen approach: 'full' "
        "= balanced full body each session; 'split' = one region per session so "
        "each recovers between sessions; 'auto' = auto-regulate by readiness "
        "(train a group again only when recovered - use per_group.ready, "
        "days_since, last_effort, and a >~10% drop from a group's recent best as "
        "the under-recovery signal). Recommend the split rotation or which groups "
        "are ready today accordingly.\n"
        "Give short, concrete, scientifically defensible observations and ONE "
        "session suggestion. It MUST include WHEN to train next (how many rest "
        "days / the earliest sensible date — use load_and_recovery."
        "recommended_rest_days and next_earliest) AND WHICH exercises, in what "
        "order (large muscle groups first unless focus says otherwise, ~15 min, "
        "matched to the goal, weekly frequency, focus, approach and the "
        "restrictions above). Never give "
        f"medical advice. Use the units given in the data ({FU} and {LU}). "
        f"Answer in {'German' if cfg.get('language','en')=='de' else 'English'}, "
        "in a few short sentences with clear headings."
    )
    msg = client.messages.create(
        model=model,
        max_tokens=6000,                         # room for adaptive thinking + answer
        thinking={"type": "adaptive"},           # on by default for Opus 5 / Fable
        output_config={"effort": "low"},         # simple phrasing task -> keep it cheap/fast
        system=system,
        messages=[{"role": "user", "content": json.dumps(summary, ensure_ascii=False)}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")


# =============================================================================
# CLI
# =============================================================================
def load_config(path: str) -> dict:
    """Load persisted settings so nothing has to be re-entered each run."""
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:   # tolerate a BOM (PowerShell writes one)
            return json.load(f)
    return {}


def main():
    ap = argparse.ArgumentParser(description="ARX Insight - read-only ARX training analyzer")
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--db", help="Path to DB.FDB4", default=os.environ.get("ARX_DB"))
    ap.add_argument("--config", default="config.json", help="Settings file (persisted)")
    ap.add_argument("--catalog", default=os.path.join(here, "exercises.json"),
                    help="Exercise catalog (code -> name/group/kind)")
    ap.add_argument("--out", default="arx_report_data.json", help="Report JSON output")
    ap.add_argument("--ai", action="store_true", help="Also generate the AI narrative")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if not args.db:
        sys.exit("No database path. Use --db or set ARX_DB, or 'db' in config.json.")
    cfg.setdefault("user_id", cfg.get("user_id", 1))
    cfg["_catalog"] = load_catalog(args.catalog)   # code -> {name, group, kind}

    con, tmp = open_readonly(args.db)
    try:
        report = build_report(con, cfg)
        if args.ai:
            report["ai_narrative"] = ai_narrative(report, cfg)
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"Wrote {args.out}: {report['sets_working']} working sets across "
          f"{len(report['training_days'])} training days.")


if __name__ == "__main__":
    main()
