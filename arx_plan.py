#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 joergsflow - ARX Insight. Free software under the GNU GPL v3 or later; see LICENSE. No warranty.
# -*- coding: utf-8 -*-
"""
ARX Insight - the planner: WHEN to train next, WHAT, in which ORDER, with which targets (v0.4.0).

ONE plan, derived from the athlete's own data instead of rules of thumb:

  when      The muscle-level recovery model is rolled forward day by day. The next date is where a
            worthwhile session is possible AND the weekly cadence (7 / sessions per week) is met
            best; trained today -> tomorrow at the earliest; a poor check-in -> not today. Every
            reason is given with its numbers (why_this_date).
  what      Exercises score by how overdue their muscles are, the athlete's focus per body region,
            coverage gaps (no direct work in 4 weeks), momentum - and by the FRESH BENCHMARK
            rotation: progress can only be judged on fresh sets, so every session keeps one
            exercise free of anything that loads its muscles before it (never measured fresh comes
            first, then the oldest fresh measurement). Twins (same target muscles) are not doubled
            in a full-body session. With 3+ sessions a week a session covers only the most due
            movement groups (Push / Pull / Drive), so the others are ready the next day - a
            rotating split that keeps every helper muscle with the exercises it helps in.
  order     All permutations (<= 720) are scored by what each exercise is expected to lose to the
            ones before it: the athlete's MEASURED order effects where they exist, catalog priors
            otherwise (arx_evidence). Rest between two exercises changes that loss only when the
            athlete's own data shows such a relation - no assumed decay. "A means before the
            target" (Row before Biceps Curl) stays a hard rule. Deterministic.
  how       Targets come from the last comparable value in the reference context, reduced by the
            expected loss when the exercise is planned after something that loads its muscles. A
            step only when the exercise is progressing and the last set left room; a plateau gets
            a second set (volume is the lever that still works), not a bigger number. Today's
            check-in band, the commitment profile (time vs effort), restrictions and the age band
            cap effort and steps. Settings stay what the athlete uses (consistency first).
  limiters  For every limiter (the grip above all) the planned load is compared with what the
            athlete usually puts on it in a session, next to the loss measured so far; an aid
            (hooks / straps) is suggested where it frees the most.
  week      The same logic rolled forward HORIZON_DAYS, with the benchmark rotating on.
  breaks    (v0.5.1) A gap of more than two weeks is named as what it is. Short break: continue, hold
            the numbers. Four weeks and more without direct work for a muscle: the old reference is
            an orientation, the value reached is the new starting point - never "catching up" with
            extra sets or sessions (science.json: training_breaks). A split the athlete's REAL
            attendance cannot carry (a muscle would wait > MAX_MUSCLE_GAP_DAYS) becomes fuller
            sessions until the rhythm is back.
  stimulus  (v0.7.0) The big slot of a movement group goes to the exercise that reaches the most
            URGENT muscles - trained muscles that would wait longer than MAX_MUSCLE_GAP_DAYS for
            their next stimulus if they were skipped today. With Overhead Press or High Pull in the
            repertoire "a compound of the group" no longer means "the group's main muscles": once a
            week chest and lats would otherwise alternate with them and wait two weeks. Nothing
            urgent -> the choice is what it always was.
  size      (v0.8.0) How many exercises a session holds follows from the athlete's time budget at his
            own pace (set + change-over) - up to SESSION_MAX_EX; the profile shows what every budget
            buys (size_by_minutes) and the coach gets the price of one more exercise in minutes. An
            athlete who trains in turns with a partner says so (profile: partner): the long
            change-over is then the partner's set, not something to be shortened.
  window    (v0.8.1) The check-in may carry "minutes I have today" - an UPPER LIMIT for a session planned
            for today, never a reason to do more. A plan that does not fit is cut in the order a
            trainer would cut it: extra sets first (volume goes, the effort of the one hard set
            stays), then the session gets smaller - and what stays is what the selection puts first
            anyway: the big exercise of each movement group, urgent muscles, then the score. What
            was left out is named and comes back by itself (its muscles are the most due next time).
  excluded  (v0.7.0) Exercises the athlete does not do on the ARX (profile: excluded_exercises =
            {code: elsewhere | unwanted}) do not exist for the planner - never planned, never
            suggested, not in the coach's decision space. "elsewhere" = trained outside the ARX:
            the muscles it is FOR count as covered there (no gap suggestion, no frequency warning,
            not "neglected") and the plan says that it cannot see that load. "unwanted" = the
            other exercises cover its muscles where they can. Said openly in plan["excluded"].

Every decision carries a meaning / action item (meanings.json). check_plan() is the rule set the
AI's plan will have to pass as well (v0.5.0). The plan ledger remembers what was recommended, so
the next report can say what was done with it (plan_vs_actual).

Pure functions on the report's data; imports arx_base, arx_detail (constants), arx_evidence and
arx_history only. GPL-3.0-or-later (see LICENSE). No warranty. Not medical advice.
"""

from __future__ import annotations
import itertools, math, statistics as st
from datetime import date, timedelta

from arx_base import EFFORT_RANK, REQUIRED_REST, _ts
from arx_detail import INROAD_DEEP, INROAD_MODERATE, BORDERLINE
import arx_evidence as evidence
from arx_history import REGIONS, REGION_OF, item

PLAN_ALGO_VERSION = 2          # 2 = v0.7.0: urgent muscles decide the big slot, excluded exercises

HORIZON_DAYS = 10              # how far the week plan looks
DATE_SEARCH_DAYS = 10          # the next session is searched within this many days
MIN_READY_EXERCISES = 2        # fewer ready exercises are not worth a session
CADENCE_W = 0.35               # date score: deviation from the ideal gap (in ideal gaps) ...
CADENCE_FREE_DAYS = 0.5        # ... beyond this many days
SPLIT_EVENNESS_W = 0.01        # a split session SOONER than the rhythm while the week still needs sessions: only a
                               # tie-breaker towards even spacing - consecutive days are what a split is for
WEEK_W = 0.15                  # date score: the week's session target is still open ...
WEEK_LAST_W = 0.15             # ... and this is one of the last days on which it can still be met
HABIT_W = 0.10                 # date score: the athlete's usual weekday (only with a real pattern)
HABIT_MIN_DAYS, HABIT_SHARE = 8, 0.75
BAND_FILL = {"moderate": 0.85, "light_or_rest": 0.6}      # a session on a poor day is worth less
REST_SCORE_BELOW = 35          # check-in score below this (or an elevated resting HR) = rest today
DUE_INTERVAL_RANGE = (3.0, 7.0)  # days after which a muscle is "due" (7 x split / sessions per week)
COVER_DAYS = 28                # no direct work for this long = a coverage gap
REDUNDANCY = 0.7               # score multiplier (1 - REDUNDANCY x overlap) after a similar pick
TWIN_OVERLAP = 0.99            # same target muscles = twins: never doubled in a full-body session
MAX_PER_REGION = 2             # exercises per body region in one session (3 on a split day or a "more" region)
MAX_MUSCLE_GAP_DAYS = 8        # a trained muscle without any stimulus for longer than about a week cannot be expected to grow:
                               # once a week is the lowest frequency with evidence for gains (science.json: frequency, minimum_dose)
SPLIT_MIN_SESSIONS = 2         # below this a split would stretch every muscle's gap to 2-3 weeks -> always full body
COVER_FIRST = (10.0, 5.0)      # full body: the best big exercise of each movement group is picked first (ready / limited)
NEW_WEIGHT = 0.5               # a known exercise wins a tie against a never-performed one
URGENT_WEIGHT = {"target": 1.0, "limiter": 0.5}   # how much an exercise does for an urgent muscle (as in arx_evidence.ROLE_WEIGHT)
# Exercises the athlete does not do on the ARX (profile): "elsewhere" = trained outside the machine (its
# target muscles count as covered there), "unwanted" = not wanted at all (the plan covers the muscles otherwise),
# "injury" (v0.8.7) = not possible for health reasons right now - planned like "unwanted", but the report mirrors
# it when the exercise is done anyway, and the coach knows why. This is how a shoulder problem rules out exactly the
# overhead pressing and nothing else, while the body part itself can go back to "ok".
EXCLUDE_REASONS = ("elsewhere", "unwanted", "injury")
# ... and a fourth choice on the same tiles that does NOT exclude: "careful" (v0.8.8) = possible for health reasons
# only with care - the exercise stays in the plan sub-maximal, without a target number (movement is good for a
# recovering joint, the all-out set is not), exactly like a body part set to "careful", but for this exercise alone.
EXERCISE_CHOICES = EXCLUDE_REASONS + ("careful",)
MINUTES_PER_EXERCISE = 6.0     # set + change-over when the athlete's own pace is not known yet
TRANSITION_NOTE_MIN = 5.0      # a longer change-over between exercises is worth a word (time is the goal)
TRANSITION_TARGET_MIN = 4.0    # what is enough between two DIFFERENT exercises
# How many exercises a session holds is the athlete's TIME decision, not a training rule: alternating unrelated
# muscle groups costs no result and more weekly sets per muscle is the best-supported lever for size (science.json:
# paired_sets, weekly_volume). The cap only keeps a session plannable (order search, one page) - v0.8.0: 6 -> 8.
SESSION_MIN_EX, SESSION_MAX_EX = 3, 8
BIG_SESSION_EX = 7             # from here a full-body session may go one deeper per body region, like a split day
MINUTES_OPTIONS = (15, 20, 30, 45, 60, 75, 90)      # the time budgets offered in the profile (size_by_minutes) and the check-in
WINDOW_MIN_EX = 2              # a session under a time window (check-in: "minutes I have today") keeps at least this many
WINDOW_REST_MARGIN_MIN = 0.5   # exercises that share muscles rest a little longer than the bare pace: keeps the first
                               # estimate (shown in the check-in) in line with the finished session
SESSION_MINUTES_RANGE = (15, 45)
DEFAULT_SESSION_MINUTES = 25
DEFAULT_SET_SECONDS = 105.0
POS_LOSS_RANGE = (0.0, 3.0)    # expected loss per position in the session (own value, shrunk, clamped)
REST_GAP_SWING = 0.5           # a measured rest effect may move a pair loss by at most +-50 %
REST_MIN = {"compound": 3.0, "isolation": 2.0}             # minutes after an exercise of that kind
REST_EXTRA_MAX = 3.0           # extra minutes when the next exercise shares muscles with it
REST_EXTRA_PER_PCT = 5.0       # one extra minute per this much expected loss
TRANSITION_MIN = 2.0           # set-up between two exercises when the athlete's own data is thin
STEP_RANGE_PCT = (1.0, 3.0)    # progression step per session when it is earned
CONTEXT_LOSS_MIN_PCT = 3.0     # smaller expected losses do not change a target
LIGHT_DAY_SHARE = 0.70         # force on a light day
RETURN_DAYS = 180              # an exercise not done for this long restarts gently: the protection against
                               # eccentric muscle damage (repeated-bout effect) fades within 6-12 months
# Training breaks (science.json: training_breaks). Up to about three weeks off cost no long-term
# progress; after longer breaks strength and size are measurably down and come back much faster
# than they were built. So a break never calls for "catching up" (extra sets, sessions or harder
# sessions): the first session back holds the numbers (short) or sets a new starting point (long),
# then the normal progression continues. Judged per exercise on its MUSCLES (days without direct
# work), so a muscle that was simply neglected for a month is treated the same way.
BREAK_SHORT_DAYS = 14          # longer without training = a break: no progression step in the first session back
BREAK_LONG_DAYS = 28           # from here a loss is to be expected: the old reference is only an orientation
BREAK_TARGET_FLOOR_PCT = 15.0  # ... and the coach may set such a target this far BELOW the engine's
REAL_FREQ_DAYS = 28            # the rhythm really lived: training days in the four weeks up to the last session
BUDGET_TOLERANCE = 1.15        # planned limiter load above this share of the usual one = high
BUDGET_MIN_SESSIONS = 3
AID_MIN_EXERCISES = 3          # this many exercises on one limiter also trigger an aid hint
HIT_RATE_OK = 0.5              # below: the effort is the problem, not the volume
DECISION_DATES = 5             # feasible dates offered to the coach
TARGET_LEEWAY_PCT = 5.0        # the coach may move a force target this far from the engine's
REST_MAX_MIN = 10.0
LEDGER_MAX = 40                # plans kept per athlete
TARGET_HIT_SHARE = 0.98        # actual >= this share of the target = target met
FOCUS_WEIGHT = {"more": 1.5, "normal": 1.0, "less": 0.6, "off": 0.0}
LEGACY_FOCUS = {"Drive": ["legs"], "Pull": ["back", "arms"], "Push": ["chest", "shoulders", "arms"]}
MINOR_BANDS, OLDER_BANDS = ("13-15", "16-17"), ("60-69", "70+")
MINOR_MAX_EXERCISES = 4

# Commitment profiles: the honest trade-off between time and effort (chosen in the goal interview;
# the engine pre-selects one). "maintain" cuts the volume but keeps the effort - intensity is what
# preserves an adaptation (science.json: maintenance). effort: "goal" = what the goal asks for, "moderate" = capped below
# failure; extra_sets: second set on the first two exercises; step: progression steps allowed;
# size: exercises more / fewer than the time budget gives ("cover" = one per trained body region
# at most - the minimum that still reaches everything); plateau_set: a plateau may get a 2nd set.
COMMITMENT = {
    "min_time_max_effort":   {"effort": "goal",     "extra_sets": 0, "step": True,  "size": "cover", "plateau_set": False},
    "balanced":              {"effort": "goal",     "extra_sets": 0, "step": True,  "size": 0,  "plateau_set": True},
    "more_time_less_brutal": {"effort": "moderate", "extra_sets": 1, "step": True,  "size": 1,  "plateau_set": True},
    "maintain":              {"effort": "goal",     "extra_sets": 0, "step": False, "size": -1, "plateau_set": False},
}
EFFORT_TARGETS = {   # label -> what the set should reach (effort v3, see arx_detail)
    "deep":     {"label": "deep", "inroad_min": INROAD_DEEP},
    "moderate": {"label": "moderate", "inroad_min": INROAD_MODERATE},
    "submax":   {"label": "submax", "inroad_min": 0},
}
# A missed effort target is acted on (v0.10.0, owner): once -> the number holds and the cue is intent; twice in a
# row -> the set-up changes (slower per direction, no pauses at the turnarounds - or two reps more), never a third
# "hold" without a change. The tempo step stays inside the band where results are equal (science.json: tempo).
EFFORT_RESET_AFTER = 2         # misses in a row that trigger a parameter change
EFFORT_TEMPO_STEP_S = 1.0      # seconds per direction added per change
EFFORT_TEMPO_MAX_S = 5.0       # ... never beyond this (10 s per repetition)
EFFORT_REPS_STEP = 2           # the alternative lever once tempo and pauses are exhausted
EFFORT_STREAK_DAYS = 42        # lookback for the streak, from the exercise's last training day
REPEAT_SOON_DAYS = 2           # a fruitless session's exercises back in the plan within this many days -> "again, properly" (v0.11.0)
BEGINNER_SESSIONS = 2          # a new athlete's first sessions: effort moderate, the athlete sets the level, nothing is a miss (v0.12.0)
# Every default above that rests on sport science names its entry in science.json (a test checks
# that the entry exists, is referenced and was reviewed). The athlete's own data outranks all of them.
SCIENCE = {
    "REQUIRED_REST": "recovery_between_sessions", "REST_MIN": "rest_intervals", "REST_EXTRA_MAX": "rest_intervals",
    "EFFORT_TARGETS": "proximity_to_failure", "COMMITMENT": "minimum_dose", "COMMITMENT.maintain": "maintenance",
    "EFFORT_RESET_AFTER": "proximity_to_failure", "EFFORT_REPS_STEP": "proximity_to_failure",
    "EFFORT_TEMPO_STEP_S": "tempo", "EFFORT_TEMPO_MAX_S": "tempo", "REPEAT_SOON_DAYS": "recovery_between_sessions",
    "BEGINNER_SESSIONS": "eccentric",
    "STEP_RANGE_PCT": "progression", "COVER_DAYS": "weekly_volume", "COMMITMENT.plateau_set": "weekly_volume",
    "DUE_INTERVAL_RANGE": "frequency", "SPLIT_EVENNESS_W": "split_vs_full_body", "MINOR_BANDS": "youth", "OLDER_BANDS": "older_adults",
    "best_order": "exercise_order", "aid_hints": "grip_and_straps", "BAND_FILL": "autoregulation",
    "LIGHT_DAY_SHARE": "autoregulation", "REST_SCORE_BELOW": "autoregulation", "TRANSITION_TARGET_MIN": "paired_sets",
    "STRUCTURES": "split_vs_full_body", "split_factor": "split_vs_full_body", "theme_groups": "split_vs_full_body",
    "MAX_MUSCLE_GAP_DAYS": "frequency", "SPLIT_MIN_SESSIONS": "frequency", "COVER_FIRST": "minimum_dose",
    "RETURN_DAYS": "eccentric", "NEW_WEIGHT": "eccentric",
    "BREAK_SHORT_DAYS": "training_breaks", "BREAK_LONG_DAYS": "training_breaks", "BREAK_TARGET_FLOOR_PCT": "training_breaks",
    "training_break": "training_breaks", "REAL_FREQ_DAYS": "frequency", "real_frequency": "frequency",
    "URGENT_WEIGHT": "frequency",
}


