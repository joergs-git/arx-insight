#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARX Insight - the AI coach (v0.5.0): a structured board, a memory, a live chat.

Engine computes, model judges. The engine's report (arx_report) holds every number; this module

  payload   turns it into a compact, name-free payload in the athlete's units (per-rep data of the
            last session, per-exercise series on comparable days, weekly windows, progress factors,
            the athlete's own measured effects with n, findings, plan vs actual, the planner's
            PROPOSAL and the DECISION SPACE around it, and what the coach recommended before),
  board     asks Claude for ONE structured answer (JSON schema: exercise names and dates are enums,
            so nothing can be invented) with a text for each report chapter - last session, next
            training, history - a focus, key recommendations and questions the athlete may ask,
  validate  checks the plan rows against the engine's rules (arx_plan.check_rows): feasible date,
            trainable exercises, sub-max where required, effort cap, target bounds, helper-before-
            target order; every change against the proposal needs a reason. One repair round, then
            the ENGINE's plan is used and the board says so - an invalid plan never reaches the athlete,
  memory    keeps the delivered boards (ai/boards.json); the next payload carries the last
            recommendations, so the coach stays consistent and names what it changes and why,
  jobs      runs the call in the background (the report never waits for the API),
  chat      a live conversation about this report: the payload and the delivered board are the
            cached prefix, answers stream into a buffer the phone can poll (survives a screen lock).

v0.7.0: the payload names the exercises the athlete switched off for this machine (profile:
exercises_switched_off with the reason trained_elsewhere | not_wanted, muscles_trained_elsewhere) -
they are not in the decision space, and rule 10 of the prompt keeps them out of the text as well.

Privacy: never a name, date of birth, height, weight or free-text note. Age band + sex and
relative body changes only behind their switches. ARX_AI_FAKE=<scenario> replaces the API with a
deterministic fake (tests, screenshots, demos - no key, no cost).

Imports arx_base and arx_plan only. Public domain / CC0. No warranty. Not medical advice.
"""

from __future__ import annotations
import hashlib, json, os, re, threading, time
from datetime import date

from arx_base import data_dir, write_json_atomic
import arx_plan as planner

PROMPT_VERSION = 16
MODELS = (("claude-opus-5", "Claude Opus 5"), ("claude-fable-5-1", "Claude Fable 5.1"))
DEFAULT_MODEL = "claude-opus-5"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
BOARD_EFFORT_DEFAULT, CHAT_EFFORT_DEFAULT = "high", "medium"
BOARD_MAX_TOKENS, CHAT_MAX_TOKENS = 32000, 8000
FALLBACK_BETA = "server-side-fallback-2026-07-01"     # fallbacks="default": a declined request is re-run server-side
REQUEST_TIMEOUT_S = 600.0

BOARDS_KEEP = 12               # delivered boards kept per athlete
MEMORY_BOARDS = 3              # how many earlier recommendations the next payload carries
# Guards against a runaway client, not a ration (owner, v0.8.2: 8 / 20 / 40 got in the way of real use). The daily
# counters start again at midnight (the PC's date); a conversation starts anew with every new board (new set,
# check-in or setting). A cached board costs nothing and is never counted.
BOARDS_PER_DAY = 100           # new boards per athlete and day
CHAT_TURNS_MAX = 100           # turns per conversation (one conversation per delivered board)
CHAT_TURNS_PER_DAY = 100
CHAT_QUESTION_MAX = 1000       # characters
SERIES_POINTS = 12             # comparable-day points per exercise in the payload
WEEKS_IN_PAYLOAD = 13
WHY_MIN_CHARS = 20             # a change against the proposal needs at least a sentence of reason

HERE = os.path.dirname(os.path.abspath(__file__))


class AIError(Exception):
    """A failed coach call with a short machine-readable code, so the UI can say what happened
    (and what to do) instead of showing the "API key missing" hint for every failure."""
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code, self.message = code, (message or code)

    def info(self) -> dict:
        return {"code": self.code, "message": self.message[:300]}


def classify_ai_error(exc: Exception) -> AIError:
    """Map an exception of the Anthropic SDK to an AIError code. Most specific class first
    (APITimeoutError is a subclass of APIConnectionError). Never raises itself."""
    if isinstance(exc, AIError):
        return exc
    msg = str(getattr(exc, "message", "") or exc)
    try:
        import anthropic
    except ImportError:
        return AIError("sdk_missing", msg)
    body = getattr(exc, "body", None)
    err = (body.get("error") if isinstance(body, dict) else None) or {}
    etype = err.get("type") if isinstance(err, dict) else None
    if isinstance(err, dict) and err.get("message"):
        msg = str(err["message"])                # the API's own sentence instead of the raw repr
    table = (("APITimeoutError", "timeout"), ("APIConnectionError", "network"),
             ("AuthenticationError", "invalid_key"), ("PermissionDeniedError", "forbidden"),
             ("NotFoundError", "model_unavailable"), ("RateLimitError", "rate_limit"),
             ("BadRequestError", "bad_request"), ("InternalServerError", "server_error"))
    if etype == "billing_error":
        return AIError("no_credit", msg)
    if etype == "overloaded_error" or getattr(exc, "status_code", None) == 529:
        return AIError("overloaded", msg)
    for cls_name, code in table:
        cls = getattr(anthropic, cls_name, None)
        if cls is not None and isinstance(exc, cls):
            return AIError(code, msg)
    if isinstance(exc, getattr(anthropic, "APIStatusError", ())):
        return AIError("server_error" if (getattr(exc, "status_code", 0) or 0) >= 500 else "api_error", msg)
    return AIError("unknown", msg)


# =============================================================================
# Settings
# =============================================================================
def model_of(cfg: dict) -> str:
    m = cfg.get("model")
    return m if m in dict(MODELS) else DEFAULT_MODEL


def effort_of(cfg: dict, key: str, default: str) -> str:
    """ai_effort_board / ai_effort_chat; the pre-0.5 setting 'ai_effort' still steers the board."""
    e = str(cfg.get(key) or (cfg.get("ai_effort") if key == "ai_effort_board" else "") or default).lower()
    return e if e in EFFORTS else default


def has_key(cfg: dict) -> bool:
    return bool(os.environ.get("ARX_AI_FAKE") or cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY"))


# =============================================================================
# Payload v2
# =============================================================================
def dumps_payload(obj) -> str:
    """Deterministic JSON: the same data always gives the same bytes (cache key, prompt cache)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(it) -> str | None:
    """The rendered 'meaning -> action' of an engine item (already in the athlete's language / units)."""
    if not it or not it.get("text"):
        return None
    t = it["text"]
    return (t.get("meaning", "") + (" -> " + t["action"] if t.get("action") else "")).strip() or None


