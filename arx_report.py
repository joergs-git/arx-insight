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
# A normal ARX set is ~8 reps over 80-190 s. Anything with fewer than MIN_REPS
# reps or under MIN_SECONDS is a machine test, a familiarisation set, a
# positioning attempt or an abort - never training. See classify_set() and
# flag_false_starts(); only 'working' sets reach any analysis.
MIN_SECONDS = 40.0             # shorter = test / familiarisation / false start
MIN_REPS = 4                   # fewer   = test set or aborted attempt
RESTART_GAP_MIN = 3            # same exercise started again within this many minutes
                               # with MORE reps -> the earlier one was a false start

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
def classify_set(sec: float, reps: int, concentric: float, eccentric: float,
                 ended_early: bool, has_events: bool) -> tuple[str, str]:
    """Decide whether a recorded set is a real WORKING set or noise.

    Everything downstream (bests, trends, effort, recovery, sequences, totals,
    the AI) only ever sees working sets, so the "general intelligence" about
    aborted attempts lives here. Statuses:
      working      cleared every filter below
      no_data      no force data / no event stream - nothing to analyse
      aborted      the machine logged SequenceEndedBeforeCompletion and fewer
                   than MIN_REPS reps were done (athlete quit or restarted)
      short        fewer than MIN_REPS reps or under MIN_SECONDS - a machine
                   test, familiarisation or positioning set, not training
    A fifth status, false_start, is assigned afterwards by flag_false_starts()
    because it needs the NEXT set of the same exercise. A timed protocol that
    ends on the clock after 11-14 reps carries the ended-early event too but
    has plenty of reps, so it stays a working set. Returns (status, reason).
    """
    if concentric <= 0 or eccentric <= 0 or not has_events:
        return "no_data", "no force data"
    if ended_early and reps < MIN_REPS:
        return "aborted", f"ended early after {reps} reps ({sec:.0f} s)"
    if reps < MIN_REPS or sec < MIN_SECONDS:
        return "short", f"{reps} reps / {sec:.0f} s - test or familiarisation set"
    return "working", ""