# =============================================================================
# Inputs
# =============================================================================
def focus_regions(cfg: dict) -> dict:
    """{region: more|normal|less|off}. The Push/Pull/Drive focus of older profiles is mapped."""
    fr = dict(cfg.get("focus_regions") or {})
    if not fr:
        for group, level in (cfg.get("focus") or {}).items():
            if level in FOCUS_WEIGHT and level != "normal":
                for r in LEGACY_FOCUS.get(group, []):
                    fr.setdefault(r, level)
    return {r: (fr.get(r) if fr.get(r) in FOCUS_WEIGHT else "normal") for r in REGIONS if r != "grip"}


def excluded_of(cfg: dict, catalog: dict) -> dict:
    """{exercise code: reason} - the exercises the athlete does not do on the ARX (profile), reduced to
    catalog codes and known reasons. The app validates the profile on the way in; this is the
    engine's own guard (CLI, tests, a hand-edited goals.json)."""
    return {str(c): r for c, r in ((cfg or {}).get("excluded_exercises") or {}).items()
            if str(c) in (catalog or {}) and r in EXCLUDE_REASONS}


TODAY_CHOICES = ("careful", "injury")      # what the check-in may say about one exercise for TODAY (v0.8.9)


def today_exercise_levels(cfg: dict, catalog: dict) -> dict:
    """{exercise code: careful | injury} from TODAY's check-in - a limit that came up spontaneously. It touches only
    a session planned for today (the profile tiles hold what lasts) and never the mirror of past sets."""
    ck = (cfg or {}).get("checkin") or {}
    raw = ck.get("exercises") if isinstance(ck.get("exercises"), dict) else {}
    return {str(c): lvl for c, lvl in raw.items() if str(c) in (catalog or {}) and lvl in TODAY_CHOICES}


# the check-in's body vocabulary (v0.9.0: body parts are SHORTCUTS to exercises - the page flags what a tapped region /
# joint touches, the athlete may change single exercises, and the plan obeys the exercise flags only)
CHECKIN_REGIONS = {
    "legs": ["quads", "glutes", "hamstrings", "calves"],
    "back": ["lats", "upper_back", "lower_back"],
    "chest": ["chest"],
    "arms": ["elbow_flexors", "triceps", "grip"],
    "shoulders": ["shoulders"],
}


def body_map(catalog: dict) -> dict:
    """What a body part touches: {"regions": {region: {code: target | helper}}, "joints": {joint: [codes]}} -
    THE mapping behind the check-in's shortcuts (the page renders it, a test pins it). A region reaches an
    exercise through a target muscle or, one step weaker, through a limiter; a joint through the catalog's joints."""
    region_of = {m: r for r, ms in CHECKIN_REGIONS.items() for m in ms}
    regions: dict = {r: {} for r in CHECKIN_REGIONS}
    joints: dict = {}
    for code, meta in (catalog or {}).items():
        for m in meta.get("targets") or []:
            if m in region_of:
                regions[region_of[m]][str(code)] = "target"
        for m in meta.get("limiters") or []:
            if m in region_of:
                regions[region_of[m]].setdefault(str(code), "helper")
        for j in meta.get("joints") or []:
            joints.setdefault(j, []).append(str(code))
    return {"regions": regions, "joints": {j: sorted(c) for j, c in joints.items()}}


def today_exercise_why(cfg: dict, catalog: dict) -> dict:
    """{code: soreness | pain | checkin} - the reason behind a flag of today's check-in, read off the body parts
    the athlete tapped (a sore region the exercise works, a painful joint it loads); else plain "checkin"."""
    ck = (cfg or {}).get("checkin") or {}
    sore = {r for r, l in (ck.get("soreness") or {}).items() if l in ("mild", "strong")}
    pain = set(ck.get("pain") or [])
    bm = body_map(catalog)
    out = {}
    for code in today_exercise_levels(cfg, catalog):
        if any(code in bm["regions"].get(r, {}) for r in sore):
            out[code] = "soreness"
        elif any(code in bm["joints"].get(j, []) for j in pain):
            out[code] = "pain"
        else:
            out[code] = "checkin"
    return out


def careful_of(cfg: dict, catalog: dict) -> list[str]:
    """Codes of the exercises the athlete set to "careful" for health reasons (same profile field as the
    exclusions, but these stay in the plan - sub-maximal, no target number)."""
    return sorted(str(c) for c, r in ((cfg or {}).get("excluded_exercises") or {}).items()
                  if str(c) in (catalog or {}) and r == "careful")


def external_muscles(cfg: dict, catalog: dict) -> list[str]:
    """Muscles the athlete trains OUTSIDE the ARX: what the exercises switched off with the reason
    "elsewhere" are FOR. They count as covered there - nothing new is suggested for them, their
    frequency is not the plan's business and they are never called neglected."""
    return sorted({m for c, r in excluded_of(cfg, catalog).items() if r == "elsewhere"
                   for m in (catalog[c].get("targets") or [])})


def muscles_out_of_plan(cfg: dict, catalog: dict) -> list[str]:
    """Muscles the plan has nothing to say about any more: trained outside the ARX (external_muscles)
    or left without ANY exercise - every catalog exercise that is for them was switched off, whatever
    the reason. A finding must not call them neglected or promise them a slot."""
    excluded = excluded_of(cfg, catalog)
    still = {m for c, meta in (catalog or {}).items() if str(c) not in excluded for m in (meta.get("targets") or [])}
    gone = {m for c in excluded for m in (catalog[c].get("targets") or []) if m not in still}
    return sorted(gone | set(external_muscles(cfg, catalog)))


def excluded_block(cfg: dict, catalog: dict) -> dict | None:
    """What was left out on the athlete's own wish, said openly (a plan never omits silently):
    {exercises: [{code, name, reason}], external_muscles, notes: [items]}. None = nothing excluded."""
    excluded = excluded_of(cfg, catalog)
    if not excluded:
        return None
    rows = sorted(({"code": c, "name": catalog[c].get("name") or f"Exercise {c}", "reason": r} for c, r in excluded.items()),
                  key=lambda x: x["name"])
    away = [x["name"] for x in rows if x["reason"] == "elsewhere"]
    off = [x["name"] for x in rows if x["reason"] == "unwanted"]
    hurt = [x["name"] for x in rows if x["reason"] == "injury"]
    external = external_muscles(cfg, catalog)
    notes = []
    if away:
        notes.append(item("excluded_elsewhere", {"exercises": away, "muscles": external, "k": len(away)}, cfg))
    if off:
        notes.append(item("excluded_unwanted", {"exercises": off, "k": len(off)}, cfg))
    if hurt:
        notes.append(item("excluded_injury", {"exercises": hurt, "k": len(hurt)}, cfg))
    return {"exercises": rows, "external_muscles": external, "notes": notes}


def goal_effort(goal: dict) -> str:
    """The effort the training goal asks for: muscle -> deep; strength / conditioning -> moderate
    (full force on every rep, stop before form breaks)."""
    g = goal or {}
    if (g.get("conditioning") or 0) >= 0.3 or (g.get("strength") or 0) > (g.get("muscle") or 0):
        return "moderate"
    return "deep"


STRUCTURES = ("auto", "full_body", "split")    # how sessions are built (profile); science.json: split_vs_full_body


def split_factor(spw: int, structure: str = "auto", real_spw: float | None = None) -> int:
    """1 = full body; 2 / 3 = a session covers a half / a third of the movement groups.
    auto: full body up to 2 sessions a week, a split from 3, thirds from 5. At equal weekly volume
    split and full body give the same results (science.json) - so the athlete may choose: a split
    trains different muscles on consecutive days (more, shorter sessions, more sets per muscle per
    session), full body reaches everything with fewer sessions.
    real_spw (see real_frequency): in auto mode the rhythm the athlete really lives decides, not the
    wish - with a k-way split a muscle waits 7 * k / real days, and more than MAX_MUSCLE_GAP_DAYS
    means no weekly stimulus, so the sessions get fuller until the rhythm is back."""
    if structure == "full_body" or spw < SPLIT_MIN_SESSIONS:
        return 1                                   # one session a week is ALWAYS full body (see MAX_MUSCLE_GAP_DAYS)
    auto = 1 if spw <= 2 else (2 if spw <= 4 else 3)
    if structure == "split":
        return max(2, auto)                        # an explicit wish is kept (and commented, see structure_note)
    while real_spw is not None and auto > 1 and 7.0 * auto / max(real_spw, 0.1) > MAX_MUSCLE_GAP_DAYS:
        auto -= 1
    return auto


def real_frequency(days: list[str]) -> float | None:
    """Sessions per week the athlete really trained in the REAL_FREQ_DAYS up to the LAST session - a
    break after it is a break, not a rhythm. None while the history is shorter than that window."""
    if not days:
        return None
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    if (last - first).days < REAL_FREQ_DAYS - 1:
        return None
    start = last - timedelta(days=REAL_FREQ_DAYS - 1)
    return round(sum(1 for d in days if date.fromisoformat(d) >= start) * 7.0 / REAL_FREQ_DAYS, 2)


def training_break(last_day: date | None, day: date) -> dict | None:
    """The gap between the last session and the planned one as a break tier: short (nothing lost -
    continue, hold the numbers), long (expect less, new starting point), very_long (restart gently,
    see RETURN_DAYS). None = no break worth a word."""
    if last_day is None or (day - last_day).days <= BREAK_SHORT_DAYS:
        return None
    n = (day - last_day).days
    tier = "short" if n < BREAK_LONG_DAYS else ("long" if n <= RETURN_DAYS else "very_long")
    return {"days": n, "weeks": round(n / 7), "tier": tier, "last_date": last_day.isoformat()}


def recommend_commitment(cfg: dict, minutes: float, spw: int) -> str:
    """What fits goal + time budget + experience when the athlete has not chosen."""
    if cfg.get("outcome") == "maintain":
        return "maintain"
    if cfg.get("experience") == "new":
        return "balanced"                          # learn the machine before going all out
    return "min_time_max_effort" if minutes * spw <= 60 else "balanced"


def _muscles(meta: dict, code, day: str, aids: dict) -> dict:
    targets = list(meta.get("targets") or []) or [meta.get("group") or "?"]
    out = {m: "target" for m in targets}
    for m in evidence.effective_limiters(meta, code, day, aids):
        out.setdefault(m, "limiter")
    return out


def _targets(c: dict) -> set:
    return {m for m, role in c["muscles"].items() if role == "target"}


def _typical(ss: list[dict], key: str, default: float) -> float:
    vals = [s[key] for s in ss[-3:] if s.get(key)]
    return st.median(vals) if vals else default


def transition_minutes(work: list[dict]) -> tuple[float, bool]:
    """(minutes, measured): from the end of one exercise to the start of the next one within a
    visit (rest + set-up) - the athlete's own median when there are enough of them."""
    gaps = []
    by_visit: dict = {}
    for s in work:
        by_visit.setdefault((s["date"][:10], s.get("visit", 1)), []).append(s)
    for ss in by_visit.values():
        ss.sort(key=lambda x: x["date"])
        for a, b in zip(ss, ss[1:]):
            ta, tb = _ts(a["date"]), _ts(b["date"])
            if ta is not None and tb is not None and a["exercise"] != b["exercise"]:
                g = (tb - (ta + a["seconds"])) / 60.0
                if 0 < g <= 15:
                    gaps.append(g)
    return (round(st.median(gaps), 1), True) if len(gaps) >= 4 else (REST_MIN["compound"] + TRANSITION_MIN, False)


# =============================================================================
# State of the muscles, rolled forward
# =============================================================================
def rest_days(rank: int, age: str | None = None) -> int:
    """Days of rest after a load. The same at every age: whether older athletes recover more slowly
    is not established (science.json: older_adults) - check-in, soreness and the athlete's own
    recovery response set the spacing, not the birth date."""
    return REQUIRED_REST[rank]


def muscle_state(load: dict, work: list[dict], catalog: dict, aids: dict, age: str | None = None) -> dict:
    """{muscle: {ready_on, last_target, last_stim, sore}} from today's recovery model + the date of
    the last direct (target) work of each muscle. last_stim = the last training stimulus in ANY role:
    it starts as last_target (the basis frequency_notes uses for the history) and apply_session()
    moves it on for every muscle of a planned session, target or helper."""
    out = {}
    for m, v in ((load.get("recovery") or {}).get("muscles") or {}).items():
        out[m] = {"ready_on": v["ready_on"], "last_target": None}
    for s in work:
        for m, role in _muscles(catalog.get(str(s["exercise"]), {}), s["exercise"], s["date"], aids).items():
            if role == "target":
                cell = out.setdefault(m, {"ready_on": None, "last_target": None})
                cell["last_target"] = max(cell["last_target"] or "", s["date"][:10])
    for cell in out.values():
        cell["last_stim"] = cell["last_target"]
    return out


