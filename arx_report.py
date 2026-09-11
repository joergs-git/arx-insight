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
import os, sys, json, gzip, shutil, tempfile, argparse, random, statistics as st
from datetime import datetime, date, timedelta


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

# --- effort / inroad classification --------------------------------------------
# Bump EFFORT_ALGO_VERSION whenever set_effort's logic or these thresholds change:
# the per-set effort cache is keyed by set id and would otherwise keep stale
# results forever (a recorded set never changes, but our reading of it may).
EFFORT_ALGO_VERSION = 2
INROAD_DEEP = 20               # % force decline across reps -> genuinely deep fatigue
INROAD_MODERATE = 8            # % ... -> moderate effort (unless the peaks were still rising)

# --- range-of-motion validity ---------------------------------------------------
# Force on an adaptive-resistance machine depends on the position range the set
# was performed over: a shorter ROM stays in the strong part of the movement and
# yields a higher peak. So a day's best is only comparable with other days when
# the ROM matches. Reference = the ROM that most of the last ROM_REF_DAYS
# training days of that exercise agree on; a day within +-ROM_TOLERANCE of it
# counts as comparable.
ROM_TOLERANCE = 0.05           # 5 % relative deviation
ROM_REF_DAYS = 5               # days that form the reference window
MIN_TREND_POINTS = 3           # fewer comparable days -> no trend / forecast (None)

# --- scientifically-defensible "textbook" machine settings for hypertrophy/strength
# ~8 reps, ~5 s per movement direction, a ~3 s hold at the end position, no pause on
# the return; the end-hold does not suit every exercise type (e.g. some presses).
IDEAL_SETTINGS = {
    "reps": 8, "seconds_per_direction": 5,
    "pause_end_s": 3, "pause_return_s": 0, "pre_timer_s": 5,
    "note": "The 3 s end-position hold does not suit every exercise type.",
}


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

        # machine settings of THIS set (REPSCHEMEDATA): range of motion and the
        # programmed pauses - needed per set for the session-sequence analysis
        rom_cm = pause_end = pause_return = None
        try:
            cfg = json.loads(rsd.decode("latin1"))
            rom_cm = round(abs(cfg.get("StartPosition", 0) - cfg.get("EndPosition", 0)) * IN_TO_CM, 1)
            pause_end = round(float(cfg.get("PauseAfterEndPosition", 0) or 0), 1)
            pause_return = round(float(cfg.get("PauseAfterStartPosition", 0) or 0), 1)
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
            "pause_end_s": pause_end,          # programmed hold at the end position
            "pause_return_s": pause_return,    # programmed pause at the start position
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


def _ts(s: str):
    """ISO timestamp string -> epoch seconds (None if unparsable)."""
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def load_curve(con, set_id: int) -> tuple[list[dict], list[dict]]:
    """Decode the raw force/position curve and the event stream of one set.

    SERIALIZEDDETAILEDDATA is gzip-compressed UTF-16 JSON: an array of ~20 Hz
    samples with Value (force lb), EncoderValue (position inch) and SpeedValue.
    EVENTSTREAMDATA is plain JSON with rep/countdown events (BeginRep, EndRep,
    ...). Returns (curve, events) where curve is a list of
    {t: seconds since first sample, force_kg, pos_cm}. Shared by the featured
    curve and the per-set effort so both use ONE definition of the data.
    """
    cur = con.cursor()
    cur.execute('select serializeddetaileddata, eventstreamdata from "ExerciseSet" where id = ?', (set_id,))
    det_raw, ev_raw = cur.fetchone()
    samples = json.loads(gzip.decompress(blob_bytes(det_raw)).decode("utf-16"))
    try:
        events = json.loads(blob_bytes(ev_raw).decode("latin1"))
    except Exception:
        events = []
    t0 = _ts(samples[0]["Time"]) if samples else 0.0
    curve = [{
        "t": round((_ts(s["Time"]) or t0) - t0, 2),
        "force_kg": round(s["Value"] * LB_TO_KG, 1),
        "pos_cm": round((s.get("EncoderValue") or 0) * IN_TO_CM, 1),
    } for s in samples if isinstance(s.get("Value"), (int, float))]
    # event times relative to the same t0 so they line up with the curve
    for e in events:
        e["_t"] = (_ts(e.get("Time", "")) or t0) - t0
    return curve, events


def rep_peaks_and_inroad(curve: list[dict], events: list[dict]) -> tuple[list[float], int | None]:
    """Segment the curve per rep (BeginRep/EndRep pairs) and compute 'inroad'.

    Inroad = decline from the strongest rep's peak to the LAST rep's peak, in %.
    It is THE effort signal on an adaptive-resistance machine: a set that reached
    deep fatigue ends well below its strongest rep; a sub-maximal or ramping set
    ends at or near it. An unmatched trailing BeginRep (aborted last rep) is
    simply dropped by the pairwise zip. Returns (rep_peaks, inroad_pct) with
    inroad None when fewer than two complete reps exist.
    """
    begins = [e["_t"] for e in events if e.get("Type") == "BeginRep"]
    ends = [e["_t"] for e in events if e.get("Type") == "EndRep"]
    rep_peaks = []
    for a, b in zip(begins, ends):
        seg = [p["force_kg"] for p in curve if a <= p["t"] <= b]
        if seg:
            rep_peaks.append(round(max(seg), 1))
    inroad_pct = None
    if len(rep_peaks) >= 2 and max(rep_peaks) > 0:
        inroad_pct = round((max(rep_peaks) - rep_peaks[-1]) / max(rep_peaks) * 100)
    return rep_peaks, inroad_pct


