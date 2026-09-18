#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARX Insight - detail metrics: what really happened INSIDE a set (v0.4.0).

Until v0.3.x the whole within-set analysis was one number per set: the decline of the highest
force of each rep ("inroad"). On an ARX that highest force is almost always an ECCENTRIC spike, so
concentric fatigue, the isometric holds, the tempo actually driven and the weak part of the range
were invisible - two Row sets with the same "inroad 19 %" turned out to have 17 % and 42 %
concentric fatigue.

This module reads the ~20 Hz samples (force, position, exact timestamps) together with the
machine's own phase markers and produces, per repetition and per set:

  * concentric / eccentric force as TIME-WEIGHTED means (the sampling is irregular: gaps up to a
    second and duplicate timestamps - a plain sample mean would be wrong),
  * the isometric holds at both ends (only when the pause really lasted),
  * force by third of the range of motion (where in the range is the athlete weak?),
  * time under tension per phase, tempo actually driven, pacing, steadiness,
  * a robust effort figure ("effort v3") that replaces the eccentric-spike inroad.

Facts verified on a real database (67 sets, against the machine's own CONCENTRICMAX /
ECCENTRICMAX columns):
  - Phase orientation: ENCODER VALUE RISING = CONCENTRIC. EndPosition > StartPosition (Row, Biceps
    Curl, Triceps Pressdown, presses) -> the first half of a rep is concentric; otherwise (Pull
    Down, Dead Lift, Belt Squat, Romanian Dead Lift) the first half is eccentric.
  - With a 0 s pause the first second of a phase still carries the force level of the phase before
    (turnaround). Skipping PHASE_SETTLE_S reproduces the machine's maxima (57/67 concentric, 65/67
    eccentric exact); position windows do worse for the eccentric peak, which sits at the range edge.
  - The measured range equals the programmed range (66/66), so the encoder adds nothing for the ROM
    itself - it is used for the force-by-third profile only.
  - Sudden force drops are NOT a grip signal (most of them occur on the Belt Squat, without any
    grip). They are reported as a neutral steadiness figure, to be read against the exercise's own
    typical value only.