def build_payload(report: dict, cfg: dict, previous: list[dict] | None = None) -> dict:
    """The name-free payload the coach sees, in the athlete's units (see the module docstring).
    Never in it: names, ids, date of birth, height, weight, free-text notes."""
    imp = cfg.get("units", "imperial") == "imperial"
    F = lambda v: None if v is None else round(v * 2.20462 if imp else v, 1)
    L = lambda v: None if v is None else round(v / 2.54 if imp else v, 1)
    today = report.get("today")
    plan = report.get("plan") or {}
    ns = plan.get("next_session")
    hist = report.get("history") or {}
    prof = report.get("profile") or {}
    pp = plan.get("profile") or {}

    profile = {
        "goal_mix": report.get("goal"), "primary_outcome": prof.get("outcome"), "experience": prof.get("experience"),
        "sessions_per_week_target": report.get("sessions_per_week"), "session_minutes": pp.get("session_minutes"),
        "commitment_profile": pp.get("commitment"), "commitment_chosen_by_athlete": pp.get("commitment_chosen"),
        "session_structure": pp.get("structure"), "focus_regions": {k: v for k, v in (pp.get("focus_regions") or {}).items() if v != "normal"},
        "restrictions": report.get("restrictions_saved") or {}, "pain_today": report.get("pain_today") or [],
        "grip_aids": {k: v.get("aids") for k, v in (report.get("aids") or {}).items()},
        "supervision_required_minor": bool(pp.get("supervision")),
        # training in turns with a partner: the change-over time in the data is the partner's set, not set-up time
        "takes_turns_with_partner": bool(pp.get("partner")),
        "minutes_per_exercise_at_own_pace": pp.get("per_exercise_min"), "exercises_by_minutes_per_session": pp.get("size_by_minutes"),
        # exercises the athlete does not do on this machine (own choice in the profile) - never to be recommended
        "exercises_switched_off": [{"exercise": x["name"], "reason": {"elsewhere": "trained_elsewhere", "injury": "health_not_possible"}.get(x["reason"], "not_wanted")}
                                   for x in (plan.get("excluded") or {}).get("exercises", [])],
        "muscles_trained_elsewhere": (plan.get("excluded") or {}).get("external_muscles", []),
        # exercises the athlete set to "careful" for health reasons himself: in the plan, sub-maximal, no number
        "exercises_with_care": list(report.get("careful_exercises") or []),
    }
    if cfg.get("ai_share_profile", True):            # age band + sex: the owner's default; never a birth date
        profile.update({"age_band": prof.get("age_band"), "sex": prof.get("sex")})
    gp = report.get("goal_progress")
    if gp:
        conv = F if gp["kind"] in ("force", "body_weight") else L
        profile["measurable_target"] = {"kind": gp["kind"], "exercise": gp.get("exercise"), "target": conv(gp["target"]),
                                        "current": conv(gp["current"]) if gp["kind"] == "force" else None,   # body values stay relative
                                        "until": gp.get("date"), "status": gp.get("status"),
                                        "needed_pct_per_week": gp.get("needed_pct_per_week"), "own_pct_per_week": gp.get("own_pct_per_week")}

    # ---- last session: per exercise incl. the run of the reps -------------------------------------------------------
    ls = report.get("last_session")
    last = None
    if ls:
        def row(x):
            p = x.get("prev") or {}
            pct = lambda a, b: round((a / b - 1) * 100, 1) if (a and b) else None
            return {"order": x.get("order"), "exercise": x["name"], "group": x["group"], "context": x.get("context"),
                    "rest_before_min": x.get("rest_before_min"), "peak": F(x["max_kg"]), "concentric_top3": F(x.get("con_top3_kg")),
                    "eccentric_top3": F(x.get("ecc_top3_kg")), "inroad_pct": x.get("inroad"), "effort": x.get("effort"),
                    "repeat_now": x.get("repeat_now"),           # below the goal's target and not repeated properly in this visit (v0.11.0)
                    # contract modes-1: movement/ending(/phase), the Output of the set and the machine's own inroad scale
                    "mode": f"{x.get('movement', 'dynamic')}/{x.get('ending', 'reps')}" + (f"/{x.get('phase')}" if x.get("phase") in ("negative", "positive") else ""),
                    "output_kg_s": x.get("output_kg_s"), "inroad_machine_pct": x.get("inroad_machine"),
                    "effort_capped": x.get("effort_capped"), "fatigue_concentric_pct": x.get("fatigue_con_pct"),
                    "fatigue_eccentric_pct": x.get("fatigue_ecc_pct"), "best_rep": x.get("best_rep"),
                    "pacing_deficit_pct": x.get("pacing_deficit_pct"), "reps": x.get("reps"), "seconds": x.get("seconds"),
                    "range_of_motion": L(x.get("rom_cm")), "pause_end_s": x.get("pause_end_s"), "pause_return_s": x.get("pause_return_s"),
                    "sets_today": x.get("sets_today"), "new_comparable_best": x.get("is_pb"), "restriction": x.get("restriction"),
                    "settings_changed": x.get("settings_changed") or [],
                    "vs_previous": ({"date": p.get("date"), "same_settings": p.get("same_settings"), "peak_change_pct": x.get("delta_pct"),
                                     "concentric_change_pct": pct(x.get("con_top3_kg"), p.get("con_top3_kg")),
                                     "eccentric_change_pct": pct(x.get("ecc_top3_kg"), p.get("ecc_top3_kg")),
                                     "fatigue_concentric_pct": p.get("fatigue_con_pct"), "fatigue_eccentric_pct": p.get("fatigue_ecc_pct")}
                                    if p else None),
                    "per_rep_concentric": [F(v) for v in x.get("rep_con") or []] or None,
                    "per_rep_eccentric": [F(v) for v in x.get("rep_ecc") or []] or None,
                    "rep_trend": _text(x.get("rep_trend"))}
        last = {"date": ls["date"], "days_ago": (date.fromisoformat(today) - date.fromisoformat(ls["date"])).days if today else None,
                # still on the machine (last set < 60 min ago)? then "once more, now" for these (v0.11.0)
                "session_open": bool(ls.get("open")), "repeat_now": ls.get("repeat_now") or [], "inroad_target_pct": ls.get("inroad_target"),
                "working_sets": ls.get("working_sets"), "false_starts": ls.get("false_starts"), "wall_minutes": ls.get("wall_minutes"),
                "minutes_under_load": ls.get("time_under_load_min"), "flags": [f.get("type") for f in ls.get("flags", [])],
                "vs_previous_session": (ls.get("transition") or {}).get("verdict"), "exercises": [row(x) for x in ls.get("exercises", [])]}
    pva = report.get("plan_vs_actual")
    plan_vs_actual = None
    if pva:
        plan_vs_actual = {"plan_made_on": pva["plan_created"], "planned_for": pva["planned_date"], "trained_on": pva["actual_date"],
                          "done": pva["done"], "skipped": pva["skipped"], "added": pva["added"], "order_agreement": pva["order_agreement"],
                          "force_targets_met": [pva["targets_met"], pva["targets_judged"]], "effort_targets_met": [pva["effort_met"], pva["effort_judged"]]}

    # ---- today: check-in, what is recovered --------------------------------------------------------------------------
    rd = report.get("readiness")
    rec = (report.get("load") or {}).get("recovery") or {}
    tw = (ns or {}).get("time_window")
    readiness = {"checkin": ({k: rd.get(k) for k in ("score", "band", "components", "sore_regions", "rhr_status", "rhr_diff", "pain")} if rd else None),
                 # the athlete's time window for TODAY from the check-in (an upper limit; None = no limit given)
                 "minutes_available_today": pp.get("window_minutes"),
                 # single exercises from today's check-in - today only: not at all / only sub-maximal
                 "exercises_not_today": sorted(x for x, lvl in (report.get("today_exercises") or {}).items() if lvl == "injury"),
                 "exercises_with_care_today": sorted(x for x, lvl in (report.get("today_exercises") or {}).items() if lvl == "careful"),
                 "load_flag": (report.get("load") or {}).get("flag"),
                 "muscles_not_ready": {m: {"ready_on": v["ready_on"], "why": v["reason"]} for m, v in (rec.get("muscles") or {}).items() if not v.get("ready")},
                 "everything_ready_on": (report.get("load") or {}).get("all_ready_on")}

    # ---- the planner's proposal and the space around it ------------------------------------------------------------------
    planner_block = None
    if ns:
        space = plan.get("decision_space") or {}
        planner_block = {
            "train_today": plan["today"]["train_today"], "today_note": _text(plan["today"].get("interp")),
            "proposal": {"date": ns["date"], "session_type": ns["session_type"], "regions": ns["regions"], "est_minutes": ns["est_minutes"],
                         "fresh_benchmark_exercise": ns.get("benchmark"), "why_this_date": [_text(w) for w in ns["why_this_date"]],
                         "rows": [{"order": x["order"], "exercise": x["name"], "sets": x["sets"], "target": F(x["target_peak_kg"]) or 0,
                                   "target_rule": x["target_rule"], "reference": F(x.get("base_kg")), "reference_date": x.get("base_date"),
                                   "reference_context": x.get("base_context"), "effort": x["effort_target"]["label"],
                                   "inroad_min_pct": x["effort_target"]["inroad_min"], "rest_before_min": x["rest_before_min"],
                                   "planned_context": x.get("planned_context"), "expected_loss_vs_fresh_pct": x.get("expected_loss_pct"),
                                   "new_for_the_athlete": x["new"], "aid_in_use": x.get("aid"), "aid_suggested": x.get("aid_hint"),
                                   "settings": ({"reps": x["settings"].get("reps"), "seconds_per_direction": x["settings"].get("tempo_s"),
                                                 "pause_end_s": x["settings"].get("pause_end_s"), "pause_return_s": x["settings"].get("pause_return_s"),
                                                 "range_of_motion": L(x["settings"].get("rom_cm"))} if x.get("settings") else None),
                                   # after two fruitless sessions the engine changes the set-up (v0.10.0): {field: {from, to}}
                                   "settings_change": ({("seconds_per_direction" if k == "tempo_s" else k): v for k, v in x["settings_change"].items()}
                                                       if x.get("settings_change") else None),
                                   "why": [t for t in [_text(w) for w in (x.get("why_selected") or []) + [x.get("interp"), x.get("context_note")]
                                                       + (x.get("order_rules") or [])] if t]} for x in ns["exercises"]],
                         "order_notes": [_text(n) for n in ns.get("order_notes") or []],
                         "helper_muscle_budget": {m: {"status": b["status"], "share_of_usual_pct": b["share_pct"]} for m, b in (ns.get("limiter_budget") or {}).items()},
                         "aid_hints": [_text(h) for h in ns.get("aid_hints") or []], "time_note": _text(ns.get("time_note")),
                         # the check-in (not the clock) made a session planned for today smaller
                         "checkin_adjustment": _text(ns.get("checkin_cut")),
                         # a new athlete's first sessions are moderate on purpose (v0.12.0)
                         "beginner_note": _text(ns.get("beginner_note")),
                         # the last day left exercises without a stimulus and they are back: "again, properly" (v0.11.0)
                         "repeat_note": _text(ns.get("repeat_note")),
                         # today's session was cut to the athlete's time window: what stayed, what was left out
                         "time_window": ({"minutes": tw["minutes"], "exercises_kept": tw["size"], "exercises_of_the_normal_plan": tw["normal_size"],
                                          "left_out_for_next_time": tw["left_out"], "engine_says": _text(tw.get("interp"))} if tw else None),
                         "guard": _text(ns.get("guard"))},
            "decision_space": {"dates": [{"date": d["date"], "candidates": [{"exercise": c["name"], "status": c["status"], "new": c["new"],
                                                                             "restriction": c["restriction"], "group": c["group"]} for c in d["candidates"]]}
                                         for d in space.get("dates", [])],
                               "bounds": space.get("bounds"), "effort_cap": space.get("effort_cap"),
                               # bounds.rows_max = what a session can hold at all; what the athlete's TIME holds + the price of one more
                               "rows_in_time_budget": space.get("rows_in_time_budget"),
                               "minutes_per_extra_exercise": space.get("minutes_per_extra_exercise"),
                               # after weeks away a target may go this many % BELOW the proposal (exercise -> %)
                               "target_floor_pct": space.get("target_floor_pct") or {}},
            "week_outlook": [{"date": w["date"], "type": w["session_type"], "exercises": w["exercises"], "fresh_benchmark": w.get("benchmark")}
                             for w in plan.get("week_plan", [])],
            "cadence_note": _text(plan.get("cadence_note")),
            # a gap of more than two weeks before the planned session: tier short / long / very_long with the engine's line
            "training_break": ({"days_since_last_session": plan["training_break"]["days"], "tier": plan["training_break"]["tier"],
                                "engine_says": _text(plan["training_break"].get("interp"))} if plan.get("training_break") else None),
            "real_sessions_per_week": (plan.get("profile") or {}).get("real_sessions_per_week"),
            # no trained muscle may wait longer than about a week: what the engine flags, and what one weekly session buys
            "frequency_warnings": [_text(n) for n in plan.get("frequency_notes") or []],
            "dose_note": _text(plan.get("dose_note")), "structure_note": _text(plan.get("structure_note")),
            "session_possible_today_instead": [x["name"] for x in (plan.get("today_session") or {}).get("exercises", [])] or None,
            # ... and why that one is smaller than a normal session (check-in band or time window), in the engine's words
            "today_instead_adjustment": (_text((plan.get("today_session") or {}).get("checkin_cut"))
                                         or _text(((plan.get("today_session") or {}).get("time_window") or {}).get("interp"))),
        }

    # ---- history: series on comparable days, weeks, factors, evidence, findings ---------------------------------------------
    pf = {p["name"]: p for p in (hist.get("progress_factors") or {}).get("exercises", [])}
    exercises = []
    off = {e["name"] for e in report.get("exercises", []) if e.get("excluded")}    # switched off in the profile: no business of the coach
    for e in report.get("exercises", []):
        if e["name"] in off:
            continue
        p = pf.get(e["name"], {})
        pts = [o for o in e["occ"]][-SERIES_POINTS:]
        exercises.append({
            "exercise": e["name"], "group": e["group"], "kind": e["kind"], "targets": e.get("targets"), "helpers": e.get("limiters_eff", e.get("limiters")),
            "restriction": e.get("restriction"), "training_days": e["n"], "last_date": e.get("last_date"),
            "best_comparable": F(e.get("pb_comparable")), "range_reference": L(e.get("rom_cm_reference")), "range_drift_pct": e.get("rom_drift_pct"),
            # a missed effort target is acted on (v0.10.0): the streak of last days below the goal's force drop
            "effort_misses_in_a_row": e.get("effort_misses"), "last_inroad_pct": e.get("effort_last_inroad"), "inroad_target_pct": e.get("effort_target_inroad"),
            "progress": {"status": p.get("status"), "change_pct": p.get("change_pct"), "concentric_change_pct": p.get("change_con_pct"),
                         "eccentric_change_pct": p.get("change_ecc_pct"), "comparable_days": p.get("n"), "span_days": p.get("span_days"),
                         "rate_pct_per_week": p.get("rate_pct_per_week"), "reference_context": p.get("reference_context"),
                         "never_measured_fresh": p.get("never_fresh"), "effort_target_hit_rate": p.get("effort_hit_rate"),
                         "meaning": _text(p.get("interp"))},
            # columnar series: one entry per training day (best set of the day)
            "series": {"date": [o["date"] for o in pts], "peak": [F(o["kg"]) for o in pts], "concentric": [F(o.get("con_top3_kg")) for o in pts],
                       "eccentric": [F(o.get("ecc_top3_kg")) for o in pts], "inroad_pct": [o.get("inroad") for o in pts],
                       "context": [o.get("context") for o in pts], "comparable": [bool(o.get("comparable")) for o in pts],
                       "mode": [f"{o.get('movement', 'dynamic')}/{o.get('ending', 'reps')}" for o in pts], "output_kg_s": [o.get("output_kg_s") for o in pts]},
            "modes_seen": e.get("modes_seen"), "output_delta_pct": e.get("output_delta_pct"),
        })
    win = hist.get("windows") or {}
    weeks = [{"week": w["label"], "start": w["start"], "partial": w["partial"], "sessions": w["sessions"], "working_sets": w["working_sets"],
              "hard_sets": w["hard_sets"], "deep_sets": w["deep_sets"], "minutes_under_load": w["tul_min"], "wall_minutes": w["wall_min"],
              "new_bests": w["pbs"], "strength_index": w.get("strength_index"), "effort_hit_rate": w.get("effort_hit_rate"),
              "target_met": w.get("target_met"), "readiness_avg": w.get("readiness_avg")} for w in (win.get("weeks") or [])[-WEEKS_IN_PAYLOAD:]]
    ev = report.get("evidence") or {}
    evidence = {"fresh_sets": [ev.get("fresh_sets"), ev.get("total_sets")], "never_measured_fresh": ev.get("never_fresh"),
                "order_effects": [{"id": x["id"], "first": x["before"], "then": x["then"], "n": x["n"], "observed_loss_pct": x["observed_loss_pct"],
                                   "general_estimate_pct": x["prior_loss_pct"], "blended_loss_pct": x["loss_pct"], "confidence": x["confidence"]}
                                  for x in ev.get("pair_effects", []) if x["before"] not in off and x["then"] not in off],
                "second_set_effects": [{"id": x["id"], "exercise": x["exercise"], "n": x["n"], "observed_loss_pct": x["observed_loss_pct"],
                                        "confidence": x["confidence"]} for x in ev.get("repeat_effects", []) if x["exercise"] not in off],
                "rest_effect": (ev.get("rest_effect") or {}).get("status"),
                "position_effect_pct_per_position": (ev.get("position_effect") or {}).get("pct_per_position")}
    findings = [{"ref": f["id"], "type": f["type"], "severity": f["severity"], "exercise": f.get("exercise"), "n": f.get("n"),
                 "confidence": f.get("confidence"), "meaning": _text(f.get("interp"))} for f in ((hist.get("findings") or {}).get("all") or [])[:12]]
    coach = report.get("coach") or {}
    overall = (hist.get("progress_factors") or {}).get("overall") or {}
    history = {"training_days": report.get("training_days"), "sets": {"working": report.get("sets_working"), "recorded": report.get("sets_total"),
                                                                     "not_counted": report.get("sets_excluded")},
               "availability": win.get("availability"), "weeks": weeks,
               "strength_index": {"value": overall.get("strength_index"), "change_pct": overall.get("change_pct"),
                                  "exercises_in_index": overall.get("exercises_in_index"), "effort_target_hit_rate": overall.get("effort_hit_rate")},
               "time_efficiency": {k: v for k, v in (hist.get("time_efficiency") or {}).items() if k != "interp"},
               "adherence": coach.get("adherence"), "deload": {"suggested": (coach.get("deload") or {}).get("suggested"), "weeks_at_target": (coach.get("deload") or {}).get("weeks_at_target"),
                          "declining": [{"exercise": x.get("name"), "change_pct": x.get("change_pct")} for x in (coach.get("deload") or {}).get("declining", [])]},
               "exercises": exercises, "own_evidence": evidence, "findings": findings,
               "range_warnings": [w["name"] for w in report.get("rom_warnings", [])],
               "limits_respected_checks": [{"date": c["date"], "exercise": c["exercise"], "issue": c["issue"]} for c in report.get("restriction_checks", [])]}
    if cfg.get("ai_share_body") and (report.get("body") or {}).get("relative_changes"):
        history["body_relative_changes"] = report["body"]["relative_changes"]      # relative only, behind its own switch

    return {"meta": {"today": today, "language": "de" if cfg.get("language") == "de" else "en",
                     "units": {"force": "lb" if imp else "kg", "length": "in" if imp else "cm"}, "payload_version": 2},
            "profile": profile, "last_session": last, "plan_vs_actual": plan_vs_actual, "readiness_today": readiness,
            "planner": planner_block, "history": history, "previous_recommendations": previous or []}