def classify_effort(inroad: int | None, rising: bool) -> str:
    """Map inroad % (+ whether force was still rising at the end) to an effort label."""
    if inroad is None:
        return "unknown"
    if inroad >= INROAD_DEEP:
        return "deep"
    if inroad >= INROAD_MODERATE and not rising:
        return "moderate"
    return "submax"


def decode_force_curve(con, set_id: int) -> dict:
    """Full curve + per-rep peaks + inroad for the featured set (see load_curve)."""
    curve, events = load_curve(con, set_id)
    rep_peaks, inroad_pct = rep_peaks_and_inroad(curve, events)
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
    """Least-squares slope/intercept. The slope's unit follows xs: pass the
    occurrence index for 'per training day occurrence', the day offset for 'per
    calendar day' - the two differ by the training frequency (2-3x at 3 sessions
    a week), so never mix them up."""
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
    (slope decays 15%/step) are computed on those daily bests - but ONLY on days
    whose range of motion matches the exercise's reference ROM (see
    ROM_TOLERANCE): a different ROM makes the force values incomparable, so such
    days are kept in the series for display but excluded from trend/forecast,
    and the trend is None when fewer than MIN_TREND_POINTS comparable days exist."""
    by_ex = {}
    for s in work:
        by_ex.setdefault(s["exercise"], []).append(s)
    d0 = min(datetime.fromisoformat(s["date"][:10]).toordinal() for s in work) if work else 0

    out = []
    for ex, ss in by_ex.items():
        meta = catalog.get(str(ex), {})
        # aggregate to daily best (and carry the ROM of that best set)
        by_day = {}
        for s in ss:
            day = s["date"][:10]
            d = by_day.setdefault(day, {"kg": 0, "rom": None, "sets": 0, "sessions": set()})
            if s["max_kg"] >= d["kg"]:
                d["kg"], d["rom"] = s["max_kg"], s.get("rom_cm")
            d["sets"] += 1
            d["sessions"].add(s["session"])
        days_sorted = sorted(by_day)
        occ = [{"i": i,
                "day": datetime.fromisoformat(day).toordinal() - d0,
                "date": day, "kg": by_day[day]["kg"],
                "rom_cm": by_day[day]["rom"],          # ROM of that day's best set
                "sets": by_day[day]["sets"],           # sets of this exercise that day
                "sessions": len(by_day[day]["sessions"])}
               for i, day in enumerate(days_sorted)]

        # ROM validity: the reference is the ROM (among the last ROM_REF_DAYS days)
        # that the most days agree with - i.e. the largest cluster, most recent
        # on a tie. A plain median would land BETWEEN two clusters when the days
        # split evenly (e.g. 13.6/21.0/21.0/16.2 -> 18.6) and no day would match it.
        ref_window = [o["rom_cm"] for o in occ[-ROM_REF_DAYS:] if o["rom_cm"]]
        rom_ref = None
        if ref_window:
            def agree(ref):
                return sum(1 for r in ref_window if abs(r - ref) / ref <= ROM_TOLERANCE)
            rom_ref = max(reversed(ref_window), key=agree)   # reversed -> latest wins ties
        for o in occ:
            o["rom_valid"] = bool(rom_ref and o["rom_cm"]
                                  and abs(o["rom_cm"] - rom_ref) / rom_ref <= ROM_TOLERANCE)
        valid = [o for o in occ if o["rom_valid"]]
        rom_latest = occ[-1]["rom_cm"]
        rom_drift = round((rom_latest - rom_ref) / rom_ref * 100, 1) if (rom_ref and rom_latest) else None
        rom_stable = all(o["rom_valid"] for o in occ[-ROM_REF_DAYS:])

        # Two slopes, two meanings: per training-day occurrence (index) and per
        # calendar day (ordinal offset). Both are reported; the AI is told which
        # is which. Computed on ROM-comparable days only; None when too few.
        slope = slope_per_day = None
        fc = []
        if len(valid) >= MIN_TREND_POINTS:
            slope, _ = _linfit([o["i"] for o in valid], [o["kg"] for o in valid])
            slope_per_day, _ = _linfit([o["day"] for o in valid], [o["kg"] for o in valid])
            cur, step = valid[-1]["kg"], slope
            for _ in range(6):
                step *= 0.85
                cur += step
                fc.append(round(cur, 1))
        out.append({
            "ex": ex,
            "name": meta.get("name", f"Übung {ex}"),
            "group": meta.get("group", "?"),
            "kind": meta.get("kind", "?"),
            "targets": list(meta.get("targets") or []),     # muscles the exercise is for
            "limiters": list(meta.get("limiters") or []),   # what gives out first (a means, not the goal)
            "pb": max(o["kg"] for o in occ),
            "n": len(occ),                              # number of training DAYS
            "total_sets": len(ss),                      # all sets across all days
            "multi_set_days": sum(1 for o in occ if o["sets"] > 1),
            "first": occ[0]["kg"], "last": occ[-1]["kg"],  # first/last DAY best
            "trend_per_session": round(slope, 1) if slope is not None else None,     # kg per training-day occurrence
            "trend_per_day": round(slope_per_day, 2) if slope_per_day is not None else None,  # kg per calendar day
            "trend_n": len(valid),                      # comparable days behind the trend
            "rom_cm_reference": rom_ref,
            "rom_cm_latest": rom_latest,
            "rom_drift_pct": rom_drift,                 # latest vs reference
            "rom_stable": rom_stable,                   # all days in the reference window comparable
            "days_excluded_for_rom": len(occ) - len(valid),
            "occ": occ, "forecast": fc,
        })
    out.sort(key=lambda e: e["pb"], reverse=True)
    return out


def _rom_warnings(exercises: list[dict]) -> list[dict]:
    """Exercises whose range of motion drifted between days, with the observed
    values, so the UI and the AI can name the problem instead of comparing
    incomparable force values. Only exercises with >1 training day qualify."""
    out = []
    for e in exercises:
        if e["n"] < 2 or e["rom_stable"]:
            continue
        out.append({
            "name": e["name"], "group": e["group"],
            "rom_cm_reference": e["rom_cm_reference"],
            "rom_cm_latest": e["rom_cm_latest"],
            "rom_drift_pct": e["rom_drift_pct"],
            "days_excluded_for_rom": e["days_excluded_for_rom"],
            "trend_available": e["trend_per_session"] is not None,
            "observed": [{"date": o["date"], "rom_cm": o["rom_cm"], "kg": o["kg"], "valid": o["rom_valid"]}
                         for o in e["occ"]],
        })
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


# --- session sequence analysis --------------------------------------------------
SEQ_DAYS = 10                  # training days handed to the UI / AI as ordered sequences
VISIT_GAP_MIN = 60             # a longer gap between sets = a new visit (wall-clock resets)
INTRA_REST_MIN = 5             # minutes: a repeat of the same exercise sooner than this is flagged
DENSE_TUL_RATIO = 0.5          # time-under-load / wall-clock >= this = a dense (rushed) session
MANY_SETS_DENSE = 4            # more working sets than this in a dense session -> flag
MANY_SETS = 6                  # more working sets than this in any session -> flag
DENSITY_SHIFT_PCT = 20         # session work density deviating this much from the last 3 -> flag


def _session_sequences(work: list[dict], approach: str, catalog: dict | None = None) -> list[dict]:
    """The ORDER of sets within each training day, plus rule-based flags.

    Cross-day metrics cannot see that six sets were spread over all muscle
    groups, or that an exercise was repeated four minutes after a set that
    loaded the same muscle chain. So per training day we list the working sets
    in time order with the rest before each one, its effort, its machine
    pauses, and whether it repeats an exercise (or, via the catalog's
    'limiters', a limiting muscle) already used that day.

    Definitions:
      minutes_since_prev_set  rest from the END of the previous set to the START
                              of this one (None for the day's first set).
      density_kg_per_s        impulse / seconds - by construction equal to the
                              set's mean force; kept as the per-set density figure.
      work_density_per_min    session impulse per wall-clock minute - THIS is
                              what shifts when pause length / tempo settings
                              change, so density_shift is judged on it.
      wall_minutes            first start to last end, summed per visit (a gap
                              longer than VISIT_GAP_MIN starts a new visit).

    Flags (each carries the numbers behind it):
      too_many_sets            > MANY_SETS working sets, or > MANY_SETS_DENSE in
                               a dense session (tul_ratio >= DENSE_TUL_RATIO).
      scattered_session        all three groups on one day although approach='split'.
      short_intra_session_rest a repeat of the same exercise / same limiter with
                               < INTRA_REST_MIN minutes rest since that earlier set.
      density_shift            work_density_per_min off by > DENSITY_SHIFT_PCT %
                               from the mean of the previous three sessions."""
    catalog = catalog or {}
    by_day: dict[str, list[dict]] = {}
    for s in work:
        by_day.setdefault(s["date"][:10], []).append(s)

    out = []
    for day in sorted(by_day):
        ss = sorted(by_day[day], key=lambda s: s["date"])
        sets, prev_end, prev_start = [], None, None
        seen_ex: dict = {}          # exercise -> end time of its last set today
        seen_lim: dict = {}         # limiter -> (end time, exercise name) of the last set loading it
        visits, wall = 1, 0.0
        visit_start = None
        for i, s in enumerate(ss):
            t = _ts(s["date"])
            end = (t + s["seconds"]) if t is not None else None
            rest = round((t - prev_end) / 60.0, 1) if (t is not None and prev_end is not None) else None
            # wall-clock per visit: reset when the gap is long
            if visit_start is None:
                visit_start = t
            elif rest is not None and rest > VISIT_GAP_MIN:
                wall += (prev_end - visit_start) / 60.0
                visits += 1
                visit_start = t
            meta = catalog.get(str(s["exercise"]), {})
            limiters = list(meta.get("limiters") or [])
            pre_fatigued = sorted(l for l in limiters if l in seen_lim)
            repeat = s["exercise"] in seen_ex
            rec = {
                "order": i + 1,
                "exercise": s["name"], "group": s["group"],
                "minutes_since_prev_set": rest,
                "max_kg": s["max_kg"], "mean_force_kg": s["mean_force_kg"], "seconds": s["seconds"],
                "reps": s["reps"],
                "density_kg_per_s": round(s["impulse_kg_s"] / s["seconds"], 1) if s["seconds"] else None,
                "rom_cm": s["rom_cm"], "inroad": s.get("inroad"), "effort": s.get("effort"),
                "pause_end_s": s.get("pause_end_s"), "pause_return_s": s.get("pause_return_s"),
                "repeat_of_earlier_set": repeat,
                "minutes_since_same_exercise": (round((t - seen_ex[s["exercise"]]) / 60.0, 1)
                                                if repeat and t is not None else None),
                "limiters": limiters,
                "limiters_pre_fatigued": pre_fatigued,
                "limiters_pre_fatigued_by": {l: seen_lim[l][1] for l in pre_fatigued},
                "minutes_since_limiter_loaded": ({l: round((t - seen_lim[l][0]) / 60.0, 1) for l in pre_fatigued}
                                                 if t is not None else {}),
            }
            sets.append(rec)
            if end is not None:
                seen_ex[s["exercise"]] = end
                for l in limiters:
                    seen_lim[l] = (end, s["name"])
                prev_end = end
            prev_start = t
        if visit_start is not None and prev_end is not None:
            wall += (prev_end - visit_start) / 60.0
        wall = round(wall, 1)

        tul = sum(s["seconds"] for s in ss)
        impulse = sum(s["impulse_kg_s"] for s in ss)
        tul_ratio = round(tul / (wall * 60), 2) if wall else None
        work_density = round(impulse / wall, 1) if wall else None
        groups = sorted({s["group"] for s in ss if s["group"] in ("Push", "Pull", "Drive")})
        n = len(ss)

        flags = []
        dense = tul_ratio is not None and tul_ratio >= DENSE_TUL_RATIO
        if n > MANY_SETS or (dense and n > MANY_SETS_DENSE):
            flags.append({"type": "too_many_sets", "working_sets": n, "tul_ratio": tul_ratio,
                          "wall_minutes": wall,
                          "detail": f"{n} working sets" + (f" in a dense session (time under load {tul_ratio:.0%} of {wall} min)" if dense else "")})
        if approach == "split" and len(groups) == 3:
            flags.append({"type": "scattered_session", "groups": groups,
                          "detail": "Push, Pull and Drive all trained on one day although the approach is 'split'"})
        for r in sets:
            m = r["minutes_since_same_exercise"]
            if r["repeat_of_earlier_set"] and m is not None and m < INTRA_REST_MIN:
                flags.append({"type": "short_intra_session_rest", "exercise": r["exercise"], "order": r["order"],
                              "minutes_rest": m, "detail": f"{r['exercise']} repeated after only {m} min rest"})
            # same limiter loaded again too soon (one flag per set, all limiters listed)
            soon = {l: m2 for l, m2 in r["minutes_since_limiter_loaded"].items()
                    if m2 < INTRA_REST_MIN and not (r["repeat_of_earlier_set"] and r["minutes_since_same_exercise"] == m2)}
            if soon:
                m2 = min(soon.values())
                after = sorted({r["limiters_pre_fatigued_by"][l] for l in soon})
                flags.append({"type": "short_intra_session_rest", "exercise": r["exercise"], "order": r["order"],
                              "limiters": sorted(soon), "after": after, "minutes_rest": m2,
                              "detail": f"{r['exercise']} loads {', '.join(sorted(soon))} only {m2} min after {', '.join(after)}"})
        prev3 = [d["work_density_per_min"] for d in out[-3:] if d["work_density_per_min"]]
        if work_density and prev3:
            ref = st.mean(prev3)
            dev = round((work_density - ref) / ref * 100)
            if abs(dev) > DENSITY_SHIFT_PCT:
                flags.append({"type": "density_shift", "work_density_per_min": work_density,
                              "previous_mean": round(ref, 1), "deviation_pct": dev,
                              "detail": f"work density {work_density} vs {round(ref, 1)} in the previous {len(prev3)} session(s) ({dev:+d} %) - check pause/tempo settings or rest between sets"})

        out.append({
            "date": day, "sets": sets, "working_sets": n,
            "groups_touched": groups, "visits": visits, "wall_minutes": wall,
            "time_under_load_min": round(tul / 60, 1), "tul_ratio": tul_ratio,
            "work_density_per_min": work_density,
            "mean_set_density_kg_per_s": round(st.mean([r["density_kg_per_s"] for r in sets if r["density_kg_per_s"]]), 1)
                                          if any(r["density_kg_per_s"] for r in sets) else None,
            "flags": flags,
        })
    return out[-SEQ_DAYS:]


def _limiter_conflicts(sequences: list[dict]) -> list[dict]:
    """Compact list of the days on which a set loaded a limiter (e.g. the grip)
    that an earlier set of the same day had already fatigued - which exercise,
    after which, and how many minutes later."""
    out = []
    for d in sequences:
        rows = []
        for s in d["sets"]:
            if not s["limiters_pre_fatigued"]:
                continue
            mins = [m for m in s["minutes_since_limiter_loaded"].values() if m is not None]
            rows.append({"order": s["order"], "exercise": s["exercise"],
                         "limiters": s["limiters_pre_fatigued"],
                         "after": s["limiters_pre_fatigued_by"],          # limiter -> earlier exercise
                         "minutes_after": min(mins) if mins else None,     # shortest of those gaps
                         "effort": s["effort"], "inroad": s["inroad"]})
        if rows:
            out.append({"date": d["date"], "sets_affected": len(rows), "conflicts": rows})
    return out


def _order_by_limiters(plan: list[dict]) -> list[dict]:
    """Planner rule on top of 'large muscle groups first': an exercise whose
    TARGET is another planned exercise's LIMITER goes after that exercise. The
    grip is only a means on a Dead Lift or Row (legs/back could still go on
    when it fails) but the elbow flexors are the goal of a Biceps Curl - so the
    curl must not pre-fatigue what the Row still needs. Stable: the existing
    order is kept wherever no such conflict exists."""
    items = list(plan)
    for _ in range(len(items)):                    # bounded number of passes
        moved = False
        for i in range(len(items)):
            a = items[i]
            later_users = [j for j in range(i + 1, len(items))
                           if set(a.get("targets") or []) & set(items[j].get("limiters") or [])]
            if later_users:
                j = max(later_users)
                items.insert(j + 1, items.pop(i))  # move a directly after the last exercise that needs it fresh
                moved = True
                break
        if not moved:
            break
    return items


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
                "restriction": r, "target": "gentle" if r == "careful" else "max",
                "targets": e.get("targets", []), "limiters": e.get("limiters", [])}

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

    return _order_by_limiters([mk(e) for e in chosen[:5]])


EFFORT_CACHE = os.path.join(data_dir(), ".effort_cache.json")


def load_effort_cache() -> dict:
    """Load the per-set effort cache; discard it if it was written by another
    algorithm version (see EFFORT_ALGO_VERSION). Returns the 'sets' dict."""
    try:
        with open(EFFORT_CACHE, encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict) and raw.get("_version") == EFFORT_ALGO_VERSION:
            return dict(raw.get("sets") or {})
    except Exception:
        pass
    return {}


def save_effort_cache(sets: dict) -> None:
    try:
        with open(EFFORT_CACHE, "w", encoding="utf-8") as fh:
            json.dump({"_version": EFFORT_ALGO_VERSION, "sets": sets}, fh)
    except Exception:
        pass


def set_effort(con, set_id: int, cache: dict) -> dict:
    """Per-set effort / true fatigue from the force curve.

    Primary method: rep-segmented inroad (BeginRep/EndRep events, same helper
    as the featured curve), so the recovery model and the displayed curve can
    never disagree. Fallback when rep events are missing/incomplete: compare
    the peak of the first third of the raw samples with the last third. Either
    way a set that reached deep fatigue declines toward the end; a ramping /
    sub-maximal set ends at or above its early peak (rising, ~0 inroad).
    Cached by set id because a recorded set never changes."""
    key = str(set_id)
    if key in cache:
        return cache[key]
    result = {"inroad": None, "effort": "unknown", "method": None}
    try:
        curve, events = load_curve(con, set_id)
        rep_peaks, inroad = rep_peaks_and_inroad(curve, events)
        if inroad is not None:
            rising = rep_peaks[-1] >= rep_peaks[0] * 0.98
            result = {"inroad": inroad, "effort": classify_effort(inroad, rising), "method": "reps"}
        else:
            f = [p["force_kg"] for p in curve]
            if len(f) >= 20:
                peak = max(f)
                third = max(1, len(f) // 3)
                early_peak, late_peak = max(f[:third]), max(f[-third:])
                inroad = round((peak - late_peak) / peak * 100) if peak else 0
                rising = late_peak >= early_peak * 0.98
                result = {"inroad": inroad, "effort": classify_effort(inroad, rising), "method": "thirds"}
    except Exception:
        pass
    cache[key] = result
    return result


def featured_settings(con, set_id: int, reps: int) -> dict:
    """Actual machine settings of one set (from REPSCHEMEDATA), so the report and
    the AI can compare them with IDEAL_SETTINGS."""
    try:
        cur = con.cursor()
        cur.execute('select repschemedata from "ExerciseSet" where id = ?', (set_id,))
        cfg = json.loads(blob_bytes(cur.fetchone()[0]).decode("latin1"))
        rom_in = abs(cfg.get("StartPosition", 0) - cfg.get("EndPosition", 0))
        spd = (cfg.get("StartToEndSpeed") or {}).get("InchesPerSecond") or 0
        sec_per_dir = round(rom_in / spd, 1) if spd else None
        return {
            "reps": reps,
            "seconds_per_direction": sec_per_dir,
            "pause_end_s": round(cfg.get("PauseAfterEndPosition", 0), 1),
            "pause_return_s": round(cfg.get("PauseAfterStartPosition", 0), 1),
            "pre_timer_s": cfg.get("PreExerciseTimer"),
        }
    except Exception:
        return {"reps": reps}


def _pick_featured(work: list[dict], user_id=None):
    """Choose the set to feature in the big curve - NOT simply the strongest one
    (that is always the leg press / Belt Squat and gets boring). Prefer a set
    with a story: a fresh personal best, a deeply fatiguing set (well executed),
    or a notably sub-maximal one (actionable). Among the interesting candidates
    pick pseudo-randomly, seeded by today's date + user, so the highlight (and
    the AI narrative built on it) is reproducible within a day yet varies across
    days. Returns (set, reason)."""
    if not work:
        return None, None
    rng = random.Random(f"{date.today()}-{user_id}")
    days = sorted({s["date"][:10] for s in work})
    recent_days = set(days[-2:])                      # last two training days
    pb = {}
    for s in work:
        pb[s["exercise"]] = max(pb.get(s["exercise"], 0), s["max_kg"])

    scored = []
    for s in work:
        ir = s.get("inroad")
        is_pb = s["max_kg"] >= pb[s["exercise"]] - 0.01
        recent = s["date"][:10] in recent_days
        score, reason = 0.0, None
        if is_pb and recent:
            score += 3; reason = "pb"
        if ir is not None and ir >= 25:
            score += 2; reason = reason or "deep"
        if ir is not None and ir < 8:
            score += 1.8; reason = reason or "submax"
        if recent:
            score += 1
        if reason:
            scored.append((score, s, reason))
    if not scored:                                    # nothing notable -> a recent set
        pool = [(1.0, s, "recent") for s in work if s["date"][:10] in recent_days] \
               or [(1.0, s, "recent") for s in work]
    else:
        pool = scored
    pool.sort(key=lambda x: x[0], reverse=True)
    _, s, reason = rng.choice(pool[:6])               # variety among the top candidates
    return s, reason


def build_report(con, cfg: dict) -> dict:
    """Assemble the full analysis payload for one athlete."""
    catalog = cfg.get("_catalog", {})
    sets = load_sets(con, cfg["user_id"])
    work = [s for s in sets if s["working"]]
    # effort per set (real fatigue reached), cached by set id + algorithm version
    cache = load_effort_cache()
    for s in work:                       # attach name/group + effort/inroad
        m = catalog.get(str(s["exercise"]), {})
        s["name"] = m.get("name", f"Übung {s['exercise']}")
        s["group"] = m.get("group", "?")
        e = set_effort(con, s["id"], cache)
        s["inroad"] = e["inroad"]
        s["effort"] = e["effort"]
    save_effort_cache(cache)

    exercises = _exercise_series(work, catalog)
    restrictions = cfg.get("restrictions", {}) or {}
    for e in exercises:                       # annotate each exercise with its restriction
        e["restriction"] = exercise_restriction(e["name"], restrictions)
    days = sorted({s["date"][:10] for s in work})

    featured = None
    if work:
        top, reason = _pick_featured(work, cfg.get("user_id"))
        featured = {"meta": top, "reason": reason,
                    "settings": featured_settings(con, top["id"], top.get("reps", 0)),
                    **decode_force_curve(con, top["id"])}

    load = _load_analysis(work)
    approach = cfg.get("approach", "auto")
    sequences = _session_sequences(work, approach, catalog)

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
        "rom_warnings": _rom_warnings(exercises),
        "whole_body": _whole_body(work, exercises),
        "load": load,
        "session_sequences": sequences,
        "limiter_conflicts": _limiter_conflicts(sequences),
        "totals": _totals(work, load.get("weekly_rate")),
        "ideal_settings": IDEAL_SETTINGS,
        "restrictions": restrictions,
        "focus": cfg.get("focus", {}) or {},
        "approach": approach,
        "session_plan": _session_plan(exercises, restrictions,
                                      cfg.get("focus", {}) or {},
                                      approach, load),
        "featured": featured,
    }


# =============================================================================
# Optional AI narrative (uses the user's own Claude API key)
# =============================================================================
AI_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
AI_EFFORT_DEFAULT = "medium"


def ai_narrative(report: dict, cfg: dict) -> str | None:
    """Ask Claude to analyse the findings + suggest a session. Key stays local.

    The model never sees raw personal data - only aggregated, name-free
    metrics. Two kinds of input are handed over and kept apart in the system
    prompt: the deterministic metrics (trend, load/recovery, ROM validity,
    flags, limiter conflicts) are FINAL and must not be recomputed or
    overridden; the ordered session sequences are raw material in which the
    model may look for patterns the rules do not cover (ordering, pacing,
    settings changes). Cost is steered by the 'ai_effort' config option
    (low | medium | high | xhigh | max, default medium).
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
    effort = str(cfg.get("ai_effort") or AI_EFFORT_DEFAULT).lower()
    if effort not in AI_EFFORT_LEVELS:
        effort = AI_EFFORT_DEFAULT

    # Units: convert force/length so the model answers in the user's units.
    imp = cfg.get("units", "imperial") == "imperial"
    ff = 2.20462 if imp else 1.0
    lf = (1 / 2.54) if imp else 1.0
    FU, LU = ("lb", "in") if imp else ("kg", "cm")
    kg = lambda v: round(v * ff, 1) if v is not None else None
    cm = lambda v: round(v * lf, 1) if v is not None else None

    # Hand the model a compact, name-free summary: precomputed metrics (final)
    # plus the ordered session sequences (raw material for its own observations).
    # Exercise names/groups come from the catalog.
    f = report.get("featured")
    summary = {
        "units": {"force": FU, "length": LU},
        "goal": report["goal"],
        "restrictions": report.get("restrictions", {}),   # body part -> ok/careful/avoid
        "focus": report.get("focus", {}),                 # group -> more/normal/less/off
        "approach": report.get("approach", "auto"),       # full | split | auto
        "ideal_settings": report.get("ideal_settings"),   # textbook machine settings
        "sessions_per_week_target": report.get("sessions_per_week"),
        "training_days": report["training_days"],
        "exercises": [{
            "name": e["name"], "group": e["group"], "kind": e["kind"],
            "targets": e.get("targets", []), "limiters": e.get("limiters", []),
            "restriction": e.get("restriction", "ok"),
            "personal_best": kg(e["pb"]),
            "latest_day_best": kg(e["last"]),   # best set of the most recent training day
            "training_days": e["n"], "total_sets": e["total_sets"],
            "days_with_multiple_sets": e["multi_set_days"],
            "trend_per_session": kg(e["trend_per_session"]),   # per training-day occurrence
            "trend_per_day": kg(e["trend_per_day"]),           # per calendar day
            "trend_based_on_days": e["trend_n"],               # ROM-comparable days only
            "rom_reference": cm(e["rom_cm_reference"]),
            "rom_latest": cm(e["rom_cm_latest"]),
            "rom_drift_pct": e["rom_drift_pct"],
            "rom_stable": e["rom_stable"],
            "days_excluded_for_rom": e["days_excluded_for_rom"],
        } for e in report["exercises"]],
        "rom_warnings": [{
            "name": w["name"], "rom_reference": cm(w["rom_cm_reference"]),
            "rom_latest": cm(w["rom_cm_latest"]), "rom_drift_pct": w["rom_drift_pct"],
            "days_excluded_for_rom": w["days_excluded_for_rom"],
            "trend_available": w["trend_available"],
            "observed": [{"date": o["date"], "rom": cm(o["rom_cm"]), "best": kg(o["kg"]),
                          "comparable": o["valid"]} for o in w["observed"]],
        } for w in report.get("rom_warnings", [])],
        "whole_body_index_per_day": report["whole_body"]["series"],
        "load_and_recovery": report["load"],
        # Raw material for the model's OWN observations (see system prompt):
        # the ordered sets of the last training days, in the user's units.
        "session_sequences": [{
            "date": d["date"], "working_sets": d["working_sets"], "visits": d["visits"],
            "groups_touched": d["groups_touched"], "wall_minutes": d["wall_minutes"],
            "time_under_load_min": d["time_under_load_min"], "tul_ratio": d["tul_ratio"],
            "work_density_per_min": kg(d["work_density_per_min"]),
            "flags": d["flags"],
            "sets": [{
                "order": x["order"], "exercise": x["exercise"], "group": x["group"],
                "minutes_since_prev_set": x["minutes_since_prev_set"],
                "peak": kg(x["max_kg"]), "mean_force": kg(x["mean_force_kg"]),
                "seconds": x["seconds"], "reps": x["reps"], "rom": cm(x["rom_cm"]),
                "inroad": x["inroad"], "effort": x["effort"],
                "pause_end_s": x["pause_end_s"], "pause_return_s": x["pause_return_s"],
                "repeat_of_earlier_set": x["repeat_of_earlier_set"],
                "minutes_since_same_exercise": x["minutes_since_same_exercise"],
                "limiters": x["limiters"], "limiters_pre_fatigued": x["limiters_pre_fatigued"],
                "limiters_pre_fatigued_by": x["limiters_pre_fatigued_by"],
            } for x in d["sets"]],
        } for d in report.get("session_sequences", [])],
        "limiter_conflicts": report.get("limiter_conflicts", []),
        "totals": report["totals"],
        "session_plan": report["session_plan"],
        "featured_set": {
            "exercise": f["meta"].get("name") if f else None,
            "group": f["meta"].get("group") if f else None,
            "why_featured": f.get("reason") if f else None,   # pb | deep | submax | recent
            "peak": kg(f["meta"]["max_kg"]) if f else None,
            "rep_peaks": [kg(v) for v in f["rep_peaks"]] if f else None,
            "inroad_pct": f["inroad_pct"] if f else None,
            "settings": f.get("settings") if f else None,   # actual machine settings of this set
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
        "and by whether sets reach real inroad. Two trend figures are given per "
        "exercise: trend_per_session = change per training-day occurrence, "
        "trend_per_day = change per calendar day (the two differ by the training "
        "frequency); quote whichever you mean and never swap them.\n"
        "RANGE OF MOTION (ROM) VALIDITY: on this machine the force depends on "
        "the position range the set was performed over - a shorter ROM stays in "
        "the strong part of the movement and gives a higher peak. A force "
        "comparison between days is therefore ONLY valid when the ROM matched "
        "(within ~5% of the exercise's reference ROM). Per exercise you get "
        "rom_reference, rom_latest, rom_drift_pct, rom_stable, "
        "days_excluded_for_rom and trend_based_on_days; trend values are already "
        "computed on comparable days only and are null when too few exist. "
        "rom_warnings lists exercises whose ROM drifted, with the observed ROM "
        "and best force per day. For those, do NOT claim progress or a drop from "
        "the force numbers; say the values are not comparable and ask the user "
        "to set fixed start and end positions for that exercise so future "
        "sessions can be compared.\n"
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
        "TWO KINDS OF INPUT - keep them apart: (1) all precomputed metrics "
        "(trends, load_and_recovery, ROM validity, session flags, "
        "limiter_conflicts, session_plan) are FINAL - do not recompute, "
        "contradict or override them. (2) session_sequences is RAW MATERIAL: for "
        "each of the last training days the working sets in time order with the "
        "rest before each set (minutes_since_prev_set = rest since the previous "
        "set ended), peak and mean force, duration, reps, ROM, inroad/effort, the "
        "machine pauses (pause_end_s / pause_return_s), whether the set repeats "
        "an exercise already done that day, and which limiting muscles were "
        "already fatigued by an earlier set. You MAY and SHOULD look for patterns "
        "in it that the rules do not cover - e.g. exercise order within a day, "
        "pacing, repeated sets that add nothing, settings that changed between "
        "sessions - and report them as your own observations, clearly grounded "
        "in the listed numbers. Never invent values that are not in the data.\n"
        "SESSION FLAGS (rule-based, in session_sequences[].flags, each with its "
        "numbers): too_many_sets (more than 6 working sets, or more than 4 in a "
        "dense session); scattered_session (Push, Pull and Drive all on one day "
        "although the approach is 'split'); short_intra_session_rest (the same "
        "exercise or the same limiting muscle loaded again within 5 min); "
        "density_shift (work per wall-clock minute deviating > 20 % from the "
        "previous sessions - usually a pause/tempo/rest change). Explain what "
        "each raised flag means for the athlete and how to fix it next time.\n"
        "SHARED LIMITERS: exercises carry 'targets' (what they are for) and "
        "'limiters' (structures that give out first although they are only a "
        "means - e.g. the grip on Dead Lift, Row and Pull Down). Several sets "
        "sharing a limiter fatigue it cumulatively within a session, so a later "
        "set may end early on the limiter while the target muscles were not "
        "fully worked. limiter_conflicts lists, per day, the sets whose limiter "
        "was already loaded by an earlier set (by which exercise, how many "
        "minutes before). Rule for ordering: an exercise where the limiter is "
        "only a means (Dead Lift, Row) goes BEFORE an exercise whose target is "
        "that same structure (Biceps Curl), and the same limiter should not be "
        "hit twice within a few minutes; session_plan already follows this on "
        "top of 'large muscle groups first'.\n"
        "Give short, concrete, scientifically defensible observations and ONE "
        "session suggestion. It MUST include WHEN to train next (how many rest "
        "days / the earliest sensible date — use load_and_recovery."
        "recommended_rest_days and next_earliest) AND WHICH exercises, in what "
        "order (large muscle groups first unless focus says otherwise, ~15 min, "
        "matched to the goal, weekly frequency, focus, approach and the "
        "restrictions above). Also state the recommended machine SETTINGS from "
        "ideal_settings (~8 reps, ~5 s per movement direction, ~3 s hold at the "
        "end position, no pause on the return; note the end-hold does not suit "
        "every exercise type) and, if the featured set's actual settings deviate "
        "from these, point it out. Never give "
        f"medical advice. Use the units given in the data ({FU} and {LU}). "
        f"Answer in {'German' if cfg.get('language','en')=='de' else 'English'}, "
        "in a few short sentences with clear headings."
    )
    msg = client.messages.create(
        model=model,
        max_tokens=16000,                        # room for adaptive thinking + a real analysis
        thinking={"type": "adaptive"},           # on by default for Opus 5 / Fable
        output_config={"effort": effort},        # analysis task now; user-tunable via 'ai_effort'
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