Leaf module: imports arx_base only. Public domain / CC0. No warranty. Not medical advice.
"""

from __future__ import annotations
import os, json, gzip, math, bisect, statistics as st

from arx_base import data_dir, blob_bytes, _ts, LB_TO_KG, IN_TO_CM

# Bump DETAIL_ALGO_VERSION whenever a formula below changes: the cache is keyed per set and would
# otherwise serve stale readings forever (a recorded set never changes, our reading of it may).
DETAIL_ALGO_VERSION = 1
EFFORT_ALGO_VERSION = 3        # effort v3 (see effort_v3); v2 was the whole-rep-peak inroad

PHASE_SETTLE_S = 1.0           # start of a phase that still carries the previous phase's force
SHORT_PHASE_S = 2.0            # a phase shorter than this is used whole (nothing left after settling)
HOLD_MIN_S = 0.5               # a pause shorter than this is a turnaround, not a hold
DROP_REL = 0.30                # steadiness: force falls by this share of the running level ...
DROP_WINDOW_S = 0.4            # ... within this time ...
DROP_EDGE_START_S = 1.0        # ... outside the first second ...
DROP_EDGE_END_S = 0.7          # ... and the last 0.7 s of a moving phase ...
DROP_MIN_KG = 27.0             # ... at a force level that is more than noise
WEAK_THIRD_REL = 0.75          # a third below this share of the strongest third ...
WEAK_THIRD_REPS = 0.75         # ... in at least this share of the reps = the weak range
STATIC_SLICES = 6              # a static (isometric) set is read as this many equal time slices

INROAD_DEEP = 20               # effort v3 >= this -> the set reached deep fatigue
INROAD_MODERATE = 10           # ... >= this -> moderate; below -> sub-maximal
BORDERLINE = 2                 # within this many points of a threshold = say so

MOVING = ("BeginFirstHalf", "BeginSecondHalf")
REP_EVENTS = ("BeginRep", "BeginFirstHalf", "BeginPauseAfterFirstHalf", "BeginSecondHalf",
              "BeginPauseAfterSecondHalf", "EndRep")


# =============================================================================
# Decoding
# =============================================================================
def decode_raw(det_blob, ev_blob, scheme_blob, repscheme: str | None = None) -> dict:
    """Decode the three blobs of one set into plain arrays.

    Returns {t[], f[], pos[], events[(t, type, data)], scheme{}, static}: t in seconds since the
    first sample, f = force in kg, pos = encoder position in cm - sorted by time, full precision.
    The countdown ticks (WaitingTimeLeft, ~2000 per set) are dropped."""
    samples = json.loads(gzip.decompress(blob_bytes(det_blob)).decode("utf-16")) if det_blob else []
    try:
        events = json.loads(blob_bytes(ev_blob).decode("latin1")) if ev_blob else []
    except Exception:
        events = []
    try:
        scheme = json.loads(blob_bytes(scheme_blob).decode("latin1")) if scheme_blob else {}
    except Exception:
        scheme = {}
    rows = []
    for s in samples:
        ts, v = _ts(s.get("Time", "")), s.get("Value")
        if ts is not None and isinstance(v, (int, float)):
            rows.append((ts, float(v) * LB_TO_KG, float(s.get("EncoderValue") or 0.0) * IN_TO_CM))
    rows.sort(key=lambda r: r[0])
    t0 = rows[0][0] if rows else 0.0
    ev = []
    for e in events or []:
        ts, typ = _ts(e.get("Time", "")), e.get("Type")
        if ts is not None and typ and typ != "WaitingTimeLeft":
            ev.append((ts - t0, typ, e.get("AdditionalData")))
    ev.sort(key=lambda x: x[0])
    start, end = scheme.get("StartPosition"), scheme.get("EndPosition")
    static = (repscheme or "").strip() == "StaticModeData" or (start is not None and start == end)
    return {"t": [r[0] - t0 for r in rows], "f": [r[1] for r in rows], "pos": [r[2] for r in rows],
            "events": ev, "scheme": scheme if isinstance(scheme, dict) else {}, "static": static}


def load_raw(con, set_id: int) -> dict:
    """decode_raw() for one set id (one query, one decoding for everything that needs the curve)."""
    cur = con.cursor()
    cur.execute('select serializeddetaileddata, eventstreamdata, repschemedata, repscheme '
                'from "ExerciseSet" where id = ?', (set_id,))
    det, ev, scheme, repscheme = cur.fetchone()
    return decode_raw(det, ev, scheme, repscheme)


# =============================================================================
# Time-weighted statistics on the irregular sample grid
# =============================================================================
def _value_at(t: list, y: list, x: float) -> float:
    """y(x) on the piecewise-linear curve (clamped at the ends)."""
    if x <= t[0]:
        return y[0]
    if x >= t[-1]:
        return y[-1]
    hi = bisect.bisect_right(t, x)               # t[hi-1] <= x < t[hi]
    lo = hi - 1
    dt = t[hi] - t[lo]
    return y[lo] if dt <= 0 else y[lo] + (y[hi] - y[lo]) * (x - t[lo]) / dt


def _window(t: list, y: list, a: float, b: float) -> tuple[list, list]:
    """The curve between a and b incl. interpolated border points."""
    i, j = bisect.bisect_right(t, a), bisect.bisect_left(t, b)     # samples strictly inside (a, b)
    xs = [a] + t[i:j] + [b]
    ys = [_value_at(t, y, a)] + y[i:j] + [_value_at(t, y, b)]
    return xs, ys


def tw_mean(t: list, y: list, a: float, b: float) -> float | None:
    """Time-weighted mean of y over [a, b]: trapezoid integral / duration. The samples come in
    bursts with gaps, so every sample must count for the time it stands for - never a plain mean."""
    if not t or b <= a:
        return None
    xs, ys = _window(t, y, a, b)
    area = sum((xs[i + 1] - xs[i]) * (ys[i + 1] + ys[i]) / 2.0 for i in range(len(xs) - 1))
    return area / (b - a)


def tw_max(t: list, y: list, a: float, b: float) -> float | None:
    if not t or b <= a:
        return None
    return max(_window(t, y, a, b)[1])


# =============================================================================
# Repetitions and phases
# =============================================================================
def segment_reps(events: list) -> tuple[list[dict], dict | None]:
    """Group the machine's events into repetitions.

    Each complete rep: {begin, h1, p1, h2, p2, end} = BeginRep, BeginFirstHalf,
    BeginPauseAfterFirstHalf, BeginSecondHalf, BeginPauseAfterSecondHalf, EndRep (seconds; a
    missing marker is None). A trailing rep without EndRep (the set was stopped, or a countdown
    protocol ran out) is returned separately as 'tail': it counts for time under tension, never
    for averages or for the effort figure."""
    key = {"BeginRep": "begin", "BeginFirstHalf": "h1", "BeginPauseAfterFirstHalf": "p1",
           "BeginSecondHalf": "h2", "BeginPauseAfterSecondHalf": "p2", "EndRep": "end"}
    reps, cur = [], None
    for t, typ, _data in events:
        if typ not in key:
            continue
        if typ == "BeginRep":
            cur = {k: None for k in key.values()}
            cur["begin"] = t
            reps.append(cur)
        elif cur is not None:
            if cur[key[typ]] is None:
                cur[key[typ]] = t
            if typ == "EndRep":
                cur = None
    complete = [r for r in reps if r["end"] is not None and r["h1"] is not None and r["h2"] is not None]
    tail = next((r for r in reversed(reps) if r["end"] is None and r["h1"] is not None), None)
    return complete, tail


def _phase_windows(rep: dict, last_t: float) -> dict:
    """{first: (a, b), hold_end: (a, b), second: (a, b), hold_start: (a, b)} - b None-safe."""
    h1, p1, h2, p2, end = rep["h1"], rep["p1"], rep["h2"], rep["p2"], rep["end"]
    stop = end if end is not None else last_t          # an unfinished rep ends with the recording
    first_end = p1 if p1 is not None else (h2 if h2 is not None else stop)
    second_end = p2 if p2 is not None else stop
    return {"first": (h1, first_end) if first_end is not None else None,
            "hold_end": (p1, h2) if (p1 is not None and h2 is not None) else None,
            "second": (h2, second_end) if h2 is not None else None,
            "hold_start": (p2, end) if (p2 is not None and end is not None) else None}


def _settled(a: float, b: float) -> tuple[float, float]:
    """The part of a moving phase that is free of the turnaround carry-over."""
    return (a, b) if (b - a) < SHORT_PHASE_S else (a + PHASE_SETTLE_S, b)


def _thirds(t, f, pos, a, b, start_cm, end_cm) -> list | None:
    """Mean force in each third of the RANGE (index 0 = nearest the start position), found from
    where the encoder crosses the third borders inside this phase."""
    if b - a < 0.3 or start_cm is None or end_cm is None or start_cm == end_cm:
        return None
    xs, ps = _window(t, pos, a, b)
    lo, hi = min(start_cm, end_cm), max(start_cm, end_cm)
    borders = [lo + (hi - lo) / 3.0, lo + 2 * (hi - lo) / 3.0]
    rising = ps[-1] >= ps[0]
    cuts = []
    for border in (borders if rising else reversed(borders)):
        cut = None
        for i in range(len(xs) - 1):
            p0, p1 = ps[i], ps[i + 1]
            if (p0 - border) * (p1 - border) <= 0 and p0 != p1:
                cut = xs[i] + (xs[i + 1] - xs[i]) * (border - p0) / (p1 - p0)
                break
        if cut is None:
            return None
        cuts.append(cut)
    if not (a < cuts[0] < cuts[1] < b):
        return None
    parts = [tw_mean(t, f, a, cuts[0]), tw_mean(t, f, cuts[0], cuts[1]), tw_mean(t, f, cuts[1], b)]
    if any(p is None for p in parts):
        return None
    # parts are in the order TRAVELLED. Re-index them by POSITION: low -> high first, then so that
    # index 0 is the third nearest the programmed start position (same meaning for both phases).
    parts = parts if rising else parts[::-1]
    if start_cm > end_cm:
        parts = parts[::-1]
    return [round(p, 1) for p in parts]


def _drops(t, f, a, b) -> int:
    """Steadiness: sudden losses of force inside a moving phase (see the DROP_* constants)."""
    a2, b2 = a + DROP_EDGE_START_S, b - DROP_EDGE_END_S
    if b2 - a2 <= DROP_WINDOW_S:
        return 0
    idx = range(bisect.bisect_left(t, a2), bisect.bisect_right(t, b2))
    n, i = 0, 0
    while i < len(idx):
        k = idx[i]
        level = f[k]
        if level >= DROP_MIN_KG:
            j = i + 1
            low = level
            while j < len(idx) and t[idx[j]] - t[k] <= DROP_WINDOW_S:
                low = min(low, f[idx[j]])
                j += 1
            if low <= level * (1 - DROP_REL):
                n += 1
                i = j                              # one event per drop
                continue
        i += 1
    return n


def _r(v, nd=1):
    return None if v is None else round(v, nd)


# =============================================================================
# Effort v3
# =============================================================================
def _best_window(vals: list, w: int, upto: int) -> float | None:
    """Best mean of w consecutive values among vals[0:upto]."""
    pool = vals[:upto]
    if len(pool) < w:
        return st.mean(pool) if pool else None
    return max(st.mean(pool[i:i + w]) for i in range(len(pool) - w + 1))


def _decline(vals: list) -> tuple[float | None, float | None]:
    """(reference, end) of a per-rep series: reference = best window in the FIRST HALF of the set,
    end = median of the last reps. Restricting the reference to the first half matters: a 'best
    window anywhere vs the end' statistic reads ~10 % on randomly shuffled reps (noise looks like
    effort); this one reads ~0 there, and a set whose force RISES reads 0."""
    n = len(vals)
    half = math.ceil(n / 2)
    w = max(2, n // 4)
    k = min(max(3, n // 4), n - half)
    if n < 4 or k < 1:
        return None, None
    return _best_window(vals, w, half), st.median(vals[-k:])


def effort_v3(con: list, ecc: list) -> dict:
    """Effort of a set from its per-rep concentric and eccentric mean forces.

    Both phases are normalised to their own reference and combined rep by rep, so a set that only
    shifted work from one phase to the other reads ~0, while a concentric collapse with a held
    eccentric still shows. Returns inroad_v3 (0..100), the signed output change, the per-phase
    fatigue figures (explanatory) and the class: deep >= 20, moderate >= 10, else submax."""
    n = min(len(con), len(ecc))
    out = {"inroad_v3": None, "output_change_pct": None, "fatigue_con_pct": None, "fatigue_ecc_pct": None,
           "effort": "unknown", "borderline": False, "pacing_deficit_pct": None, "best_rep": None}
    if n < 4:
        return out
    con, ecc = con[:n], ecc[:n]
    c_ref, c_end = _decline(con)
    e_ref, e_end = _decline(ecc)
    if not c_ref or not e_ref:
        return out
    series = [0.5 * (c / c_ref + e / e_ref) for c, e in zip(con, ecc)]
    ref, end = _decline(series)
    if not ref:
        return out
    change = (end / ref - 1) * 100
    inroad = max(0, round(-change))
    w = max(2, n // 4)
    first = st.mean(series[:w])
    best = _best_window(series, w, n)
    out.update({
        "inroad_v3": inroad,
        "output_change_pct": round(change, 1),
        "fatigue_con_pct": max(0, round((1 - c_end / c_ref) * 100)),
        "fatigue_ecc_pct": max(0, round((1 - e_end / e_ref) * 100)),
        "effort": "deep" if inroad >= INROAD_DEEP else ("moderate" if inroad >= INROAD_MODERATE else "submax"),
        "borderline": min(abs(inroad - INROAD_DEEP), abs(inroad - INROAD_MODERATE)) <= BORDERLINE,
        # how much the athlete held back at the start: first reps vs the best stretch of the set
        "pacing_deficit_pct": max(0, round((1 - first / best) * 100)) if best else None,
        "best_rep": max(range(n), key=lambda i: series[i]) + 1,
    })
    return out


def _legacy_inroad(t, f, events) -> tuple[int | None, str]:
    """The v2 figure exactly as versions <= 0.3.x computed it (arx_report.rep_segments / _inroad /
    classify_effort): BeginRep/EndRep pairs by position, highest SAMPLE of each rep (force rounded
    to 0.1 kg, time to 0.01 s), decline from the best rep peak to the last one; 'rising' = the last
    peak is within 2 % of the first. Kept for display and for the v2 -> v3 comparison."""
    begins = [x[0] for x in events if x[1] == "BeginRep"]
    ends = [x[0] for x in events if x[1] == "EndRep"]
    peaks = []
    for a, b in zip(begins, ends):
        seg = [round(fi, 1) for ti, fi in zip(t, f) if a <= round(ti, 2) <= b]
        if seg:
            peaks.append(max(seg))
    if len(peaks) < 2 or max(peaks) <= 0:
        return None, "unknown"
    inroad = round((max(peaks) - peaks[-1]) / max(peaks) * 100)
    rising = peaks[-1] >= peaks[0] * 0.98
    effort = "deep" if inroad >= 20 else ("moderate" if (inroad >= 8 and not rising) else "submax")
    return inroad, effort


# =============================================================================
# One set
# =============================================================================
def set_detail(raw: dict, intensity_lb: float | None = None, seconds: float | None = None) -> dict:
    """All detail metrics of one decoded set (see the module docstring). Forces in kg, lengths in
    cm, times in seconds. 'method' says how the effort figure was obtained:
    phases (normal) | static | reps (no phase markers: whole-rep peaks) | none."""
    t, f, pos, scheme = raw["t"], raw["f"], raw["pos"], raw["scheme"]
    out = {"v": DETAIL_ALGO_VERSION, "method": "none", "reps": [], "n_reps": 0, "tail": None}
    if len(t) < 5:
        return out
    last_t = t[-1]
    start_in, end_in = scheme.get("StartPosition"), scheme.get("EndPosition")
    start_cm = start_in * IN_TO_CM if isinstance(start_in, (int, float)) else None
    end_cm = end_in * IN_TO_CM if isinstance(end_in, (int, float)) else None
    out["arx_output"] = round(intensity_lb * seconds / 10.0) if (intensity_lb and seconds) else None

    if raw.get("static"):                         # isometric set: no reps, no phases - time slices
        edges = [last_t * i / STATIC_SLICES for i in range(STATIC_SLICES + 1)]
        slices = [tw_mean(t, f, edges[i], edges[i + 1]) for i in range(STATIC_SLICES)]
        ref = max(slices[:STATIC_SLICES // 2])
        change = (slices[-1] / ref - 1) * 100 if ref else 0.0
        inroad = max(0, round(-change))
        out.update({"method": "static", "static_slices_kg": [_r(x) for x in slices],
                    "inroad_v3": inroad, "output_change_pct": round(change, 1),
                    "effort": "deep" if inroad >= INROAD_DEEP else ("moderate" if inroad >= INROAD_MODERATE else "submax"),
                    "borderline": min(abs(inroad - INROAD_DEEP), abs(inroad - INROAD_MODERATE)) <= BORDERLINE,
                    "tut": {"con_s": 0.0, "ecc_s": 0.0, "hold_s": round(last_t, 1), "total_s": round(last_t, 1)},
                    "peak_kg": _r(max(f)), "inroad_legacy": None, "effort_legacy": "unknown"})
        return out

    reps, tail = segment_reps(raw["events"])
    rising_first = (end_cm is not None and start_cm is not None and end_cm > start_cm)   # first half concentric?
    rows, con, ecc = [], [], []
    tut = {"con_s": 0.0, "ecc_s": 0.0, "hold_s": 0.0}
    drops = 0

    def phase_row(rep, complete: bool, pause_before_first):
        """Metrics of one rep. pause_before_first = the hold window that preceded its first half
        (the previous rep's pause at the start position; None for the first rep)."""
        nonlocal drops
        w = _phase_windows(rep, last_t)
        row = {}
        halves = (("first", "con" if rising_first else "ecc", pause_before_first),
                  ("second", "ecc" if rising_first else "con", w["hold_end"]))
        for half, name, pause in halves:
            win = w[half]
            if not win or win[1] is None or win[1] <= win[0]:
                continue
            a, b = win
            tut[f"{name}_s"] += b - a
            if not complete:
                continue
            sa, sb = _settled(a, b)
            row[f"{name}_mean"] = _r(tw_mean(t, f, sa, sb))
            row[f"{name}_peak"] = _r(tw_max(t, f, sa, sb))
            row[f"{name}_s"] = round(b - a, 2)
            row[f"{name}_thirds"] = _thirds(t, f, pos, a, b, start_cm, end_cm)
            # after a real pause a phase starts clean; after a 0 s turnaround its first moments
            # (and so its first third) still carry the force level of the phase before
            row[f"{name}_carryover"] = bool(pause is not None and (pause[1] - pause[0]) < HOLD_MIN_S)
            drops += _drops(t, f, a, b)
        for hold in ("hold_end", "hold_start"):
            win = w[hold]
            if win and win[1] - win[0] >= HOLD_MIN_S:
                tut["hold_s"] += win[1] - win[0]
                if complete:
                    row[f"{hold}_mean"] = _r(tw_mean(t, f, win[0], win[1]))
                    row[f"{hold}_s"] = round(win[1] - win[0], 2)
        return row, w["hold_start"]

    pause_before = None
    for i, rep in enumerate(reps):
        row, pause_before = phase_row(rep, True, pause_before)
        if row.get("con_mean") is None or row.get("ecc_mean") is None:
            continue
        row["i"] = i + 1
        rows.append(row)
        con.append(row["con_mean"])
        ecc.append(row["ecc_mean"])
    if tail:
        phase_row(tail, False, pause_before)        # time under tension only
        out["tail"] = {"begin_s": round(tail["begin"], 1)}

    legacy_inroad, legacy_effort = _legacy_inroad(t, f, raw["events"])
    out.update({"reps": rows, "n_reps": len(rows), "peak_kg": _r(max(f)),
                "inroad_legacy": legacy_inroad, "effort_legacy": legacy_effort,
                "tut": {k: round(v, 1) for k, v in tut.items()} | {"total_s": round(sum(tut.values()), 1)},
                "drops_mid": drops})
    if len(rows) < 2:
        if legacy_inroad is not None:               # no usable phase markers: fall back to whole-rep peaks
            out.update({"method": "reps", "inroad_v3": legacy_inroad, "effort": legacy_effort,
                        "output_change_pct": None, "borderline": False})
        return out

    top3 = lambda xs: st.mean(sorted(xs, reverse=True)[:3])
    con_avg, ecc_avg = st.mean(con), st.mean(ecc)
    holds = [r[k] for r in rows for k in ("hold_end_mean", "hold_start_mean") if r.get(k) is not None]
    out.update({
        "method": "phases",
        "first_half": "con" if rising_first else "ecc",
        "con_top3_kg": _r(top3(con)), "ecc_top3_kg": _r(top3(ecc)),     # settings-robust strength metric
        "con_avg_kg": _r(con_avg), "ecc_avg_kg": _r(ecc_avg),
        "mov_kg": _r((con_avg + ecc_avg) / 2),                          # most sensitive to pre-fatigue
        "ecc_con_ratio": _r(ecc_avg / con_avg, 2) if con_avg else None,
        "hold_kg": _r(st.mean(holds)) if holds else None,
        "hold_rel": _r(st.mean(holds) / con_avg, 2) if (holds and con_avg) else None,
        "tempo": {"con_s": _r(st.median([r["con_s"] for r in rows]), 2),
                  "ecc_s": _r(st.median([r["ecc_s"] for r in rows]), 2)},
    })
    out.update(effort_v3(con, ecc))
    if out.get("inroad_v3") is None and legacy_inroad is not None:      # fewer than 4 clean reps
        out.update({"method": "reps", "inroad_v3": legacy_inroad, "effort": legacy_effort})
    # weak range: the third that is clearly the weakest in most reps, per phase
    for name in ("con", "ecc"):
        tri = [r[f"{name}_thirds"] for r in rows if r.get(f"{name}_thirds")]
        if len(tri) >= 3:
            out[f"{name}_thirds_kg"] = [_r(st.mean(x[k] for x in tri)) for k in range(3)]
            weakest = min(range(3), key=lambda k: out[f"{name}_thirds_kg"][k])
            share = sum(1 for x in tri if x[weakest] < WEAK_THIRD_REL * max(x)) / len(tri)
            out[f"{name}_weak_third"] = weakest if share >= WEAK_THIRD_REPS else None
    return out


# =============================================================================
# Cache
# =============================================================================
DETAIL_CACHE = os.path.join(data_dir(), ".detail_cache.json")


def cache_key(set_id, exercise_date) -> str:
    """Set id AND its date: an id alone would serve another athlete's set after a DB switch."""
    return f"{set_id}@{str(exercise_date)[:19]}"


def load_detail_cache() -> dict:
    try:
        with open(DETAIL_CACHE, encoding="utf-8") as fh:
            raw = json.load(fh)
        if (isinstance(raw, dict) and raw.get("_version") == DETAIL_ALGO_VERSION
                and raw.get("_effort_version") == EFFORT_ALGO_VERSION):
            return dict(raw.get("sets") or {})
    except Exception:
        pass
    return {}


def save_detail_cache(sets: dict) -> None:
    try:
        tmp = DETAIL_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"_version": DETAIL_ALGO_VERSION, "_effort_version": EFFORT_ALGO_VERSION, "sets": sets}, fh)
        os.replace(tmp, DETAIL_CACHE)               # never leave a half-written cache behind
    except Exception:
        pass


def get_detail(con, set_row: dict, cache: dict, intensity_lb: float | None = None) -> dict:
    """Detail of one set from the cache, or decoded now. set_row needs id, date, seconds. A set that
    cannot be decoded yields {'method': 'none'} - and that is NOT cached, so a transient failure
    does not stick."""
    key = cache_key(set_row["id"], set_row["date"])
    if key in cache:
        return cache[key]
    try:
        detail = set_detail(load_raw(con, set_row["id"]), intensity_lb, set_row.get("seconds"))
    except Exception:
        return {"v": DETAIL_ALGO_VERSION, "method": "none", "reps": [], "n_reps": 0, "tail": None}
    cache[key] = detail
    return detail