def apply_session(state: dict, day: date, items: list[dict], age: str | None) -> dict:
    """The muscle state after a planned session: targets at the planned effort rank, limiters one
    rank lower (the same rule as the recovery model)."""
    new = {m: dict(v) for m, v in state.items()}
    for it in items:
        rank = EFFORT_RANK.get(it["effort_target"]["label"], 2)
        for m, role in it["muscles"].items():
            r = rank if role == "target" else max(rank - 1, 1)
            ready = (day + timedelta(days=rest_days(r, age))).isoformat()
            cell = new.setdefault(m, {"ready_on": None, "last_target": None, "last_stim": None})
            cell["ready_on"] = max(cell["ready_on"] or "", ready)
            cell["last_stim"] = day.isoformat()    # a helper gets a training stimulus as well (see frequency_notes)
            if role == "target":
                cell["last_target"] = day.isoformat()
    return new


def exercise_status(c: dict, state: dict, day: date, today: date | None = None) -> tuple[str, list[str]]:
    """ready | limited | not_ready on a given day (+ the muscles behind it). What today's check-in says
    about single exercises is applied by score_candidates (v0.9.0: soreness is a shortcut to those flags)."""
    iso = day.isoformat()
    late = lambda m: (state.get(m, {}).get("ready_on") or "") > iso
    blocked = sorted(m for m, role in c["muscles"].items() if role == "target" and late(m))
    if blocked:
        return "not_ready", blocked
    limited = sorted(m for m, role in c["muscles"].items() if role == "limiter" and late(m))
    return ("limited", limited) if limited else ("ready", [])


# =============================================================================
# Candidates
# =============================================================================
def build_pool(exercises: list[dict], work: list[dict], catalog: dict, cfg: dict, today: date, progress: dict,
               restriction_of=None) -> list[dict]:
    """Everything that could be planned: the exercises the athlete does + catalog / library
    exercises never performed (flagged new - only used to close a coverage gap). Exercises the
    athlete switched off in the profile (excluded_of) are left out - performed or not."""
    aids = cfg.get("aids") or {}
    excluded = excluded_of(cfg, catalog)
    restriction_of = restriction_of or (lambda name, joints: "ok")
    by_ex: dict = {}
    for s in work:
        by_ex.setdefault(str(s["exercise"]), []).append(s)
    status = {p["name"]: p for p in (progress or {}).get("exercises", [])}
    pool, seen = [], set()

    def entry(code, meta, e):
        name = (e or {}).get("name") or meta.get("name") or f"Exercise {code}"
        ss = by_ex.get(str(code), [])
        muscles = _muscles(meta, code, today.isoformat(), aids)
        fresh_days = [o["date"] for o in (e or {}).get("occ", []) if o.get("context") == "fresh" and o.get("comparable")]
        configured = (aids.get(str(code)) or {}).get("aids") or []
        return {"code": str(code), "name": name, "group": meta.get("group", "?"), "kind": meta.get("kind") or "compound",
                "muscles": muscles, "regions": sorted({REGION_OF[m] for m in muscles if muscles[m] == "target" and m in REGION_OF}),
                "new": e is None, "library": bool(meta.get("library")), "series": e, "progress": status.get(name),
                "restriction": (e or {}).get("restriction") or restriction_of(name, meta.get("joints")),
                "aids_possible": list(meta.get("aids") or []), "aid": configured[0] if configured else None,
                "set_seconds": _typical(ss, "seconds", DEFAULT_SET_SECONDS), "impulse": _typical(ss, "impulse_kg_s", 0.0),
                "last_fresh": max(fresh_days) if fresh_days else None, "n_days": len((e or {}).get("occ", []))}

    for e in exercises:                            # what the athlete does (also codes the catalog does not know)
        if str(e["ex"]) in excluded:
            continue
        pool.append(entry(e["ex"], catalog.get(str(e["ex"]), {}), e))
        seen.add(pool[-1]["name"])
    for code, meta in catalog.items():             # never performed: catalog + library entries
        if meta.get("name") and meta["name"] not in seen and str(code) not in excluded:
            pool.append(entry(code, meta, None))
            seen.add(meta["name"])
    return pool


def _trained_muscles(pool: list[dict], focus: dict, external=()) -> set:
    """The muscles the weekly-stimulus rule is about: what the athlete trains on the ARX as a TARGET
    (known exercises, not avoided), in regions that are not switched to less / off - minus what is
    trained outside the ARX (see external_muscles)."""
    return {m for c in pool if not c["new"] and c["restriction"] != "avoid" for m in _targets(c)
            if REGION_OF.get(m) and focus.get(REGION_OF[m], "normal") not in ("off", "less") and m not in external}


def due_interval(spw: int, structure: str = "auto", real_spw: float | None = None) -> float:
    lo, hi = DUE_INTERVAL_RANGE
    return min(hi, max(lo, 7.0 * split_factor(spw, structure, real_spw) / max(1, spw)))


def score_candidates(pool: list[dict], state: dict, day: date, focus: dict, spw: int, cfg: dict,
                     today: date | None = None) -> list[dict]:
    """Score every exercise that may be trained on that day, with the reasons as items.
    Every candidate also says which URGENT muscles it reaches: trained muscles that would wait longer
    than MAX_MUSCLE_GAP_DAYS for their next stimulus if this session skipped them (days since the
    last stimulus + the days until their next chance). select() gives the big slot to that exercise."""
    interval = due_interval(spw, structure_of(cfg), cfg.get("_real_spw"))
    external = set(cfg.get("_external") or [])      # trained outside the ARX (exercises switched off as "elsewhere")
    next_gap = 7.0 * split_factor(spw, structure_of(cfg), cfg.get("_real_spw")) / max(1, spw)

    def waited(m):
        cell = state.get(m) or {}
        last = cell.get("last_stim") or cell.get("last_target")
        return (day - date.fromisoformat(last)).days if last else None
    urgent_now = {m for m in _trained_muscles(pool, focus, external)
                  if waited(m) is not None and waited(m) + next_gap > MAX_MUSCLE_GAP_DAYS}
    # today's check-in about single exercises (v0.8.9): "injury" = not today, "careful" = sub-maximal today
    today_levels = (cfg.get("_today_exercises") or {}) if (today is not None and day == today) else {}
    never_fresh = sorted((c for c in pool if not c["new"] and not c["last_fresh"]), key=lambda c: (-c["n_days"], c["name"]))
    measured = sorted((c for c in pool if not c["new"] and c["last_fresh"]), key=lambda c: (c["last_fresh"], c["name"]))
    bench_rank = {c["name"]: i for i, c in enumerate(never_fresh + measured)}
    out = []
    for c in pool:
        if c["restriction"] == "avoid" or today_levels.get(c["code"]) == "injury":
            continue
        if today_levels.get(c["code"]) == "careful" and c["restriction"] == "ok":
            c = dict(c, restriction="careful", careful_today=True)         # this day only - the pool itself stays as it is
        weight = max([FOCUS_WEIGHT[focus.get(r, "normal")] for r in c["regions"]] or [1.0])
        if weight == 0.0:
            continue
        status, behind = exercise_status(c, state, day, today)
        if status == "not_ready":
            continue
        targets = sorted(_targets(c))
        since = {m: ((day - date.fromisoformat(state[m]["last_target"])).days if (state.get(m) or {}).get("last_target") else None)
                 for m in targets}
        never = all(v is None for v in since.values())
        days_due = max((v for v in since.values() if v is not None), default=None)
        days_away = min((v for v in since.values() if v is not None), default=None)   # the most recently trained target
        worst = next((m for m in targets if since[m] == days_due), targets[0] if targets else None)
        if c["new"]:
            # a NEW exercise is only ever suggested for a gap in muscles the ARX is responsible for. Muscles the
            # athlete trains elsewhere never justify it - and they never make an exercise MORE eligible either:
            # the gap is judged on all targets as always, then it has to lie in a muscle that is not external.
            gap = [m for m in targets if (since[m] is None if never else (since[m] or 0) > COVER_DAYS) and m not in external]
            if not gap:
                continue
            worst = gap[0] if worst in external or worst not in gap else worst
        due = 2.0 if never else min((days_due or 0) / interval, 2.0)
        cover = 1.0 if (never or (days_due or 0) > COVER_DAYS) else 0.0
        if c["new"] and not cover:
            continue                               # a new exercise only to close a gap
        ps = (c["progress"] or {}).get("status")
        prog = {"progressing": 1.0, "stable": 0.5, "plateau": 0.5, "insufficient": 0.5}.get(ps, 0.0)
        can_bench = not c["new"] and status == "ready" and c["restriction"] == "ok"
        bench = (1.0 / (1 + bench_rank[c["name"]])) if (can_bench and c["name"] in bench_rank) else 0.0
        score = weight * (due + 0.5 * cover + 0.3 * prog + 0.5 * bench)
        score *= 0.7 if c["restriction"] == "careful" else 1.0
        score *= 0.6 if status == "limited" else 1.0
        if c["new"]:                               # among new ones: big movements and mapped exercises first
            wanted = any(focus.get(r) == "more" for r in c["regions"])      # the athlete asked for more of this region:
            score = score * (1.0 if wanted else NEW_WEIGHT) + (0.05 if c["kind"] == "compound" else 0.0) + (0.0 if c["library"] else 0.02)
        why = []
        if c["new"]:
            why.append(item("sel_new", {"muscle": worst}, cfg))
        elif cover:
            why.append(item("sel_coverage", {"muscle": worst, "days": days_due}, cfg))
        elif due >= 1.0:
            why.append(item("sel_overdue", {"muscle": worst, "days": days_due}, cfg))
        else:
            why.append(item("sel_ready_again", {"muscle": worst, "days": days_due}, cfg))
        if weight > 1.0:
            why.append(item("sel_focus", {"region": next(r for r in c["regions"] if focus.get(r) == "more")}, cfg))
        if ps == "progressing":
            why.append(item("sel_progress", {"change_spct": (c["progress"] or {}).get("change_pct")}, cfg))
        urgent = sorted(m for m in c["muscles"] if m in urgent_now)
        out.append(dict(c, score=round(score, 3), base_score=round(score, 3), status=status, limited_by=behind,
                        urgent=urgent, urgent_weight=sum(URGENT_WEIGHT[c["muscles"][m]] for m in urgent),
                        why_selected=why, days_since_target=days_due, days_away=days_away, due_muscle=worst,
                        bench_rank=bench_rank.get(c["name"]) if can_bench else None))
    out.sort(key=lambda c: (-c["score"], c["name"]))
    return out


def theme_groups(cands: list[dict], pool: list[dict], spw: int, structure: str = "auto",
                 real_spw: float | None = None) -> list[str] | None:
    """With 3+ sessions a week a session covers only the most due movement groups (Push / Pull /
    Drive of the catalog), so the others are ready the day after. Groups, not body regions: a
    group keeps a muscle together with the exercises it helps in (biceps with the rows, triceps
    with the presses) - split the other way round, yesterday's curl would limit today's row.
    None = full body."""
    k_split = split_factor(spw, structure, real_spw)
    known = sum(1 for c in pool if not c["new"] and c["restriction"] != "avoid")
    while k_split > 1 and known < MIN_READY_EXERCISES * k_split:          # too few exercises to split them up
        k_split -= 1
    if k_split == 1:
        return None
    trained = {c["group"] for c in pool if not c["new"]}                  # what the athlete really does
    best: dict = {}
    fresh: set = set()                             # groups with at least one known exercise that is fully ready
    for c in cands:
        if c["score"] > 0:                         # a never-performed exercise alone does not make a group "due"
            best[c["group"]] = max(best.get(c["group"], 0.0), c["score"] * (0.01 if c["new"] else 1.0))
            if c["status"] == "ready" and not c["new"]:
                fresh.add(c["group"])
    # a group whose exercises are only "limited" (a helper muscle not fresh yet) does not make the theme while another
    # group is fully ready: a rested group trains today, the limited one gets its turn fresh - instead of a half
    # session that waits for the helper and pushes the rested group back by days (v0.8.6)
    if fresh:
        best = {g: s for g, s in best.items() if g in fresh}
    k = max(1, math.ceil(len(trained | {g for g in best if any(not c["new"] for c in cands if c["group"] == g)}) / k_split))
    return sorted(sorted(best, key=lambda g: (-best[g], g))[:k])


def select(cands: list[dict], size: int, focus: dict, theme: list[str] | None, max_new: int = 1) -> tuple[list[dict], list[dict]]:
    """Greedy pick with a redundancy penalty: after each pick the remaining scores shrink by the
    overlap of their target muscles with it (halved on a focus region or in a themed session).
    Twins are skipped in a full-body session; at most max_new never-performed exercises (one - a
    whole starter session only for an athlete without a history), and only for a focus region or
    a slot no known exercise wants (else it is listed as "closes a gap").
    The big slot: in a full-body session the best big exercise of every movement group is picked
    first; WHICH one is decided by the urgent muscles it reaches (see score_candidates), then by
    readiness and score - so an Overhead Press cannot take the chest's turn when the chest would
    wait two weeks. A split day gets such a pick only when a muscle of the group is urgent.
    The other slots: an exercise that is the first of the session to reach an urgent muscle goes
    before one whose muscles can wait (calves once a week before a second arm exercise); with
    nothing urgent the score alone decides, as it always did."""
    left = [dict(c) for c in cands if theme is None or c["group"] in theme]
    known = sum(1 for c in left if not c["new"])
    for c in left:                                 # a never-performed exercise serves a focus or fills a free
        if c["new"] and known >= size and not any(focus.get(r) == "more" for r in c["regions"]):
            c["score"], c["gap_only"] = 0.0, True  # slot - it never displaces what the athlete already does
    # Full body means FULL body: the best big exercise of every movement group the athlete trains is
    # picked before anything else - so with few sessions a week no muscle waits two weeks for its turn.
    # "Best" = reaches the most urgent muscles (as a target 1, as a helper 0.5), then ready before
    # limited, then the score: with nothing urgent that is exactly the old choice.
    for g in sorted({c["group"] for c in left if c["group"] != "?"}):
        big = [c for c in left if c["group"] == g and not c["new"] and c["kind"] == "compound" and c["score"] > 0]
        if theme is not None and not any(c.get("urgent") for c in big):
            continue                               # a split day keeps its freedom unless a muscle would wait too long
        plain = max(big, key=lambda c: (c["status"] == "ready", c["score"], c["name"]), default=None)
        best = max(big, key=lambda c: (c.get("urgent_weight", 0.0), c["status"] == "ready", c["score"], c["name"]), default=None)
        if best:
            best["score"] += COVER_FIRST[0] if best["status"] == "ready" else COVER_FIRST[1]
            best["cover_group"] = g
            if best.get("urgent") and (theme is not None or best is not plain):
                best["urgent_for"] = list(best["urgent"])          # urgency decided it: worth a sentence
    chosen, dropped = [], [{"name": c["name"], "reason": "other_groups_today", "score": c["score"], "new": c["new"]}
                           for c in cands if theme is not None and c["group"] not in theme]
    covered: set = set()                           # muscles the session reaches so far (target or helper)

    def first_to_reach(c) -> list[str]:            # urgent muscles nobody in the session reaches yet
        return [m for m in c.get("urgent") or [] if m not in covered] if c["score"] > 0 else []
    while left and len(chosen) < size:
        by_score = min(left, key=lambda c: (-c["score"], c["name"]))
        # the big exercises first (their bonus), then whatever keeps a muscle at its weekly stimulus, then the score
        left.sort(key=lambda c: (0 if c.get("cover_group") else 1, -sum(URGENT_WEIGHT[c["muscles"][m]] for m in first_to_reach(c)),
                                 -c["score"], c["name"]))
        pick = left.pop(0)
        if pick["score"] <= 0:
            left.insert(0, pick)
            break
        if pick is not by_score and not pick.get("cover_group") and first_to_reach(pick):
            pick["urgent_for"] = first_to_reach(pick)
        chosen.append(pick)
        covered |= set(pick["muscles"])
        per_region: dict = {}
        for x in chosen:
            for r in x["regions"]:
                per_region[r] = per_region.get(r, 0) + 1
        keep = []
        for c in left:
            # a split day concentrates on fewer regions, so it may go one deeper - like a focus region, and like
            # a full-body session with the time for seven or eight exercises (else that time could not be used)
            full = [r for r in c["regions"]
                    if per_region.get(r, 0) >= MAX_PER_REGION + (1 if (theme is not None or focus.get(r) == "more" or size >= BIG_SESSION_EX) else 0)]
            if full and len(full) == len(c["regions"]):
                dropped.append({"name": c["name"], "reason": "region_full", "with": full[0], "score": c["score"], "new": c["new"]})
                continue
            union = _targets(pick) | _targets(c)
            overlap = len(_targets(pick) & _targets(c)) / len(union) if union else 0.0
            wide = theme is not None or any(focus.get(r) == "more" for r in c["regions"])
            if overlap >= TWIN_OVERLAP and not wide:
                dropped.append({"name": c["name"], "reason": "same_muscles", "with": pick["name"], "score": c["score"], "new": c["new"]})
                continue
            if c["new"] and sum(1 for x in chosen if x["new"]) >= max_new:
                dropped.append({"name": c["name"], "reason": "one_new_at_a_time", "score": c["score"], "new": True})
                continue
            if overlap:
                c["score"] = round(c["score"] * (1 - REDUNDANCY * overlap * (0.5 if wide else 1.0)), 3)
                c.setdefault("overlaps", pick["name"])
            keep.append(c)
        left = keep
    dropped += [{"name": c["name"], "reason": "closes_a_gap" if c.get("gap_only") else "no_slot", "with": c.get("overlaps"),
                 "score": c["base_score"] if c.get("gap_only") else c["score"], "new": c["new"], "muscle": c.get("due_muscle")}
                for c in left]
    return chosen, dropped


