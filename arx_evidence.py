#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARX Insight - evidence: what THIS athlete's own data says about order, rest, limiters and
recovery (v0.4.0).

A human trainer orders exercises by rules of thumb ("legs first, then arms"). This module measures
instead: every working set gets its CONTEXT (was a muscle it needs already loaded in this visit?),
and a set done in a pre-loaded context is compared with the athlete's own fresh performance of the
same exercise around that date. From these comparisons come the effects the planner and the coach
use - each with n, the observations behind it, and an honest confidence label.

Principles (hard-won, see tasks/lessons.md):
  * Progress and effects are only judged on COMPARABLE sets: same range of motion (+-5 %), same
    tempo (+-15 %), same protocol class, not a familiarisation day, not a low-force set.
  * With few observations a prior from the exercise catalog (shared targets / limiters) carries
    the estimate; observations pull it their way as n grows (shrinkage). n is always shown.
  * Observations are reported as measured. They are never extrapolated with an assumed decay:
    normalising a 16 % loss at 16 minutes to "gap 0" with a 5-minute half-life yields 160 %.
  * Generic for every limiter. There is no grip special case - only the per-exercise AIDS setting
    (lifting hooks / straps), which removes the grip as a limiter of that exercise from the day
    the athlete started using them.

Leaf module apart from arx_base. Public domain / CC0. No warranty. Not medical advice.
"""

from __future__ import annotations
import random, statistics as st
from datetime import date

from arx_base import _ts

VISIT_GAP_MIN = 60             # a longer gap between sets starts a new visit (same as the sequences)
ROLE_WEIGHT = {"target": 1.0, "limiter": 0.5}    # how much of a set's work lands on a muscle
AID_REMOVES = {"hooks": ["grip"], "straps": ["grip"]}   # which limiter an aid takes out of the game

# Priors for "exercise a before exercise b costs b x % of its force", from catalog overlap.
# Additive, capped. They are deliberately modest: the data should win quickly.
PRIOR_SHARED_TARGET = 8.0      # both work the same muscle as a target
PRIOR_TARGET_IS_LIMITER = 10.0 # a's target is what gives out first in b (curl before row)
PRIOR_SHARED_LIMITER = 4.0     # both hang on the same limiter (grip on dead lift and row)
PRIOR_LIMITER_IS_TARGET = 3.0  # a's limiter is b's target
PRIOR_CAP = 20.0
SHRINK_K = 3                   # prior counts like this many observations
POSITION_PRIOR_PCT = -1.0      # general loss per position in the session when nothing is shared
RECOVERY_BANDS = ((1, 2), (3, 4), (5, 7), (8, 999))     # days since the muscle's last hard load
REST_EFFECT_MIN_N = 6          # observations before a rest effect is even looked for ...
REST_EFFECT_SHUFFLES, REST_EFFECT_P = 500, 0.10   # ... and it has to beat shuffled data (p <= 0.10)


def confidence(n: int, agree: float = 1.0) -> str:
    """How much an aggregate deserves to be believed. agree = share of observations with the same
    sign as their mean."""
    if n < 3:
        return "anecdotal"
    if n < 6:
        return "low"
    if n < 12:
        return "medium" if agree >= 0.75 else "low"
    return "high" if agree >= 0.75 else "medium"


# =============================================================================
# Aids and effective limiters
# =============================================================================
def effective_limiters(meta: dict, exercise, day: str, aids: dict | None) -> list[str]:
    """The limiters of an exercise on a given day, minus those an aid takes out. aids is the
    athlete's setting {exercise code: {"aids": ["hooks"], "since": "YYYY-MM-DD"}}; sets before
    'since' were done without the aid and keep the limiter."""
    limiters = list(meta.get("limiters") or [])
    cfg = (aids or {}).get(str(exercise)) or {}
    if not cfg or (cfg.get("since") and str(day)[:10] < str(cfg["since"])[:10]):
        return limiters
    removed = {m for a in (cfg.get("aids") or []) for m in AID_REMOVES.get(a, [])}
    return [l for l in limiters if l not in removed]


def set_muscles(s: dict, catalog: dict, aids: dict | None = None) -> dict:
    """{muscle: role} of one set: targets, and the limiters that are not targets and not removed by
    an aid. An exercise without catalog targets loads its group name as a pseudo-muscle."""
    meta = catalog.get(str(s["exercise"]), {})
    targets = list(meta.get("targets") or []) or [s.get("group") or "?"]
    out = {m: "target" for m in targets}
    for m in effective_limiters(meta, s["exercise"], s["date"], aids):
        out.setdefault(m, "limiter")
    return out


# =============================================================================
# Context of every set within its visit
# =============================================================================
def annotate_context(work: list[dict], catalog: dict, aids: dict | None = None) -> None:
    """Mark every working set with what came before it in the same visit (in place):

      visit            running number of the visit within its day
      position         position within the visit (1 = first set)
      context          fresh      no earlier set of the visit loaded any muscle this set needs
                       repeat     the same exercise was already done in this visit
                       preloaded  another exercise loaded a muscle this set needs
      preload          {muscle: role-weighted impulse (kg*s) already put on it in this visit}
      preloaded_by     [{exercise, muscles, minutes}] most recent first - minutes from the END of
                       that set to the START of this one
    """
    by_day: dict = {}
    for s in work:
        by_day.setdefault(s["date"][:10], []).append(s)
    for day_sets in by_day.values():
        day_sets.sort(key=lambda x: x["date"])
        visit, position, prev_end, earlier = 1, 0, None, []
        for s in day_sets:
            start = _ts(s["date"])
            if prev_end is not None and start is not None and (start - prev_end) / 60.0 > VISIT_GAP_MIN:
                visit, position, earlier = visit + 1, 0, []
            position += 1
            mine = set_muscles(s, catalog, aids)
            preload: dict = {}
            by = []
            for e in reversed(earlier):
                shared = sorted(set(mine) & set(e["muscles"]))
                for m in shared:
                    preload[m] = preload.get(m, 0.0) + ROLE_WEIGHT[e["muscles"][m]] * e["impulse"]
                if shared:
                    gap = round((start - e["end"]) / 60.0, 1) if (start is not None and e["end"] is not None) else None
                    by.append({"exercise": e["name"], "code": e["exercise"], "muscles": shared, "minutes": gap})
            repeat = any(e["exercise"] == s["exercise"] for e in earlier)
            s["visit"], s["position"] = visit, position
            s["context"] = "repeat" if repeat else ("preloaded" if by else "fresh")
            s["preload"] = {m: round(v) for m, v in preload.items()}
            s["preloaded_by"] = by
            end = (start + s["seconds"]) if start is not None else None
            earlier.append({"exercise": s["exercise"], "name": s.get("name", str(s["exercise"])), "muscles": mine,
                            "impulse": s.get("impulse_kg_s") or 0, "end": end})
            prev_end = end if end is not None else prev_end


# =============================================================================
# Relative performance against the athlete's own fresh baseline
# =============================================================================
def strength(s: dict) -> float | None:
    """The day-independent strength reading of one set: mean of the phase means over the whole
    set (kg). Unlike the peak (an eccentric spike) or the mean force (which jumps with the pause
    settings) it moves with pre-fatigue and with real strength only."""
    return s.get("mov_kg")


def _baseline(s: dict, fresh_first: list[dict]) -> tuple[float | None, str]:
    """Fresh, comparable first sets of the same exercise bracketing this set in time."""
    earlier = [x for x in fresh_first if x["date"] < s["date"] and x["id"] != s["id"]]
    later = [x for x in fresh_first if x["date"] > s["date"] and x["id"] != s["id"]]
    a = strength(earlier[-1]) if earlier else None
    b = strength(later[0]) if later else None
    if a and b:
        return (a + b) / 2.0, "two_sided"
    if a or b:
        return (a or b), "one_sided"
    return None, "none"


def relative_performance(work: list[dict]) -> None:
    """s["rp_pct"] = strength of a NON-fresh comparable set relative to the athlete's own fresh
    baseline of that exercise (in place; None when there is nothing to compare with)."""
    by_ex: dict = {}
    for s in sorted(work, key=lambda x: x["date"]):
        by_ex.setdefault(s["exercise"], []).append(s)
    for ss in by_ex.values():
        fresh_first = [x for x in ss if x.get("comparable") and x.get("context") == "fresh" and strength(x)]
        for s in ss:
            s["rp_pct"], s["rp_baseline"] = None, "none"
            if not s.get("comparable") or not strength(s) or s.get("context") == "fresh":
                continue
            base, kind = _baseline(s, fresh_first)
            if base:
                s["rp_pct"], s["rp_baseline"] = round((strength(s) / base - 1) * 100, 1), kind


# =============================================================================
# Aggregations
# =============================================================================
def pair_prior(a_muscles: dict, b_muscles: dict) -> float:
    """Expected loss (%) of exercise b when a was done before it, from the catalog alone."""
    loss = 0.0
    for m in set(a_muscles) & set(b_muscles):
        ra, rb = a_muscles[m], b_muscles[m]
        loss += {("target", "target"): PRIOR_SHARED_TARGET, ("target", "limiter"): PRIOR_TARGET_IS_LIMITER,
                 ("limiter", "limiter"): PRIOR_SHARED_LIMITER, ("limiter", "target"): PRIOR_LIMITER_IS_TARGET}[(ra, rb)]
    return min(loss, PRIOR_CAP)


def shrink(observed: list[float], prior: float) -> float:
    """Estimate between the prior and the observed mean, weighted by n (SHRINK_K = weight of the prior)."""
    n = len(observed)
    return (sum(observed) + SHRINK_K * prior) / (n + SHRINK_K) if n else prior


def _theil_sen(xs: list[float], ys: list[float]) -> float | None:
    slopes = [(ys[j] - ys[i]) / (xs[j] - xs[i]) for i in range(len(xs)) for j in range(i + 1, len(xs)) if xs[j] != xs[i]]
    return st.median(slopes) if slopes else None


def rest_effect_from(observations: list[tuple]) -> dict:
    """Does more rest after the pre-loading set cost less? observations = [(minutes, loss %, the
    pair's prior %)]. Pairs differ in what they share, so every loss is taken relative to its
    pair's prior (1.0 = as expected) before minutes are compared. "Detected" needs
    REST_EFFECT_MIN_N observations AND has to beat shuffled data: with four mixed observations
    almost any slope appears by chance - and the planner must never act on an assumed decay."""
    obs = [(m, l, round(l / p, 3)) for m, l, p in observations if m is not None and p and p > 0]
    slope, p_value = None, None
    if len(obs) >= REST_EFFECT_MIN_N:
        xs, ys = [m for m, _, _ in obs], [r for _, _, r in obs]
        slope = _theil_sen(xs, ys)
        if slope is not None and slope < 0:
            rng = random.Random(len(obs))                   # deterministic
            worse = 0
            for _ in range(REST_EFFECT_SHUFFLES):
                shuffled = ys[:]
                rng.shuffle(shuffled)
                worse += 1 if (_theil_sen(xs, shuffled) or 0.0) <= slope else 0
            p_value = worse / REST_EFFECT_SHUFFLES
    detected = p_value is not None and p_value <= REST_EFFECT_P
    return {"n": len(obs), "needs_n": REST_EFFECT_MIN_N, "status": "detected" if detected else "not_detectable",
            "p_shuffle": p_value,
            # share of a pair's expected loss that one more minute of rest takes away (negative)
            "loss_share_change_per_min": round(slope, 3) if detected else None,
            "reference_minutes": round(st.median(m for m, _, _ in obs), 1) if detected else None,
            "observations": [{"minutes": m, "loss_pct": l, "vs_prior": r} for m, l, r in sorted(obs)]}


def build_evidence(work: list[dict], catalog: dict, aids: dict | None = None) -> dict:
    """The athlete's measured effects (see the module docstring). Needs annotate_context() and the
    per-set 'comparable' flag (arx_report._exercise_series) to have run; calls
    relative_performance() itself.

    pair_effects      a before b: observations[{date, loss_pct, minutes}], observed mean, prior,
                      shrunk estimate, n, confidence - only pairs where a is the MOST RECENT
                      earlier set sharing a muscle with b (the clean cases)
    repeat_effects    per exercise: what a SECOND set of the same exercise in a visit delivered
                      relative to the fresh baseline (its loss mixes the own first set and whatever
                      came in between - so it is reported on its own, never as an order effect)
    limiter_effects   per muscle: losses of sets whose only shared muscle with the earlier set was
                      this one, as a limiter on at least one side
    position_effect   fresh sets only: % per position (Theil-Sen), prior when n is small
    rest_effect       loss vs minutes since the pre-loading set; 'not_detectable' unless the
                      relation is consistent
    recovery_response per muscle: strength of fresh first sets vs days since the muscle's last
                      moderate-or-deep target load, in bands
    never_fresh       exercises that were never measured in a fresh context
    """
    relative_performance(work)
    names = {s["exercise"]: s.get("name", str(s["exercise"])) for s in work}
    muscles_of = {s["exercise"]: set_muscles(s, catalog, aids) for s in work}

    # --- pair effects -------------------------------------------------------------------------------
    pairs: dict = {}
    for s in work:
        if s.get("rp_pct") is None or s.get("context") != "preloaded" or not s.get("preloaded_by"):
            continue
        last = s["preloaded_by"][0]
        key = (last["code"], s["exercise"])
        pairs.setdefault(key, []).append({"date": s["date"][:10], "loss_pct": round(-s["rp_pct"], 1),
                                          "minutes": last["minutes"], "shared": last["muscles"],
                                          "baseline": s["rp_baseline"]})
    pair_effects = []
    for (a, b), obs in sorted(pairs.items(), key=lambda kv: -len(kv[1])):
        losses = [o["loss_pct"] for o in obs]
        prior = pair_prior(muscles_of.get(a, {}), muscles_of.get(b, {}))
        mean = st.mean(losses)
        agree = sum(1 for x in losses if (x >= 0) == (mean >= 0)) / len(losses)
        pair_effects.append({"id": f"order:{names.get(b)}|after:{names.get(a)}", "before": names.get(a), "then": names.get(b),
                             "n": len(losses), "observed_loss_pct": round(mean, 1), "prior_loss_pct": prior,
                             "loss_pct": round(shrink(losses, prior), 1), "confidence": confidence(len(losses), agree),
                             "observations": obs})

    # --- repeat effects: the same exercise again within one visit -------------------------------------------
    reps_by_ex: dict = {}
    for s in work:
        if s.get("rp_pct") is not None and s.get("context") == "repeat":
            same = next((b for b in s.get("preloaded_by", []) if b["code"] == s["exercise"]), None)
            reps_by_ex.setdefault(s["exercise"], []).append(
                {"date": s["date"][:10], "loss_pct": round(-s["rp_pct"], 1), "minutes_since_first": same["minutes"] if same else None,
                 "in_between": [b["exercise"] for b in s.get("preloaded_by", []) if b["code"] != s["exercise"]]})
    repeat_effects = [{"id": f"repeat:{names.get(e)}", "exercise": names.get(e), "n": len(obs),
                       "observed_loss_pct": round(st.mean(o["loss_pct"] for o in obs), 1),
                       "confidence": confidence(len(obs)), "observations": obs}
                      for e, obs in sorted(reps_by_ex.items(), key=lambda kv: -len(kv[1]))]

    # --- limiter effects: pairs that share exactly one muscle, a limiter on at least one side -------------
    lim: dict = {}
    for (a, b), obs in pairs.items():
        shared = set(muscles_of.get(a, {})) & set(muscles_of.get(b, {}))
        if len(shared) != 1:
            continue
        m = next(iter(shared))
        if "limiter" in (muscles_of[a][m], muscles_of[b][m]):
            lim.setdefault(m, []).extend(o["loss_pct"] for o in obs)
    limiter_effects = {m: {"n": len(v), "observed_loss_pct": round(st.mean(v), 1),
                           "loss_pct": round(shrink(v, PRIOR_SHARED_LIMITER), 1), "confidence": confidence(len(v))}
                       for m, v in lim.items()}

    # --- position effect on fresh sets ---------------------------------------------------------------
    pos_obs = []
    by_ex: dict = {}
    for s in work:
        if s.get("comparable") and s.get("context") == "fresh" and strength(s):
            by_ex.setdefault(s["exercise"], []).append(s)
    for ss in by_ex.values():
        if len(ss) < 2:
            continue
        ref = st.median(strength(x) for x in ss)
        pos_obs += [(x["position"], (strength(x) / ref - 1) * 100) for x in ss]
    slope = _theil_sen([p for p, _ in pos_obs], [v for _, v in pos_obs]) if len({p for p, _ in pos_obs}) >= 2 else None
    n_pos = len(pos_obs)
    position_effect = {"n": n_pos, "observed_pct_per_position": round(slope, 1) if slope is not None else None,
                       "pct_per_position": round((n_pos * slope + SHRINK_K * 2 * POSITION_PRIOR_PCT) / (n_pos + SHRINK_K * 2), 1)
                                           if slope is not None else POSITION_PRIOR_PCT,
                       "confidence": confidence(n_pos) if slope is not None else "anecdotal"}

    # --- rest effect: does more rest after the pre-loading set cost less? --------------------------------
    rest_obs = []
    for (a, b), obs in pairs.items():
        prior = pair_prior(muscles_of.get(a, {}), muscles_of.get(b, {}))
        rest_obs += [(o["minutes"], o["loss_pct"], prior) for o in obs if o["minutes"] is not None]
    rest_effect = rest_effect_from(rest_obs)

    # --- recovery response per muscle ---------------------------------------------------------------------
    hard_days: dict = {}                          # muscle -> ordinals of days with a moderate/deep TARGET load
    for s in work:
        if s.get("effort") in ("moderate", "deep"):
            for m, role in muscles_of[s["exercise"]].items():
                if role == "target":
                    hard_days.setdefault(m, set()).add(date.fromisoformat(s["date"][:10]).toordinal())
    rec: dict = {}
    for ss in by_ex.values():
        if len(ss) < 3:
            continue
        ref = st.median(strength(x) for x in ss)
        for x in ss:
            o = date.fromisoformat(x["date"][:10]).toordinal()
            for m, role in muscles_of[x["exercise"]].items():
                if role != "target":
                    continue
                prev = [d for d in hard_days.get(m, ()) if d < o]
                if prev:
                    rec.setdefault(m, []).append((o - max(prev), (strength(x) / ref - 1) * 100))
    recovery_response = {}
    for m, obs in rec.items():
        bands = []
        for lo, hi in RECOVERY_BANDS:
            vals = [v for d, v in obs if lo <= d <= hi]
            if vals:
                bands.append({"days": f"{lo}-{hi}" if hi < 999 else f"{lo}+", "n": len(vals),
                              "strength_vs_own_median_pct": round(st.mean(vals), 1)})
        recovery_response[m] = {"n": len(obs), "bands": bands, "confidence": confidence(len(obs))}

    performed = {s["exercise"] for s in work}
    fresh_seen = {s["exercise"] for s in work if s.get("context") == "fresh"}
    return {
        "fresh_sets": sum(1 for s in work if s.get("context") == "fresh"), "total_sets": len(work),
        "pair_effects": pair_effects, "repeat_effects": repeat_effects, "limiter_effects": limiter_effects,
        "position_effect": position_effect,
        "rest_effect": rest_effect, "recovery_response": recovery_response,
        "never_fresh": sorted(names[e] for e in performed - fresh_seen),
    }
