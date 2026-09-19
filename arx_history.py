#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARX Insight - history: time windows, progress factors per exercise and notable findings (v0.4.0).

Three things the report's third chapter needs, all deterministic and all carrying their own
interpretation (owner's rule: no number without "what does it mean for me -> what follows"):

  windows           weekly buckets (up to 13) and monthly buckets (up to 12): sessions, sets, hard
                    sets per body region, time, ARX Output, personal bests, effort hit-rate, strength
                    index - plus honest availability rules for the quarter / year view.
  progress_factors  per exercise: how much stronger than the athlete's own baseline, separately for
                    the concentric and the eccentric phase, on COMPARABLE days only (same ROM, tempo,
                    protocol; no familiarisation day) and in ONE context (fresh sets when the exercise
                    has been measured fresh at least twice) - with a status, not just a number.
  findings          the anomalies a good trainer would bring up, ranked, at most two per exercise.

Every item carries meaning/action CODES with parameters; meanings.json turns them into sentences
(English / German, in the athlete's units), so the report explains itself without an API key. The AI
later refines the wording - it never invents the facts.

Leaf module apart from arx_base. Public domain / CC0. No warranty. Not medical advice.
"""

from __future__ import annotations
import os, json, math, statistics as st
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))

# muscle vocabulary of exercises.json -> body regions (the grip gets its own row: it is the limiter
# of most pulling and hinging work and deserves to be seen on its own)
REGIONS = {
    "legs": ["quads", "glutes", "hamstrings", "calves"],
    "back": ["lats", "upper_back", "lower_back"],
    "chest": ["chest"],
    "shoulders": ["shoulders"],
    "arms": ["elbow_flexors", "triceps"],
    "grip": ["grip"],
}
REGION_OF = {m: r for r, ms in REGIONS.items() for m in ms}
MUSCLE_NAMES = {   # identifier -> (en, de): identifiers with underscores never reach the athlete
    "grip": ("Grip", "Griff"), "elbow_flexors": ("Elbow flexors", "Armbeuger"), "triceps": ("Triceps", "Trizeps"),
    "shoulders": ("Shoulders", "Schultern"), "chest": ("Chest", "Brust"), "lats": ("Lats", "Latissimus"),
    "upper_back": ("Upper back", "Oberer Rücken"), "lower_back": ("Lower back", "Unterer Rücken"),
    "glutes": ("Glutes", "Gesäß"), "quads": ("Quads", "Oberschenkel vorn"),
    "hamstrings": ("Hamstrings", "Oberschenkel hinten"), "calves": ("Calves", "Waden"),
}
ROLE_WEIGHT = {"target": 1.0, "limiter": 0.5}     # a limiter works, but only as a means
REGION_NAMES = {"legs": ("Legs", "Beine"), "back": ("Back", "Rücken"), "chest": ("Chest", "Brust"),
                "shoulders": ("Shoulders", "Schultern"), "arms": ("Arms", "Arme"), "grip": ("Grip", "Griff")}
AID_NAMES = {"hooks": ("lifting hooks", "Zughaken"), "straps": ("lifting straps", "Zugschlaufen")}
BODY_NAMES = {"weight_kg": ("Body weight", "Körpergewicht"), "arm_cm": ("Upper arm", "Oberarm"), "chest_cm": ("Chest", "Brust"),
              "waist_cm": ("Waist", "Taille"), "thigh_cm": ("Thigh", "Oberschenkel"), "fat_pct": ("Body fat", "Körperfett")}
GROUP_NAMES = {"Push": ("Push (chest, shoulders, triceps)", "Drücken (Brust, Schultern, Trizeps)"),
               "Pull": ("Pull (back, biceps)", "Ziehen (Rücken, Bizeps)"), "Drive": ("Drive (legs, hips)", "Beine (Drive)")}
WEEKDAY_NAMES = (("Monday", "Montag"), ("Tuesday", "Dienstag"), ("Wednesday", "Mittwoch"), ("Thursday", "Donnerstag"),
                 ("Friday", "Freitag"), ("Saturday", "Samstag"), ("Sunday", "Sonntag"))

WEEKS_MAX, MONTHS_MAX = 13, 12
QUARTER_MIN_WEEKS, QUARTER_MIN_DAYS = 8, 8        # quarter view: span and training days in 13 weeks
YEAR_MIN_WEEKS, YEAR_MIN_MONTHS = 26, 4           # year view: span and months with >= 2 training days

PROGRESS_PCT = 5.0             # >= +5 % / <= -5 % on comparable days = progressing / regressing
PLATEAU_PCT = 3.0              # within +-3 % (day-to-day noise of the phase means is about +-5 %) ...
PLATEAU_MIN_N, PLATEAU_MIN_DAYS = 4, 21           # ... on at least 4 days over 3 weeks = plateau
INSUFFICIENT_DAYS = 10         # two points or a shorter span: show the change, give no verdict
RATE_MIN_N, RATE_MIN_DAYS = 4, 14                 # below this no %/week at all
CONTEXT_DROP_PCT = 5.0         # latest value this far below the reference, but measured pre-loaded

ORDER_EFFECT_PCT, ORDER_EFFECT_N = 8.0, 2         # a measured order effect worth bringing up
REPEAT_EFFECT_PCT = 10.0
ECC_CON_SHIFT_PCT = 15.0
HOLD_MIN_PAUSE_S, HOLD_REL_MIN = 2.0, 0.25        # a programmed hold that was not really held
UNSTEADY_FACTOR, UNSTEADY_MIN = 2.0, 3
PACING_DEFICIT_PCT, UNDERLOAD_INROAD = 15, 10
HIT_RATE_MIN_SETS, HIT_RATE_LOW = 8, 0.30
NEGLECTED_DAYS = 14
FINDINGS_TOP, FINDINGS_PER_EXERCISE = 6, 2
SEVERITY_SCORE = {"warn": 3.0, "good": 2.0, "info": 1.0}
CONFIDENCE_SCORE = {"high": 1.0, "medium": 0.85, "low": 0.7, "anecdotal": 0.5, None: 1.0}


# =============================================================================
# Meaning / action sentences
# =============================================================================
_MEANINGS: dict | None = None


def _meanings() -> dict:
    global _MEANINGS
    if _MEANINGS is None:
        try:
            with open(os.path.join(HERE, "meanings.json"), encoding="utf-8") as f:
                _MEANINGS = json.load(f)
        except Exception:
            _MEANINGS = {}
    return _MEANINGS


def _fmt(key: str, val, imperial: bool, lang: str):
    """Format one template parameter by its suffix: _kg / _cm are converted to the athlete's units,
    _pct gets its sign, _date becomes day.month (de) or month-day (en)."""
    if val is None:
        return "–"
    if key == "index":
        return (f"{val:.2f}").replace(".", ",") if lang == "de" else f"{val:.2f}"
    if key == "muscle":
        names = MUSCLE_NAMES.get(str(val))
        return names[1 if lang == "de" else 0] if names else str(val).replace("_", " ")
    if key == "muscles" and isinstance(val, (list, tuple)):         # several muscles, spelled out (never a raw identifier)
        return ", ".join(_fmt("muscle", m, imperial, lang) for m in val)
    if key in ("region", "aid", "metric"):
        names = {"region": REGION_NAMES, "aid": AID_NAMES, "metric": BODY_NAMES}[key].get(str(val))
        return names[1 if lang == "de" else 0] if names else str(val).replace("_", " ")
    if key == "groups" and isinstance(val, (list, tuple)):          # the machine's movement groups, spelled out
        return " + ".join((GROUP_NAMES.get(str(g)) or (str(g), str(g)))[1 if lang == "de" else 0] for g in val)
    if key == "weekday" and isinstance(val, int) and 0 <= val <= 6:
        return WEEKDAY_NAMES[val][1 if lang == "de" else 0]
    comma = (lambda x: x.replace(".", ",")) if lang == "de" else (lambda x: x)
    if key.endswith("_kg"):
        v, u = (val * 2.20462, "lb") if imperial else (val, "kg")
        return comma(f"{v:.0f} {u}" if abs(v) >= 100 else f"{v:.1f} {u}")
    if key.endswith("_cm"):
        v, u = (val / 2.54, "in") if imperial else (val, "cm")
        return comma(f"{v:.1f} {u}")
    if key.endswith("_spct"):                        # signed percentage
        return comma(f"{val:+.1f} %")
    if key.endswith("_pct"):
        return comma(f"{val:.0f} %" if float(val).is_integer() else f"{val:.1f} %")
    if key.endswith("_date") and isinstance(val, str) and len(val) >= 10:
        return f"{val[8:10]}.{val[5:7]}." if lang == "de" else val[5:10]
    if isinstance(val, float):
        return comma(f"{val:.1f}")
    if isinstance(val, (list, tuple)):
        return ", ".join(str(x) for x in val)
    return str(val)


def explain(code: str, params: dict, cfg: dict) -> dict:
    """{meaning, action} sentences for a code from meanings.json in the athlete's language and
    units. Unknown codes yield empty strings - the codes and params always travel with the item, so
    the UI or the AI can still use them."""
    lang = "de" if (cfg or {}).get("language", "en") == "de" else "en"
    imperial = (cfg or {}).get("units", "imperial") == "imperial"
    entry = _meanings().get(code) or {}
    shown = {k: _fmt(k, v, imperial, lang) for k, v in (params or {}).items()}
    out = {}
    for part in ("meaning", "action"):
        tpl = (entry.get(part) or {}).get(lang) or (entry.get(part) or {}).get("en") or ""
        try:
            out[part] = tpl.format(**shown)
        except (KeyError, IndexError):
            out[part] = tpl
    return out


def item(code: str, params: dict, cfg: dict, **extra) -> dict:
    """A self-explaining item: code + params + the rendered sentences."""
    return {"code": code, "params": params, "text": explain(code, params, cfg), **extra}


# =============================================================================
# Windows
# =============================================================================
def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())           # ISO week: Monday


def _muscle_roles(s: dict, catalog: dict) -> dict:
    meta = catalog.get(str(s["exercise"]), {})
    targets = list(meta.get("targets") or [])
    out = {m: "target" for m in targets}
    for m in s.get("limiters_eff", meta.get("limiters") or []):
        out.setdefault(m, "limiter")
    return out


def _bucket(sets: list[dict], day_info: dict, catalog: dict, effort_min: int) -> dict:
    days = sorted({s["date"][:10] for s in sets})
    hard = [s for s in sets if s.get("effort") in ("moderate", "deep")]
    by_region = {r: 0.0 for r in REGIONS}
    by_muscle: dict = {}
    for s in hard:
        for m, role in _muscle_roles(s, catalog).items():
            w = ROLE_WEIGHT[role]
            by_muscle[m] = by_muscle.get(m, 0.0) + w
            if m in REGION_OF:
                by_region[REGION_OF[m]] += w
    judged = [s for s in sets if s.get("inroad") is not None and not s.get("effort_capped")]
    hits = sum(1 for s in judged if s["inroad"] >= effort_min)
    return {
        "training_days": len(days),
        "sessions": sum(day_info.get(d, {}).get("visits", 1) for d in days),
        "working_sets": len(sets), "hard_sets": len(hard),
        "deep_sets": sum(1 for s in sets if s.get("effort") == "deep"),
        "hard_sets_by_region": {r: round(v, 1) for r, v in by_region.items()},
        "hard_sets_by_muscle": {m: round(v, 1) for m, v in sorted(by_muscle.items())},
        "tul_min": round(sum(s["seconds"] for s in sets) / 60.0, 1),
        "wall_min": round(sum(day_info.get(d, {}).get("wall_minutes") or 0 for d in days), 1),
        "arx_output": round(sum(s.get("arx_output") or 0 for s in sets)),
        "impulse_kg_s": round(sum(s.get("impulse_kg_s") or 0 for s in sets)),
        "effort_hit_rate": round(hits / len(judged), 2) if judged else None,
        "effort_judged_sets": len(judged),
    }


def build_windows(work: list[dict], sequences_all: list[dict], catalog: dict, today: date,
                  sessions_per_week, effort_min: int, checkins: list[dict] | None,
                  pb_days: dict, index_series: list[dict]) -> dict:
    """Weekly and monthly buckets + availability of the quarter / year view.

    pb_days = {date: [exercise names]} (comparable records, see progress_factors);
    index_series = [{date, index}] of the overall strength index per training day."""
    day_info = {d["date"]: d for d in sequences_all}
    target = int(sessions_per_week or 2)
    first = min((date.fromisoformat(s["date"][:10]) for s in work), default=today)
    span_weeks = max(0.0, (today - first).days / 7.0)
    ci = {c["date"]: c for c in (checkins or []) if c.get("date")}

    def enrich(b: dict, start: date, end: date, label: str, key: str, kind: str) -> dict:
        idx = [p["index"] for p in index_series if start.isoformat() <= p["date"] <= end.isoformat()]
        scores = [c["score"] for d, c in ci.items() if start.isoformat() <= d <= end.isoformat() and c.get("score") is not None]
        rhr = [c["rhr"] for d, c in ci.items() if start.isoformat() <= d <= end.isoformat() and c.get("rhr")]
        pbs = [n for d, names in pb_days.items() if start.isoformat() <= d <= end.isoformat() for n in names]
        b.update({"key": key, "label": label, "kind": kind, "start": start.isoformat(), "end": end.isoformat(),
                  "partial": end >= today and start <= today,
                  "pbs": len(pbs), "pb_exercises": sorted(set(pbs)),
                  "strength_index": idx[-1] if idx else None,
                  "readiness_avg": round(st.mean(scores)) if scores else None,
                  "rhr_avg": round(st.mean(rhr), 1) if rhr else None})
        if kind == "week":
            b["target_sessions"] = target
            b["target_met"] = b["training_days"] >= target
        return b

    weeks = []
    w0 = _week_start(today)
    for back in range(WEEKS_MAX - 1, -1, -1):
        start = w0 - timedelta(weeks=back)
        end = start + timedelta(days=6)
        if end < _week_start(first):
            continue                                   # before the athlete's first week: no bucket
        sets = [s for s in work if start.isoformat() <= s["date"][:10] <= end.isoformat()]
        y, w, _ = start.isocalendar()
        weeks.append(enrich(_bucket(sets, day_info, catalog, effort_min), start, end, f"W{w:02d}", f"{y}-W{w:02d}", "week"))

    months = []
    for back in range(MONTHS_MAX - 1, -1, -1):
        y, m = today.year, today.month - back
        while m <= 0:
            y, m = y - 1, m + 12
        start = date(y, m, 1)
        end = (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1))
        if end < first.replace(day=1):
            continue
        sets = [s for s in work if start.isoformat() <= s["date"][:10] <= end.isoformat()]
        months.append(enrich(_bucket(sets, day_info, catalog, effort_min), start, end, f"{y}-{m:02d}", f"{y}-{m:02d}", "month"))

    days_13w = len({s["date"][:10] for s in work if date.fromisoformat(s["date"][:10]) > today - timedelta(weeks=13)})
    months_2 = sum(1 for b in months if b["training_days"] >= 2)
    quarter_ok = span_weeks >= QUARTER_MIN_WEEKS and days_13w >= QUARTER_MIN_DAYS
    year_ok = span_weeks >= YEAR_MIN_WEEKS and months_2 >= YEAR_MIN_MONTHS
    availability = {
        "weeks_of_history": round(span_weeks, 1),
        "w4": {"available": True},
        "quarter": {"available": quarter_ok,
                    "have": {"weeks": round(span_weeks, 1), "training_days_13w": days_13w},
                    "need": {"weeks": QUARTER_MIN_WEEKS, "training_days_13w": QUARTER_MIN_DAYS},
                    "unlock_in_weeks": None if quarter_ok else max(1, math.ceil(QUARTER_MIN_WEEKS - span_weeks))},
        "year": {"available": year_ok,
                 "have": {"weeks": round(span_weeks, 1), "months_with_training": months_2},
                 "need": {"weeks": YEAR_MIN_WEEKS, "months_with_training": YEAR_MIN_MONTHS},
                 "unlock_in_weeks": None if year_ok else max(1, math.ceil(YEAR_MIN_WEEKS - span_weeks))},
    }

    def total(buckets: list[dict]) -> dict | None:
        if not buckets:
            return None
        keys = ("training_days", "sessions", "working_sets", "hard_sets", "deep_sets", "tul_min", "wall_min", "arx_output", "pbs")
        out = {k: round(sum(b[k] for b in buckets), 1) for k in keys}
        out["weeks"] = len(buckets)
        return out
    last4, prev4 = weeks[-4:], weeks[-8:-4]
    compare = None
    if len(prev4) == 4 and sum(b["training_days"] for b in prev4) >= 3 and sum(b["training_days"] for b in last4) >= 3:
        a, b = total(prev4), total(last4)
        compare = {"previous": a, "current": b,
                   "change_pct": {k: round((b[k] / a[k] - 1) * 100) for k in ("sessions", "hard_sets", "tul_min", "arx_output") if a[k]}}
    return {"availability": availability, "weeks": weeks, "months": months,
            "last4": total(last4), "compare_4w": compare, "target_sessions_per_week": target}


# =============================================================================
# Progress factors
# =============================================================================
def _theil_sen(xs: list[float], ys: list[float]) -> float | None:
    slopes = [(ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(len(xs)) for j in range(i + 1, len(xs)) if xs[j] != xs[i]]
    return st.median(slopes) if slopes else None


def _ends(vals: list[float]) -> tuple[float, float]:
    """(baseline, latest): medians of the first / last two values from 4 points on, else the ends."""
    if len(vals) >= 4:
        return st.median(vals[:2]), st.median(vals[-2:])
    return vals[0], vals[-1]


def exercise_progress(e: dict, work_ex: list[dict], today: date, effort_min: int, cfg: dict) -> dict:
    """Progress factor of one exercise (see the module docstring and the constants above)."""
    occ = e["occ"]
    days = [o for o in occ if o.get("comparable") and o.get("con_top3_kg") and o.get("ecc_top3_kg")]
    fresh = [o for o in days if o.get("context") == "fresh"]
    ref_context = "fresh" if len(fresh) >= 2 else "any"
    used = fresh if ref_context == "fresh" else days
    out = {"name": e["name"], "ex": e["ex"], "group": e["group"], "n": len(used), "reference_context": ref_context,
           "never_fresh": not any(o.get("context") == "fresh" for o in occ),
           "days_total": len(occ), "days_familiarisation": e.get("days_familiarisation", 0),
           "days_excluded_for_rom": e.get("days_excluded_for_rom", 0), "days_excluded_other": e.get("days_excluded_other", 0),
           "change_pct": None, "change_con_pct": None, "change_ecc_pct": None, "change_4w_pct": None,
           "change_quarter_pct": None, "rate_pct_per_week": None, "rate_quality": None, "span_days": None,
           "baseline_date": None, "latest_date": None, "latest_con_kg": None, "latest_ecc_kg": None,
           "latest_context_drop_pct": None, "points": []}
    # effort quality of this exercise over the last 4 weeks
    recent = [s for s in work_ex if date.fromisoformat(s["date"][:10]) > today - timedelta(days=28)
              and s.get("inroad") is not None and not s.get("effort_capped")]
    out["effort_hit_rate"] = round(sum(1 for s in recent if s["inroad"] >= effort_min) / len(recent), 2) if recent else None
    out["effort_sets"] = len(recent)

    if occ and occ[-1].get("fam"):
        status = "familiarisation"
    elif len(used) < 2:
        status = "not_comparable"
    else:
        con, ecc = [o["con_top3_kg"] for o in used], [o["ecc_top3_kg"] for o in used]
        c0, c1 = _ends(con)
        e0, e1 = _ends(ecc)
        index = [0.5 * (c / c0 + x / e0) for c, x in zip(con, ecc)]        # 1.00 = the athlete's own baseline
        i0, i1 = _ends(index)
        ords = [date.fromisoformat(o["date"]).toordinal() for o in used]
        span = ords[-1] - ords[0]
        change = (i1 / i0 - 1) * 100
        out.update({"change_pct": round(change, 1), "change_con_pct": round((c1 / c0 - 1) * 100, 1),
                    "change_ecc_pct": round((e1 / e0 - 1) * 100, 1), "span_days": span,
                    "baseline_date": used[0]["date"], "latest_date": used[-1]["date"],
                    "latest_con_kg": used[-1]["con_top3_kg"], "latest_ecc_kg": used[-1]["ecc_top3_kg"],
                    "index_latest": round(i1, 3),
                    "points": [{"date": o["date"], "index": round(v, 3), "con_kg": o["con_top3_kg"], "ecc_kg": o["ecc_top3_kg"],
                                "context": o.get("context")} for o, v in zip(used, index)]})
        for key, window in (("change_4w_pct", 28), ("change_quarter_pct", 91)):
            inside = [v for o, v in zip(ords, index) if o > today.toordinal() - window]
            if len(inside) >= 2:
                out[key] = round((_ends(inside)[1] / _ends(inside)[0] - 1) * 100, 1)
        if len(used) >= RATE_MIN_N and span >= RATE_MIN_DAYS:
            slope = _theil_sen([o / 7.0 for o in ords], [v * 100 for v in index])
            diffs = [b - a for a, b in zip(index, index[1:]) if b != a]
            agree = (sum(1 for d in diffs if (d > 0) == ((slope or 0) > 0)) / len(diffs)) if diffs else 0.0
            quality = ("good" if (len(used) >= 6 and span >= 28 and agree >= 0.75)
                       else "fair" if agree >= 0.6 else "low")
            out["rate_quality"] = quality
            out["rate_pct_per_week"] = round(slope, 1) if (slope is not None and quality != "low") else None
        # the latest day overall may be a comparable but PRE-LOADED measurement below the reference
        latest = next((o for o in reversed(occ) if o.get("comparable") and o.get("con_top3_kg")), None)
        if latest and latest is not used[-1] and latest["date"] > used[-1]["date"]:
            li = 0.5 * (latest["con_top3_kg"] / c0 + latest["ecc_top3_kg"] / e0)
            out["latest_context_drop_pct"] = round((li / i1 - 1) * 100, 1)
            out["latest_preloaded_date"], out["latest_preloaded_position"] = latest["date"], latest.get("position")
        if len(used) == 2 or span < INSUFFICIENT_DAYS:
            status = "insufficient"
        elif change >= PROGRESS_PCT:
            status = "progressing"
        elif change <= -PROGRESS_PCT:
            status = "regressing_context" if (ref_context == "any" and used[-1].get("context") != "fresh") else "regressing"
        elif len(used) >= PLATEAU_MIN_N and span >= PLATEAU_MIN_DAYS and abs(change) <= PLATEAU_PCT:
            status = "plateau"
        else:
            status = "stable"
        if status in ("progressing", "stable", "plateau") and (out["latest_context_drop_pct"] or 0) <= -CONTEXT_DROP_PCT:
            out["context_note"] = True                 # the newest (pre-loaded) value is lower: not a regression
    out["status"] = status
    params = {"n": out["n"], "change_spct": out["change_pct"], "con_spct": out["change_con_pct"],
              "ecc_spct": out["change_ecc_pct"], "span_days": out["span_days"],
              "fam_days": out["days_familiarisation"], "excluded": out["days_excluded_for_rom"] + out["days_excluded_other"],
              "drop_spct": out["latest_context_drop_pct"], "position": out.get("latest_preloaded_position")}
    code = f"progress_{status}" + ("_context" if out.get("context_note") else "")
    if status == "insufficient" and out["n"] > 2:
        code = "progress_insufficient_span"            # enough days, but within too short a period
    if status == "not_comparable" and params["excluded"] == 0 and not out["days_familiarisation"]:
        code = "progress_single_day"                   # nothing was excluded - there simply is one day
    out["interp"] = item(code, params, cfg)
    return out


def build_progress(exercises: list[dict], work: list[dict], today: date, effort_min: int, cfg: dict) -> dict:
    """progress_factors = {overall, exercises[]} - attention order: regressing first."""
    by_ex: dict = {}
    for s in work:
        by_ex.setdefault(s["exercise"], []).append(s)
    rows = [exercise_progress(e, by_ex.get(e["ex"], []), today, effort_min, cfg) for e in exercises]
    order = {"regressing": 0, "regressing_context": 1, "plateau": 2, "stable": 3, "progressing": 4,
             "insufficient": 5, "not_comparable": 6, "familiarisation": 7}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["name"]))
    with_index = [r for r in rows if r.get("index_latest")]
    overall = {"strength_index": None, "exercises_in_index": len(with_index), "exercises_total": len(rows)}
    if len(with_index) >= 3 and len(with_index) >= 0.5 * len(rows):
        gm = math.exp(st.mean(math.log(r["index_latest"]) for r in with_index))
        overall["strength_index"] = round(gm, 3)
        overall["change_pct"] = round((gm - 1) * 100, 1)
    judged = [s for s in work if date.fromisoformat(s["date"][:10]) > today - timedelta(days=28)
              and s.get("inroad") is not None and not s.get("effort_capped")]
    overall["effort_hit_rate"] = round(sum(1 for s in judged if s["inroad"] >= effort_min) / len(judged), 2) if judged else None
    overall["effort_sets"], overall["effort_target_inroad"] = len(judged), effort_min
    overall["interp"] = item("overall_index" if overall["strength_index"] else "overall_index_pending",
                             {"index": overall["strength_index"], "change_spct": overall.get("change_pct"),
                              "k": len(with_index), "total": len(rows)}, cfg)
    return {"overall": overall, "exercises": rows}


def index_series(progress: dict) -> list[dict]:
    """Overall strength index per training day: each exercise carries its latest index forward; the
    day's value is the geometric mean over the exercises that have one by then (>= 3 needed)."""
    events = sorted((p["date"], r["name"], p["index"]) for r in progress["exercises"] for p in r.get("points", []))
    latest: dict = {}
    out = []
    for day in sorted({d for d, _, _ in events}):
        for d, name, v in events:
            if d == day:
                latest[name] = v
        if len(latest) >= 3:
            out.append({"date": day, "index": round(math.exp(st.mean(math.log(v) for v in latest.values())), 3), "exercises": len(latest)})
    return out


def pb_days(exercises: list[dict]) -> dict:
    """{date: [exercise]} - days on which a comparable day-best exceeded every earlier comparable one."""
    out: dict = {}
    for e in exercises:
        best = None
        for o in e["occ"]:
            if not o.get("comparable"):
                continue
            if best is not None and o["kg"] > best + 0.01:
                out.setdefault(o["date"], []).append(e["name"])
            best = o["kg"] if best is None else max(best, o["kg"])
    return out


# =============================================================================
# Findings ("Auffaelligkeiten")
# =============================================================================
def build_findings(exercises: list[dict], work: list[dict], progress: dict, ev: dict, last_session: dict | None,
                   load: dict, windows: dict, sets_all: list[dict], catalog: dict, today: date, cfg: dict) -> dict:
    """The notable things, each {id, type, severity, exercise?, muscle?, date, n, confidence, interp}.
    Ranked by severity x recency x confidence; at most FINDINGS_PER_EXERCISE per exercise;
    'top' holds the first FINDINGS_TOP, 'all' everything."""
    found = []

    def add(ftype, severity, params, *, exercise=None, muscle=None, day=None, n=None, conf=None, code=None):
        ident = f"an:{ftype}:{exercise or muscle or 'all'}"
        found.append({"id": ident, "type": ftype, "severity": severity, "exercise": exercise, "muscle": muscle,
                      "date": day or today.isoformat(), "n": n, "confidence": conf,
                      "interp": item(code or f"finding_{ftype}", params, cfg)})

    by_name = {e["name"]: e for e in exercises}
    # range of motion drifted (not for a deliberately shortened range of a restricted exercise)
    for e in exercises:
        if e["n"] >= 2 and not e["rom_stable"] and not e.get("restricted"):
            latest_bad = not e["occ"][-1]["rom_valid"]
            add("rom_drift", "warn" if latest_bad else "info",
                {"ref_cm": e["rom_cm_reference"], "latest_cm": e["rom_cm_latest"], "drift_spct": e["rom_drift_pct"],
                 "days": e["days_excluded_for_rom"]}, exercise=e["name"], day=e["last_date"], n=e["days_excluded_for_rom"])
    # progress statuses worth a word
    for r in progress["exercises"]:
        p = r["interp"]["params"]
        if r["status"] in ("regressing", "regressing_context", "plateau"):
            add(r["status"], "warn" if r["status"] == "regressing" else "info", p, exercise=r["name"],
                day=r.get("latest_date"), n=r["n"], code=r["interp"]["code"])
        elif r.get("context_note"):
            add("context_drop", "info", p, exercise=r["name"], day=r.get("latest_preloaded_date"), n=r["n"], code="finding_context_drop")
    # personal bests on comparable days
    for e in exercises:
        if e.get("last_is_pb") and e["last_date"] > (today - timedelta(days=14)).isoformat():
            add("pb", "good", {"best_kg": e["pb_comparable"], "n": e["trend_n"]}, exercise=e["name"], day=e["last_date"], n=e["trend_n"])
    # the athlete's own measured order / repeat effects
    for p in ev.get("pair_effects", []):
        if p["n"] >= ORDER_EFFECT_N and p["loss_pct"] >= ORDER_EFFECT_PCT:
            add("order_effect", "warn", {"before": p["before"], "then": p["then"], "loss_pct": p["observed_loss_pct"], "n": p["n"]},
                exercise=p["then"], day=p["observations"][-1]["date"], n=p["n"], conf=p["confidence"])
    for p in ev.get("repeat_effects", []):
        if p["n"] >= ORDER_EFFECT_N and p["observed_loss_pct"] >= REPEAT_EFFECT_PCT:
            add("repeat_effect", "info", {"exercise": p["exercise"], "loss_pct": p["observed_loss_pct"], "n": p["n"]},
                exercise=p["exercise"], day=p["observations"][-1]["date"], n=p["n"], conf=p["confidence"])
    if ev.get("never_fresh"):
        add("never_fresh", "info", {"exercises": ev["never_fresh"], "k": len(ev["never_fresh"])}, n=len(ev["never_fresh"]))

    # the last session, set by set
    ls = last_session or {}
    ls_day = ls.get("date")
    ls_sets = [s for s in work if s["date"][:10] == ls_day] if ls_day else []
    med_drops: dict = {}
    for s in work:
        med_drops.setdefault(s["exercise"], []).append(s.get("drops_mid") or 0)
    for s in ls_sets:
        e = by_name.get(s.get("name"), {})
        if (s.get("drops_mid") or 0) >= UNSTEADY_MIN and s["drops_mid"] >= UNSTEADY_FACTOR * max(1.0, st.median(med_drops[s["exercise"]])):
            add("unsteady_force", "info", {"drops": s["drops_mid"], "usual": st.median(med_drops[s["exercise"]])}, exercise=s["name"], day=ls_day)
        # only the END-position hold is meant to be held; the pause at the start is rest by design
        pause = s.get("pause_end_s") or 0
        if pause >= HOLD_MIN_PAUSE_S and s.get("hold_end_rel") is not None and s["hold_end_rel"] < HOLD_REL_MIN:
            add("hold_not_held", "info", {"pause_s": pause, "hold_pct": round(s["hold_end_rel"] * 100)}, exercise=s["name"], day=ls_day)
        prev_ratio = [o.get("ecc_top3_kg") / o["con_top3_kg"] for o in (e.get("occ") or [])[:-1]
                      if o.get("comparable") and o.get("con_top3_kg") and o.get("ecc_top3_kg")][-3:]
        if len(prev_ratio) >= 2 and s.get("comparable") and s.get("con_top3_kg") and s.get("ecc_top3_kg"):
            now, ref = s["ecc_top3_kg"] / s["con_top3_kg"], st.median(prev_ratio)
            if abs(now / ref - 1) * 100 >= ECC_CON_SHIFT_PCT:
                add("ecc_con_shift", "info", {"from": round(ref, 2), "to": round(now, 2), "shift_spct": round((now / ref - 1) * 100, 1)},
                    exercise=s["name"], day=ls_day, n=len(prev_ratio))
    paced = [s for s in ls_sets if (s.get("pacing_deficit_pct") or 0) >= PACING_DEFICIT_PCT and (s.get("inroad") or 0) < UNDERLOAD_INROAD]
    if ls_sets and (len(paced) >= max(1, len(ls_sets) / 2) or (load or {}).get("flag") == "underload"):
        add("underload", "warn", {"k": len(paced), "sets": len(ls_sets),
                                  "deficit_pct": round(st.mean(s["pacing_deficit_pct"] for s in paced)) if paced else None}, day=ls_day, n=len(ls_sets))
    for f in (ls.get("flags") or []):
        if f.get("type") in ("short_intra_session_rest", "too_many_sets"):
            add(f["type"], "warn", {"exercise": f.get("exercise"), "minutes": f.get("minutes_rest"), "sets": f.get("working_sets"),
                                    "after": f.get("after")}, exercise=f.get("exercise"), day=ls_day)
    fs_recent = [s for s in sets_all if s.get("status") == "false_start" and s["date"][:10] > (today - timedelta(days=14)).isoformat()]
    fs_week = [s for s in fs_recent if s["date"][:10] > (today - timedelta(days=7)).isoformat()]
    if (ls.get("false_starts") or 0) >= 2 or (len(fs_recent) >= 3 and fs_week):     # a pattern that is still going on
        add("false_start_pattern", "info", {"last_session": ls.get("false_starts") or 0, "k": len(fs_recent)}, day=ls_day, n=len(fs_recent))

    # effort quality, weekly target, neglected muscles
    ov = progress["overall"]
    if ov.get("effort_sets", 0) >= HIT_RATE_MIN_SETS and (ov.get("effort_hit_rate") or 0) < HIT_RATE_LOW:
        add("effort_target_missed", "warn", {"hits": round(ov["effort_hit_rate"] * ov["effort_sets"]), "sets": ov["effort_sets"],
                                             "target_pct": ov["effort_target_inroad"]}, n=ov["effort_sets"])
    full = [w for w in windows["weeks"] if not w["partial"]]
    if full and not full[-1]["target_met"]:
        add("missed_weekly_target", "info", {"sessions": full[-1]["training_days"], "target": full[-1]["target_sessions"], "week": full[-1]["label"]},
            day=full[-1]["end"])
    last_target: dict = {}
    for s in work:
        for m, role in _muscle_roles(s, catalog).items():
            if role == "target":
                last_target[m] = max(last_target.get(m, ""), s["date"][:10])
    external = set((cfg or {}).get("_external") or [])    # trained outside the ARX (profile: exercises switched off as "elsewhere")
    for m, d in sorted(last_target.items()):
        gap = (today - date.fromisoformat(d)).days
        if gap > NEGLECTED_DAYS and m not in external:
            add("neglected_muscle", "info", {"muscle": m, "days": gap, "last_date": d}, muscle=m, day=d, n=gap)

    def score(f):
        age = (today - date.fromisoformat(f["date"][:10])).days
        recency = 1.0 if age <= 7 else (0.6 if age <= 28 else 0.3)
        return SEVERITY_SCORE.get(f["severity"], 1.0) * recency * CONFIDENCE_SCORE.get(f["confidence"], 1.0)
    found.sort(key=lambda f: (-score(f), f["id"]))
    kept, per_ex = [], {}
    for f in found:
        k = f["exercise"]
        if k and per_ex.get(k, 0) >= FINDINGS_PER_EXERCISE:
            continue
        per_ex[k] = per_ex.get(k, 0) + 1
        kept.append(dict(f, score=round(score(f), 2)))
    return {"top": kept[:FINDINGS_TOP], "all": kept, "more": max(0, len(kept) - FINDINGS_TOP)}


# =============================================================================
# Time efficiency
# =============================================================================
def time_efficiency(windows: dict, progress: dict, cfg: dict) -> dict:
    """How much training time the progress cost: minutes per week over the last 4 weeks and the
    strength-index change per training hour (only once an index exists)."""
    l4 = windows.get("last4") or {}
    weeks = max(1, l4.get("weeks") or 1)
    wall, tul = l4.get("wall_min") or 0.0, l4.get("tul_min") or 0.0
    out = {"wall_min_per_week": round(wall / weeks), "tul_min_per_week": round(tul / weeks, 1),
           "hard_sets_per_week": round((l4.get("hard_sets") or 0) / weeks, 1),
           "sessions_per_week": round((l4.get("training_days") or 0) / weeks, 1),
           "index_change_pct_per_hour": None}
    change = progress["overall"].get("change_pct")
    hours_total = sum(b["wall_min"] for b in windows["weeks"]) / 60.0
    # progress per hour needs a month of history: before that the index is still settling
    if change is not None and hours_total > 0 and windows["availability"]["weeks_of_history"] >= 4:
        out["index_change_pct_per_hour"] = round(change / hours_total, 2)
    out["interp"] = item("time_efficiency" if out["index_change_pct_per_hour"] is not None else "time_efficiency_pending",
                         {"minutes": out["wall_min_per_week"], "tul_minutes": out["tul_min_per_week"],
                          "per_hour_spct": out["index_change_pct_per_hour"], "hard_sets": out["hard_sets_per_week"]}, cfg)
    return out


def build_history(work, sets_all, exercises, sequences_all, catalog, today, cfg, ev, last_session, load,
                  effort_min: int, checkins_scored: list[dict] | None) -> dict:
    """Everything chapter 3 needs: windows, progress_factors, findings, time efficiency."""
    progress = build_progress(exercises, work, today, effort_min, cfg)
    series = index_series(progress)
    windows = build_windows(work, sequences_all, catalog, today, cfg.get("sessions_per_week"), effort_min,
                            checkins_scored, pb_days(exercises), series)
    findings = build_findings(exercises, work, progress, ev, last_session, load, windows, sets_all, catalog, today, cfg)
    return {"windows": windows, "progress_factors": progress, "strength_index_series": series,
            "findings": findings, "time_efficiency": time_efficiency(windows, progress, cfg)}


# =============================================================================
# Optional body log and the measurable target (goal interview)
# =============================================================================
BODY_FIELDS = ("weight_kg", "arm_cm", "chest_cm", "waist_cm", "thigh_cm", "fat_pct")
BODY_MIN_SPAN_DAYS = 21        # below this two readings are just two readings
BODY_NOISE_PCT = 1.5           # weight / girth changes below this are within day-to-day and tape noise (app default)
BODY_FAT_NOISE_POINTS = 2.0    # consumer scales: about 2 percentage points error for a CHANGE (science.json)
STRENGTH_NOISE_PCT = 3.0


def body_trends(body_log: list[dict] | None, progress: dict, cfg: dict) -> dict | None:
    """Rough trends of the OPTIONAL body log - None when nothing was entered (the report then shows
    nothing at all). Per metric: first / last / change; one cautious sentence on what weight, waist
    and the strength index say together. relative_changes is all the AI may ever see of it."""
    entries = sorted((e for e in (body_log or []) if e.get("date") and any(e.get(k) is not None for k in BODY_FIELDS)),
                     key=lambda e: e["date"])
    if not entries:
        return None
    metrics, relative = {}, {}
    for k in BODY_FIELDS:
        vals = [(e["date"], float(e[k])) for e in entries if e.get(k) is not None]
        if not vals:
            continue
        (d0, v0), (d1, v1) = vals[0], vals[-1]
        span = (date.fromisoformat(d1) - date.fromisoformat(d0)).days
        change = round(v1 - v0, 1)
        pct = round((v1 / v0 - 1) * 100, 1) if v0 else None
        noise = abs(change) < BODY_FAT_NOISE_POINTS if k == "fat_pct" else abs(pct or 0) < BODY_NOISE_PCT
        metrics[k] = {"n": len(vals), "first": v0, "last": v1, "first_date": d0, "last_date": d1, "span_days": span,
                      "change": change, "change_pct": pct, "within_noise": noise, "enough": len(vals) >= 2 and span >= BODY_MIN_SPAN_DAYS,
                      "series": [{"date": d, "value": v} for d, v in vals]}
        if metrics[k]["enough"]:
            relative[k] = {"change_pct": pct if k != "fat_pct" else None, "change_points": change if k == "fat_pct" else None,
                           "span_days": span, "within_noise": noise}
    w, waist = metrics.get("weight_kg"), metrics.get("waist_cm")
    strength = (progress or {}).get("overall", {}).get("change_pct")
    code, params = "body_pending", {"n": max(m["n"] for m in metrics.values())}
    if w and w["enough"]:
        params = {"weight_spct": w["change_pct"], "span_days": w["span_days"], "strength_spct": strength,
                  "waist_spct": waist["change_pct"] if (waist and waist["enough"]) else None}
        down, up = w["change_pct"] <= -BODY_NOISE_PCT, w["change_pct"] >= BODY_NOISE_PCT
        if down and strength is not None and strength <= -STRENGTH_NOISE_PCT:
            code = "body_losing_strength"
        elif down and strength is not None:
            code = "body_lighter_strength_holds"
        elif up and waist and waist["enough"] and waist["change_pct"] >= BODY_NOISE_PCT:
            code = "body_gain_with_waist"
        elif up and strength is not None and strength >= STRENGTH_NOISE_PCT:
            code = "body_gain_with_strength"
        else:
            code = "body_stable" if not (down or up) else "body_trend_only"
    return {"entries": entries, "metrics": metrics, "relative_changes": relative, "interp": item(code, params, cfg)}


def goal_progress(target: dict | None, exercises: list[dict], progress: dict, body: dict | None, today: date, cfg: dict) -> dict | None:
    """Where the athlete stands against the measurable target from the goal interview:
    {kind: force | body_weight | waist, exercise?, value, date?}. Force targets are judged on
    comparable days only; the pace needed is set against the athlete's own measured rate."""
    if not target or target.get("value") in (None, "") or target.get("kind") not in ("force", "body_weight", "waist"):
        return None
    goal = float(target["value"])
    out = {"kind": target["kind"], "exercise": target.get("exercise"), "target": goal, "date": target.get("date"),
           "current": None, "gap_pct": None, "days_left": None, "needed_pct_per_week": None, "own_pct_per_week": None}
    if target.get("date"):
        try:
            out["days_left"] = (date.fromisoformat(target["date"]) - today).days
        except ValueError:
            out["date"] = None
    lower_is_better = False
    if target["kind"] == "force":
        e = next((x for x in exercises if x["name"] == target.get("exercise")), None)
        row = next((p for p in (progress or {}).get("exercises", []) if p["name"] == target.get("exercise")), None)
        comparable = [o for o in (e or {}).get("occ", []) if o.get("comparable")]
        out["current"] = comparable[-1]["kg"] if comparable else None
        out["own_pct_per_week"] = (row or {}).get("rate_pct_per_week")
        unit_key = "kg"
    else:
        m = ((body or {}).get("metrics") or {}).get("weight_kg" if target["kind"] == "body_weight" else "waist_cm")
        out["current"] = m["last"] if m else None
        lower_is_better = bool(m) and goal < m["first"]
        if m and m["enough"] and m["span_days"]:
            out["own_pct_per_week"] = round(m["change_pct"] / (m["span_days"] / 7.0), 2)
        unit_key = "kg" if target["kind"] == "body_weight" else "cm"
    params = {"exercise": out["exercise"], f"target_{unit_key}": goal, f"current_{unit_key}": out["current"], "until_date": out["date"]}
    if out["current"] is None:
        code = "target_no_data"
    else:
        gap = (goal / out["current"] - 1) * 100
        out["gap_pct"] = round(gap, 1)
        reached = gap >= 0 if lower_is_better else gap <= 0
        params["gap_pct"] = abs(out["gap_pct"])
        if reached:
            code = "target_reached"
        elif out["days_left"] is None or out["days_left"] <= 0:
            code = "target_open" if out["days_left"] is None else "target_date_passed"
        else:
            need = gap / (out["days_left"] / 7.0)
            out["needed_pct_per_week"] = round(need, 2)
            own = out["own_pct_per_week"]
            params.update({"need_spct": out["needed_pct_per_week"], "own_spct": own, "weeks": round(out["days_left"] / 7.0, 1)})
            if own is None:
                code = "target_no_rate"
            else:
                code = "target_on_track" if (own <= need if lower_is_better else own >= need) else "target_behind"
    out["status"] = code.replace("target_", "")
    out["interp"] = item(code, params, cfg)
    return out