# =============================================================================
# Order
# =============================================================================
def pair_loss(a: dict, b: dict, ev: dict) -> tuple[float, dict | None]:
    """Expected loss (%) of b when a is done before it: the athlete's measured effect when there is
    one (already shrunk towards the prior), else the catalog prior. (0, None) when nothing is shared."""
    for p in (ev or {}).get("pair_effects", []):
        if p["before"] == a["name"] and p["then"] == b["name"]:
            return max(0.0, p["loss_pct"]), {"id": p["id"], "source": "measured", "n": p["n"], "confidence": p["confidence"],
                                             "loss_pct": p["loss_pct"], "before": a["name"]}
    prior = evidence.pair_prior(a["muscles"], b["muscles"])
    if prior <= 0:
        return 0.0, None
    return prior, {"id": f"prior:{b['name']}|after:{a['name']}", "source": "prior", "n": 0, "confidence": "prior",
                   "loss_pct": prior, "before": a["name"]}


def _rest_model(ev: dict):
    """(share of a pair's loss per minute, reference minutes) when the athlete's data shows that
    more rest costs less (arx_evidence: enough observations AND better than shuffled data) - else
    None: no assumed decay."""
    re_ = (ev or {}).get("rest_effect") or {}
    if re_.get("status") != "detected" or not re_.get("loss_share_change_per_min") or re_.get("reference_minutes") is None:
        return None
    return re_["loss_share_change_per_min"], re_["reference_minutes"]


def position_loss(ev: dict) -> float:
    pe = (ev or {}).get("position_effect") or {}
    val = -(pe.get("pct_per_position") if pe.get("pct_per_position") is not None else evidence.POSITION_PRIOR_PCT)
    return min(POS_LOSS_RANGE[1], max(POS_LOSS_RANGE[0], val))


def means_before_target(a: dict, b: dict) -> list[str]:
    """Muscles that are a's TARGET and b's LIMITER: a must not come before b."""
    return sorted(_targets(a) & {m for m, r in b["muscles"].items() if r == "limiter"})


def best_order(chosen: list[dict], ev: dict) -> tuple[list[dict], list[dict], dict]:
    """The cheapest permutation that respects 'a means before the target' and keeps the benchmark
    exercise free of anything that loads its muscles before it."""
    items = sorted(chosen, key=lambda c: c["name"])
    loss = {(a["name"], b["name"]): pair_loss(a, b, ev) for a in items for b in items if a is not b}
    pos_loss, rest = position_loss(ev), _rest_model(ev)
    # "a's target is b's helper -> a after b": computed once per pair (eight exercises = 40 320 orders to look at)
    forbidden = {(a["name"], b["name"]) for a in items for b in items if a is not b and means_before_target(a, b)}
    perms = [p for p in itertools.permutations(items)
             if not forbidden or not any((a["name"], b["name"]) in forbidden for i, a in enumerate(p) for b in p[i + 1:])]
    relaxed = not perms
    if relaxed:                                    # cannot happen with a sane catalog (a cycle of means)
        perms = list(itertools.permutations(items))
    bench = None
    for cand in sorted((c for c in items if c.get("bench_rank") is not None), key=lambda c: c["bench_rank"]):
        feasible = [p for p in perms if all(loss[(a["name"], cand["name"])][0] == 0 for a in p[:p.index(cand)])]
        if feasible:
            bench, perms = cand["name"], feasible
            break

    def cost(p):
        total, rows, clock, ends = 0.0, [], 0.0, []
        for pos, b in enumerate(p):
            exp, used = pos_loss * pos, []
            for a, a_end in zip(p[:pos], ends):
                base, src = loss[(a["name"], b["name"])]
                if base <= 0:
                    continue
                eff = base
                if rest:                           # only a MEASURED rest effect moves the loss, bounded
                    eff = base * min(1 + REST_GAP_SWING, max(1 - REST_GAP_SWING, 1 + rest[0] * ((clock - a_end) - rest[1])))
                exp += eff
                used.append(dict(src, expected_loss_pct=round(eff, 1)))
            total += b["base_score"] * exp
            rows.append({"name": b["name"], "expected_loss_pct": round(exp, 1), "shared_loss_pct": round(exp - pos_loss * pos, 1),
                         "evidence": used})
            clock += b["set_seconds"] / 60.0
            ends.append(clock)
            clock += REST_MIN.get(b["kind"], 2.5) + TRANSITION_MIN
        return total, rows

    # Equal cost (nothing shared, no position effect measured): the order a trainer would write down -
    # the clean measurement first, big exercises before small ones, what matters most before the
    # rest, something new last. Fewest inversions against that order wins.
    natural = sorted(items, key=lambda c: (0 if c["name"] == bench else 1, 1 if c["new"] else 0,
                                           0 if c["kind"] == "compound" else 1, -c["base_score"], c["name"]))
    rank = {c["name"]: i for i, c in enumerate(natural)}
    inversions = lambda p: sum(1 for i, a in enumerate(p) for b in p[i + 1:] if rank[a["name"]] > rank[b["name"]])
    scored = [(cost(p), p) for p in perms]
    (total, rows), seq = min(scored, key=lambda x: (round(x[0][0], 6), inversions(x[1]), [c["name"] for c in x[1]]))
    worst = max(s[0][0] for s in scored)
    return list(seq), rows, {"benchmark": bench, "cost": round(total, 2), "worst_cost": round(worst, 2),
                             "orders_compared": len(perms), "position_loss_pct": round(pos_loss, 1),
                             "rest_effect_used": bool(rest), "rule_relaxed": relaxed}


# =============================================================================
# Effort, targets, rests
# =============================================================================
def beginner_phase(cfg: dict) -> bool:
    """A new athlete's first BEGINNER_SESSIONS training days (profile experience "new"): the effort target is
    moderate, the athlete sets the level himself ("about half of what you have", then "beat your previous number by
    what feels comfortable" - ARX Academy practice), and no set is judged a miss; the real set comes after (v0.12.0)."""
    return cfg.get("experience") == "new" and (cfg.get("_n_days") or 0) < BEGINNER_SESSIONS


def effort_for(cfg: dict, commitment: str, band: str | None, age: str | None) -> tuple[dict, list[str]]:
    """The effort a set should reach + what capped it (commitment | checkin | age | beginner)."""
    label, caps = goal_effort(cfg.get("goal") or {}), []

    def cap(to: str, why: str):
        nonlocal label
        if EFFORT_RANK[to] < EFFORT_RANK[label]:
            label = to
            caps.append(why)
    if COMMITMENT[commitment]["effort"] == "moderate":
        cap("moderate", "commitment")
    if band == "moderate":
        cap("moderate", "checkin")
    if band == "light_or_rest":
        cap("submax", "checkin")
    if age in MINOR_BANDS:
        cap("moderate", "age")
    if beginner_phase(cfg):
        cap("moderate", "beginner")
    return dict(EFFORT_TARGETS[label]), caps


def planned_minima(entries: list[dict] | None, name: str) -> dict:
    """{planned date: inroad_min} the plan ledger asked of one exercise (0 = sub-maximal by plan)."""
    out = {}
    for e in entries or []:
        for x in e.get("exercises") or []:
            if x.get("name") == name:
                out[e["date"]] = x.get("inroad_min") or 0
    return out


def effort_streak(occ: list[dict], inroad_min: float, planned: dict | None = None) -> dict:
    """How many of the exercise's most recent training days IN A ROW missed the effort target (best set's
    force drop below inroad_min - BORDERLINE), stopping at the first day that reached it. Not counted:
    familiarisation / low-force capped days, days with other settings, days the ledger had planned sub-maximal
    ({date: inroad_min}, 0 = sub-max); a day planned with a lower target is judged against that one.
    Lookback EFFORT_STREAK_DAYS from the last training day (v0.10.0)."""
    misses, last = 0, None
    if not occ:
        return {"misses": 0, "last_inroad": None, "inroad_min": inroad_min}
    floor = (date.fromisoformat(occ[-1]["date"][:10]) - timedelta(days=EFFORT_STREAK_DAYS)).isoformat()
    for o in reversed(occ):
        if o["date"][:10] < floor:
            break
        if o.get("inroad") is None or o.get("effort_capped") or not o.get("settings_ok", True):
            continue
        want = inroad_min
        if planned and o["date"][:10] in planned:
            if not planned[o["date"][:10]]:        # planned sub-maximal: neither a miss nor a hit
                continue
            want = min(want, planned[o["date"][:10]])
        if last is None:
            last = o["inroad"]
        if o["inroad"] >= want - BORDERLINE:
            break
        misses += 1
    return {"misses": misses, "last_inroad": last, "inroad_min": inroad_min}


def effort_reset_settings(settings: dict | None, keep_pauses: bool = False) -> dict | None:
    """The parameter change after EFFORT_RESET_AFTER misses, built from the last comparable settings: seconds per
    direction + EFFORT_TEMPO_STEP_S (never beyond EFFORT_TEMPO_MAX_S), pauses at the turnarounds to 0 - only what
    actually changes; once tempo and pauses are exhausted, EFFORT_REPS_STEP repetitions more. None = nothing known.
    keep_pauses (a strength goal, v0.12.0): the rest-pause serves tension - the lever is tempo, then repetitions."""
    if not settings:
        return None
    out = {}
    t = settings.get("tempo_s")
    if t and t < EFFORT_TEMPO_MAX_S - 0.25:        # a quarter second below the cap is "at the cap" already
        out["tempo_s"] = {"from": round(t, 1), "to": float(min(EFFORT_TEMPO_MAX_S, round(t) + EFFORT_TEMPO_STEP_S))}
    for k in ("pause_end_s", "pause_return_s"):
        if not keep_pauses and (settings.get(k) or 0) > 0:
            out[k] = {"from": settings[k], "to": 0}
    if not out and settings.get("reps"):
        out["reps"] = {"from": settings["reps"], "to": settings["reps"] + EFFORT_REPS_STEP}
    return out or None