def lint_payload(payload) -> list[str]:
    """Keys that must never appear: metric suffixes (values are in the athlete's units), and anything
    that could carry a person."""
    bad = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for k, v in node.items():
                if re.search(r"(_kg|_cm)$", str(k)) or str(k) in ("name", "alias", "athlete_alias", "note", "notes", "birthdate", "user", "user_id", "height_cm", "weight_kg"):
                    bad.append(f"{path}/{k}")
                walk(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
    walk(payload)
    return bad


def payload_key(payload: dict, model: str, effort: str) -> str:
    """What a board depends on: everything that is sent - except the memory section (it is derived
    from earlier boards and must not make every answer look new)."""
    basis = {k: v for k, v in payload.items() if k != "previous_recommendations"}
    blob = dumps_payload([basis, model, effort, PROMPT_VERSION])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


# =============================================================================
# Prompts (static per release - cache friendly)
# =============================================================================
_SCIENCE: str | None = None


def science_lines() -> str:
    """The curated science base, one line per topic: rule of thumb + what does not transfer."""
    global _SCIENCE
    if _SCIENCE is None:
        try:
            with open(os.path.join(HERE, "science.json"), encoding="utf-8") as f:
                sci = json.load(f)
            lines = [f"- {k} ({t['evidence_level']}): {t['practical_rule']}" + (f" Caveat on this machine: {t['arx_caveat']}" if t.get("arx_caveat") else "")
                     for k, t in sorted(sci.get("topics", {}).items())]
            _SCIENCE = f"(reviewed {sci.get('reviewed', '?')})\n" + "\n".join(lines)
        except Exception:
            _SCIENCE = "(science base not available)"
    return _SCIENCE


COACH_CORE = """You are the strength coach inside ARX Insight, a local analysis tool for the ARX motor-driven adaptive-resistance machine. On this machine a motor moves at a set speed; the athlete pushes or pulls as hard as possible through the concentric phase and resists the motor through the eccentric phase. There is no "weight": FORCE is the performance measure, eccentric force is normally about 1.4 times the concentric one, and typical use is ONE all-out set per exercise in 1-4 short sessions a week.

WHAT YOU RECEIVE: one JSON payload about one athlete - name-free, already in the athlete's units (meta.units) and language code (meta.language). Every number was computed by the app's engine from the machine's raw data (20 Hz force curves, phase markers). Treat them as facts; never recompute or contradict them.
- profile: goal, time budget, the time-vs-effort profile the athlete chose, focus, restrictions, grip aids, exercises the athlete switched off for this machine (exercises_switched_off) and the muscles trained outside it (muscles_trained_elsewhere), optional age band / sex, optional measurable target.
- last_session: every exercise in the order performed - peak, concentric / eccentric strength (mean of the three best reps; robust against tempo changes), inroad_pct (force drop across the set on the combined per-rep series: deep >= 20, moderate >= 10), fatigue per phase, per-rep tables, context (fresh = nothing before it loaded its muscles; preloaded; repeat = second set), and the comparison with the previous time (only meaningful when same_settings is true).
- plan_vs_actual: what the athlete did with the previous plan.
- readiness_today: check-in (if any), load flag, muscles still recovering with dates.
- planner: the engine's PROPOSAL for the next session (date with reasons, rows in order, targets, sets, rests, reasons per row) and the DECISION SPACE around it.
- history: per exercise a series on training days (comparable marks the days that may be compared), progress status against the athlete's own baseline (concentric and eccentric apart), weekly windows, strength index, the athlete's OWN measured effects (order, second set) with n and a confidence label, ranked findings with a ref, adherence, deload facts.
- previous_recommendations: what you told this athlete before.

HOW TO JUDGE - non-negotiable:
1. Compare only what is comparable. A value measured preloaded or as a repeat set is lower by design: never call it a regression; say what it came after. A learning day is not progress.
2. Evidence has an n. Say "measured on you (n = 3)" or "a general estimate" - never present an estimate as the athlete's data, never turn n = 1 into a rule.
3. Never invent numbers. Every force, percentage, date, count or minute you write must be in the payload (rounding is fine). If something is not there, say so in words.
4. Safety first: restrictions (careful / avoid), pain today, the minors guard and a poor check-in outrank progress. You are not a doctor: no diagnosis, no medical advice.
5. Minimum effective dose: the athlete's time is part of the goal. Never add volume "to be safe"; respect the chosen time-vs-effort profile (you may recommend another one when intent and delivered effort diverge - with its price in time).
6. Frequency is a floor: a muscle needs a training stimulus at least about once a week to grow. One session a week therefore means full body with the big push, pull and leg exercises; a split only makes sense from two, better three sessions a week. If planner.frequency_warnings names a muscle, or your own change would leave a trained muscle without a stimulus for more than about 8 days, say so and fix it - never recommend alternating muscle groups at one session a week.
7. Breaks: planner.training_break tells you when the athlete has been away (short = up to about four weeks, long = more, very_long = more than half a year). Up to about three weeks nothing is lost: simply continue and hold the numbers. After a longer break expect lower values, treat what is reached as the new starting point and say that it comes back much faster than it was built. NEVER try to "catch up": no extra sets, no extra sessions, no harder session than the profile asks for - and no split just because of the break (the structure follows the sessions per week the athlete really trains, see planner.real_sessions_per_week). After everything is recovered the big push, pull and leg exercise come first: that is the best use of the athlete's time.
8. Stay consistent: keep your earlier line unless the data changed. When you change something, name it and give the reason.
9. Every statement answers two questions for the athlete: what does this mean for me, and what do I do next. No filler, no praise without a number behind it, no generic gym advice the data does not support.
10. The repertoire is the athlete's decision: an exercise in profile.exercises_switched_off is never recommended - not in the rows, not in the text, not as an alternative. reason trained_elsewhere: the athlete trains it outside this machine, so profile.muscles_trained_elsewhere get their stimulus there - do not call them neglected, do not add machine work for them, and keep in mind that this load is invisible here: when such a muscle helps in a planned exercise, say once that soreness from that training belongs into the check-in. reason not_wanted: cover its muscles with the athlete's other exercises where the decision space allows, and say plainly when nothing in the repertoire reaches them. reason health_not_possible: the athlete cannot do it for now for a health reason he has not described to you - never recommend it, never suggest "trying" it, and do not treat the joint behind it as fine just because the other exercises are allowed; the exercises that stay in the plan are the ones he can do. profile.exercises_with_care are exercises he keeps for health reasons ONLY sub-maximally (movement helps a recovering joint, the all-out set does not): they stay in the plan with effort submax and target 0, and you never push them harder or read a lower value there as a regression. readiness_today.exercises_not_today and exercises_with_care_today say the same for TODAY only (from the check-in): the engine has already taken them out of / eased them in today's session - keep it that way, and if it looks lasting, tell him the profile tiles are the place for it. readiness_today.checkin.sore_regions and .pain are the reasons behind those flags - on the check-in screen a sore region or a painful joint is a shortcut that flags the exercises it touches, and the athlete may have kept single exercises anyway (his call) - so never derive a second rule from soreness or pain.
11. How many exercises a session has is a TIME decision of the athlete - never present it as a training rule or as "the frame". planner.decision_space.rows_in_time_budget is what the stated time holds at the athlete's own pace, bounds.rows_max what a session can hold at all; one more exercise costs minutes_per_extra_exercise. Exercises for unrelated muscles cost no result, and for a size goal more weekly sets per muscle is the best-supported lever - so when the athlete says there is more time, build the fuller session inside the decision space, name its price in minutes and tell him that "Minutes per session" in the profile makes it permanent (profile.exercises_by_minutes_per_session shows what each budget buys). Without that signal stay within rows_in_time_budget unless volume is the lever (size goal AND the effort target is being met). Two exercises that share a TARGET muscle are more volume for it, not "another muscle group": say so, and say which of them keeps the clean measurement. With profile.takes_turns_with_partner the long change-over is the partner's set: never recommend shortening it, never count it as wasted time. readiness_today.minutes_available_today is the athlete's time window for TODAY (an upper limit from the check-in): a session planned for today has to fit - the engine already cut it (planner.proposal.time_window: extra sets first, then the least urgent exercises) and bounds.rows_max is then what fits; never add rows or sets beyond it, keep the effort, say what was left out and that it comes first next time. A moderate or light check-in makes a session planned for TODAY smaller and caps its effort on purpose (planner.proposal.checkin_adjustment / planner.today_instead_adjustment): that is a readiness rule, not a time limit - never promise more exercises for today because there is time; say that the check-in is the reason and that the full session is there on a better day.
12. A missed effort target is answered, never just noted. history.exercises[].effort_misses_in_a_row counts the exercise's last training days in a row that ended below inroad_target_pct (last_inroad_pct = the latest); a day planned sub-maximal (careful, light, limited) is not a miss. ONE miss: the number holds and the cue is intent - all-out from the first repetition (a start deficit is given away), building the force fast but smoothly (the machine sets the speed - never a jerk), the set ends when the force breaks down, not when the repetitions are over. TWO misses in a row: the set-up changes and the row's tempo field says so - the engine proposes planner.proposal.rows[].settings_change (seconds per direction +1 up to 5, the pauses at the turnarounds to 0; two repetitions more once those are exhausted): keep it or choose the repetitions instead, but never hold the number a third time without changing something, and say that the comparison basis restarts with new settings. While last_session.session_open is true the consequence of a missed effort target is: once more, NOW, properly - say it first in last_session.consequence (last_session.repeat_now lists the exercises). Afterwards: a set that did not reach the target caused no rest debt - sets not taken to failure recover in about a day, so those muscles are ready the next day and the plan re-plans the same exercises (planner.proposal.repeat_note); never make the athlete wait for a rhythm after a fruitless session. A BEGINNER (profile.experience "new" with fewer than two training days; planner.proposal.beginner_note is set) is the exception to all of this: the target is moderate on purpose, "about half of what you have", learn the movement, next time "beat your previous number by what feels comfortable" - nothing is a miss, nothing escalates, never all-out from rep 1. For a STRENGTH goal the pauses at the turnarounds stay (rest-pause serves tension): the lever is tempo, then repetitions.

SCIENCE BASE - curated general evidence; the athlete's own measured data outranks these defaults, and a default must be called a default:
{science}
"""

BOARD_RULES = """YOUR TASK: fill the board - one JSON object in the given schema. Write every text in the language meta.language, plain text only (no markdown), numbers with the units of meta.units.

last_session: headline (max 90 characters, the one thing that defines the session) - meaning (what it means for this athlete) - consequence (what follows concretely) - verdicts: one per exercise of last_session in the order performed; rating better / same / worse only when vs_previous.same_settings is true, else not_comparable (or first); note max 140 characters with the decisive number (prefer concentric / eccentric strength and the run of the reps over the raw peak) - bullets: 2 to 4 observations a human trainer would have missed (order effects, a phase that gives up early, pacing, rests, settings drift, plan vs actual), each with its number. If there is no last session write that there is no session yet and keep the lists empty.

next_training: readiness (one sentence: check-in verdict or "no check-in", what is recovered) - date and why_date - rows - rest_note - grip_note (helper muscles / aids; empty string when nothing is worth saying) - week_outlook (one or two sentences) - changes_vs_previous (what differs from previous_recommendations and why; empty list when nothing).
VOCABULARY: a set's mode is movement/ending (dynamic or static hold; ended by reps, by the clock = "Countdown", by the machine's inroad rule = "Inroad-Modus", or unknown) plus a phase when only one direction carried work (negative-only / positive-only); "Output" is the impulse of a set and the progress figure of timed sets ("beat your gray line"); inroad_machine_pct is the machine's OWN inroad scale (best rep peak to last rep peak, what its Inroad Mode uses) - call it "Maschinen-Inroad" / "machine inroad" and never confuse it with the fatigue in the set. A hold or a single-phase set is never compared with a dynamic both-phase set. The within-set force decline (inroad_pct) is called "fatigue in the set" / "Ermüdung im Satz" to the athlete - levels deep / medium / light ("tief / mittel / leicht"); the goal's minimum is the "fatigue target" / "Ermüdungsziel". Never say "inroad" or "Kraftabfall" to the athlete; a set below the target is "a real load, but half the stimulus", never "no stimulus".
THE PLAN: planner.proposal is a valid plan. Keeping it is usually right - then copy its rows (exercise, sets, target, effort, rest_before_min) in its order. why of an UNCHANGED row = one sentence why the row stays as it is (what it measures or does now) - never a filler word such as "Platzhalter" or "placeholder". You MAY change it inside planner.decision_space: another date from dates[]; exercises from THAT date's candidates; the order (an exercise whose target muscle is another exercise's helper comes AFTER it - Row before Biceps Curl, press before Triceps Pressdown); a target within bounds.target_pct of the proposal's target for that exercise (an exercise listed in decision_space.target_floor_pct may go that many percent BELOW it - first session after weeks away); sets from 1 to bounds.sets_max; rest_before_min from 0 to bounds.rest_max_min; effort never above effort_cap. Candidates with status "limited", restriction "careful" or new = true are only allowed with effort "submax" and target 0. target 0 always means "no number - by feel". Every change against the proposal needs a concrete reason from the data in that row's why (at least one full sentence); an unchanged row gets a short why in your own words. The server validates the rows; an invalid plan is replaced by the engine's proposal. tempo: reps, seconds per direction and pauses from the row's settings, as one short string. cue: one technique or intent cue, max 80 characters.

history: four_weeks (what the last weeks show: sessions against the target, hard sets, strength index, effort hit rate - with numbers) - longer_term (quarter / year; empty string while history.availability says these views are not available) - kpi_notes: for the 2 to 5 exercises where it matters, what the progress status means and what follows - anomalies: the findings that deserve attention, each with its ref from history.findings, the meaning and the action.

focus: ONE sentence - the single most valuable thing to do differently next time. key_recommendations: 2 to 4 short imperatives the athlete can remember at the machine. suggested_questions: 3 short questions this athlete might want to ask you next, in their language.
"""

CHAT_RULES = """YOU ARE NOW IN A LIVE CONVERSATION with this athlete, often standing at the machine with a phone. The first message carries the payload and the board you delivered; then the athlete's questions follow.
- Answer in the athlete's language (meta.language unless they write in another one), plain text, 2 to 6 sentences unless they ask for more. Lead with the answer.
- Ground every answer in the payload: name the number and where it comes from. If the data cannot answer the question, say so and say what would be needed.
- Stay consistent with the board you delivered. If the question reveals something new (pain, no time today, equipment), adapt inside the decision space and say what changes and why. You cannot store changes: tell the athlete where to set them in the app (profile: goal, time - minutes per session decide how many exercises are planned -, training in turns with a partner, focus, grip aids, session structure, exercises they do not do on the ARX; check-in; "next session only ..." in the plan).
- Safety first, no medical advice, no diagnosis; for pain or illness: stop, rest, see a professional.
- You only ever see this one athlete's data. Never guess about other people.
"""


def system_prompt(kind: str = "board") -> list[dict]:
    """The frozen system prompt as one text block (same bytes for every athlete -> cacheable)."""
    text = COACH_CORE.replace("{science}", science_lines()) + "\n" + (BOARD_RULES if kind == "board" else CHAT_RULES)
    return [{"type": "text", "text": text}]


# =============================================================================
# Board schema (structured outputs: every object closed, every property required)
# =============================================================================
RATINGS = ("better", "same", "worse", "first", "not_comparable")


def board_schema(report: dict) -> dict:
    plan = report.get("plan") or {}
    space = plan.get("decision_space") or {}
    dates = [d["date"] for d in space.get("dates", [])] or [report.get("today") or "1970-01-01"]
    cands = sorted({c["name"] for d in space.get("dates", []) for c in d["candidates"]}) or ["-"]
    done = [x["name"] for x in (report.get("last_session") or {}).get("exercises", [])] or ["-"]
    known = sorted({e["name"] for e in report.get("exercises", []) if not e.get("excluded")}) or ["-"]    # not: switched-off exercises
    refs = [f["id"] for f in ((report.get("history") or {}).get("findings") or {}).get("all", [])[:12]]
    S = {"type": "string"}
    obj = lambda props: {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    arr = lambda items: {"type": "array", "items": items}
    return obj({
        "last_session": obj({"headline": S, "meaning": S, "consequence": S,
                             "verdicts": arr(obj({"exercise": {"type": "string", "enum": done}, "rating": {"type": "string", "enum": list(RATINGS)}, "note": S})),
                             "bullets": arr(S)}),
        "next_training": obj({"readiness": S, "date": {"type": "string", "enum": dates}, "why_date": S,
                              "rows": arr(obj({"exercise": {"type": "string", "enum": cands}, "sets": {"type": "integer"}, "target": {"type": "number"},
                                               "effort": {"type": "string", "enum": ["deep", "moderate", "submax"]}, "rest_before_min": {"type": "number"},
                                               "tempo": S, "cue": S, "why": S})),
                              "rest_note": S, "grip_note": S, "week_outlook": S, "changes_vs_previous": arr(S)}),
        "history": obj({"four_weeks": S, "longer_term": S,
                        "kpi_notes": arr(obj({"exercise": {"type": "string", "enum": known}, "note": S})),
                        "anomalies": arr(obj({"ref": ({"type": "string", "enum": refs} if refs else S), "meaning": S, "action": S}))}),
        "focus": S, "key_recommendations": arr(S), "suggested_questions": arr(S),
    })


# =============================================================================
# Validation: the engine's rules + "every change needs a reason" + number check
# =============================================================================
def _canon(name: str, options: list[str]) -> str:
    """Enum values may come back with another capitalisation - map them to the real spelling."""
    low = {o.lower(): o for o in options}
    return low.get(str(name).lower(), str(name))


def normalise_rows(board: dict, report: dict, cfg: dict) -> list[dict]:
    """The board's rows as the rule checker wants them: real exercise names, targets in kg."""
    imp = cfg.get("units", "imperial") == "imperial"
    space = (report.get("plan") or {}).get("decision_space") or {}
    names = sorted({c["name"] for d in space.get("dates", []) for c in d["candidates"]})
    rows = []
    for r in (board.get("next_training") or {}).get("rows", []):
        target = float(r.get("target") or 0)
        rows.append({"exercise": _canon(r.get("exercise"), names), "sets": int(r.get("sets") or 0), "effort": r.get("effort"),
                     "target_kg": (round(target / 2.20462, 1) if imp else round(target, 1)) if target > 0 else None,
                     "rest_before_min": float(r.get("rest_before_min") or 0), "why": str(r.get("why") or "")})
    return rows


def plan_changes(rows: list[dict], day: str, report: dict) -> list[dict]:
    """What differs from the engine's proposal: [{exercise | None, field, from, to}]."""
    ns = (report.get("plan") or {}).get("next_session") or {}
    prop = {x["name"]: x for x in ns.get("exercises", [])}
    out = []
    if day != ns.get("date"):
        out.append({"exercise": None, "field": "date", "from": ns.get("date"), "to": day})
    if [r["exercise"] for r in rows] != [x["name"] for x in ns.get("exercises", [])]:
        if sorted(r["exercise"] for r in rows) == sorted(prop):
            out.append({"exercise": None, "field": "order", "from": [x["name"] for x in ns.get("exercises", [])], "to": [r["exercise"] for r in rows]})
    for r in rows:
        p = prop.get(r["exercise"])
        if not p:
            out.append({"exercise": r["exercise"], "field": "added", "from": None, "to": r["exercise"]})
            continue
        if (p["target_peak_kg"] or 0) and r["target_kg"] and abs(r["target_kg"] / p["target_peak_kg"] - 1) > 0.005:
            out.append({"exercise": r["exercise"], "field": "target", "from": p["target_peak_kg"], "to": r["target_kg"]})
        elif bool(p["target_peak_kg"]) != bool(r["target_kg"]):
            out.append({"exercise": r["exercise"], "field": "target", "from": p["target_peak_kg"], "to": r["target_kg"]})
        if r["sets"] != p["sets"]:
            out.append({"exercise": r["exercise"], "field": "sets", "from": p["sets"], "to": r["sets"]})
        if r["effort"] != p["effort_target"]["label"]:
            out.append({"exercise": r["exercise"], "field": "effort", "from": p["effort_target"]["label"], "to": r["effort"]})
        if abs(r["rest_before_min"] - (p["rest_before_min"] or 0)) >= 1.0:
            out.append({"exercise": r["exercise"], "field": "rest_before_min", "from": p["rest_before_min"], "to": r["rest_before_min"]})
    for name in prop:
        if name not in {r["exercise"] for r in rows}:
            out.append({"exercise": name, "field": "removed", "from": name, "to": None})
    return out


def validate_board(board: dict, report: dict, cfg: dict) -> tuple[list[dict], list[dict]]:
    """(problems, changes). problems = [{code, exercise, detail}] - empty when the board may be shown."""
    plan = report.get("plan") or {}
    if not plan.get("next_session"):
        return [], []                                 # nothing to plan (no feasible session) - texts only
    nt = board.get("next_training") or {}
    rows = normalise_rows(board, report, cfg)
    day = str(nt.get("date") or "")
    problems = planner.check_rows(day, rows, plan.get("decision_space") or {})
    changes = plan_changes(rows, day, report)
    changed = {c["exercise"] for c in changes if c["exercise"]}
    for r in rows:                                    # J-rule: a change without a reason is not a coaching decision
        if r["exercise"] in changed and len(r["why"].strip()) < WHY_MIN_CHARS:
            problems.append({"code": "change_without_reason", "exercise": r["exercise"], "detail": "why"})
    if any(c["field"] == "date" for c in changes) and len(str(nt.get("why_date") or "").strip()) < WHY_MIN_CHARS:
        problems.append({"code": "change_without_reason", "exercise": None, "detail": "why_date"})
    for r in rows:                                    # a filler word is not a reason for anything (seen live: "Platzhalter")
        if is_filler(r["why"]):
            problems.append({"code": "filler_why", "exercise": r["exercise"], "detail": "why"})
    return problems, changes


FILLERS = ("platzhalter", "placeholder", "n/a", "na", "tbd", "todo", "keine angabe", "none")


def is_filler(text) -> bool:
    """True for an empty or filler 'why' (dashes, dots, 'Platzhalter', 'placeholder', 'n/a' ...)."""
    w = str(text or "").strip().lower().strip("-—–.·*_ ")
    return not w or w in FILLERS or any(w.startswith(f) for f in ("platzhalter", "placeholder"))


def unverified_forces(board: dict, payload_text: str, unit: str) -> list[str]:
    """Force values in the board's texts (a number followed by the force unit) that do not occur in
    the payload (within rounding). A diagnostic, not a gate: the plan rows are validated on their own."""
    known = sorted({float(x) for x in re.findall(r"(?<![\w.])\d+(?:\.\d+)?", payload_text)})
    texts = []

    def walk(node):
        if isinstance(node, str):
            texts.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(board)
    bad = []
    for t in texts:
        for m in re.finditer(r"(?<![+\-−\w.,])(\d+(?:[.,]\d+)?)\s?" + re.escape(unit) + r"\b", t):
            v = float(m.group(1).replace(",", "."))
            if v >= 20 and not any(abs(v - k) <= 0.6 for k in known):
                bad.append(m.group(0))
    return sorted(set(bad))


# =============================================================================
# The API call (streaming; fallbacks="default" with a clean retry without it)
# =============================================================================
def make_client(cfg: dict):
    fake = os.environ.get("ARX_AI_FAKE")
    if fake:
        return FakeAnthropic(fake)
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise AIError("no_key", "no API key")
    try:
        from anthropic import Anthropic
    except ImportError:
        raise AIError("sdk_missing", "the 'anthropic' package is not installed - run the installer again")
    return Anthropic(api_key=key, timeout=REQUEST_TIMEOUT_S, max_retries=2)


def stream_message(client, *, model: str, system: list, messages: list, max_tokens: int, effort: str,
                   schema: dict | None = None, on_text=None, cache: bool = False):
    """One streamed request -> the final message. Tries the server-side refusal fallback first
    (beta); a 400 on that attempt is retried once without it (an account or model that does not
    know the beta must not lose the coach)."""
    output_config = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    params = dict(model=model, max_tokens=max_tokens, system=system, messages=messages,
                  thinking={"type": "adaptive"}, output_config=output_config)
    if cache:
        params["cache_control"] = {"type": "ephemeral"}       # caches the growing conversation tail

    def run(with_fallbacks: bool):
        ctx = (client.beta.messages.stream(betas=[FALLBACK_BETA], fallbacks="default", **params) if with_fallbacks
               else client.messages.stream(**params))
        with ctx as stream:
            if on_text:
                for chunk in stream.text_stream:
                    on_text(chunk)
            return stream.get_final_message()
    try:
        try:
            return run(True)
        except Exception as exc:
            if classify_ai_error(exc).code != "bad_request":
                raise
            return run(False)
    except Exception as exc:
        raise classify_ai_error(exc) from exc


def message_text(msg) -> str:
    return "".join(getattr(b, "text", "") for b in getattr(msg, "content", []) if getattr(b, "type", None) == "text")


def content_as_dicts(msg) -> list[dict]:
    """The assistant turn exactly as it has to be sent back (thinking blocks included, unchanged)."""
    out = []
    for b in getattr(msg, "content", []):
        d = b.to_dict() if hasattr(b, "to_dict") else (b.model_dump() if hasattr(b, "model_dump") else dict(b))
        out.append({k: v for k, v in d.items() if v is not None})
    return out


def usage_of(msg) -> dict:
    u = getattr(msg, "usage", None)
    g = lambda k: getattr(u, k, None) if u is not None else None
    return {"input_tokens": g("input_tokens"), "output_tokens": g("output_tokens"),
            "cache_read_input_tokens": g("cache_read_input_tokens"), "cache_creation_input_tokens": g("cache_creation_input_tokens"),
            "model": getattr(msg, "model", None)}


def _parse_board(msg) -> dict:
    stop = getattr(msg, "stop_reason", None)
    if stop == "refusal":
        raise AIError("refusal", "the model declined to answer")
    if stop == "max_tokens":
        raise AIError("truncated", "the answer hit the token limit - choose a lower AI effort in Settings")
    try:
        board = json.loads(message_text(msg))
    except ValueError:
        raise AIError("bad_answer", "the answer was not valid JSON")
    if not isinstance(board, dict):
        raise AIError("bad_answer", "the answer was not a JSON object")
    return board


def make_board(report: dict, cfg: dict, previous: list[dict] | None = None, client=None) -> dict:
    """Payload -> request -> validation -> (one repair round) -> the board record to store and show:
    {key, created, today, model, effort, board, plan_source, changes, problems, usage, unverified}."""
    payload = build_payload(report, cfg, previous)
    leaks = lint_payload(payload)
    if leaks:                                         # a programming error - never send it
        raise AIError("payload_lint", "payload contains forbidden keys: " + ", ".join(leaks[:5]))
    model, effort = model_of(cfg), effort_of(cfg, "ai_effort_board", BOARD_EFFORT_DEFAULT)
    text = dumps_payload(payload)
    client = client or make_client(cfg)
    schema = board_schema(report)
    messages = [{"role": "user", "content": text}]
    msg = stream_message(client, model=model, system=system_prompt("board"), messages=messages,
                         max_tokens=BOARD_MAX_TOKENS, effort=effort, schema=schema)
    board = _parse_board(msg)
    usage = [usage_of(msg)]
    problems, changes = validate_board(board, report, cfg)
    repaired = False
    if problems:
        # append-only repair: the first answer (thinking blocks unchanged) + what the rule check found
        messages = messages + [{"role": "assistant", "content": content_as_dicts(msg)},
                               {"role": "user", "content": "The server's rule check rejected next_training.rows:\n" + dumps_payload(problems)
                                + "\nReturn the complete board again with valid rows. When in doubt keep the proposal's rows unchanged."}]
        try:
            msg2 = stream_message(client, model=model, system=system_prompt("board"), messages=messages,
                                  max_tokens=BOARD_MAX_TOKENS, effort=effort, schema=schema)
            board2 = _parse_board(msg2)
            usage.append(usage_of(msg2))
            problems2, changes2 = validate_board(board2, report, cfg)
            if not problems2:
                board, problems, changes, repaired = board2, [], changes2, True
        except AIError:
            pass                                       # keep the first board's texts, fall back to the engine's plan
    plan_source = "coach" if not problems else "engine_fallback"
    unit = payload["meta"]["units"]["force"]
    return {"key": payload_key(payload, model, effort), "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "today": report.get("today"),
            "model": getattr(msg, "model", None) or model, "effort": effort, "board": board, "plan_source": plan_source,
            "repaired": repaired, "changes": changes if plan_source == "coach" else [], "problems": problems,
            "unverified": unverified_forces(board, text, unit), "usage": usage, "payload_sha": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]}


def memory_of(records: list[dict]) -> list[dict]:
    """What the next payload says about earlier boards (newest last)."""
    out = []
    for r in (records or [])[-MEMORY_BOARDS:]:
        b = r.get("board") or {}
        nt = b.get("next_training") or {}
        out.append({"given_on": r.get("today"), "focus": b.get("focus"), "key_recommendations": b.get("key_recommendations"),
                    "planned_for": nt.get("date"), "plan_source": r.get("plan_source"),
                    "rows": [{"exercise": x.get("exercise"), "target": x.get("target"), "sets": x.get("sets"), "effort": x.get("effort")}
                             for x in nt.get("rows", [])]})
    return out


# =============================================================================
# Stores (ai/boards.json, ai/chats.json) - per athlete, atomic writes, one lock
# =============================================================================
_STORE_LOCK = threading.RLock()


def _ai_dir() -> str:
    d = os.path.join(data_dir(), "ai")
    os.makedirs(d, exist_ok=True)
    return d


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class BoardStore:
    """Delivered boards per athlete (newest last). The store IS the coach's memory."""
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(_ai_dir(), "boards.json")

    def all(self, user_id) -> list[dict]:
        with _STORE_LOCK:
            return list(_read(self.path).get(str(user_id), []))

    def get(self, user_id, key: str) -> dict | None:
        return next((r for r in reversed(self.all(user_id)) if r.get("key") == key), None)

    def put(self, user_id, record: dict) -> None:
        with _STORE_LOCK:
            data = _read(self.path)
            rows = [r for r in data.get(str(user_id), []) if r.get("key") != record["key"]] + [record]
            data[str(user_id)] = rows[-BOARDS_KEEP:]
            write_json_atomic(self.path, data)

    def made_today(self, user_id) -> int:
        day = time.strftime("%Y-%m-%d")
        return sum(1 for r in self.all(user_id) if str(r.get("created", "")).startswith(day))


class JobManager:
    """One background job per (athlete, key); two devices asking for the same board share it."""
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: dict = {}

    def status(self, user_id, key: str) -> dict | None:
        with self._lock:
            j = self._jobs.get((str(user_id), key))
            return dict(j) if j else None

    def start(self, user_id, key: str, fn) -> dict:
        with self._lock:
            k = (str(user_id), key)
            j = self._jobs.get(k)
            if j and j["state"] == "running":
                return dict(j)
            j = self._jobs[k] = {"state": "running", "started": time.time(), "error": None}
            for old in [x for x in self._jobs if x != k and self._jobs[x]["state"] != "running"][:-8]:
                self._jobs.pop(old, None)

        def run():
            try:
                fn()
                state, err = "done", None
            except Exception as exc:                   # AIError or anything unexpected - the app stays up
                state, err = "error", classify_ai_error(exc).info()
            with self._lock:
                self._jobs[k].update(state=state, error=err, finished=time.time())
        threading.Thread(target=run, daemon=True).start()
        return dict(j)


# =============================================================================
# Chat
# =============================================================================
def scrub(text: str, names: list[str]) -> str:
    """Typed text may contain the athlete's own name - it never leaves the machine."""
    out = text
    for n in sorted({p for full in names for p in re.split(r"\s+", full or "") if len(p) >= 3}, key=len, reverse=True):
        out = re.sub(r"(?i)(?<!\w)" + re.escape(n) + r"(?!\w)", "the athlete", out)
    return out


class ChatManager:
    """One conversation per athlete and delivered board. A turn streams into a buffer that the
    browser polls; it is written to the transcript only when it finished (a failed turn commits
    nothing). The transcript keeps the exact request messages, so the prefix stays byte-identical."""
    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(_ai_dir(), "chats.json")
        self._live: dict = {}
        self._lock = threading.Lock()

    def _conv(self, user_id, board_key: str) -> dict:
        conv = _read(self.path).get(str(user_id)) or {}
        return conv if conv.get("board_key") == board_key else {"board_key": board_key, "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "messages": [], "turns": []}

    def history(self, user_id, board_key: str) -> dict:
        with _STORE_LOCK:
            conv = self._conv(user_id, board_key)
        return {"turns": [{"id": t["id"], "q": t["q"], "a": t["a"], "at": t["at"]} for t in conv["turns"]],
                "left": max(0, CHAT_TURNS_MAX - len(conv["turns"]))}

    def turns_today(self, user_id) -> int:
        """Questions asked today across all conversations of this athlete (a counter in the store)."""
        with _STORE_LOCK:
            return int((_read(self.path).get("_daily") or {}).get(f"{user_id}@{time.strftime('%Y-%m-%d')}", 0))

    def send(self, user_id, question: str, client_msg_id: str, *, cfg: dict, payload_text: str, board_record: dict, names: list[str]) -> dict:
        question = scrub(str(question or "").strip()[:CHAT_QUESTION_MAX], names)
        if not question:
            raise AIError("empty_question", "nothing to ask")
        turn_id = re.sub(r"[^A-Za-z0-9_-]", "", str(client_msg_id or ""))[:40] or hashlib.sha256(f"{time.time()}{question}".encode()).hexdigest()[:16]
        key = (str(user_id), turn_id)
        with self._lock:
            if key in self._live:                       # the same message sent twice (retry, second device)
                return {"turn": turn_id, "state": self._live[key]["state"]}
        with _STORE_LOCK:
            conv = self._conv(user_id, board_record["key"])
        if any(t["id"] == turn_id for t in conv["turns"]):
            return {"turn": turn_id, "state": "done"}
        if len(conv["turns"]) >= CHAT_TURNS_MAX:
            raise AIError("chat_limit", f"this conversation has reached {CHAT_TURNS_MAX} questions - it starts anew with the next report")
        if self.turns_today(user_id) >= CHAT_TURNS_PER_DAY:
            raise AIError("chat_daily_limit", f"{CHAT_TURNS_PER_DAY} questions a day")
        if not conv["messages"]:
            # the big, stable prefix: payload + delivered board, cached for an hour (questions come minutes apart)
            first = [{"type": "text", "text": "PAYLOAD\n" + payload_text},
                     {"type": "text", "text": "THE BOARD YOU DELIVERED\n" + dumps_payload(board_record.get("board") or {}),
                      "cache_control": {"type": "ephemeral", "ttl": "1h"}},
                     {"type": "text", "text": question}]
            user_msg = {"role": "user", "content": first}
        else:
            user_msg = {"role": "user", "content": question}
        messages = conv["messages"] + [user_msg]
        live = {"text": "", "state": "running", "error": None, "q": question}
        with self._lock:
            self._live[key] = live

        def run():
            try:
                client = make_client(cfg)
                msg = stream_message(client, model=model_of(cfg), system=system_prompt("chat"), messages=messages,
                                     max_tokens=CHAT_MAX_TOKENS, effort=effort_of(cfg, "ai_effort_chat", CHAT_EFFORT_DEFAULT),
                                     on_text=lambda chunk: live.__setitem__("text", live["text"] + chunk), cache=True)
                if getattr(msg, "stop_reason", None) == "refusal":
                    raise AIError("refusal", "the model declined to answer")
                answer = message_text(msg) or live["text"]
                with _STORE_LOCK:                       # commit the finished turn
                    data = _read(self.path)
                    c = data.get(str(user_id)) or {}
                    if c.get("board_key") != board_record["key"]:
                        c = {"board_key": board_record["key"], "created": conv["created"], "messages": [], "turns": []}
                    c["messages"] = messages + [{"role": "assistant", "content": content_as_dicts(msg)}]
                    c["turns"] = c.get("turns", []) + [{"id": turn_id, "q": question, "a": answer, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                                        "usage": usage_of(msg)}]
                    data[str(user_id)] = c
                    day = time.strftime("%Y-%m-%d")
                    daily = {k: v for k, v in (data.get("_daily") or {}).items() if k.endswith("@" + day)}   # yesterday's counters go
                    daily[f"{user_id}@{day}"] = daily.get(f"{user_id}@{day}", 0) + 1
                    data["_daily"] = daily
                    write_json_atomic(self.path, data)
                live.update(text=answer, state="done")
            except Exception as exc:
                live.update(state="error", error=classify_ai_error(exc).info())
        threading.Thread(target=run, daemon=True).start()
        return {"turn": turn_id, "state": "running", "new": True}      # new: not a repeat of a message already sent

    def poll(self, user_id, turn_id: str, start: int = 0) -> dict:
        with self._lock:
            live = self._live.get((str(user_id), turn_id))
            if live:
                return {"state": live["state"], "text": live["text"][max(0, start):], "length": len(live["text"]), "error": live["error"]}
        return {"state": "unknown", "text": "", "length": 0, "error": None}


# =============================================================================
# A deterministic stand-in for the API (tests, screenshots, demos): ARX_AI_FAKE=<scenario>
#   ok | repair | fallback | refusal | truncated | rate_limit | network | invalid_key | slow
# =============================================================================
class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text

    def to_dict(self):
        return {"type": "text", "text": self.text}


class _Usage:
    input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens = 1000, 400, 0, 0


class _Message:
    def __init__(self, text, stop="end_turn"):
        self.content, self.stop_reason, self.usage, self.model = [_Block(text)], stop, _Usage(), "fake-coach"


class _Stream:
    def __init__(self, text, stop="end_turn", delay=0.0):
        self._text, self._stop, self._delay = text, stop, delay

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    @property
    def text_stream(self):
        for i in range(0, len(self._text), 24):
            if self._delay:
                time.sleep(self._delay)
            yield self._text[i:i + 24]

    def get_final_message(self):
        return _Message(self._text, self._stop)


def fake_board(payload: dict, broken: bool = False) -> dict:
    """A plausible board made from the payload itself (proposal rows kept; 'broken' violates a bound)."""
    de = (payload.get("meta") or {}).get("language") == "de"
    unit = ((payload.get("meta") or {}).get("units") or {}).get("force", "kg")
    ls, pl = payload.get("last_session") or {}, (payload.get("planner") or {})
    prop = pl.get("proposal") or {}
    rows = []
    for x in prop.get("rows", []):
        s = x.get("settings") or {}
        rows.append({"exercise": x["exercise"], "sets": x["sets"], "target": (x["target"] * 1.3 if (broken and x["target"]) else x["target"]),
                     "effort": x["effort"], "rest_before_min": x["rest_before_min"],
                     "tempo": f"{s.get('reps') or 8} x {round(s.get('seconds_per_direction') or 5)} s, {s.get('pause_end_s') or 0}/{s.get('pause_return_s') or 0} s",
                     "cue": "Volle Kraft ab Wiederholung 1" if de else "Full force from rep 1",
                     "why": "Platzhalter" if broken else (x.get("why") or [("Plan der Engine übernommen." if de else "Engine plan kept.")])[0][:200]})
    verdicts = [{"exercise": x["exercise"], "rating": ("not_comparable" if not (x.get("vs_previous") or {}).get("same_settings") else
                                                       ("better" if ((x["vs_previous"].get("concentric_change_pct") or 0) > 1) else "same")) if x.get("vs_previous") else "first",
                 "note": f"{x.get('concentric_top3')} / {x.get('eccentric_top3')} {unit}, inroad {x.get('inroad_pct')} %"} for x in ls.get("exercises", [])]
    findings = (payload.get("history") or {}).get("findings") or []
    return {"last_session": {"headline": ("Testlauf ohne KI: Daten der letzten Einheit" if de else "Dry run without AI: data of the last session"),
                             "meaning": ("Dies ist eine Platzhalter-Antwort des eingebauten Testmodus." if de else "This is a placeholder answer of the built-in test mode."),
                             "consequence": ("Mit API-Schlüssel schreibt hier der Coach." if de else "With an API key the coach writes here."),
                             "verdicts": verdicts, "bullets": [(f"{len(ls.get('exercises', []))} Übungen ausgewertet." if de else f"{len(ls.get('exercises', []))} exercises analysed.")]},
            "next_training": {"readiness": ("Kein Check-in." if de else "No check-in."), "date": prop.get("date") or (payload.get("meta") or {}).get("today"),
                              "why_date": " ".join(t for t in (prop.get("why_this_date") or [])[:2] if t), "rows": rows,
                              "rest_note": ("Pausen wie geplant." if de else "Rests as planned."), "grip_note": "", "week_outlook": "", "changes_vs_previous": []},
            "history": {"four_weeks": ("Verlauf siehe Kennzahlen." if de else "See the figures for the history."), "longer_term": "",
                        "kpi_notes": [], "anomalies": [{"ref": f["ref"], "meaning": f.get("meaning") or "", "action": ""} for f in findings[:2]]},
            "focus": ("Jeden Satz wirklich zu Ende führen." if de else "Finish every set for real."),
            "key_recommendations": [("Plan einhalten." if de else "Stick to the plan.")],
            "suggested_questions": [("Warum diese Reihenfolge?" if de else "Why this order?"), ("Was bringt mir eine Griffhilfe?" if de else "What would a grip aid give me?"),
                                    ("Wie erreiche ich mein Ziel schneller?" if de else "How do I reach my goal faster?")]}


class FakeAnthropic:
    def __init__(self, scenario: str):
        self.scenario, self.calls = scenario, 0
        self.messages = self
        self.beta = self

    def stream(self, **kw):
        self.calls += 1
        sc = self.scenario
        if sc in ("rate_limit", "network", "invalid_key", "no_credit", "overloaded"):
            raise AIError(sc, f"fake {sc}")
        if sc == "refusal":
            return _Stream("", "refusal")
        if sc == "truncated":
            return _Stream('{"last_session": {"headline": "cut', "max_tokens")
        schema = ((kw.get("output_config") or {}).get("format") or {}).get("schema")
        if not schema:                                   # chat
            last = kw["messages"][-1]["content"]
            q = last if isinstance(last, str) else last[-1]["text"]
            return _Stream(f"(Testmodus) Du hast gefragt: {q} - mit API-Schlüssel antwortet hier der Coach auf Basis deiner Daten.", delay=0.01 if sc == "slow" else 0.0)
        first = kw["messages"][0]["content"]
        payload = json.loads(first if isinstance(first, str) else first[0]["text"])
        broken = sc == "fallback" or (sc == "repair" and len(kw["messages"]) == 1)
        if sc == "slow":
            time.sleep(1.5)
        return _Stream(json.dumps(fake_board(payload, broken), ensure_ascii=False))