def flag_false_starts(sets: list[dict]) -> None:
    """Mark a set 'false_start' when the SAME exercise was started again within
    RESTART_GAP_MIN minutes of its end and that later set has more reps: the
    athlete re-positioned / re-armed and the real set is the second one.
    Typical on ARX: a 12-40 s attempt followed by the full 8-rep set a minute
    later. The attempt must not count as a set - it would otherwise look like a
    weak repeat, a short intra-session rest or a bogus day-best. In place."""
    by_ex: dict = {}
    for s in sets:
        by_ex.setdefault(s["exercise"], []).append(s)
    for ss in by_ex.values():
        ss.sort(key=lambda s: s["date"])
        for a, b in zip(ss, ss[1:]):
            ta, tb = _ts(a["date"]), _ts(b["date"])
            if ta is None or tb is None or a["status"] == "no_data":
                continue
            gap_min = (tb - (ta + a["seconds"])) / 60.0
            if gap_min <= RESTART_GAP_MIN and b["reps"] > a["reps"]:
                a["status"] = "false_start"
                a["reason"] = f"restarted {max(gap_min, 0):.1f} min later with {b['reps']} reps"
                a["working"] = False


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

        reps, ended_early, has_events = 0, False, False
        try:
            events = json.loads(ev.decode("latin1"))
            has_events = bool(events)
            reps = sum(1 for e in events if e.get("Type") == "EndRep")
            # logged by the machine when the set stopped before the programmed
            # rep count / time was reached (athlete quit, false start, restart)
            ended_early = any(e.get("Type") == "SequenceEndedBeforeCompletion" for e in events)
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
        status, reason = classify_set(sec, reps, c, e, ended_early, has_events)

        out.append({
            "id": r["ID"],
            "date": str(r["EXERCISEDATE"]),
            "session": r["SESSION"],
            "exercise": r["EXERCISE"],
            "protocol": r["PROTOCOL"],
            "reps": reps,
            "ended_early": ended_early,
            "status": status,                  # working | short | aborted | no_data (| false_start)
            "reason": reason,                  # why it is not a working set ("" when it is)
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
            "working": status == "working",
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


def rep_segments(curve: list[dict], events: list[dict]) -> list[dict]:
    """Per rep (BeginRep/EndRep pairs): the peak force and WHEN it occurred, so
    the UI can place the rep markers at their real position on the time axis
    (matters once a second curve is overlaid). An unmatched trailing BeginRep
    (aborted last rep) is simply dropped by the pairwise zip."""
    begins = [e["_t"] for e in events if e.get("Type") == "BeginRep"]
    ends = [e["_t"] for e in events if e.get("Type") == "EndRep"]
    out = []
    for a, b in zip(begins, ends):
        seg = [p for p in curve if a <= p["t"] <= b]
        if seg:
            top = max(seg, key=lambda p: p["force_kg"])
            out.append({"t": top["t"], "peak": top["force_kg"]})
    return out


def _inroad(rep_peaks: list[float]) -> int | None:
    """Inroad = decline from the strongest rep's peak to the LAST rep's peak, in %.
    It is THE effort signal on an adaptive-resistance machine: a set that
    reached deep fatigue ends well below its strongest rep; a sub-maximal or
    ramping set ends at or near it. None with fewer than two complete reps."""
    if len(rep_peaks) >= 2 and max(rep_peaks) > 0:
        return round((max(rep_peaks) - rep_peaks[-1]) / max(rep_peaks) * 100)
    return None


def rep_peaks_and_inroad(curve: list[dict], events: list[dict]) -> tuple[list[float], int | None]:
    """Segment the curve per rep and compute the inroad (see rep_segments / _inroad).
    Returns (rep_peaks, inroad_pct)."""
    rep_peaks = [s["peak"] for s in rep_segments(curve, events)]
    return rep_peaks, _inroad(rep_peaks)


def classify_effort(inroad: int | None, rising: bool) -> str:
    """Map inroad % (+ whether force was still rising at the end) to an effort label."""
    if inroad is None:
        return "unknown"
    if inroad >= INROAD_DEEP:
        return "deep"
    if inroad >= INROAD_MODERATE and not rising:
        return "moderate"
    return "submax"


CURVE_POINTS = 300             # curve samples handed to the UI per set (downsampled)


def _downsample(curve: list[dict], n: int = CURVE_POINTS) -> list[dict]:
    """Thin a ~20 Hz curve to about n evenly spaced samples (the last one kept)."""
    if len(curve) <= n:
        return curve
    step = len(curve) / n
    return [curve[int(i * step)] for i in range(n)] + [curve[-1]]


def set_curve(con, set_id: int) -> dict:
    """Downsampled curve + per-rep peaks with their times + inroad of one set
    (see load_curve) - what a force-curve card in the UI needs."""
    curve, events = load_curve(con, set_id)
    segs = rep_segments(curve, events)
    peaks = [s["peak"] for s in segs]
    return {"curve": _downsample(curve), "rep_peaks": peaks,
            "rep_times": [s["t"] for s in segs], "inroad_pct": _inroad(peaks)}


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


def _exercise_series(work: list[dict], catalog: dict, restrictions: dict | None = None) -> list[dict]:
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
    and the trend is None when fewer than MIN_TREND_POINTS comparable days exist.
    The last day is also compared with the previous one (delta_pct, only
    meaningful when delta_comparable = same ROM) so the coach can talk about
    the most recent session in concrete numbers."""
    by_ex = {}
    for s in work:
        by_ex.setdefault(s["exercise"], []).append(s)
    d0 = min(datetime.fromisoformat(s["date"][:10]).toordinal() for s in work) if work else 0
    last_day_all = max((s["date"][:10] for s in work), default=None)

    out = []
    for ex, ss in by_ex.items():
        meta = catalog.get(str(ex), {})
        # aggregate to daily best (the best SET carries the day's context)
        by_day = {}
        for s in ss:
            day = s["date"][:10]
            d = by_day.setdefault(day, {"kg": 0, "best": None, "sets": 0, "sessions": set()})
            if s["max_kg"] >= d["kg"]:
                d["kg"], d["best"] = s["max_kg"], s
            d["sets"] += 1
            d["sessions"].add(s["session"])
        days_sorted = sorted(by_day)
        def occ_row(i, day):
            b = by_day[day]["best"]
            return {"i": i,
                    "day": datetime.fromisoformat(day).toordinal() - d0,
                    "date": day, "kg": by_day[day]["kg"],
                    "id": b["id"],                          # the best set of that day
                    "rom_cm": b.get("rom_cm"),              # ROM of that day's best set
                    "inroad": b.get("inroad"), "effort": b.get("effort"),
                    "mean_force_kg": b.get("mean_force_kg"), "eccentric_kg": b.get("eccentric_kg"),
                    "reps": b.get("reps"), "seconds": b.get("seconds"),
                    "pause_end_s": b.get("pause_end_s"), "pause_return_s": b.get("pause_return_s"),
                    "sets": by_day[day]["sets"],            # sets of this exercise that day
                    "sessions": len(by_day[day]["sessions"])}
        occ = [occ_row(i, day) for i, day in enumerate(days_sorted)]

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
        restricted = exercise_restriction(meta.get("name", ""), restrictions or {}, meta.get("joints")) != "ok"
        if restricted and occ[-1]["rom_cm"]:
            # a limited athlete may deliberately shorten the range: the latest
            # setting is the baseline, not a "fix your positions" nag
            rom_ref = occ[-1]["rom_cm"]
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
        # last day vs the previous day of this exercise (the coach's "vs last time")
        last, prev = occ[-1], (occ[-2] if len(occ) > 1 else None)
        comparable = bool(prev and prev["rom_cm"] and last["rom_cm"]
                          and abs(last["rom_cm"] - prev["rom_cm"]) / prev["rom_cm"] <= ROM_TOLERANCE)
        pb = max(o["kg"] for o in occ)
        out.append({
            "ex": ex,
            "name": meta.get("name", f"Übung {ex}"),
            "group": meta.get("group", "?"),
            "kind": meta.get("kind", "?"),
            "targets": list(meta.get("targets") or []),     # muscles the exercise is for
            "limiters": list(meta.get("limiters") or []),   # what gives out first (a means, not the goal)
            "joints": list(meta.get("joints") or []),       # body parts a restriction can hit
            "pb": pb,
            "n": len(occ),                              # number of training DAYS
            "total_sets": len(ss),                      # all sets across all days
            "multi_set_days": sum(1 for o in occ if o["sets"] > 1),
            "first": occ[0]["kg"], "last": occ[-1]["kg"],  # first/last DAY best
            "last_date": last["date"],
            "in_last_session": last["date"] == last_day_all,
            "last_inroad": last["inroad"], "last_effort": last["effort"],
            "prev_date": prev["date"] if prev else None,
            "prev_kg": prev["kg"] if prev else None,
            "prev_inroad": prev["inroad"] if prev else None,
            "delta_pct": round((last["kg"] - prev["kg"]) / prev["kg"] * 100, 1) if (prev and prev["kg"]) else None,
            "delta_comparable": comparable,             # same ROM on both days -> delta is meaningful
            "delta_vs_pb_pct": round((last["kg"] - pb) / pb * 100, 1) if pb else None,
            "restricted": restricted,
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
            "restricted": e.get("restricted", False),   # ROM may be shortened on purpose
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
    makes sense with broad muscle-group coverage, so we report coverage too.
    Only ROM-comparable days count (see _exercise_series): a best set at a
    shorter range is not a personal best, and a day at a different ROM cannot
    be scored against one - so both the PB and the day entries are ROM-gated."""
    valid = {(e["ex"], o["date"]) for e in exercises for o in e["occ"] if o.get("rom_valid")}
    pb = {}
    for e in exercises:
        kgs = [o["kg"] for o in e["occ"] if o.get("rom_valid")]
        pb[e["ex"]] = max(kgs) if kgs else e["pb"]
    total = len(pb)
    series = []
    for day in sorted({s["date"][:10] for s in work}):
        best = {}
        for s in (x for x in work if x["date"][:10] == day and (x["exercise"], day) in valid):
            best[s["exercise"]] = max(best.get(s["exercise"], 0), s["max_kg"])
        if not best:
            continue                       # nothing comparable that day
        idx = st.mean([v / pb[e] * 100 for e, v in best.items()])
        series.append({"date": day[5:], "index": round(idx),
                       "coverage": len(best), "of": total})
    return {"series": series,
            "coverage_max": max((w["coverage"] for w in series), default=0),
            "total_exercises": total}


def _totals(work: list[dict], weekly_rate, sequences: list[dict]) -> dict:
    """Motivational totals: how much work and time the training added up to.

    'Time under load' is the actual working time (sum of set durations - the
    number that stays near the ~15 min ideal). 'Session wall-clock' includes the
    rest between sets. 'Work' is the summed impulse (mean force x time), a
    volume proxy. Per-week/month/year figures are projections from the current
    cadence, so they are labelled as such in the UI. A 'session' is a VISIT as
    found by _session_sequences (a gap > VISIT_GAP_MIN starts a new one) - the
    DB's SESSION ids are no visit markers (several ids within minutes, or one
    id across a 7 h gap)."""
    tul_sec = sum(s["seconds"] for s in work)
    work_impulse = sum(s["impulse_kg_s"] for s in work)

    n_sessions = sum(d["visits"] for d in sequences)
    walls = [d["wall_minutes"] for d in sequences if d["wall_minutes"]]
    total_wall = round(sum(walls), 1) if walls else None
    avg_wall = round(total_wall / n_sessions, 1) if (n_sessions and total_wall is not None) else None
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
        "total_wall_min": total_wall,
        "avg_session_wall_min": avg_wall,
        "avg_session_tul_min": avg_tul,
        "total_work_impulse": round(work_impulse),
        "projection": proj,
    }


# --- recovery model ------------------------------------------------------------
# Recovery is judged per MUSCLE - not per Push/Pull/Drive group and not by the
# calendar alone. A set loads its target muscles at the effort the set actually
# reached and its limiters one rank lower (a limiter works, but only as a means).
# A muscle is ready again when the rest its hardest recent load required has
# elapsed. That is what makes two sessions on consecutive days fine when they
# used different muscles, and what lets one deep isolation set block only the
# exercises that need that muscle fresh instead of the whole next day.
EFFORT_RANK = {"deep": 3, "moderate": 2, "submax": 1, "unknown": 2}
RANK_LABEL = {3: "deep", 2: "moderate", 1: "submax"}
REQUIRED_REST = {3: 3, 2: 2, 1: 1}     # days a muscle needs after a load of that rank
RECOVERY_LOOKBACK_DAYS = 14            # older loads cannot still be limiting
RECENT_WINDOW_DAYS = 7                 # the load flag is judged on this window before today
DETRAINING_GAP_DAYS = 10               # longer without training -> adaptation is being lost
# daily check-in: soreness regions -> the muscle vocabulary of exercises.json
SORENESS_REGIONS = {
    "legs": ["quads", "glutes", "hamstrings", "calves"],
    "back": ["lats", "upper_back", "lower_back"],
    "chest": ["chest"],
    "arms": ["elbow_flexors", "triceps", "grip"],
    "shoulders": ["shoulders"],
}


def _today(cfg: dict) -> date:
    """Today - overridable via cfg['_today'] or the ARX_TODAY environment
    variable (ISO date) for reproducible tests and screenshots."""
    t = (cfg or {}).get("_today") or os.environ.get("ARX_TODAY")
    try:
        return date.fromisoformat(t) if t else date.today()
    except Exception:
        return date.today()


def _set_loads(s: dict, catalog: dict) -> list[tuple[str, int, str]]:
    """Which muscles one set loads and how hard: (muscle, rank, role). Targets
    at the set's effort rank, limiters one rank lower (min 1). An exercise
    without catalog targets loads its group name as a pseudo-muscle, so an
    unknown exercise never silently drops out of the model."""
    meta = catalog.get(str(s["exercise"]), {})
    rank = EFFORT_RANK.get(s.get("effort"), 2)
    targets = list(meta.get("targets") or []) or [s.get("group") or "?"]
    limiters = [l for l in (meta.get("limiters") or []) if l not in targets]
    return [(m, rank, "target") for m in targets] + [(m, max(rank - 1, 1), "limiter") for m in limiters]


def _recovery(work: list[dict], catalog: dict, exercises: list[dict], today: date,
              checkin: dict | None = None) -> dict:
    """Muscle-level readiness for TODAY, per muscle, per exercise and per group.

    Per muscle the binding load is the recent load whose rest requirement ends
    last: ready_on = that day + REQUIRED_REST[rank]. The daily check-in can
    override the calendar: strong soreness in a region keeps its muscles not
    ready today, mild soreness marks them 'limited' (train, but sub-max).
    Per exercise: ready (all targets ready) | limited (targets ready, but a
    limiter is not fresh or a target is mildly sore) | not_ready (a target
    is not ready). Groups get a compatibility rollup for the UI / planner."""
    today_ord = today.toordinal()
    per_muscle_day: dict = {}          # muscle -> {ordinal: (rank, exercise, role)}
    for s in work:
        o = date.fromisoformat(s["date"][:10]).toordinal()
        if today_ord - o > RECOVERY_LOOKBACK_DAYS:
            continue
        for m, rank, role in _set_loads(s, catalog):
            cell = per_muscle_day.setdefault(m, {})
            if rank > cell.get(o, (0, "", ""))[0]:
                cell[o] = (rank, s["name"], role)

    muscles: dict = {}
    for m, days in per_muscle_day.items():
        last_ord = max(days)
        binding = max(days, key=lambda o: (o + REQUIRED_REST[days[o][0]], days[o][0]))
        rank_b, ex_b, role_b = days[binding]
        ready_ord = binding + REQUIRED_REST[rank_b]
        muscles[m] = {
            "ready_on": date.fromordinal(ready_ord).isoformat(),
            "ready": today_ord >= ready_ord,
            "days_since": today_ord - last_ord,
            "last_date": date.fromordinal(last_ord).isoformat(),
            "last_effort": RANK_LABEL[days[last_ord][0]],
            "last_exercise": days[last_ord][1],
            "needed_days": REQUIRED_REST[rank_b],
            "reason": f"{RANK_LABEL[rank_b]} {ex_b} {date.fromordinal(binding).isoformat()}"
                      + (" (as limiter)" if role_b == "limiter" else ""),
            "sore": None,
        }
    # the check-in beats the calendar: sore muscles are not recovered, whatever the dates say
    for region, level in ((checkin or {}).get("soreness") or {}).items():
        if level not in ("mild", "strong"):
            continue
        for m in SORENESS_REGIONS.get(region, []):
            cell = muscles.setdefault(m, {"ready_on": today.isoformat(), "ready": True, "days_since": None,
                                          "last_date": None, "last_effort": None, "last_exercise": None,
                                          "needed_days": 0, "reason": "", "sore": None})
            cell["sore"] = level
            if level == "strong":
                tomorrow = (today + timedelta(days=1)).isoformat()
                cell["ready_on"] = max(cell["ready_on"], tomorrow)
                cell["ready"] = False
                cell["reason"] = "strong soreness (check-in)" + (f" · {cell['reason']}" if cell["reason"] else "")

    ex_rows = []
    for e in exercises:
        targets = list(e.get("targets") or []) or [e.get("group") or "?"]
        limiters = [l for l in (e.get("limiters") or []) if l not in targets]
        t_block = [m for m in targets if m in muscles and not muscles[m]["ready"]]
        l_block = [m for m in limiters if m in muscles and not muscles[m]["ready"]]
        t_mild = [m for m in targets if muscles.get(m, {}).get("sore") == "mild"]
        if t_block:
            status, ready_on, fresh_on = "not_ready", max(muscles[m]["ready_on"] for m in t_block), None
            why = "; ".join(f"{m}: {muscles[m]['reason']}" for m in t_block)
        elif l_block or t_mild:
            status, ready_on = "limited", today.isoformat()
            fresh_on = max([muscles[m]["ready_on"] for m in l_block] + [today.isoformat()])
            why = "; ".join([f"{m}: {muscles[m]['reason']} (fresh {muscles[m]['ready_on']})" for m in l_block]
                            + [f"{m}: mild soreness (check-in)" for m in t_mild])
        else:
            status, ready_on, fresh_on, why = "ready", today.isoformat(), today.isoformat(), ""
        ex_rows.append({"name": e["name"], "group": e["group"], "ex": e["ex"], "status": status,
                        "ready_on": ready_on, "fresh_on": fresh_on,
                        "limited_by": sorted(set(l_block + t_mild)), "blocked_by": t_block, "reason": why})

    # group rollup (compatibility for the UI and the planner)
    per_group = {}
    for grp in ("Push", "Pull", "Drive"):
        rows = [r for r in ex_rows if r["group"] == grp]
        by_day: dict = {}
        for s in (x for x in work if x.get("group") == grp):
            d = s["date"][:10]
            r = EFFORT_RANK.get(s.get("effort"), 2)
            cell = by_day.setdefault(d, {"rank": 0, "inroad": None})
            if r >= cell["rank"]:
                cell["rank"], cell["inroad"] = r, s.get("inroad")
        gdays = sorted(by_day)
        gords = [date.fromisoformat(d).toordinal() for d in gdays]
        last_day = gdays[-1] if gdays else None
        per_group[grp] = {
            "days": len(gdays),
            "min_gap": min((b - a for a, b in zip(gords, gords[1:])), default=None),
            "hard_days": sum(1 for d in gdays if by_day[d]["rank"] >= 2),
            "submax_days": sum(1 for d in gdays if by_day[d]["rank"] == 1),
            "last_effort": RANK_LABEL.get(by_day[last_day]["rank"]) if last_day else None,
            "last_inroad": by_day[last_day]["inroad"] if last_day else None,
            "days_since": (today_ord - gords[-1]) if gords else None,
            "ready": any(r["status"] in ("ready", "limited") for r in rows) if rows else True,
            "ready_on": min((r["ready_on"] for r in rows), default=today.isoformat()),
        }

    ready_today = [r["name"] for r in ex_rows if r["status"] == "ready"]
    limited_today = [{"name": r["name"], "limited_by": r["limited_by"], "fresh_on": r["fresh_on"], "reason": r["reason"]}
                     for r in ex_rows if r["status"] == "limited"]
    not_ready = [{"name": r["name"], "ready_on": r["ready_on"], "blocked_by": r["blocked_by"], "reason": r["reason"]}
                 for r in ex_rows if r["status"] == "not_ready"]
    if ready_today or limited_today or not ex_rows:
        next_earliest = today.isoformat()
    else:
        next_earliest = min(r["ready_on"] for r in not_ready)
    return {
        "muscles": muscles,
        "exercises": ex_rows,
        "ready_today": ready_today,
        "limited_today": limited_today,
        "not_ready": not_ready,
        "next_earliest": next_earliest,
        "recommended_rest_days": (date.fromisoformat(next_earliest) - today).days,
        "all_ready_on": max((r["ready_on"] for r in ex_rows), default=today.isoformat()),
        "per_group": per_group,
    }


def _session_transitions(work: list[dict], catalog: dict) -> list[dict]:
    """Consecutive training days compared on the muscle level.

    For every training day after the first: the gap to the previous training
    day, which muscles both days loaded, and - checked against each muscle's
    OWN previous load, not only the previous day - whether a muscle was loaded
    moderate-or-harder again before the rest its previous hard load required
    had elapsed ('conflict'). A day right after another day is fine when
    different muscles were used, or when the repeat was light."""
    loads: dict = {}                   # date -> {muscle: (rank, exercise)}
    for s in work:
        d = s["date"][:10]
        for m, rank, _role in _set_loads(s, catalog):
            if rank > loads.setdefault(d, {}).get(m, (0, ""))[0]:
                loads[d][m] = (rank, s["name"])
    days = sorted(loads)
    prev_load: dict = {}               # muscle -> (ordinal, rank, exercise, date)
    out = []
    for i, d in enumerate(days):
        o = date.fromisoformat(d).toordinal()
        conflicts, repeated = [], []
        for m, (rank, ex) in loads[d].items():
            if m in prev_load:
                po, prank, pex, pdate = prev_load[m]
                gap, need = o - po, REQUIRED_REST[prank]
                if prank >= 2 and rank >= 2 and gap < need:
                    conflicts.append({"muscle": m, "prev_date": pdate, "prev_effort": RANK_LABEL[prank],
                                      "prev_exercise": pex, "effort": RANK_LABEL[rank], "exercise": ex,
                                      "gap_days": gap, "needed_days": need})
                if i and pdate == days[i - 1]:
                    repeated.append(m)
        for m, (rank, ex) in loads[d].items():
            prev_load[m] = (o, rank, ex, d)
        if i == 0:
            continue
        gap_prev = o - date.fromisoformat(days[i - 1]).toordinal()
        verdict = "conflict" if conflicts else ("fine" if repeated else "no_overlap")
        if conflicts:
            detail = (f"{gap_prev} day(s) after {days[i - 1]} - conflict: "
                      + ", ".join(f"{c['muscle']} {c['prev_effort']} -> {c['effort']} after {c['gap_days']} of {c['needed_days']} days"
                                  for c in conflicts))
        elif repeated:
            detail = f"{gap_prev} day(s) after {days[i - 1]} - {', '.join(sorted(repeated))} loaded again, no conflict"
        else:
            detail = f"{gap_prev} day(s) after {days[i - 1]} - different muscles, no overlap"
        out.append({"date": d, "prev_date": days[i - 1], "gap_days": gap_prev,
                    "muscles_repeated": sorted(repeated), "conflicts": conflicts,
                    "verdict": verdict, "detail": detail})
    return out


def _load_analysis(work: list[dict], catalog: dict, exercises: list[dict], today: date,
                   checkin: dict | None = None) -> dict:
    """Recovery / load signals from how training is spaced over time.

    Base idea of ARX-style high-intensity training: brief, all-out, and
    INFREQUENT. Recovery is effort-conditioned (~48-72 h only after a truly
    maximal load, far less after a sub-maximal one) and judged per MUSCLE
    (see _recovery / _session_transitions). The overall flag looks only at the
    last RECENT_WINDOW_DAYS before today - what happened in the first weeks is
    history, not today's state:
      overload_risk    a hard-on-hard conflict inside the window, or the
                       check-in reports an elevated resting HR plus strong soreness
      detraining_risk  more than DETRAINING_GAP_DAYS since the last session
      underload        two or more sessions in the window, none moderate or harder
      ok               otherwise
    History totals are kept for the AI. The final judgement is the AI's, with
    these numbers and the guidance in its prompt."""
    today_ord = today.toordinal()
    days = sorted({s["date"][:10] for s in work})
    ords = [date.fromisoformat(d).toordinal() for d in days]
    gaps = [b - a for a, b in zip(ords, ords[1:])]           # days between training days
    span = (ords[-1] - ords[0]) if len(ords) > 1 else 0
    weekly_rate = round(len(days) / (span / 7), 1) if span else None
    sessions_last7 = sum(1 for o in ords if today_ord - o < 7)
    day_rank = {d: max(EFFORT_RANK.get(s.get("effort"), 2) for s in work if s["date"][:10] == d) for d in days}
    hard_days = sum(1 for d in days if day_rank[d] >= 2)
    submax_days = sum(1 for d in days if day_rank[d] < 2)

    recovery = _recovery(work, catalog, exercises, today, checkin)
    transitions = _session_transitions(work, catalog)
    all_conflicts = [c for t in transitions for c in t["conflicts"]]

    window_start = today_ord - RECENT_WINDOW_DAYS
    w_days = [d for d in days if date.fromisoformat(d).toordinal() >= window_start]
    w_conflicts = [dict(c, date=t["date"]) for t in transitions
                   if date.fromisoformat(t["date"]).toordinal() >= window_start for c in t["conflicts"]]
    w_light = [d for d in w_days if day_rank[d] < 2]
    ci = checkin or {}
    strong_sore = any(v == "strong" for v in (ci.get("soreness") or {}).values())
    if w_conflicts or (ci.get("rhr_status") == "elevated" and strong_sore):
        flag = "overload_risk"
    elif days and (today_ord - ords[-1] > DETRAINING_GAP_DAYS or (gaps and gaps[-1] > DETRAINING_GAP_DAYS)):
        flag = "detraining_risk"
    elif len(w_days) >= 2 and len(w_light) == len(w_days):
        flag = "underload"
    else:
        flag = "ok"

    return {
        "today": today.isoformat(),
        "training_days": len(days),
        "gaps_days": gaps,
        "median_gap_days": sorted(gaps)[len(gaps) // 2] if gaps else None,
        "min_gap_days": min(gaps) if gaps else None,
        "days_since_last": (today_ord - ords[-1]) if ords else None,
        "sessions_last7": sessions_last7,
        "weekly_rate": weekly_rate,
        "hard_sessions": hard_days,                  # training days with at least one moderate/deep set
        "submax_sessions": submax_days,
        "insufficient_recovery_after_hard": len(w_conflicts),        # conflicts in the recent window
        "insufficient_recovery_after_hard_total": len(all_conflicts),  # ... over the whole history
        "conflicts_recent": w_conflicts,
        "window": {"days": RECENT_WINDOW_DAYS, "sessions": len(w_days), "light_sessions": len(w_light),
                   "conflicts": len(w_conflicts)},
        "recommended_rest_days": recovery["recommended_rest_days"],   # 0 = something is ready today
        "next_earliest": recovery["next_earliest"],
        "all_ready_on": recovery["all_ready_on"],
        "per_group": recovery["per_group"],
        "recovery": {k: recovery[k] for k in ("muscles", "exercises", "ready_today", "limited_today", "not_ready")},
        "transitions": transitions[-SEQ_DAYS:],
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
        sets, prev_end = [], None
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
                "set_id": s["id"],                 # local join key only (never sent to the AI)
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
        # A density shift needs a BASELINE: at least two previous sessions that
        # agree with each other (each within DENSITY_SHIFT_PCT of their mean).
        # During the first weeks, when every session differs from the last, the
        # flag would otherwise fire every time and mean nothing.
        prev3 = [d["work_density_per_min"] for d in out[-3:] if d["work_density_per_min"]]
        if work_density and len(prev3) >= 2:
            ref = st.mean(prev3)
            consistent = all(abs(v - ref) / ref * 100 <= DENSITY_SHIFT_PCT for v in prev3)
            dev = round((work_density - ref) / ref * 100)
            if consistent and abs(dev) > DENSITY_SHIFT_PCT:
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
    return out                     # ALL days; the caller hands the last SEQ_DAYS to UI / AI


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


def exercise_restriction(name: str, restrictions: dict, joints: list | None = None) -> str:
    """Worst restriction level that applies to an exercise via the body parts
    it loads. The catalog's optional 'joints' list per exercise is the source
    of truth (users can edit it); an exercise without one falls back to the
    name-based BODYPART_EXERCISES map, which also covers exercises that are
    not in the catalog yet."""
    worst = "ok"
    for part, level in (restrictions or {}).items():
        if _LEVEL_RANK.get(level, 0) == 0:
            continue
        hit = (part in joints) if joints else (name in BODYPART_EXERCISES.get(part, []))
        if hit and _LEVEL_RANK[level] > _LEVEL_RANK[worst]:
            worst = level
    return worst


# --- restriction checks --------------------------------------------------------
RESTRICTION_CHECK_DAYS = 14    # how far back the coach looks for sets that ignored a restriction
ECC_JUMP_PCT = 10              # eccentric peak this much above the previous day on a 'careful' exercise


def _restriction_checks(work: list[dict], exercises: list[dict], restrictions: dict, today: date) -> list[dict]:
    """Did the athlete respect their own restrictions? A mirror, not advice.

    Within the last RESTRICTION_CHECK_DAYS: any set on an 'avoid' exercise
    (trained_avoid); a moderate/deep set on a 'careful' exercise
    (hard_on_careful); and an eccentric peak more than ECC_JUMP_PCT above the
    previous day-best of a 'careful' exercise (eccentric_jump) - on ARX the
    eccentric runs ~1.6x the concentric, and a jump there is what an irritated
    joint feels first. Empty when nothing is restricted."""
    if not any(_LEVEL_RANK.get(v, 0) for v in (restrictions or {}).values()):
        return []
    level_of = {e["name"]: e.get("restriction", "ok") for e in exercises}
    ecc_by_day: dict = {}                       # (exercise, date) -> eccentric day-best
    for s in work:
        k = (s["name"], s["date"][:10])
        ecc_by_day[k] = max(ecc_by_day.get(k, 0), s["eccentric_kg"])
    days_by_ex: dict = {}
    for n, d in ecc_by_day:
        days_by_ex.setdefault(n, []).append(d)
    for n in days_by_ex:
        days_by_ex[n].sort()
    start = today.toordinal() - RESTRICTION_CHECK_DAYS
    out = []
    for s in sorted(work, key=lambda x: x["date"]):
        lvl, d = level_of.get(s["name"], "ok"), s["date"][:10]
        if lvl == "ok" or date.fromisoformat(d).toordinal() < start:
            continue
        base = {"date": d, "exercise": s["name"], "level": lvl,
                "inroad": s.get("inroad"), "effort": s.get("effort"), "eccentric_kg": s["eccentric_kg"]}
        if lvl == "avoid":
            out.append(dict(base, issue="trained_avoid"))
            continue
        if EFFORT_RANK.get(s.get("effort"), 0) >= 2 and s.get("effort") != "unknown":
            out.append(dict(base, issue="hard_on_careful"))
        days = days_by_ex.get(s["name"], [])
        i = days.index(d)
        if i > 0:
            prev_ecc = ecc_by_day[(s["name"], days[i - 1])]
            if prev_ecc and s["eccentric_kg"] > prev_ecc * (1 + ECC_JUMP_PCT / 100):
                out.append(dict(base, issue="eccentric_jump", prev_date=days[i - 1], prev_eccentric_kg=prev_ecc,
                                jump_pct=round((s["eccentric_kg"] - prev_ecc) / prev_ecc * 100)))
    return out


# --- daily check-in / readiness -------------------------------------------------
# A short wellness questionnaire in the spirit of Hooper & Mackinnon (1995) and
# McLean et al. (2010): sleep, energy, muscle soreness, plus the resting heart
# rate against the athlete's own baseline. Subjective wellness tracks training
# load at least as well as most objective markers, so the coach asks before
# deciding. The score is a transparent 0-100 sum of equal parts - a signal for
# today's intensity, NOT a diagnosis. Questions left unanswered simply do not
# count (the score is rescaled to what was answered).
READINESS_POINTS = {"sleep": {"poor": 0, "ok": 15, "good": 25},
                    "energy": {"low": 0, "ok": 15, "high": 25}}
READINESS_SORENESS = {"none": 25, "mild": 12, "strong": 0}
READINESS_RHR = {"normal": 25, "raised": 10, "elevated": 0}
RHR_RAISED_BPM = 4             # resting HR this far above the baseline = mildly raised
RHR_ELEVATED_BPM = 7           # ... clearly elevated: not recovered, or getting ill
RHR_BASELINE_MIN = 3           # earlier values needed before a baseline exists
RHR_BASELINE_N = 7             # baseline = median of the last N earlier values
READINESS_BANDS = ((75, "go_hard"), (50, "moderate"), (0, "light_or_rest"))


def _readiness(checkin: dict | None, rhr_history: list[dict] | None) -> dict | None:
    """Score today's check-in (see the constants above). rhr_history = earlier
    {date, rhr} entries of this athlete, oldest first; the baseline is the
    median of the last RHR_BASELINE_N of them. Returns None without a check-in."""
    if not checkin:
        return None
    pts, maxp, parts = 0, 0, {}
    for k, table in READINESS_POINTS.items():
        v = checkin.get(k)
        if v in table:
            pts += table[v]; maxp += 25; parts[k] = v
    sore = {r: l for r, l in (checkin.get("soreness") or {}).items() if l in ("mild", "strong")}
    level = "strong" if "strong" in sore.values() else ("mild" if sore else "none")
    if checkin.get("soreness") is not None:      # answered (possibly "nothing sore")
        pts += READINESS_SORENESS[level]; maxp += 25; parts["soreness"] = level
    rhr = checkin.get("rhr") or None
    vals = [h["rhr"] for h in (rhr_history or []) if h.get("rhr")][-RHR_BASELINE_N:]
    baseline = round(st.median(vals), 1) if len(vals) >= RHR_BASELINE_MIN else None
    status = diff = None
    if rhr and baseline:
        diff = round(rhr - baseline, 1)
        status = "elevated" if diff > RHR_ELEVATED_BPM else ("raised" if diff > RHR_RAISED_BPM else "normal")
        pts += READINESS_RHR[status]; maxp += 25; parts["rhr"] = status
    score = round(pts / maxp * 100) if maxp else None
    band = next((b for th, b in READINESS_BANDS if score is not None and score >= th), None)
    return {"date": checkin.get("date"), "score": score, "band": band, "components": parts,
            "sore_regions": sore, "rhr": rhr, "rhr_baseline": baseline, "rhr_diff": diff,
            "rhr_status": status, "rhr_values_known": len(vals),
            "pain": list(checkin.get("pain") or []), "note": checkin.get("note") or None}


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
        one most due & recovered region this session), or 'auto' (auto-regulate
        by the MUSCLE-level readiness: exercises whose target muscles are not
        recovered are left out, exercises whose limiter is not fresh go last
        with a cue, and nothing ready means an empty plan = rest day).
    Every item carries its readiness (status / ready_on / limited_by) so the UI
    and the AI can mark it. The AI plan on top individualizes further."""
    focus = focus or {}
    approach = approach or "full"
    frank = {"more": 0, "normal": 1, "less": 2, "off": 3}
    fstate = lambda g: focus.get(g, "normal")
    rec = {r["name"]: r for r in ((load.get("recovery") or {}).get("exercises") or [])}
    def rstate(e):
        return rec.get(e["name"], {"status": "ready", "ready_on": None, "fresh_on": None, "limited_by": [], "reason": ""})

    def restr(e): return exercise_restriction(e["name"], restrictions, e.get("joints"))
    avail = [e for e in exercises if restr(e) != "avoid" and fstate(e["group"]) != "off"]
    if approach == "auto":
        avail = [e for e in avail if rstate(e)["status"] != "not_ready"]
        avail.sort(key=lambda e: 0 if rstate(e)["status"] == "ready" else 1)   # stable: PB order kept within

    def mk(e):
        r, rs = restr(e), rstate(e)
        return {"name": e["name"], "group": e["group"], "last": e["last"],
                "restriction": r, "target": "gentle" if r == "careful" else "max",
                "targets": e.get("targets", []), "limiters": e.get("limiters", []),
                "status": rs["status"], "ready_on": rs["ready_on"], "fresh_on": rs.get("fresh_on"),
                "limited_by": rs.get("limited_by", []), "readiness_reason": rs.get("reason", "")}

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
        # 'full' and 'auto' both produce a classic balanced plan; 'auto' has
        # already dropped what is not recovered and puts the most-recovered
        # groups first. 'full' is never gated, so the standard plan is always
        # visible - e.g. as a preview of what to do when a rest day ends.
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

    items = [mk(e) for e in chosen[:5]]
    if approach == "auto":                    # fresh exercises get the athlete's best energy
        items = [i for i in items if i["status"] != "limited"] + [i for i in items if i["status"] == "limited"]
    return _order_by_limiters(items)


# --- coach layer -----------------------------------------------------------------
# What a trainer writes on the whiteboard before the session: a concrete target
# per exercise (progressive overload, but only where the last set showed room),
# the effort the goal asks for, rest between sets, adherence to the weekly
# target, the next milestones - and a deload signal when weeks of consistent
# training meet falling numbers or poor readiness. All of it rule-based facts;
# the AI phrases and individualises them, it does not invent them.
TARGET_STEP_PCT = 2            # progressive-overload nudge when the trend is up and there was room
TARGET_ROOM_INROAD = 20        # last inroad below this = the set stopped short of real fatigue
ROUND_MARK = {"kg": 10, "lb": 25}                          # "next round number" step per unit
REST_AFTER_MIN = {"compound": "3-4", "isolation": "2-3"}   # minutes between sets by exercise kind
WORK_MARK_KG_S = 50000         # work-total milestone step (summed impulse, kg*s)
DELOAD_WEEKS_AT_TARGET = 4     # consecutive weeks on target before a deload is even considered
DELOAD_DECLINE_PCT = 5         # comparable-ROM decline over the last two occurrences that counts ...
DELOAD_MIN_EXERCISES = 2       # ... on at least this many exercises
DELOAD_READINESS_BELOW = 50    # or readiness below this on 2 of the last 3 check-ins


def _target_effort(goal: dict) -> dict:
    """The effort the training goal asks for (goal sliders = muscle / strength /
    conditioning fractions)."""
    g = goal or {}
    muscle, strength, cond = g.get("muscle") or 0, g.get("strength") or 0, g.get("conditioning") or 0
    if cond >= 0.3:
        return {"mode": "conditioning", "inroad_min": 10, "inroad_target": "10-20 %",
                "note": "timed protocol, steady force, shorter rests"}
    if strength > muscle:
        return {"mode": "strength", "inroad_min": 10, "inroad_target": "10-20 %",
                "note": "maximal force on every rep, stop before form breaks"}
    return {"mode": "muscle", "inroad_min": 20, "inroad_target": ">= 20 %",
            "note": "take the set to real inroad"}


def _adherence(days: list[str], sessions_per_week, today: date) -> dict:
    """Training days per ISO week against the weekly target: this week so far,
    the last four full weeks, and the streak of weeks that met the target."""
    target = int(sessions_per_week or 2)
    per_week: dict = {}
    for d in days:
        y, w, _ = date.fromisoformat(d).isocalendar()
        per_week[(y, w)] = per_week.get((y, w), 0) + 1
    first = date.fromisoformat(days[0]) if days else today
    fy, fw, _ = first.isocalendar()
    ty, tw, _ = today.isocalendar()
    weeks = []
    for back in range(4, 0, -1):
        ref = today - timedelta(weeks=back)
        y, w, _ = ref.isocalendar()
        n = per_week.get((y, w), 0)
        weeks.append({"week": f"{y}-W{w:02d}", "sessions": n, "met": n >= target,
                      "before_start": (y, w) < (fy, fw)})     # no history yet -> not a missed week
    streak = 0
    for wk in reversed(weeks):
        if wk["met"]:
            streak += 1
        elif not wk["before_start"]:
            break
    this_week = per_week.get((ty, tw), 0)
    return {"target_per_week": target, "this_week": this_week, "this_week_met": this_week >= target,
            "weeks": weeks, "streak_weeks": streak,
            "weeks_of_history": round(max(0, (today - first).days) / 7, 1) if days else 0}


def _coach_facts(exercises: list[dict], recovery: dict, goal: dict, days: list[str],
                 sessions_per_week, today: date, totals: dict, units: str,
                 checkin_history: list[dict] | None, readiness_today: dict | None) -> dict:
    """Rule-based whiteboard facts for today (see the constants above)."""
    unit = "lb" if units == "imperial" else "kg"
    step_kg = ROUND_MARK["lb"] * LB_TO_KG if unit == "lb" else ROUND_MARK["kg"]
    rec = {r["name"]: r for r in (recovery or {}).get("exercises") or []}
    rows = []
    for e in exercises:
        rs = rec.get(e["name"], {"status": "ready", "ready_on": None, "limited_by": [], "fresh_on": None})
        restr = e.get("restriction", "ok")
        valid = [o for o in e["occ"] if o.get("rom_valid")]
        base = valid[-1] if valid else e["occ"][-1]      # last COMPARABLE day-best = the reference
        last = e["occ"][-1]
        row = {"name": e["name"], "group": e["group"], "kind": e.get("kind", "?"),
               "status": rs["status"], "ready_on": rs.get("ready_on"), "fresh_on": rs.get("fresh_on"),
               "limited_by": rs.get("limited_by", []), "restriction": restr,
               "base_kg": base["kg"], "base_date": base["date"], "base_rom_cm": base["rom_cm"],
               "base_is_last": base["date"] == last["date"],
               "last_inroad": e.get("last_inroad"), "trend_per_session": e.get("trend_per_session"),
               "rest_after_min": REST_AFTER_MIN.get(e.get("kind"), "2-3"),
               "last_settings": {"reps": last.get("reps"), "seconds": last.get("seconds"),
                                 "pause_end_s": last.get("pause_end_s"), "pause_return_s": last.get("pause_return_s")},
               "next_mark_kg": None, "to_next_mark_kg": None,
               "target_rule": None, "target_peak_kg": None}
        if e["pb"]:
            # kept unrounded so a 25 lb mark converts back to exactly 275.0 lb, not 274.9
            mark = (int(e["pb"] / step_kg) + 1) * step_kg
            row["next_mark_kg"], row["to_next_mark_kg"] = mark, round(mark - e["pb"], 2)
        if restr == "avoid":
            row["target_rule"] = "excluded"
        elif restr == "careful":
            row["target_rule"] = "sub_max_careful"
        elif rs["status"] == "not_ready":
            row["target_rule"] = "not_ready"
        elif rs["status"] == "limited":
            row["target_rule"] = "sub_max_limiter"
        else:
            room = e.get("last_inroad") is None or e["last_inroad"] < TARGET_ROOM_INROAD
            if (e.get("trend_per_session") or 0) > 0 and room:
                row["target_rule"] = "trend_up_room"           # trend up, last set not to failure -> nudge
                row["target_peak_kg"] = round(base["kg"] * (1 + TARGET_STEP_PCT / 100), 1)
            else:
                row["target_rule"] = "hold_reach_inroad"       # same force, but make the set real
                row["target_peak_kg"] = base["kg"]
        rows.append(row)

    since = (today - timedelta(days=14)).isoformat()
    recent_pbs = [e["name"] for e in exercises
                  if e["n"] >= 2 and e["last_date"] >= since and e["last"] >= e["pb"] - 0.01]
    work = (totals or {}).get("total_work_impulse") or 0
    adherence = _adherence(days, sessions_per_week, today)

    declining = []
    for e in exercises:
        v = [o["kg"] for o in e["occ"] if o.get("rom_valid")]
        if len(v) >= 3 and v[-3] and (v[-1] - v[-3]) / v[-3] * 100 <= -DELOAD_DECLINE_PCT:
            declining.append({"name": e["name"], "change_pct": round((v[-1] - v[-3]) / v[-3] * 100, 1)})
    hist = list(checkin_history or [])
    scores = []
    for i in range(max(0, len(hist) - 3), len(hist)):
        r = _readiness(hist[i], hist[:i])
        if r and r["score"] is not None:
            scores.append(r["score"])
    if readiness_today and readiness_today.get("score") is not None:
        scores = (scores + [readiness_today["score"]])[-3:]
    low = sum(1 for s in scores if s < DELOAD_READINESS_BELOW)
    suggested = (adherence["streak_weeks"] >= DELOAD_WEEKS_AT_TARGET
                 and (len(declining) >= DELOAD_MIN_EXERCISES or low >= 2))
    deload = {"suggested": suggested, "weeks_at_target": adherence["streak_weeks"],
              "declining": declining, "low_readiness_checkins": low, "recent_readiness_scores": scores,
              "rule": (f">= {DELOAD_WEEKS_AT_TARGET} weeks on target AND (>= {DELOAD_MIN_EXERCISES} exercises "
                       f"down >= {DELOAD_DECLINE_PCT} % over two comparable sessions OR readiness < "
                       f"{DELOAD_READINESS_BELOW} on 2 of the last 3 check-ins)")}
    return {"target_effort": _target_effort(goal), "exercises": rows, "adherence": adherence,
            "milestones": {"pbs_last_14_days": recent_pbs, "work_total_kg_s": work,
                           "next_work_mark_kg_s": (int(work / WORK_MARK_KG_S) + 1) * WORK_MARK_KG_S,
                           "round_mark_step_kg": round(step_kg, 1)},
            "deload": deload, "rest_after_min": REST_AFTER_MIN}


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


LAST_SESSION_CHARTS = 3        # exercises of the last session that get a force-curve card


def _last_session(con, work: list[dict], exercises: list[dict], sequences_all: list[dict],
                  transitions: list[dict]) -> dict | None:
    """The most recent training day, exercise by exercise, each compared with
    the previous time that exercise was done.

    This replaces the old "featured set" lottery: the athlete wants to see what
    they just did. Exercises appear in the order they were performed (the best
    set of the day represents an exercise done twice); the first
    LAST_SESSION_CHARTS get a force-curve card with the previous set's curve
    as a ghost - one explainable exception: a new personal best later in the
    session replaces the third card. Every exercise carries the comparison
    numbers (peak / mean force / inroad / ROM vs previous, comparable only
    when the ROM matched, new-PB flag, settings that changed) so the table and
    the AI can talk about the session in concrete terms."""
    if not sequences_all:
        return None
    day = sequences_all[-1]
    ex_by_name = {e["name"]: e for e in exercises}
    seq_by_id = {r["set_id"]: r for r in day["sets"]}
    order, best = [], {}
    for s in sorted((x for x in work if x["date"][:10] == day["date"]), key=lambda x: x["date"]):
        if s["name"] not in best:
            order.append(s["name"])
            best[s["name"]] = {"set": s, "n": 0}
        b = best[s["name"]]
        b["n"] += 1
        if s["max_kg"] >= b["set"]["max_kg"]:
            b["set"] = s

    rows = []
    for name in order:
        s, n_today = best[name]["set"], best[name]["n"]
        e = ex_by_name.get(name, {})
        occ = e.get("occ") or []
        prev = occ[-2] if (len(occ) >= 2 and occ[-1]["date"] == day["date"]) else None
        seq = seq_by_id.get(s["id"], {})
        comparable = bool(prev and prev.get("rom_cm") and s.get("rom_cm")
                          and abs(s["rom_cm"] - prev["rom_cm"]) / prev["rom_cm"] <= ROM_TOLERANCE)
        pb = e.get("pb", s["max_kg"])
        changed = []
        if prev:
            if prev.get("seconds") and abs(s["seconds"] - prev["seconds"]) / prev["seconds"] > 0.15:
                changed.append(f"duration {s['seconds']} s vs {prev['seconds']} s")
            for k, lab in (("pause_end_s", "end pause"), ("pause_return_s", "return pause")):
                if s.get(k) is not None and prev.get(k) is not None and abs(s[k] - prev[k]) >= 0.5:
                    changed.append(f"{lab} {s[k]} s vs {prev[k]} s")
            if prev.get("reps") and s["reps"] != prev["reps"]:
                changed.append(f"reps {s['reps']} vs {prev['reps']}")
        rows.append({
            "name": name, "group": s["group"], "set_id": s["id"],
            "order": seq.get("order"), "sets_today": n_today,
            "rest_before_min": seq.get("minutes_since_prev_set"),
            "max_kg": s["max_kg"], "mean_force_kg": s["mean_force_kg"],
            "concentric_kg": s["concentric_kg"], "eccentric_kg": s["eccentric_kg"],
            "reps": s["reps"], "seconds": s["seconds"], "rom_cm": s.get("rom_cm"),
            "inroad": s.get("inroad"), "effort": s.get("effort"),
            "pause_end_s": s.get("pause_end_s"), "pause_return_s": s.get("pause_return_s"),
            "limiters_pre_fatigued": seq.get("limiters_pre_fatigued", []),
            "prev": ({"date": prev["date"], "id": prev["id"], "max_kg": prev["kg"],
                      "mean_force_kg": prev["mean_force_kg"], "reps": prev["reps"], "seconds": prev["seconds"],
                      "rom_cm": prev["rom_cm"], "inroad": prev["inroad"], "effort": prev["effort"]}
                     if prev else None),
            "delta_pct": round((s["max_kg"] - prev["kg"]) / prev["kg"] * 100, 1) if (prev and prev["kg"]) else None,
            "delta_mean_pct": (round((s["mean_force_kg"] - prev["mean_force_kg"]) / prev["mean_force_kg"] * 100, 1)
                               if (prev and prev.get("mean_force_kg")) else None),
            "delta_comparable": comparable,
            "is_pb": len(occ) >= 2 and s["max_kg"] >= pb - 0.01,
            "pb_kg": pb,
            "delta_vs_pb_pct": round((s["max_kg"] - pb) / pb * 100, 1) if pb else None,
            "settings_changed": changed,
            "restriction": e.get("restriction", "ok"),
            "chart": False,
        })

    chart_names = [r["name"] for r in rows[:LAST_SESSION_CHARTS]]
    for r in rows[LAST_SESSION_CHARTS:]:
        if r["is_pb"] and chart_names:            # a new PB later in the session earns the third card
            chart_names[-1] = r["name"]
            break
    for r in rows:
        if r["name"] not in chart_names:
            continue
        r["chart"] = True
        r.update(set_curve(con, r["set_id"]))
        if r["prev"]:
            pc = set_curve(con, r["prev"]["id"])
            r["prev_curve"], r["prev_rep_peaks"], r["prev_rep_times"] = pc["curve"], pc["rep_peaks"], pc["rep_times"]
        r["settings"] = featured_settings(con, r["set_id"], r["reps"])

    transition = next((t for t in transitions if t["date"] == day["date"]), None)
    return {
        "date": day["date"],
        "prev_date": transition["prev_date"] if transition else None,
        "gap_days": transition["gap_days"] if transition else None,
        "transition": transition,
        "working_sets": day["working_sets"], "false_starts": day.get("false_starts", 0),
        "visits": day["visits"], "wall_minutes": day["wall_minutes"],
        "time_under_load_min": day["time_under_load_min"],
        "groups_touched": day["groups_touched"], "flags": day["flags"],
        "exercises": rows,
        "charts": chart_names,
        "featured": chart_names[0] if chart_names else None,
    }


def build_report(con, cfg: dict) -> dict:
    """Assemble the full analysis payload for one athlete."""
    catalog = cfg.get("_catalog", {})
    sets = load_sets(con, cfg["user_id"])
    flag_false_starts(sets)                  # needs neighbouring sets -> after loading all
    work = [s for s in sets if s["working"]]
    excluded: dict = {}                      # status -> count of the sets that do NOT count
    for s in sets:
        if not s["working"]:
            excluded[s["status"]] = excluded.get(s["status"], 0) + 1
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

    today = _today(cfg)
    checkin = cfg.get("checkin") or {}       # today's check-in (sleep, soreness, RHR, pain), see arx_app
    restrictions_saved = cfg.get("restrictions", {}) or {}
    restrictions = dict(restrictions_saved)
    pain_today = [p for p in (checkin.get("pain") or []) if _LEVEL_RANK.get(restrictions.get(p, "ok"), 0) < 1]
    for p in pain_today:                      # pain today = careful today (not persisted)
        restrictions[p] = "careful"
    exercises = _exercise_series(work, catalog, restrictions)
    for e in exercises:                       # annotate each exercise with its restriction
        e["restriction"] = exercise_restriction(e["name"], restrictions, e.get("joints"))
    days = sorted({s["date"][:10] for s in work})

    # today's check-in scored; its resting-HR verdict feeds the load flag
    readiness = _readiness(checkin, cfg.get("checkin_history"))
    if readiness and readiness["rhr_status"]:
        checkin = dict(checkin, rhr_status=readiness["rhr_status"])
    load = _load_analysis(work, catalog, exercises, today, checkin)
    approach = cfg.get("approach", "auto")
    sequences_all = _session_sequences(work, approach, catalog)
    # false starts per day (they are not sets, but four of them in one session
    # tell the coach something about the pre-timer / start position)
    fs_by_day: dict = {}
    for s in sets:
        if s["status"] == "false_start":
            fs_by_day[s["date"][:10]] = fs_by_day.get(s["date"][:10], 0) + 1
    for d in sequences_all:
        d["false_starts"] = fs_by_day.get(d["date"], 0)
    sequences = sequences_all[-SEQ_DAYS:]

    # the last session, exercise by exercise, vs the previous time - the headline
    last_session = _last_session(con, work, exercises, sequences_all, load["transitions"])
    featured = None                          # compat: the first card's set (settings table, AI)
    if last_session and last_session["featured"]:
        top = next(r for r in last_session["exercises"] if r["name"] == last_session["featured"])
        by_id = {s["id"]: s for s in work}
        featured = {"meta": by_id[top["set_id"]], "reason": "last_session", "settings": top.get("settings"),
                    "curve": top["curve"], "rep_peaks": top["rep_peaks"], "rep_times": top["rep_times"],
                    "inroad_pct": top["inroad_pct"]}

    # headline KPIs for the UI
    ce = [s["eccentric_kg"] / s["concentric_kg"] for s in work if s["concentric_kg"] > 0]
    kpi = {
        "top_force": round(max((s["max_kg"] for s in work), default=0), 1),
        "sessions": sum(d["visits"] for d in sequences_all),   # visits, not DB session ids
        "ce_ratio": round(st.mean(ce), 2) if ce else None,
        "total_impulse": sum(s["impulse_kg_s"] for s in work),
    }
    totals = _totals(work, load.get("weekly_rate"), sequences_all)
    coach = _coach_facts(exercises, load["recovery"], cfg.get("goal", {}) or {}, days,
                         cfg.get("sessions_per_week"), today, totals, cfg.get("units", "imperial"),
                         cfg.get("checkin_history"), readiness)

    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "athlete_alias": cfg.get("alias", "Athlet"),
        "goal": cfg.get("goal", {}),
        "sessions_per_week": cfg.get("sessions_per_week"),
        "sets_total": len(sets),
        "sets_working": len(work),
        "sets_excluded": excluded,               # status -> count (short / aborted / false_start / no_data)
        "training_days": days,
        "kpi": kpi,
        "exercises": exercises,
        "rom_warnings": _rom_warnings(exercises),
        "whole_body": _whole_body(work, exercises),
        "load": load,
        "session_sequences": sequences,
        "limiter_conflicts": _limiter_conflicts(sequences),
        "totals": totals,
        "coach": coach,                          # whiteboard facts: targets, adherence, milestones, deload
        "ideal_settings": IDEAL_SETTINGS,
        "restrictions": restrictions,            # effective today (saved + pain from the check-in)
        "restrictions_saved": restrictions_saved,
        "restriction_checks": _restriction_checks(work, exercises, restrictions_saved, today),
        "pain_today": pain_today,
        "checkin": checkin or None,
        "readiness": readiness,                  # scored check-in (None without one)
        "today": today.isoformat(),
        "focus": cfg.get("focus", {}) or {},
        "approach": approach,
        "session_plan": _session_plan(exercises, restrictions,
                                      cfg.get("focus", {}) or {},
                                      approach, load),
        "last_session": last_session,
        "featured": featured,
    }


# =============================================================================
# Optional AI narrative (uses the user's own Claude API key)
# =============================================================================
AI_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
AI_EFFORT_DEFAULT = "medium"


def ai_summary(report: dict, cfg: dict) -> dict:
    """The compact, name-free payload the coach model receives, in the user's
    units. Two kinds of content, kept apart in the prompt: precomputed FACTS
    (final) and the ordered session sequences (raw material). Separate from
    ai_narrative so it can be inspected and tested without an API call."""
    imp = cfg.get("units", "imperial") == "imperial"
    ff = 2.20462 if imp else 1.0
    lf = (1 / 2.54) if imp else 1.0
    FU, LU = ("lb", "in") if imp else ("kg", "cm")
    kg = lambda v: round(v * ff, 1) if v is not None else None
    cm = lambda v: round(v * lf, 1) if v is not None else None
    today = report.get("today")

    ls = report.get("last_session") or {}
    def ls_row(x):
        p = x.get("prev")
        return {
            "order": x["order"], "name": x["name"], "group": x["group"], "sets_today": x["sets_today"],
            "rest_before_min": x["rest_before_min"],
            "peak": kg(x["max_kg"]), "mean_force": kg(x["mean_force_kg"]), "eccentric_peak": kg(x["eccentric_kg"]),
            "reps": x["reps"], "seconds": x["seconds"], "rom": cm(x["rom_cm"]),
            "inroad": x["inroad"], "effort": x["effort"],
            "pause_end_s": x["pause_end_s"], "pause_return_s": x["pause_return_s"],
            "limiters_pre_fatigued": x.get("limiters_pre_fatigued", []),
            "is_new_pb": x["is_pb"], "personal_best": kg(x["pb_kg"]), "vs_pb_pct": x["delta_vs_pb_pct"],
            "restriction": x.get("restriction", "ok"),
            "vs_previous": ({"date": p["date"], "peak": kg(p["max_kg"]), "mean_force": kg(p["mean_force_kg"]),
                             "inroad": p["inroad"], "effort": p["effort"], "rom": cm(p["rom_cm"]),
                             "reps": p["reps"], "seconds": p["seconds"],
                             "delta_peak_pct": x["delta_pct"], "delta_mean_force_pct": x["delta_mean_pct"],
                             "comparable_rom": x["delta_comparable"]} if p else None),
            "settings_changed": x.get("settings_changed", []),
        }
    days_ago = None
    if ls.get("date") and today:
        days_ago = (date.fromisoformat(today) - date.fromisoformat(ls["date"])).days
    tr = ls.get("transition") or {}

    load = report["load"]
    rec = load.get("recovery") or {}
    coach = report.get("coach") or {}
    rd = report.get("readiness")

    return {
        "today": today,
        "units": {"force": FU, "length": LU},
        "goal": report["goal"],
        "restrictions": report.get("restrictions_saved", report.get("restrictions", {})),  # body part -> ok/careful/avoid
        "pain_today_from_checkin": report.get("pain_today", []),   # body parts flagged today -> careful today
        "focus": report.get("focus", {}),                 # group -> more/normal/less/off
        "approach": report.get("approach", "auto"),       # full | split | auto
        "ideal_settings": report.get("ideal_settings"),   # textbook machine settings
        "sessions_per_week_target": report.get("sessions_per_week"),
        "training_days": report["training_days"],
        "sets": {"working": report["sets_working"], "recorded": report["sets_total"],
                 "excluded": report.get("sets_excluded", {})},
        # 1) the session that just happened, exercise by exercise, vs the previous time
        "last_session": {
            "date": ls.get("date"), "days_ago": days_ago,
            "previous_session_date": ls.get("prev_date"), "gap_days": ls.get("gap_days"),
            "vs_previous_session": {"verdict": tr.get("verdict"), "muscles_loaded_again": tr.get("muscles_repeated", []),
                                    "conflicts": tr.get("conflicts", []), "detail": tr.get("detail")} if tr else None,
            "working_sets": ls.get("working_sets"), "false_starts": ls.get("false_starts"),
            "wall_minutes": ls.get("wall_minutes"), "time_under_load_min": ls.get("time_under_load_min"),
            "flags": ls.get("flags", []),
            "exercises": [ls_row(x) for x in ls.get("exercises", [])],
        } if ls else None,
        # 2) how the athlete feels today (None = no check-in) and what is recovered
        "checkin_today": rd,
        "readiness_today": {
            "muscles": {m: {"ready": v["ready"], "ready_on": v["ready_on"], "reason": v["reason"],
                            "sore": v.get("sore"), "last_effort": v["last_effort"], "last_exercise": v["last_exercise"],
                            "days_since": v["days_since"]} for m, v in (rec.get("muscles") or {}).items()},
            "exercises_ready": rec.get("ready_today", []),
            "exercises_limited": rec.get("limited_today", []),
            "exercises_not_ready": rec.get("not_ready", []),
            "next_earliest": load.get("next_earliest"), "all_ready_on": load.get("all_ready_on"),
        },
        # 3) whiteboard facts
        "coach": {
            "target_effort": coach.get("target_effort"),
            "targets": [{
                "name": t["name"], "group": t["group"], "kind": t["kind"], "status": t["status"],
                "ready_on": t["ready_on"], "limited_by": t["limited_by"], "restriction": t["restriction"],
                "target_rule": t["target_rule"], "target_peak": kg(t["target_peak_kg"]),
                "base_peak": kg(t["base_kg"]), "base_date": t["base_date"], "base_rom": cm(t["base_rom_cm"]),
                "base_is_last_session": t["base_is_last"], "last_inroad": t["last_inroad"],
                "trend_per_session": kg(t["trend_per_session"]),
                "rest_after_min": t["rest_after_min"],
                "next_round_mark": kg(t["next_mark_kg"]), "to_next_round_mark": kg(t["to_next_mark_kg"]),
                "last_settings": t["last_settings"],
            } for t in coach.get("exercises", [])],
            "adherence": coach.get("adherence"),
            "milestones": {
                "pbs_last_14_days": (coach.get("milestones") or {}).get("pbs_last_14_days", []),
                "work_total_impulse": (coach.get("milestones") or {}).get("work_total_kg_s"),
                "next_work_mark_impulse": (coach.get("milestones") or {}).get("next_work_mark_kg_s"),
                "round_mark_step": kg((coach.get("milestones") or {}).get("round_mark_step_kg")),
            },
            "deload": coach.get("deload"),
        },
        "restriction_checks": [{**c, "eccentric": kg(c.get("eccentric_kg")),
                                "prev_eccentric": kg(c.get("prev_eccentric_kg"))}
                               for c in report.get("restriction_checks", [])],
        "exercises": [{
            "name": e["name"], "group": e["group"], "kind": e["kind"],
            "targets": e.get("targets", []), "limiters": e.get("limiters", []),
            "restriction": e.get("restriction", "ok"),
            "personal_best": kg(e["pb"]),
            "latest_day_best": kg(e["last"]), "latest_date": e.get("last_date"),
            "previous_day_best": kg(e.get("prev_kg")), "previous_date": e.get("prev_date"),
            "delta_vs_previous_pct": e.get("delta_pct"), "delta_comparable_rom": e.get("delta_comparable"),
            "in_last_session": e.get("in_last_session"),
            "training_days": e["n"], "total_sets": e["total_sets"],
            "days_with_multiple_sets": e["multi_set_days"],
            "trend_per_session": kg(e["trend_per_session"]),   # per training-day occurrence
            "trend_per_day": kg(e["trend_per_day"]),           # per calendar day
            "trend_based_on_days": e["trend_n"],               # ROM-comparable days only
            "rom_reference": cm(e["rom_cm_reference"]), "rom_latest": cm(e["rom_cm_latest"]),
            "rom_drift_pct": e["rom_drift_pct"], "rom_stable": e["rom_stable"],
            "days_excluded_for_rom": e["days_excluded_for_rom"],
        } for e in report["exercises"]],
        "rom_warnings": [{
            "name": w["name"], "restricted": w.get("restricted", False),
            "rom_reference": cm(w["rom_cm_reference"]), "rom_latest": cm(w["rom_cm_latest"]),
            "rom_drift_pct": w["rom_drift_pct"], "days_excluded_for_rom": w["days_excluded_for_rom"],
            "trend_available": w["trend_available"],
            "observed": [{"date": o["date"], "rom": cm(o["rom_cm"]), "best": kg(o["kg"]),
                          "comparable": o["valid"]} for o in w["observed"]],
        } for w in report.get("rom_warnings", [])],
        "whole_body_index_per_day": report["whole_body"]["series"],
        "load_and_recovery": {k: load.get(k) for k in (
            "flag", "window", "training_days", "gaps_days", "median_gap_days", "days_since_last",
            "sessions_last7", "weekly_rate", "hard_sessions", "submax_sessions",
            "insufficient_recovery_after_hard", "insufficient_recovery_after_hard_total",
            "conflicts_recent", "transitions", "per_group")},
        # Raw material for the model's OWN observations (see system prompt)
        "session_sequences": [{
            "date": d["date"], "working_sets": d["working_sets"], "false_starts": d.get("false_starts", 0),
            "visits": d["visits"], "groups_touched": d["groups_touched"], "wall_minutes": d["wall_minutes"],
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
    }


def ai_system_prompt(cfg: dict) -> str:
    """The coach's standing instructions. Existing, validated sections are kept
    (fatigue vs regression, ROM validity, restrictions, focus, two kinds of
    input, flags, limiters); the trainer role, check-in / muscle-level
    readiness, consecutive sessions, excluded sets and the fixed whiteboard
    output format are added around them."""
    imp = cfg.get("units", "imperial") == "imperial"
    FU, LU = ("lb", "in") if imp else ("kg", "cm")
    lang = "German" if cfg.get("language", "en") == "de" else "English"
    return (
        "You are the personal strength coach of an amateur who trains on the ARX "
        "adaptive resistance machine for muscle and strength. Act like a good human "
        "trainer who sees everything except posture: review what the athlete just "
        "did, take how they feel today into account, decide today's intensity, "
        "exercise selection, order, pauses and machine settings, write it on the "
        "whiteboard in a way that cannot be misread, set concrete targets, and "
        "motivate with real numbers - never with fluff. Ground every judgement in "
        "current sports-medicine knowledge: recovery time depends on the effort "
        "actually reached; delayed-onset soreness and subjective wellness (sleep, "
        "energy) are valid signs of incomplete recovery; a resting heart rate well "
        "above the athlete's own baseline is a sign of incomplete recovery or "
        "illness; autoregulation (adjust the day to readiness) beats a fixed "
        "calendar; progressive overload needs real effort first; a planned "
        "lighter week (deload) is the answer to weeks of consistent training that "
        "meet falling numbers or poor readiness, not more volume. Never give "
        "medical advice, never diagnose; with pain or illness say to rest and "
        "see a professional.\n"
        "MACHINE BASICS: resistance auto-adapts to the user's force, so progress "
        "is measured by produced force and by 'inroad' - the force decline across "
        "reps that shows the set reached deep fatigue. Rising per-rep peaks mean "
        "the early reps were sub-maximal. Exercises are grouped Push / Pull / Drive.\n"
        "CRITICAL - fatigue vs. regression: an athlete often performs SEVERAL "
        "sets of the same exercise within one session; later sets are naturally "
        "weaker because the muscles are already fatigued. This is expected and is "
        "NOT a regression or a deficit. The per-exercise numbers are already "
        "aggregated to the BEST set of each training day. Only compare best-of-day "
        "across days. When days_with_multiple_sets > 0, treat any within-day or "
        "later-set drop as fatigue, and never tell the user to 'work back up' to "
        "a value that is just a fatigued repeat set. Judge progress by the trend "
        "of daily bests and by whether sets reach real inroad. Two trend figures "
        "are given per exercise: trend_per_session = change per training-day "
        "occurrence, trend_per_day = change per calendar day (they differ by the "
        "training frequency); quote whichever you mean and never swap them.\n"
        "EXCLUDED SETS: only real working sets are in the data. Sets that were "
        "tests, familiarisation, aborted attempts or false starts (the same "
        "exercise restarted within minutes with more reps) were removed and are "
        "only counted in sets.excluded and last_session.false_starts. Several "
        "false starts in one session usually mean the pre-timer or the start "
        "position is wrong - mention it once, do not treat them as training.\n"
        "RANGE OF MOTION (ROM) VALIDITY: on this machine the force depends on "
        "the position range the set was performed over - a shorter ROM stays in "
        "the strong part of the movement and gives a higher peak. A force "
        "comparison between days is therefore ONLY valid when the ROM matched "
        "(within ~5% of the exercise's reference ROM). Per exercise you get "
        "rom_reference, rom_latest, rom_drift_pct, rom_stable, "
        "days_excluded_for_rom and trend_based_on_days; trend values are already "
        "computed on comparable days only and are null when too few exist. "
        "rom_warnings lists exercises whose ROM drifted. For those, do NOT claim "
        "progress or a drop from the force numbers; say the values are not "
        "comparable and ask the user to set fixed start and end positions - "
        "EXCEPT when the exercise is marked restricted: then a shortened range "
        "is deliberate, the latest range is the new baseline, and the advice is "
        "to keep it constant from now on.\n"
        "LAST SESSION FIRST: last_session is the session that just happened - "
        "open with it. For every exercise compare with vs_previous (peak, mean "
        "force, inroad, ROM), but quote a force delta only when comparable_rom "
        "is true; otherwise say 'ROM differed'. settings_changed lists tempo / "
        "pause changes between the two sets - they shift force values (a "
        "shorter set with no end pause raises mean force), so name them instead "
        "of calling the shift progress. is_new_pb marks a new personal best. "
        "vs_previous_session says whether the day before was too close for the "
        "muscles it hit again (conflict) or fine.\n"
        "CHECK-IN & MUSCLE-LEVEL READINESS (this decides today): checkin_today "
        "is what the athlete reported this morning (null = no check-in: say so "
        "in one clause and judge from the training data). Its score 0-100 and "
        "band (go_hard / moderate / light_or_rest) come from sleep, energy, "
        "soreness and resting HR vs the athlete's own baseline. Strong soreness "
        "in a region already blocks its muscles in readiness_today; an elevated "
        "resting HR (> +7 bpm) or poor sleep with low energy means a light day "
        "or rest, whatever the calendar says; pain_today_from_checkin lists "
        "body parts to treat as 'careful' today. readiness_today is computed per "
        "MUSCLE: each muscle is ready again when the rest its last hard load "
        "required has passed (deep 3 days, moderate 2, sub-max 1; a limiter is "
        "loaded one level lighter than the set's target muscles). An exercise is "
        "ready when all its target muscles are, 'limited' when only a limiter "
        "(grip, elbow flexors, ...) is not fresh - it may be trained sub-max, the "
        "limiter may end the set early - and not_ready otherwise. Training on "
        "consecutive days is FINE when different muscles are used; only a hard "
        "load on a muscle that has not finished recovering is a conflict. Use "
        "exactly these lists and dates for 'what today' and 'when'; do not "
        "invent blanket rest days.\n"
        "LOAD & RECOVERY: load_and_recovery.flag is judged on the last 7 days "
        "only: overload_risk = a hard-on-hard conflict in that window (or "
        "elevated resting HR plus strong soreness); underload = every recent "
        "session light (no moderate or deep set) - then the problem is intensity, "
        "not rest; detraining_risk = more than 10 days without training. "
        "History totals are given separately. Never warn about overtraining "
        "without a conflict or a check-in signal; frequent-but-light training "
        "means push to real inroad, not rest more.\n"
        "SAFETY - restrictions (highest priority): the user may flag body parts "
        "as limited. For any exercise with restriction 'avoid', do NOT recommend "
        "it and do NOT encourage effort on that body part - state plainly that it "
        "is excluded because of the restriction. For 'careful' (ramping back "
        "up), recommend gradual, sub-maximal, controlled-range loading and "
        "technique focus - never tell the user to go to maximum or to full "
        "inroad on those. Avoiding aggravation always outranks progress. "
        "restriction_checks lists sets of the last 14 days that ignored a "
        "restriction (trained_avoid, hard_on_careful, eccentric_jump = the "
        "eccentric peak jumped > 10 % on a careful exercise; on ARX the "
        "eccentric runs ~1.6x the concentric and is what an irritated joint "
        "feels first). Name them briefly and without blame, then say what "
        "'careful' means in numbers. This is not medical or rehabilitation advice.\n"
        "FOCUS & APPROACH: honor the user's focus (per group 'more'/'less'/'off' "
        "- e.g. an upper-body preference means emphasize Push/Pull and drop or "
        "minimize Drive/legs like Belt Squat) and their chosen approach: 'full' "
        "= balanced full body each session; 'split' = one region per session so "
        "each recovers between sessions; 'auto' = auto-regulate by readiness "
        "(train what is recovered today, per readiness_today).\n"
        "COACH FACTS (final, use them): coach.target_effort = the effort the "
        "goal asks for (inroad target). coach.targets gives per exercise a "
        "target_rule and target_peak in the data's unit: trend_up_room = the "
        "trend is up and the last set stopped short of real fatigue, so aim "
        "2 % above base_peak; hold_reach_inroad = same force as base_peak, but "
        "take the set to the inroad target; sub_max_limiter / sub_max_careful = "
        "no target, sub-max; not_ready / excluded = not today. base_peak is the "
        "last ROM-comparable day-best (base_rom, base_date - if "
        "base_is_last_session is false the last session used another ROM: say "
        "'set ROM x and aim for y'). rest_after_min = minutes of rest after the "
        "set. next_round_mark / to_next_round_mark = the next round number and "
        "how far it is (a milestone to name when it is close). coach.adherence "
        "= sessions this week vs target, the last weeks and the streak "
        "(before_start weeks are not missed weeks). coach.milestones = new PBs "
        "in the last 14 days and the work total. coach.deload = whether a "
        "lighter week is due, with the rule; suggest a deload ONLY when "
        "suggested is true, and then describe it (same exercises, ~70 % force, "
        "no inroad, one week).\n"
        "TWO KINDS OF INPUT - keep them apart: (1) all precomputed facts "
        "(last_session comparisons, trends, load_and_recovery, readiness_today, "
        "coach, ROM validity, session flags, limiter_conflicts, "
        "restriction_checks, session_plan) are FINAL - do not recompute, "
        "contradict or override them. (2) session_sequences is RAW MATERIAL: for "
        "each of the last training days the working sets in time order with the "
        "rest before each set, peak and mean force, duration, reps, ROM, "
        "inroad/effort, the machine pauses, whether the set repeats an exercise "
        "already done that day, and which limiting muscles were already fatigued "
        "by an earlier set. You MAY and SHOULD look for patterns in it that the "
        "rules do not cover - exercise order within a day, pacing, repeated sets "
        "that add nothing, settings that changed between sessions - and report "
        "them as your own observations, grounded in the listed numbers. Never "
        "invent values that are not in the data.\n"
        "SESSION FLAGS (rule-based, in session_sequences[].flags and "
        "last_session.flags, each with its numbers): too_many_sets (more than 6 "
        "working sets, or more than 4 in a dense session); scattered_session "
        "(Push, Pull and Drive all on one day although the approach is 'split'); "
        "short_intra_session_rest (the same exercise or the same limiting muscle "
        "loaded again within 5 min); density_shift (work per wall-clock minute "
        "deviating > 20 % from a consistent baseline of previous sessions - "
        "usually a pause/tempo/rest change). Explain what a raised flag means "
        "for the athlete and how to fix it next time.\n"
        "SHARED LIMITERS: exercises carry 'targets' (what they are for) and "
        "'limiters' (structures that give out first although they are only a "
        "means - e.g. the grip on Dead Lift, Row and Pull Down). Several sets "
        "sharing a limiter fatigue it cumulatively within a session, so a later "
        "set may end early on the limiter while the target muscles were not "
        "fully worked. limiter_conflicts lists, per day, the sets whose limiter "
        "was already loaded by an earlier set. Rule for ordering: an exercise "
        "where the limiter is only a means (Dead Lift, Row) goes BEFORE an "
        "exercise whose target is that same structure (Biceps Curl), and the "
        "same limiter should not be hit twice within a few minutes; session_plan "
        "already follows this on top of 'large muscle groups first'.\n"
        "MACHINE SETTINGS: recommend ideal_settings (~8 reps, ~5 s per movement "
        "direction, ~3 s hold at the end position, no pause on the return; the "
        "end-hold does not suit every exercise type) and point out where the "
        "athlete's last settings (coach.targets[].last_settings, "
        "last_session settings_changed) deviate.\n"
        "OUTPUT FORMAT - a whiteboard, markdown, exactly these five sections in "
        "this order, headings with '## ', tables in GitHub pipe syntax with a "
        "header row and a separator line (|---|---|), short cells, numbers with "
        f"units ({FU}, {LU}), no preamble, no closing remarks:\n"
        "## 1. Last session - <date>\n"
        "A table: | Exercise | Now | Last time | Change | Inroad | ROM | - one row "
        "per exercise in order (Now/Last time = peak force; Change = the "
        "percentage only when comparable_rom is true, else 'ROM differed'; ROM = "
        "'same' or the two values). Then 2-3 bullets in numbers: what was good, "
        "what to fix (order, rest, false starts, settings changes, limiter "
        "conflicts, flags).\n"
        "## 2. Today - <date>\n"
        "One line: check-in verdict (score, band, the components that matter) or "
        "'no check-in', plus the load flag in plain words. Then a table: "
        "| Muscle / exercise | Status | Ready from | Why | - only what is NOT "
        "simply ready (sore, not ready, limited), then one line 'Ready today: ...' "
        "listing the ready exercises. Write muscles in plain words (elbow "
        "flexors, upper back, ...), never as identifiers with underscores.\n"
        "## 3. Plan for today (or: Plan for <next_earliest> when nothing is ready)\n"
        "A table: | # | Exercise | Target | Effort | Tempo / pauses | Rest after | Cue | "
        "- 4 to 6 rows in the order to perform them (large muscle groups first, "
        "limiter rules, restrictions, focus). Target = the concrete force from "
        "coach.targets with unit, or 'sub-max' / 'gentle'; Effort = the inroad "
        "target or 'stop 2 reps short'; Tempo / pauses = reps, s per direction, "
        "end / return pause; Rest after = minutes; Cue = one short technique or "
        "intent cue. Below the table one line on when to train next if not "
        "today, and the deload verdict if coach.deload.suggested is true.\n"
        "## 4. Progress & milestones\n"
        "3-4 bullets with numbers: trends on comparable days, new PBs, adherence "
        "(this week x of y, streak), the nearest round marks, the work total.\n"
        "## 5. Focus\n"
        "ONE sentence with the single most valuable thing to do differently next "
        "time, then ONE short motivating sentence grounded in a number.\n"
        f"Write in {lang}. Use the units given in the data ({FU} and {LU}). "
        "Keep the whole board readable in two minutes."
    )


def ai_narrative(report: dict, cfg: dict) -> str | None:
    """Ask Claude to coach: review the last session, judge today's readiness,
    write the whiteboard (see ai_summary / ai_system_prompt). Key stays local;
    the model sees only aggregated, name-free metrics. Cost is steered by the
    'ai_effort' config option (low | medium | high | xhigh | max, default medium).
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
    msg = client.messages.create(
        model=model,
        max_tokens=16000,                        # room for adaptive thinking + the whole board
        thinking={"type": "adaptive"},           # on by default for Opus 5 / Fable
        output_config={"effort": effort},        # coaching is analysis; user-tunable via 'ai_effort'
        system=ai_system_prompt(cfg),
        messages=[{"role": "user", "content": json.dumps(ai_summary(report, cfg), ensure_ascii=False)}],
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