def target_for(c: dict, effort: dict, commitment: str, band: str | None, age: str | None, is_bench: bool,
               row: dict, cfg: dict, returning: bool = False, away_days: int | None = None) -> dict:
    """Force target, sets and the rule behind them (see the module docstring). away_days = days the
    exercise's muscles have been without direct work on the planned day (see BREAK_SHORT_DAYS)."""
    e = c["series"]
    out = {"target_peak_kg": None, "target_con_mean_kg": None, "target_rule": None, "step_pct": 0.0, "base_kg": None,
           "base_date": None, "base_context": None, "settings": None, "sets": 1, "context_note": None}
    if c["new"] or not e:
        out["target_rule"], out["interp"] = "new_exercise", item("plan_new_exercise", {}, cfg)
        return out
    occ = e["occ"]
    comparable = [o for o in occ if o.get("comparable")]
    fresh = [o for o in comparable if o.get("context") == "fresh"]
    base = ((fresh if len(fresh) >= 2 else comparable) or occ)[-1]
    out.update({"base_kg": base["kg"], "base_date": base["date"], "base_context": base.get("context"),
                "settings": {"reps": base.get("reps"), "tempo_s": base.get("tempo_s"), "pause_end_s": base.get("pause_end_s"),
                             "pause_return_s": base.get("pause_return_s"), "rom_cm": base.get("rom_cm"),
                             "source": "last_comparable" if comparable else "last"}})
    prog = c["progress"] or {}
    if c["restriction"] == "careful":
        out["target_rule"] = "sub_max_careful"
        # the athlete's own choice for THIS exercise (profile tile) - or a body part he marked as careful
        if c.get("careful_today"):                 # today's check-in (v0.8.9) - with the body part behind it (v0.9.0)
            out["interp"] = item("plan_submax_careful_today", {"why": (cfg.get("_today_why") or {}).get(c["code"], "checkin")}, cfg)
        else:
            code = "plan_submax_careful_exercise" if c["name"] in (cfg.get("_careful_names") or ()) else "plan_submax_careful"
            out["interp"] = item(code, {}, cfg)
        return out
    if c["status"] == "limited":
        out["target_rule"] = "sub_max_limiter"
        out["interp"] = item("plan_submax_limited", {"muscle": (c.get("limited_by") or [None])[0]}, cfg)
        return out
    if band == "light_or_rest":
        out["target_rule"], out["target_peak_kg"] = "light_day", round(base["kg"] * LIGHT_DAY_SHARE, 1)
        out["interp"] = item("plan_light_day", {"target_kg": out["target_peak_kg"], "share_pct": LIGHT_DAY_SHARE * 100}, cfg)
        return out
    if returning:                                  # months away: the first session back is the one that hurts
        out["target_rule"] = "return_after_break"
        out["interp"] = item("plan_return_after_break", {"last_date": occ[-1]["date"], "base_kg": base["kg"]}, cfg)
        return out
    if away_days is not None and away_days >= BREAK_LONG_DAYS:
        # weeks away: some strength is gone and comes back fast - the old number is an orientation, what is
        # reached today is the new starting point (no step, no extra set: nothing is "caught up")
        out["target_rule"], out["target_peak_kg"] = "rebase_after_break", base["kg"]
        out["interp"] = item("plan_rebase_after_break", {"base_kg": base["kg"], "base_date": base["date"], "weeks": round(away_days / 7),
                                                         "effort_pct": effort["inroad_min"]}, cfg)
        return out
    after_break = away_days is not None and away_days > BREAK_SHORT_DAYS      # a short break: continue, but hold the numbers
    profile = COMMITMENT[commitment]
    last_inroad, misses = occ[-1].get("inroad"), 0
    # the last set stopped short of the effort the goal asks for (a borderline value counts as reached)
    room = last_inroad is not None and last_inroad < effort["inroad_min"] - BORDERLINE
    may_step = profile["step"] and band in (None, "go_hard") and age not in MINOR_BANDS and not after_break
    hit = prog.get("effort_hit_rate")
    step = 0.0
    # the set stopped short of the effort: once the cue is intent (the number holds), twice in a row the set-up
    # changes - for the benchmark exercise as well: a clean measurement that never fatigues is half a stimulus (v0.10.0)
    beginner = beginner_phase(cfg)                 # first sessions: nothing is a miss, nothing escalates (v0.12.0)
    streak = effort_streak(occ, effort["inroad_min"], planned_minima(cfg.get("_plan_ledger"), c["name"])) if (room and not beginner) else None
    keep_pauses = goal_effort(cfg.get("goal") or {}) == "moderate"     # a strength goal keeps the rest-pause (tension)
    change = effort_reset_settings(out["settings"], keep_pauses) if (streak and streak["misses"] >= EFFORT_RESET_AFTER) else None
    if prog.get("status") == "progressing" and may_step and not room:
        per_session = abs(prog.get("change_pct") or 0.0) / max(1, (prog.get("n") or 2) - 1)
        step = min(STEP_RANGE_PCT[1], max(STEP_RANGE_PCT[0], per_session))
        rule, code = "step", "plan_step"
    elif change:
        rule, out["settings_change"], misses = "effort_reset", change, streak["misses"]
        code = ("plan_effort_reps" if "reps" in change else "plan_effort_pauses" if "tempo_s" not in change
                else "plan_effort_tempo" if keep_pauses else "plan_effort_reset")
    elif beginner and room:                        # first sessions: a set that stopped short is fine - no "clean measurement" talk yet
        rule, code = "hold_reach_effort", "plan_beginner_row"
    elif is_bench:                                                    # today's clean measurement
        rule, code = "retest_fresh", ("plan_retest_fresh" if c["last_fresh"] else "plan_retest_first")
    elif room:
        rule, code = "hold_reach_effort", "plan_hold_effort"          # the set stopped short: effort before force
    elif after_break:
        rule, code = "hold", "plan_hold_after_break"
    elif prog.get("status") == "plateau" and (hit is None or hit >= HIT_RATE_OK):
        if profile["plateau_set"] and age not in MINOR_BANDS:
            rule, code, out["sets"] = "plateau_add_set", "plan_plateau_add_set", 2
        else:
            rule, code = "plateau_hold", "plan_plateau_min_time"
    else:
        rule, code = "hold", "plan_hold"
    out["step_pct"], out["target_rule"] = round(step, 1), rule
    factor = 1 + step / 100.0
    shared = row.get("shared_loss_pct") or 0.0
    if base.get("context") == "fresh" and shared >= CONTEXT_LOSS_MIN_PCT:
        # the reference was measured fresh, this time something loads the same muscles first
        factor *= 1 - shared / 100.0
        top = max(row["evidence"], key=lambda x: x["expected_loss_pct"])
        out["context_note"] = item("plan_context_adjusted", {"loss_pct": round(shared, 1), "before": top["before"],
                                                            "n": top["n"], "fresh_kg": round(base["kg"] * (1 + step / 100.0), 1)},
                                   cfg, source=top["source"])
    elif base.get("context") in ("preloaded", "repeat") and shared < CONTEXT_LOSS_MIN_PCT and not is_bench:
        # the reference was measured after other work for the same muscles - this time nothing is in the way
        out["context_note"] = item("plan_fresher_than_reference", {"base_kg": base["kg"], "base_date": base["date"]}, cfg)
    out["target_peak_kg"] = round(base["kg"] * factor, 1)
    if len(comparable) >= 3 and base.get("con_top3_kg"):
        out["target_con_mean_kg"] = round(base["con_top3_kg"] * factor, 1)
    chg = out.get("settings_change") or {}
    out["interp"] = item(code, {"target_kg": out["target_peak_kg"], "base_kg": base["kg"], "step_pct": out["step_pct"],
                                "effort_pct": effort["inroad_min"], "base_date": base["date"], "last_pct": last_inroad,
                                "span_days": prog.get("span_days"), "n": prog.get("n"), "days": away_days,
                                # the parameter change after repeated misses (v0.10.0)
                                "misses": misses, "tempo_from": (chg.get("tempo_s") or {}).get("from"),
                                "tempo_to": (chg.get("tempo_s") or {}).get("to", (out["settings"] or {}).get("tempo_s")),
                                "reps_from": (chg.get("reps") or {}).get("from"), "reps_to": (chg.get("reps") or {}).get("to")}, cfg)
    return out


def window_size(window: float, set_min: float, transition: float) -> int:
    """How many exercises fit into a time window at the athlete's own pace: n sets and n - 1 change-overs.
    A first estimate - build_plan checks the finished session (rests can be longer) and shrinks once more."""
    gap = max(transition, REST_MIN["compound"]) + WINDOW_REST_MARGIN_MIN
    return int(max(WINDOW_MIN_EX, min(SESSION_MAX_EX, math.floor((window + gap) / max(1.0, set_min + gap)))))


def estimate_session_minutes(items: list[dict], transition_min: float) -> int:
    """Wall-clock minutes of a planned session: sets, rests between sets, change-over between
    exercises (the athlete's own median when known)."""
    total = 0.0
    for i, it in enumerate(items):
        total += it["sets"] * (it.get("set_seconds") or DEFAULT_SET_SECONDS) / 60.0
        total += (it["sets"] - 1) * REST_MIN.get(it["kind"], 2.5)
        if i:
            total += max(it["rest_before_min"], transition_min)
    return round(total)


# =============================================================================
# One session
# =============================================================================
def structure_of(cfg: dict) -> str:
    return cfg.get("structure") if cfg.get("structure") in STRUCTURES else "auto"


def manual_groups(cfg: dict, work: list[dict]) -> list[str] | None:
    """The athlete's one-time choice "next session only these groups" ({groups, set_at} in the
    profile): valid until a working set newer than the choice exists - then it is used up."""
    choice = cfg.get("next_groups") or {}
    groups, set_at = [g for g in choice.get("groups") or [] if isinstance(g, str)], str(choice.get("set_at") or "")
    if not groups or not set_at:
        return None
    newest = max((s["date"] for s in work), default="")
    return None if newest.replace("T", " ") > set_at.replace("T", " ") else sorted(set(groups))


def select_session(day: date, pool: list[dict], state: dict, cfg: dict, focus: dict, spw: int, size: int,
                   today: date, only_groups: list[str] | None = None, comeback: bool = False,
                   min_ex: int | None = None) -> dict | None:
    """What could be trained on that day (no order yet) + how complete that session would be.
    only_groups = the athlete's one-time choice for this session (beats the automatic theme);
    comeback = first session after weeks away: back to the known exercises first, nothing new;
    min_ex = a floor for the session size that replaces the usual minimum (today under the check-in's
    limits: what the athlete left in is his session, even a single eased exercise - v0.9.0)."""
    cands = score_candidates(pool, state, day, focus, spw, cfg, today)
    theme = only_groups or theme_groups(cands, pool, spw, structure_of(cfg), cfg.get("_real_spw"))
    # no history yet: a first session. Judged on the HISTORY (cfg["_starter"], see build_plan) as well: an
    # experienced athlete who switched off most of his exercises must not get a beginner session full of new ones
    starter = cfg.get("_starter", True) and sum(1 for c in pool if not c["new"]) < SESSION_MIN_EX
    chosen, dropped = select(cands, size, focus, theme, max_new=size if starter else (0 if comeback else 1))
    ready = [c for c in chosen if c["status"] == "ready"]
    usable = sum(1 for c in pool if c["restriction"] != "avoid" and (starter or not c["new"]))
    # a day on which everything is only "limited" (a helper not fresh) is still a possible day - a sub-maximal one,
    # worth half (fill) - not "no session" (v0.8.6; since v0.9.0 the check-in's flags say what is eased or left out)
    floor = min_ex if min_ex is not None else min(MIN_READY_EXERCISES, max(1, usable))
    if len(chosen) < floor or not chosen:
        return None
    want = size if theme is None else min(size, max(MIN_READY_EXERCISES, sum(1 for c in pool if not c["new"] and c["group"] in theme)))
    fill = min(1.0, sum(1.0 if c["status"] == "ready" else 0.5 for c in chosen) / max(1, want))
    return {"date": day, "chosen": chosen, "dropped": dropped, "theme": theme, "fill": round(fill, 2), "manual": bool(only_groups),
            "candidates": [{"name": c["name"], "status": c["status"], "new": c["new"], "restriction": c["restriction"], "group": c["group"]}
                           for c in cands]}


def finish_session(sel: dict, cfg: dict, ev: dict, commitment: str, band: str | None, age: str | None,
                   transition: float, repeat_loss: dict, single_sets: bool = False) -> dict:
    """Order, effort, targets, rests and minutes for a selected session. single_sets = the athlete's time
    window for today is tight: one hard set per exercise, no extra volume (see build_plan)."""
    day = sel["date"]
    seq, rows, meta = best_order(sel["chosen"], ev)
    effort, caps = effort_for(cfg, commitment, band, age)
    items = []
    for pos, (c, row) in enumerate(zip(seq, rows), 1):
        sub_max = c["restriction"] == "careful" or c["status"] == "limited" or c["new"]
        eff = dict(EFFORT_TARGETS["submax"]) if sub_max else dict(effort)
        last = (c["series"] or {}).get("last_date")
        returning = bool(last) and (day - date.fromisoformat(last)).days > RETURN_DAYS
        if returning and EFFORT_RANK[eff["label"]] > EFFORT_RANK["moderate"]:
            eff = dict(EFFORT_TARGETS["moderate"])
        is_bench = c["name"] == meta["benchmark"]
        tgt = target_for(c, eff, commitment, band, age, is_bench, row, cfg, returning=returning, away_days=c.get("days_away"))
        if COMMITMENT[commitment]["extra_sets"] and pos <= 2 and not sub_max and band in (None, "go_hard"):
            tgt["sets"] = max(tgt["sets"], 1 + COMMITMENT[commitment]["extra_sets"])
        if single_sets and tgt["sets"] > 1:        # no time for extra volume today: the one hard set stays
            tgt["sets"] = 1
            if tgt["target_rule"] == "plateau_add_set":            # the second set is what the time window costs today
                tgt["target_rule"], tgt["interp"] = "plateau_hold", item("plan_plateau_min_time", tgt["interp"]["params"], cfg)
        prev = seq[pos - 2] if pos > 1 else None
        rest = 0.0 if prev is None else REST_MIN.get(prev["kind"], 2.5) + min(REST_EXTRA_MAX, round(row["shared_loss_pct"] / REST_EXTRA_PER_PCT))
        why = list(c["why_selected"])
        if c.get("cover_group") and not sel["theme"]:
            why.append(item("sel_cover_group", {"groups": [c["cover_group"]]}, cfg))
        if c.get("urgent_for"):                    # the weekly stimulus decided for this exercise
            why.append(item("sel_urgent", {"muscles": c["urgent_for"], "limit": MAX_MUSCLE_GAP_DAYS}, cfg))
        if is_bench:
            why.append(item("sel_benchmark" if c["last_fresh"] else "sel_benchmark_never",
                            {"last_date": c["last_fresh"], "k": c["n_days"]}, cfg))
        rules = [item("order_means_first", {"first": a["name"], "second": c["name"], "muscle": means_before_target(c, a)[0]}, cfg)
                 for a in seq[:pos - 1] if means_before_target(c, a)]
        items.append({
            "order": pos, "name": c["name"], "code": c["code"], "group": c["group"], "kind": c["kind"], "new": c["new"],
            "benchmark": is_bench, "status": c["status"], "limited_by": c.get("limited_by", []), "restriction": c["restriction"],
            "regions": c["regions"], "muscles": c["muscles"], "effort_target": eff, "rest_before_min": rest,
            "set_seconds": round(c["set_seconds"]), "aid": c["aid"], "aid_hint": None,
            "expected_loss_pct": row["expected_loss_pct"], "shared_loss_pct": row["shared_loss_pct"], "evidence": row["evidence"],
            "planned_context": "preloaded" if row["evidence"] else "fresh",   # fresh = a clean measurement this time
            "second_set_loss_pct": repeat_loss.get(c["name"]) if tgt["sets"] > 1 else None,
            "why_selected": why, "order_rules": rules, "score": c["base_score"], "days_since_target": c["days_since_target"],
            "days_away": c.get("days_away"),
            "progress_status": (c["progress"] or {}).get("status"), **tgt,
        })
    regions = sorted({r for it in items for r in it["regions"]})
    return {"date": day.isoformat(), "weekday": day.weekday(), "session_type": "split" if sel["theme"] else "full_body",
            "regions": regions, "theme": sel["theme"], "fill": sel["fill"], "est_minutes": estimate_session_minutes(items, transition),
            "exercises": items, "benchmark": meta["benchmark"], "effort_target": effort, "effort_caps": caps,
            # a new athlete's first sessions (v0.12.0): moderate on purpose, said openly
            "beginner_note": item("plan_beginner", {"k": BEGINNER_SESSIONS, "n": cfg.get("_n_days") or 0}, cfg) if "beginner" in caps else None,
            "alternatives": sel["dropped"][:8], "order_meta": meta}


def limiter_budget(session: dict, work: list[dict], catalog: dict, ev: dict, cfg: dict) -> dict:
    """Planned load on every limiter vs what the athlete usually puts on it in one session - counted
    only where the muscle works as a MEANS (the curl that targets the elbow flexors comes after the
    rows anyway) - next to the loss measured on it so far."""
    aids = cfg.get("aids") or {}
    per_visit: dict = {}
    typical: dict = {}
    for s in work:
        key = (s["date"][:10], s.get("visit", 1))
        typical.setdefault(str(s["exercise"]), []).append(s)
        for m, role in _muscles(catalog.get(str(s["exercise"]), {}), s["exercise"], s["date"], aids).items():
            if role == "limiter":                  # work the muscle does as a helper, not as the goal
                per_visit.setdefault(m, {}).setdefault(key, 0.0)
                per_visit[m][key] += s.get("impulse_kg_s") or 0
    out = {}
    for m in sorted({m for it in session["exercises"] for m, r in it["muscles"].items() if r == "limiter"}):
        rows = [{"name": it["name"], "aid": it["aid"],
                 "load": round(_typical(typical.get(it["code"], []), "impulse_kg_s", 0.0) * it["sets"])}
                for it in session["exercises"] if it["muscles"].get(m) == "limiter"]
        planned = sum(r["load"] for r in rows)
        visits = [v for _, v in sorted(per_visit.get(m, {}).items())][-6:]
        usual = st.median(visits) if len(visits) >= BUDGET_MIN_SESSIONS else None
        status = "unknown" if not usual else ("high" if planned > BUDGET_TOLERANCE * usual else "ok")
        measured = ((ev or {}).get("limiter_effects") or {}).get(m)
        out[m] = {"planned": planned, "usual": round(usual) if usual else None, "n_sessions": len(visits), "status": status,
                  "share_pct": round(planned / usual * 100) if usual else None, "by_exercise": rows, "measured": measured,
                  "interp": item(f"budget_{status}", {"muscle": m, "share_pct": round(planned / usual * 100) if usual else None,
                                                     "k": len(rows)}, cfg),
                  "measured_interp": item("limiter_measured", {"muscle": m, "loss_pct": measured["observed_loss_pct"],
                                                               "n": measured["n"]}, cfg, confidence=measured["confidence"]) if measured else None}
    return out


def aid_hints(session: dict, budget: dict, cfg: dict, possible: dict) -> list[dict]:
    """Where an aid (hooks / straps) frees the most: only when a limiter is over budget or carries
    AID_MIN_EXERCISES exercises, only for exercises that offer an aid and use none yet - and only
    for exercises that train the LEGS through the hands (dead lifts): that is where studies show a
    benefit; for pull-downs they found none (science.json: grip_and_straps)."""
    hints = []
    for m, b in budget.items():
        if b["status"] != "high" and len(b["by_exercise"]) < AID_MIN_EXERCISES:
            continue
        region_of = {it["name"]: it["regions"] for it in session["exercises"]}
        rows = [r for r in b["by_exercise"] if not r["aid"] and possible.get(r["name"]) and "legs" in region_of[r["name"]]]
        if not rows:
            continue
        pick = min(rows, key=lambda r: (-r["load"], r["name"]))
        aid = possible[pick["name"]][0]
        hints.append(item("aid_hint", {"exercise": pick["name"], "aid": aid, "muscle": m, "k": len(b["by_exercise"]),
                                       "share_pct": b["share_pct"]}, cfg, exercise=pick["name"], muscle=m, aid=aid))
        for it in session["exercises"]:
            if it["name"] == pick["name"]:
                it["aid_hint"] = aid
    return hints


# =============================================================================
# The plan
# =============================================================================
def weekday_habit(days: list[str], spw: int) -> list[int]:
    """The athlete's usual weekdays (0 = Monday) - only when a real pattern exists."""
    if len(days) < HABIT_MIN_DAYS:
        return []
    counts: dict = {}
    for d in days:
        wd = date.fromisoformat(d).weekday()
        counts[wd] = counts.get(wd, 0) + 1
    top = sorted(counts, key=lambda w: (-counts[w], w))[:max(1, spw)]
    return sorted(top) if sum(counts[w] for w in top) / len(days) >= HABIT_SHARE else []


def session_size(minutes: float, per_exercise_min: float, regions: int, commitment: str, band: str | None,
                 age: str | None, focus_more: bool = False) -> int:
    """Exercises per session: what the time budget holds at the athlete's own pace (set + change-
    over), then the commitment profile; "more" of a region costs one more exercise (the big
    exercises for push / pull / legs keep their slots); a poor check-in and the youth guard cut it."""
    size = int(min(SESSION_MAX_EX, max(SESSION_MIN_EX, round(minutes / max(1.0, per_exercise_min)))))
    delta = COMMITMENT[commitment]["size"]
    size = min(size, max(SESSION_MIN_EX, regions)) if delta == "cover" else size + delta
    size = max(SESSION_MIN_EX - 1, min(SESSION_MAX_EX, size + (1 if focus_more else 0)))
    if band == "moderate":
        size = max(2, size - 1)
    if band == "light_or_rest":
        size = min(size, 3)
    return min(size, MINOR_MAX_EXERCISES) if age in MINOR_BANDS else size


def build_plan(exercises: list[dict], work: list[dict], catalog: dict, cfg: dict, today: date, readiness: dict | None,
               load: dict, ev: dict, progress: dict, sequences: list[dict] | None = None, profile: dict | None = None,
               restriction_of=None) -> dict:
    """{today, profile, next_session, week_plan, week_strip, date_options} - see the module docstring."""
    aids = cfg.get("aids") or {}
    spw = max(1, min(7, int(cfg.get("sessions_per_week") or 2)))
    band = (readiness or {}).get("band")
    age = (profile or {}).get("age_band")
    walls = [d["wall_minutes"] / max(1, d.get("visits") or 1) for d in (sequences or []) if d.get("wall_minutes")]
    minutes = cfg.get("session_minutes")
    minutes_auto = not minutes
    # "Auto" = what the athlete usually takes (median of the last six sessions, within SESSION_MINUTES_RANGE)
    auto_minutes = min(SESSION_MINUTES_RANGE[1], max(SESSION_MINUTES_RANGE[0], st.median(walls[-6:]))) if walls else DEFAULT_SESSION_MINUTES
    if not minutes:
        minutes = auto_minutes
    minutes = float(minutes)
    chosen_profile = cfg.get("commitment") if cfg.get("commitment") in COMMITMENT else None
    recommended = recommend_commitment(cfg, minutes, spw)
    commitment = chosen_profile or recommended
    focus = focus_regions(cfg)
    structure = structure_of(cfg)
    real_spw = real_frequency(sorted({s["date"][:10] for s in work}))
    external = cfg.get("_external") if cfg.get("_external") is not None else external_muscles(cfg, catalog)
    # read by split_factor via score_candidates / select_session; _starter = is there a history at all (not: a pool);
    # _external = muscles trained outside the ARX (arx_report sets it once for the findings too)
    cfg = dict(cfg, _real_spw=real_spw, _starter=len(exercises) < SESSION_MIN_EX, _external=list(external),
               _today_exercises=today_exercise_levels(cfg, catalog), _today_why=today_exercise_why(cfg, catalog),
               _n_days=len({s["date"][:10] for s in work}))
    excluded = excluded_block(cfg, catalog)
    chosen_groups = manual_groups(cfg, work)       # "next session only these groups" - first session only
    pool = build_pool(exercises, work, catalog, cfg, today, progress, restriction_of)
    state = muscle_state(load, work, catalog, aids, age)
    transition, transition_measured = transition_minutes(work)
    set_min = st.median([c["set_seconds"] for c in pool if not c["new"]] or [DEFAULT_SET_SECONDS]) / 60.0
    per_exercise = (set_min + transition) if transition_measured else MINUTES_PER_EXERCISE
    n_regions = len({r for c in pool if not c["new"] and c["restriction"] != "avoid" for r in c["regions"] if focus.get(r) != "off"})
    size_for = lambda b: session_size(minutes, per_exercise, n_regions, commitment, b, age, "more" in focus.values())
    repeat_loss = {r["exercise"]: r["observed_loss_pct"] for r in (ev or {}).get("repeat_effects", []) if r["n"] >= 2}
    days = sorted({s["date"][:10] for s in work})
    last_day = date.fromisoformat(days[-1]) if days else None
    ideal_gap = 7.0 / spw
    habit = weekday_habit(days, spw)
    week_counts: dict = {}
    for d in days:
        key = date.fromisoformat(d).isocalendar()[:2]
        week_counts[key] = week_counts.get(key, 0) + 1

    # "minutes I have today" from the check-in: an upper limit for a session planned for TODAY (never a reason for more)
    window = (cfg.get("checkin") or {}).get("minutes")
    window = window if window in MINUTES_OPTIONS else None

    trained_today = bool(days) and days[-1] == today.isoformat()
    score_today = (readiness or {}).get("score")
    rest_today = band == "light_or_rest" and ((readiness or {}).get("rhr_status") == "elevated"
                                              or (score_today is not None and score_today < REST_SCORE_BELOW))
    earliest = today + timedelta(days=1) if (trained_today or rest_today) else today

    # --- today under a time window: the normal plan when it fits; else extra sets go first, then the session gets
    # smaller (never below WINDOW_MIN_EX) - judged on the FINISHED session, because rests can be longer than the pace
    fit = None
    if window and earliest == today:
        def plan_today(size: int, single: bool):
            sel = select_session(today, pool, state, cfg, focus, spw, size, today, chosen_groups,
                                 comeback=bool(last_day) and (today - last_day).days >= BREAK_LONG_DAYS)
            return sel and finish_session(sel, cfg, ev, commitment, band, age, transition, repeat_loss, single_sets=single)
        normal = plan_today(size_for(band), False)
        if normal and normal["est_minutes"] > window:
            size = max(WINDOW_MIN_EX, min(len(normal["exercises"]), window_size(window, set_min, transition)))
            cur = plan_today(size, True)
            while cur and cur["est_minutes"] > window and size > WINDOW_MIN_EX:
                size -= 1
                cur = plan_today(size, True)
            if cur:
                fit = {"size": size, "normal": [it["name"] for it in normal["exercises"]], "normal_minutes": normal["est_minutes"]}

    def finish(o: dict, b: str | None) -> dict:
        """finish_session + what the time window did to a session planned for today (said openly)."""
        windowed = fit is not None and o["date"] == today
        sess = finish_session(o, cfg, ev, commitment, b, age, transition, repeat_loss, single_sets=windowed)
        if windowed:
            kept = [it["name"] for it in sess["exercises"]]
            left = [n for n in fit["normal"] if n not in kept]
            code = "window_tight" if sess["est_minutes"] > window else "window_applied"
            sess["time_window"] = {"minutes": window, "size": len(kept), "normal_size": len(fit["normal"]), "left_out": left,
                                   "normal_minutes": fit["normal_minutes"],
                                   "interp": item(code, {"minutes": window, "k": len(kept), "n": len(fit["normal"]), "left": left or ["-"],
                                                         "est": sess["est_minutes"], "normal": fit["normal_minutes"]}, cfg)}
        if o["date"] == today and cfg.get("_today_exercises"):
            names_of = {c["code"]: c["name"] for c in pool}
            off = sorted(names_of[c] for c, lvl in cfg["_today_exercises"].items() if lvl == "injury" and c in names_of)
            care = sorted(names_of[c] for c, lvl in cfg["_today_exercises"].items() if lvl == "careful" and c in names_of)
            sess["checkin_notes"] = ([item("checkin_off_today", {"exercises": off, "k": len(off)}, cfg)] if off else []) \
                + ([item("checkin_careful_today", {"exercises": care, "k": len(care)}, cfg)] if care else [])
        # a poor check-in makes a session planned for today smaller (session_size) - say that it was the CHECK-IN and
        # not the clock: more minutes do not bring the exercise back (v0.8.4)
        if b in BAND_FILL and o["date"] == today and not windowed and len(sess["exercises"]) < size_for(None):
            sess["checkin_cut"] = item("size_checkin", {"score": score_today, "k": len(sess["exercises"]), "n": size_for(None)}, cfg)
        return sess

    # the check-in's limits on single exercises (v0.8.9) shape the session for TODAY - they never decide the day:
    # a twinge that rules out one exercise is no reason to move the whole session (the fill would drop and a fuller
    # day would win), exactly like the time window. The day is judged as if nothing were limited.
    limits = bool(cfg.get("_today_exercises"))
    cfg_free = dict(cfg, _today_exercises={}) if limits else cfg

    def options(start: date, prev: date | None, st8: dict, counts: dict, pool_: list[dict], with_band: bool) -> list[dict]:
        out = []
        for k in range(DATE_SEARCH_DAYS + 1):
            d = start + timedelta(days=k)
            b = band if (with_band and d == today) else None
            size = fit["size"] if (fit and with_band and d == today) else size_for(b)
            groups_ = chosen_groups if with_band else None
            comeback = bool(prev) and (d - prev).days >= BREAK_LONG_DAYS
            sel = select_session(d, pool_, st8, cfg_free, focus, spw, size, today, groups_, comeback=comeback)
            if not sel:
                continue
            if limits and with_band and d == today:
                # what the athlete left in is his session for today, even one eased exercise ("Belt Squat anyway");
                # a fuller day later is named by fuller_option. Nothing left -> today is really no option
                lim = select_session(d, pool_, st8, cfg, focus, spw, size, today, groups_, comeback=comeback, min_ex=1)
                if not lim:
                    continue
                sel = dict(lim, fill=sel["fill"], fill_limited=lim["fill"])
            gap = (d - prev).days if prev else None
            off = max(0.0, abs(gap - ideal_gap) - CADENCE_FREE_DAYS) / ideal_gap if gap is not None else 0.0
            missing = spw - counts.get(d.isocalendar()[:2], 0)     # sessions the week of d still needs
            open_week = missing > 0
            last_chance = open_week and (6 - d.weekday()) < missing and counts.get(d.isocalendar()[:2], 0) > 0
            fill = sel["fill"] * BAND_FILL.get(b, 1.0)
            # Too rare is always measured against the rhythm. Too SOON only holds a full-body session back: a split
            # session trains other muscles than the last one - consecutive days are its point (science.json:
            # split_vs_full_body) - so while the week still needs sessions it costs next to nothing (v0.8.6; before,
            # the legs waited three days although they were rested, and the week fell short of its target)
            if gap is not None and gap < ideal_gap and sel["theme"] is not None and open_week:
                cadence = SPLIT_EVENNESS_W * (ideal_gap - gap) / ideal_gap
            else:
                cadence = CADENCE_W * off
            score = (fill - cadence + (WEEK_W if open_week else 0.0) + (WEEK_LAST_W if last_chance else 0.0)
                     + (HABIT_W if d.weekday() in habit else 0.0))
            out.append(dict(sel, band=b, gap_days=gap, open_week=open_week, last_chance=last_chance, habit=d.weekday() in habit,
                            score=round(score, 3), fill_effective=round(fill, 2)))
        return out

    opts = options(earliest, last_day, state, week_counts, pool, True)
    profile_out = {"commitment": commitment, "commitment_chosen": bool(chosen_profile), "recommended_commitment": recommended,
                   "sessions_per_week": spw, "session_minutes": round(minutes), "session_minutes_auto": minutes_auto,
                   "focus_regions": focus, "structure": structure, "split_factor": split_factor(spw, structure, real_spw),
                   "real_sessions_per_week": real_spw,
                   "next_groups": chosen_groups, "groups": sorted({c["group"] for c in pool if not c["new"] and c["group"] != "?"}), "age_guard": age if age in MINOR_BANDS + OLDER_BANDS else None,
                   "supervision": age in MINOR_BANDS, "transition_min": transition, "transition_measured": transition_measured,
                   "exercises_per_session": size_for(None),
                   # what the time budget buys at the athlete's own pace: minutes -> exercises (the profile shows it)
                   "per_exercise_min": round(per_exercise, 1), "partner": bool(cfg.get("partner")),
                   "auto_minutes": round(auto_minutes),
                   "auto_size": session_size(auto_minutes, per_exercise, n_regions, commitment, None, age, "more" in focus.values()),
                   "size_by_minutes": {str(m): session_size(m, per_exercise, n_regions, commitment, None, age, "more" in focus.values())
                                       for m in MINUTES_OPTIONS},
                   # the check-in's "minutes I have today": how many exercises of the normal session fit into each window
                   "window_sizes": {str(m): min(size_for(None), window_size(m, set_min, transition)) for m in MINUTES_OPTIONS},
                   "window_minutes": window,
                   "commitment_options": commitment_options(cfg, minutes, spw, set_min, transition, per_exercise, n_regions, age)}
    if not opts:
        # nothing can be planned: usually nothing is ready - or the athlete switched off everything he does
        all_off = bool(exercises) and bool(excluded) and all(str(e["ex"]) in excluded_of(cfg, catalog) for e in exercises)
        nothing = item("plan_all_excluded", {"k": len(excluded["exercises"])}, cfg) if all_off else item("date_nothing_ready", {"days": DATE_SEARCH_DAYS}, cfg)
        return {"algo": PLAN_ALGO_VERSION, "today": {"train_today": False, "trained_today": trained_today, "interp": nothing},
                "profile": profile_out, "next_session": None, "today_session": None, "week_plan": [], "week_strip": [],
                "cadence_note": None, "frequency_notes": [], "dose_note": None, "structure_note": None, "date_options": [],
                "training_break": None, "excluded": excluded, "decision_space": None}
    best = max(opts, key=lambda o: (o["score"], -o["date"].toordinal()))
    session = finish(best, best["band"])
    d1 = best["date"]

    # --- why this date ---------------------------------------------------------------------------------
    why = []
    if trained_today:
        why.append(item("date_trained_today", {}, cfg))
    if rest_today:
        why.append(item("date_checkin_rest", {"score": score_today}, cfg))
    today_opt = next((o for o in opts if o["date"] == today), None)
    if today_opt and d1 > today and today_opt["band"] in BAND_FILL:
        why.append(item("date_checkin_low", {"score": score_today, "k": len(today_opt["chosen"])}, cfg))
    n_ready = sum(1 for it in session["exercises"] if it["status"] == "ready")
    why.append(item("date_recovery", {"k": n_ready, "total": len(session["exercises"]), "on_date": d1.isoformat()}, cfg))
    used = {m for it in session["exercises"] for m in it["muscles"]}
    waiting = sorted(((v["ready_on"], m) for m, v in state.items() if m in used and (v.get("ready_on") or "") > earliest.isoformat()
                      and v["ready_on"] <= d1.isoformat()), reverse=True)
    if waiting and d1 > earliest:
        why.append(item("date_waited_for", {"muscle": waiting[0][1], "ready_date": waiting[0][0]}, cfg))
    brk = training_break(last_day, d1)
    if brk:
        brk["interp"] = item(f"break_{brk['tier']}", {"days": brk["days"], "weeks": brk["weeks"], "last_date": brk["last_date"],
                                                      "spw": spw}, cfg)
        why.append(item("date_after_break", {"days": brk["days"], "last_date": brk["last_date"]}, cfg))
    overdue = max(session["exercises"], key=lambda it: it.get("days_since_target") or 0)
    if not brk and (overdue.get("days_since_target") or 0) > 2 * due_interval(spw, structure, real_spw):
        why.append(item("date_overdue", {"muscle": next(c["due_muscle"] for c in best["chosen"] if c["name"] == overdue["name"]),
                                         "days": overdue["days_since_target"]}, cfg))
    if not brk:                                    # after a break the weekly rhythm is no argument for the date
        why.append(item("date_cadence", {"spw": spw, "ideal": round(ideal_gap, 1), "gap": best["gap_days"]}, cfg)
                   if best["gap_days"] is not None else item("date_first", {"spw": spw}, cfg))
    wk_done = week_counts.get(d1.isocalendar()[:2], 0)
    why.append(item("date_week", {"done": wk_done, "spw": spw, "nth": wk_done + 1,
                                  "week_date": (d1 - timedelta(days=d1.weekday())).isoformat()}, cfg))
    if best["last_chance"]:
        why.append(item("date_week_last_chance", {"spw": spw, "done": wk_done}, cfg))
    if best["habit"]:
        why.append(item("date_habit", {"weekday": d1.weekday()}, cfg))
    fuller = next((o for o in opts if o["date"] > d1 and o["fill"] >= best.get("fill_limited", best["fill"]) + 0.25), None)
    if fuller:
        session["fuller_option"] = item("date_fuller_later", {"later_date": fuller["date"].isoformat(), "k": len(fuller["chosen"]),
                                                              "k_now": len(best["chosen"])}, cfg)
    if best.get("manual"):
        why.insert(0, item("date_manual_groups", {"groups": chosen_groups}, cfg))
    session.update({"why_this_date": why, "days_from_today": (d1 - today).days, "gap_days": best["gap_days"],
                    "readiness_band_applied": best["band"]})
    budget = limiter_budget(session, work, catalog, ev, cfg)
    session["limiter_budget"] = budget
    session["aid_hints"] = aid_hints(session, budget, cfg, {c["name"]: c["aids_possible"] for c in pool})
    session["order_notes"] = order_notes(session, cfg)
    if transition_measured and transition > TRANSITION_NOTE_MIN and len(session["exercises"]) > 1:
        saving = round((transition - TRANSITION_TARGET_MIN) * (len(session["exercises"]) - 1))
        if cfg.get("partner"):                     # training in turns: the change-over is the partner's set - nothing to shorten
            alone = estimate_session_minutes(session["exercises"], TRANSITION_TARGET_MIN)
            session["time_note"] = item("time_partner", {"minutes": transition, "total": session["est_minutes"], "alone": alone}, cfg)
        else:
            session["time_note"] = item("time_transitions", {"minutes": transition, "enough": TRANSITION_TARGET_MIN, "saving": saving,
                                                             "total": session["est_minutes"]}, cfg)
    if age in MINOR_BANDS:
        session["guard"] = item("guard_youth", {"k": MINOR_MAX_EXERCISES}, cfg)
    elif age in OLDER_BANDS and spw < 2:           # holding muscle size needs about two exposures a week at 60+
        session["guard"] = item("guard_older_dose", {"spw": spw}, cfg)

    # --- the week: roll the muscle state forward, rotate the benchmark, plan on -------------------------------
    compact = lambda s: {"date": s["date"], "weekday": s["weekday"], "session_type": s["session_type"], "regions": s["regions"],
                         "est_minutes": s["est_minutes"], "benchmark": s["benchmark"],
                         "exercises": [it["name"] for it in s["exercises"]]}
    sessions, st8, prev, counts, pool_ = [session], state, d1, dict(week_counts), pool
    horizon = today + timedelta(days=HORIZON_DAYS)
    while True:
        cur = sessions[-1]
        st8 = apply_session(st8, prev, cur["exercises"], age)
        counts[prev.isocalendar()[:2]] = counts.get(prev.isocalendar()[:2], 0) + 1
        done = {it["name"] for it in cur["exercises"]}       # by then: measured fresh / no longer new
        pool_ = [dict(c, new=False if c["name"] in done else c["new"],
                      last_fresh=cur["date"] if c["name"] == cur["benchmark"] else c["last_fresh"]) for c in pool_]
        nxt = options(prev + timedelta(days=1), prev, st8, counts, pool_, False)
        if not nxt:
            break
        pick = max(nxt, key=lambda o: (o["score"], -o["date"].toordinal()))
        if pick["date"] > horizon:                 # the best next date lies beyond what the week plan shows
            break
        sessions.append(finish_session(pick, cfg, ev, commitment, None, age, transition, repeat_loss))
        prev = pick["date"]
    # no muscle may wait longer than about a week; one session a week and a muscle goal: say what that dose buys
    freq_notes = frequency_notes(state, sessions, pool, focus, horizon, cfg)
    dose_note = item("dose_one_session_growth", {"spw": spw}, cfg) if (spw == 1 and goal_effort(cfg.get("goal") or {}) == "deep") else None
    structure_note = (item("structure_split_too_rare", {"spw": spw, "limit": MAX_MUSCLE_GAP_DAYS}, cfg)
                      if (structure == "split" and spw < SPLIT_MIN_SESSIONS) else None)
    if structure_note is None and real_spw is not None:
        k_wish = split_factor(spw, structure)
        wait = round(7.0 * k_wish / max(real_spw, 0.1))
        if structure == "auto" and split_factor(spw, structure, real_spw) < k_wish:
            # planned as a split, lived as far fewer sessions: fuller sessions until the rhythm is back
            structure_note = item("structure_adapted_real", {"spw": spw, "real": real_spw, "days": wait, "limit": MAX_MUSCLE_GAP_DAYS}, cfg)
        elif structure == "split" and k_wish > 1 and wait > MAX_MUSCLE_GAP_DAYS:
            structure_note = item("structure_split_real_too_rare", {"real": real_spw, "days": wait, "limit": MAX_MUSCLE_GAP_DAYS}, cfg)
    # honest about the cadence: what the recovery rules allow may be less than what was asked for
    first = date.fromisoformat(sessions[0]["date"])
    in_7_days = sum(1 for x in sessions if (date.fromisoformat(x["date"]) - first).days < 7)
    cadence_note = None
    if in_7_days < spw and len(sessions) > 1:
        cadence_note = item("cadence_limited", {"spw": spw, "possible": in_7_days}, cfg)
    # readiness per region for every day of the horizon, as it is on the morning of that day
    strip, roll = [], state
    by_date = {s["date"]: s for s in sessions}
    for k in range(HORIZON_DAYS + 1):
        d = today + timedelta(days=k)
        iso = d.isoformat()
        regions = {}
        for r, ms in REGIONS.items():
            known = [m for m in ms if m in roll]
            late = [m for m in known if (roll[m].get("ready_on") or "") > iso]
            regions[r] = "recovering" if (known and len(late) == len(known)) else ("partial" if late else "ready")
        strip.append({"date": iso, "weekday": d.weekday(), "session": compact(by_date[iso]) if iso in by_date else None,
                      "readiness_by_region": regions})
        if iso in by_date:
            roll = apply_session(roll, d, by_date[iso]["exercises"], age)

    # standing in the gym anyway? the session that is possible TODAY (lighter after a poor check-in)
    today_session = None
    if today_opt and d1 > today:
        today_session = finish(today_opt, today_opt["band"])
        today_session["limiter_budget"] = limiter_budget(today_session, work, catalog, ev, cfg)
        today_session["order_notes"] = order_notes(today_session, cfg)
        today_session["note"] = item("today_anyway", {"k": len(today_session["exercises"]), "next_date": d1.isoformat()}, cfg)

    # "again, properly" (v0.11.0): the last training day left exercises without a stimulus (below the goal's force
    # drop) and they are back in a session within REPEAT_SOON_DAYS - a set without fatigue costs no rest
    goal_min = EFFORT_TARGETS[goal_effort(cfg.get("goal") or {})]["inroad_min"]
    last_occ = {e["name"]: (e.get("occ") or [None])[-1] for e in exercises}
    for sess in (session, today_session):
        if not sess or not last_day or (date.fromisoformat(sess["date"]) - last_day).days > REPEAT_SOON_DAYS:
            continue
        missed = [it["name"] for it in sess["exercises"]
                  if it["effort_target"]["label"] != "submax"                                    # meant to go easy: no miss
                  and planned_minima(cfg.get("_plan_ledger"), it["name"]).get(last_day.isoformat()) != 0
                  and (o := last_occ.get(it["name"])) and o["date"][:10] == last_day.isoformat() and o.get("inroad") is not None
                  and not o.get("effort_capped") and o["inroad"] < goal_min - BORDERLINE]
        if missed:
            sess["repeat_note"] = item("plan_repeat_after_miss", {"last_date": last_day.isoformat(), "k": len(missed), "exercises": missed}, cfg)

    return {
        "algo": PLAN_ALGO_VERSION,
        "today_session": today_session,
        "today": {"train_today": d1 == today, "trained_today": trained_today, "rest_today": rest_today,
                  "interp": item("today_train" if d1 == today else "today_rest",
                                 {"next_date": d1.isoformat(), "days": (d1 - today).days, "weekday": d1.weekday()}, cfg)},
        "profile": profile_out, "next_session": session, "week_plan": [compact(s) for s in sessions], "week_strip": strip,
        "cadence_note": cadence_note, "frequency_notes": freq_notes, "dose_note": dose_note, "structure_note": structure_note,
        "training_break": brk,
        "excluded": excluded,                      # exercises the athlete does not do on the ARX + what follows (None = none)
        "date_options": [{"date": o["date"].isoformat(), "weekday": o["date"].weekday(), "score": o["score"], "fill": o["fill_effective"],
                          "gap_days": o["gap_days"], "exercises": [c["name"] for c in o["chosen"]],
                          "limited": [c["name"] for c in o["chosen"] if c["status"] == "limited"]} for o in opts],
        # what a coach (the AI, v0.5.0) may change without breaking a rule: the feasible dates with everything
        # trainable on them, the bounds for targets / sets / rests and the effort cap. check_rows() enforces it.
        "decision_space": {
            "dates": [{"date": o["date"].isoformat(), "candidates": o["candidates"]} for o in opts[:DECISION_DATES]],
            "muscles": {c["name"]: c["muscles"] for c in pool},
            "targets_kg": {it["name"]: it["target_peak_kg"] for it in session["exercises"]},
            # after weeks away the old reference is an orientation: the coach may go further down (never further up)
            "target_floor_pct": {it["name"]: BREAK_TARGET_FLOOR_PCT for it in session["exercises"]
                                 if it["target_rule"] == "rebase_after_break"},
            # rows_max is what a session can hold at all; how many exercises the athlete's TIME holds is a preference,
            # not a rule: rows_in_time_budget + the price of one more (the coach names it instead of refusing)
            "bounds": {"target_pct": TARGET_LEEWAY_PCT, "sets_max": 1 if age in MINOR_BANDS else 2, "rest_max_min": REST_MAX_MIN,
                       "rows_min": min(2, len(session["exercises"])),
                       # ... and a time window for today is a wall the coach cannot move: what fits into it, not more
                       "rows_max": (max(len(session["exercises"]), min(size_for(None), window_size(window, set_min, transition)))
                                    if (window and d1 == today) else
                                    max(len(session["exercises"]), MINOR_MAX_EXERCISES if age in MINOR_BANDS else SESSION_MAX_EX))},
            "rows_in_time_budget": len(session["exercises"]), "minutes_per_extra_exercise": round(per_exercise, 1),
            "effort_cap": effort_for(cfg, commitment, best["band"], age)[0]["label"],
        },
    }


def frequency_notes(state: dict, sessions: list[dict], pool: list[dict], focus: dict, horizon: date, cfg: dict) -> list[dict]:
    """Body regions the athlete trains that would go longer than MAX_MUSCLE_GAP_DAYS without ANY
    stimulus (as a target or as a helper) in this plan - the usual causes: one session a week with
    more exercises than fit into it, a one-time group choice, a region that keeps losing its slot.
    Judged per muscle, reported per region (the worst muscle of the region gives the days)."""
    trained = _trained_muscles(pool, focus, set(cfg.get("_external") or []))     # not: muscles trained outside the ARX
    worst: dict = {}
    first = sessions[0]["date"] if sessions else None
    for m in sorted(trained):
        last = (state.get(m) or {}).get("last_target")
        if not last:
            continue
        stimuli = [last] + [s_["date"] for s_ in sessions if any(m in it["muscles"] for it in s_["exercises"])]
        gaps = []
        for a, b in zip(stimuli, stimuli[1:]):
            if b == first:                          # overdue, but the plan takes it at the first chance: that is
                continue                            # said under "why this date", not counted against the plan
            gaps.append(((date.fromisoformat(b) - date.fromisoformat(a)).days, True))
        gaps.append(((horizon - date.fromisoformat(stimuli[-1])).days, False))      # and nothing after the last one?
        gap, planned = max(gaps)
        if gap > MAX_MUSCLE_GAP_DAYS and gap > worst.get(REGION_OF[m], (0,))[0]:
            worst[REGION_OF[m]] = (gap, m, planned)
    return [item("freq_gap" if planned else "freq_gap_open", {"region": r, "days": gap, "limit": MAX_MUSCLE_GAP_DAYS}, cfg,
                 region=r, muscle=m, days=gap)
            for r, (gap, m, planned) in sorted(worst.items(), key=lambda kv: (-kv[1][0], kv[0]))[:3]]


def order_notes(session: dict, cfg: dict) -> list[dict]:
    """The order decisions worth telling: which exercise keeps its muscles fresh and what the
    shared-muscle pairs are expected to cost in the chosen order."""
    notes = []
    if session.get("benchmark"):
        notes.append(item("order_benchmark", {"exercise": session["benchmark"]}, cfg))
    for it in session["exercises"]:
        for src in it["evidence"]:
            if src["expected_loss_pct"] >= CONTEXT_LOSS_MIN_PCT:
                notes.append(item("order_pair_measured" if src["source"] == "measured" else "order_pair_prior",
                                  {"first": src["before"], "second": it["name"], "loss_pct": src["expected_loss_pct"], "n": src["n"]},
                                  cfg, confidence=src["confidence"]))
    return notes


def commitment_options(cfg: dict, minutes: float, spw: int, set_min: float, transition: float, per_exercise: float,
                       regions: int, age: str | None) -> list[dict]:
    """The time-vs-effort chooser: every option with its price in minutes per week."""
    out = []
    for key, c in COMMITMENT.items():
        size = session_size(minutes, per_exercise, regions, key, None, age)
        sets = size + min(2, size) * c["extra_sets"]
        per_session = sets * set_min + (sets - size) * REST_MIN["compound"] + (size - 1) * transition
        effort = "moderate" if c["effort"] == "moderate" else goal_effort(cfg.get("goal") or {})
        if age in MINOR_BANDS and effort == "deep":
            effort = "moderate"
        out.append({"key": key, "exercises": size, "sets": sets, "minutes_per_session": round(per_session),
                    "minutes_per_week": round(per_session * spw), "effort": effort, "progression": c["step"],
                    "interp": item(f"commitment_{key}", {"minutes": round(per_session * spw), "effort_pct": EFFORT_TARGETS[effort]["inroad_min"],
                                                        "sets": sets, "spw": spw}, cfg)})
    return out


# =============================================================================
# The rule set a plan has to pass (the engine's own plan does by construction; the AI's must too)
# =============================================================================
def check_plan(session: dict, pool: list[dict], state: dict, today: date, effort_cap: str = "deep") -> list[dict]:
    """[{code, exercise, detail}] - empty when the plan is valid."""
    problems = []
    by_name = {c["name"]: c for c in pool}
    day = date.fromisoformat(session["date"])
    if day < today:
        problems.append({"code": "date_in_the_past", "exercise": None, "detail": session["date"]})
    seen = set()
    for it in session["exercises"]:
        c = by_name.get(it["name"])
        if not c:
            problems.append({"code": "unknown_exercise", "exercise": it["name"]})
            continue
        if it["name"] in seen:
            problems.append({"code": "duplicate_exercise", "exercise": it["name"]})
        seen.add(it["name"])
        label = (it.get("effort_target") or {}).get("label", "deep")
        status, behind = exercise_status(c, state, day, today)
        if c["restriction"] == "avoid":
            problems.append({"code": "avoided_exercise", "exercise": it["name"]})
        if status == "not_ready":
            problems.append({"code": "muscle_not_ready", "exercise": it["name"], "detail": behind})
        if status == "limited" and label != "submax":
            problems.append({"code": "limited_needs_submax", "exercise": it["name"], "detail": behind})
        if c["restriction"] == "careful" and label != "submax":
            problems.append({"code": "careful_needs_submax", "exercise": it["name"]})
        if c["new"] and label != "submax":
            problems.append({"code": "new_needs_submax", "exercise": it["name"]})
        if EFFORT_RANK.get(label, 3) > EFFORT_RANK.get(effort_cap, 3):
            problems.append({"code": "effort_above_cap", "exercise": it["name"], "detail": effort_cap})
    seq = [by_name[it["name"]] for it in session["exercises"] if it["name"] in by_name]
    for i, a in enumerate(seq):
        for b in seq[i + 1:]:
            if means_before_target(a, b):
                problems.append({"code": "target_before_its_means", "exercise": a["name"], "detail": b["name"]})
    return problems


def check_rows(day: str, rows: list[dict], space: dict) -> list[dict]:
    """The same rules as check_plan(), on the serialised decision space - for a plan that comes from
    outside the engine (the AI coach). rows = [{exercise, sets, target_kg (None = no number), effort,
    rest_before_min}]. -> [{code, exercise, detail}], empty when the plan is valid."""
    problems = []
    option = next((d for d in (space or {}).get("dates", []) if d["date"] == day), None)
    if not option:
        return [{"code": "date_not_feasible", "exercise": None, "detail": day}]
    cands = {c["name"].lower(): c for c in option["candidates"]}
    bounds, muscles = space["bounds"], space.get("muscles", {})
    cap = EFFORT_RANK.get(space.get("effort_cap"), 3)
    if not (bounds["rows_min"] <= len(rows) <= bounds["rows_max"]):
        problems.append({"code": "row_count", "exercise": None, "detail": f"{bounds['rows_min']}-{bounds['rows_max']}"})
    seen, seq = set(), []
    for r in rows:
        name = str(r.get("exercise") or "")
        c = cands.get(name.lower())
        if not c:
            problems.append({"code": "not_trainable_that_day", "exercise": name})
            continue
        if c["name"] in seen:
            problems.append({"code": "duplicate_exercise", "exercise": c["name"]})
        seen.add(c["name"])
        seq.append(c["name"])
        effort, target = r.get("effort"), r.get("target_kg")
        gentle = c["status"] == "limited" or c["restriction"] == "careful" or c["new"]
        if gentle and (effort != "submax" or target):
            problems.append({"code": "needs_submax", "exercise": c["name"], "detail": "limited" if c["status"] == "limited" else ("careful" if c["restriction"] == "careful" else "new")})
        if effort not in EFFORT_TARGETS:
            problems.append({"code": "unknown_effort", "exercise": c["name"], "detail": str(effort)})
        elif EFFORT_RANK[effort] > cap:
            problems.append({"code": "effort_above_cap", "exercise": c["name"], "detail": space.get("effort_cap")})
        base = (space.get("targets_kg") or {}).get(c["name"])
        if target and not gentle:
            floor = (space.get("target_floor_pct") or {}).get(c["name"], bounds["target_pct"])
            if base and not (-floor - 0.05 <= (target / base - 1) * 100 <= bounds["target_pct"] + 0.05):
                problems.append({"code": "target_out_of_bounds", "exercise": c["name"],
                                 "detail": f"{base} -{floor} / +{bounds['target_pct']} %"})
            if not base:
                problems.append({"code": "target_without_reference", "exercise": c["name"]})
        if not (1 <= int(r.get("sets") or 0) <= bounds["sets_max"]):
            problems.append({"code": "sets_out_of_bounds", "exercise": c["name"], "detail": f"1-{bounds['sets_max']}"})
        if not (0 <= float(r.get("rest_before_min") or 0) <= bounds["rest_max_min"]):
            problems.append({"code": "rest_out_of_bounds", "exercise": c["name"], "detail": f"0-{bounds['rest_max_min']}"})
    for i, a in enumerate(seq):
        for b in seq[i + 1:]:
            if means_before_target({"muscles": muscles.get(a, {})}, {"muscles": muscles.get(b, {})}):
                problems.append({"code": "target_before_its_means", "exercise": a, "detail": b})
    return problems


# =============================================================================
# Legacy views (the v0.3 UI blocks and the v1 AI payload keep working in 0.4.0)
# =============================================================================
LEGACY_RULE = {"step": "trend_up_room", "hold_reach_effort": "hold_reach_inroad", "hold": "hold_reach_inroad",
               "retest_fresh": "hold_reach_inroad", "plateau_add_set": "hold_reach_inroad", "plateau_hold": "hold_reach_inroad",
               "light_day": "sub_max_careful", "new_exercise": "sub_max_careful", "return_after_break": "sub_max_careful",
               "rebase_after_break": "hold_reach_inroad"}


def legacy_session_plan(plan: dict, exercises: list[dict], recovery: dict) -> list[dict]:
    """The old session_plan list, derived from the ONE plan (same order, same exercises)."""
    rec = {r["name"]: r for r in (recovery or {}).get("exercises") or []}
    by_name = {e["name"]: e for e in exercises}
    out = []
    for it in ((plan or {}).get("next_session") or {}).get("exercises", []):
        e, rs = by_name.get(it["name"], {}), rec.get(it["name"], {})
        out.append({"name": it["name"], "group": it["group"], "last": e.get("last"), "restriction": it["restriction"],
                    "target": "gentle" if it["effort_target"]["label"] == "submax" else "max",
                    "targets": e.get("targets", sorted(m for m, r in it["muscles"].items() if r == "target")),
                    "limiters": e.get("limiters_eff", sorted(m for m, r in it["muscles"].items() if r == "limiter")),
                    "status": it["status"], "ready_on": rs.get("ready_on"), "fresh_on": rs.get("fresh_on"),
                    "limited_by": it["limited_by"], "readiness_reason": rs.get("reason", ""),
                    "order": it["order"], "planned_date": plan["next_session"]["date"], "target_peak_kg": it["target_peak_kg"],
                    "rest_before_min": it["rest_before_min"], "new": it["new"]})
    return out


def apply_to_coach(coach: dict, plan: dict) -> None:
    """Make the whiteboard targets agree with the plan for the exercises it contains (in place)."""
    rows = {r["name"]: r for r in (coach or {}).get("exercises", [])}
    for it in ((plan or {}).get("next_session") or {}).get("exercises", []):
        r = rows.get(it["name"])
        if not r or it["target_rule"] in ("sub_max_careful", "sub_max_limiter"):
            continue
        r["target_rule"] = LEGACY_RULE.get(it["target_rule"], r["target_rule"])
        r["target_peak_kg"] = it["target_peak_kg"]
        if it.get("base_kg"):
            r["base_kg"], r["base_date"] = it["base_kg"], it["base_date"]


# =============================================================================
# Plan ledger: what was recommended, and what was done with it
# =============================================================================
def ledger_entry(plan: dict, today: date) -> dict | None:
    """The part of a plan worth remembering (no names of people - exercises and numbers only)."""
    s = (plan or {}).get("next_session")
    if not s:
        return None
    return {"created": today.isoformat(), "date": s["date"], "session_type": s["session_type"], "est_minutes": s["est_minutes"],
            "benchmark": s["benchmark"], "commitment": plan["profile"]["commitment"],
            "exercises": [{"name": it["name"], "order": it["order"], "sets": it["sets"], "target_peak_kg": it["target_peak_kg"],
                           "target_rule": it["target_rule"], "effort": it["effort_target"]["label"],
                           "inroad_min": it["effort_target"]["inroad_min"], "rest_before_min": it["rest_before_min"],
                           "aid_hint": it["aid_hint"], "settings": it.get("settings")} for it in s["exercises"]]}


def ledger_signature(entry: dict) -> str:
    """Two plans with the same date, exercises, order and targets are the same recommendation."""
    return "|".join([entry["date"]] + [f"{x['name']}:{x['sets']}:{x['target_peak_kg']}:{x['effort']}" for x in entry["exercises"]])


def update_ledger(entries: list[dict], plan: dict, today: date) -> tuple[list[dict], bool]:
    """Append the plan when it differs from the latest one made today (-> replaces it) or earlier."""
    new = ledger_entry(plan, today)
    entries = list(entries or [])
    if not new:
        return entries, False
    if entries and ledger_signature(entries[-1]) == ledger_signature(new):
        return entries, False
    if entries and entries[-1]["created"] == new["created"]:
        entries[-1] = new                          # the same day's plan changed (check-in, settings): keep the latest
    else:
        entries.append(new)
    return entries[-LEDGER_MAX:], True


def plan_vs_actual(entries: list[dict], last_session: dict | None, cfg: dict) -> dict | None:
    """What the athlete did with the latest plan made BEFORE the last training day."""
    if not last_session or not last_session.get("date"):
        return None
    day = last_session["date"]
    earlier = [e for e in (entries or []) if e["created"] < day]
    if not earlier:
        return None
    plan = earlier[-1]
    done = [x["name"] for x in last_session.get("exercises", [])]
    planned = [x["name"] for x in plan["exercises"]]
    both = [n for n in planned if n in done]
    pairs = [(a, b) for i, a in enumerate(both) for b in both[i + 1:]]
    same_order = sum(1 for a, b in pairs if done.index(a) < done.index(b))
    actual = {x["name"]: x for x in last_session.get("exercises", [])}
    rows, hits, judged, effort_hits, effort_judged = [], 0, 0, 0, 0
    for p in plan["exercises"]:
        a = actual.get(p["name"])
        row = {"name": p["name"], "done": bool(a), "target_peak_kg": p["target_peak_kg"], "actual_peak_kg": (a or {}).get("max_kg"),
               "target_met": None, "inroad_min": p["inroad_min"], "inroad": (a or {}).get("inroad"), "effort_met": None,
               "rest_planned_min": p["rest_before_min"], "rest_actual_min": (a or {}).get("rest_before_min")}
        if a and p["target_peak_kg"] and a.get("max_kg"):
            row["target_met"] = a["max_kg"] >= TARGET_HIT_SHARE * p["target_peak_kg"]
            judged += 1
            hits += 1 if row["target_met"] else 0
        if a and p["inroad_min"] and a.get("inroad") is not None:
            row["effort_met"] = a["inroad"] >= p["inroad_min"]
            effort_judged += 1
            effort_hits += 1 if row["effort_met"] else 0
        rows.append(row)
    out = {"plan_created": plan["created"], "planned_date": plan["date"], "actual_date": day,
           "date_delta_days": (date.fromisoformat(day) - date.fromisoformat(plan["date"])).days,
           "done": both, "skipped": [n for n in planned if n not in done], "added": [n for n in done if n not in planned],
           "order_agreement": round(same_order / len(pairs), 2) if pairs else None,
           "targets_met": hits, "targets_judged": judged, "effort_met": effort_hits, "effort_judged": effort_judged,
           "exercises": rows}
    code = "pva_followed" if (len(both) == len(planned) and not out["added"]) else ("pva_partly" if both else "pva_other")
    out["interp"] = item(code, {"k": len(both), "total": len(planned), "skipped": out["skipped"], "added": out["added"],
                                "hits": hits, "judged": judged, "effort_hits": effort_hits, "effort_judged": effort_judged,
                                "delta_days": out["date_delta_days"]}, cfg)
    return out
