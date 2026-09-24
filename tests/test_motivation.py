"""v0.25.0 - motivation in the report, from the verified evidence (science.json adherence_and_motivation): the two
one-liners of the compact column, the gain-framing pass over the wording, adherence judged against real norms, and
the feature stamp that lets the athlete's own data say later what a text generation did."""
import json, os, re, unittest
from datetime import datetime, timedelta

import tests                                   # noqa: F401  (points ARX_DATA_DIR at a temp folder first)
from tests import fixtures as fx
from tests.test_history import series, report, ROW, PRESS
import arx_history as hist
import arx_plan as planner

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# words a gain-framed line never carries (the miss is never the subject - what a set gains is)
LOSS_WORDS = {"de": r"verpasst|nichts|kein reiz|\bsonst\b|verfehlt|krafttest|halbe?r? reiz|\bnur\b",
              "en": r"missed|nothing|no stimulus|or else|strength test|half the stimulus|\bonly\b"}
CFG = {"language": "de", "units": "metric"}
CFG_EN = {"language": "en", "units": "imperial"}


def meanings():
    with open(os.path.join(HERE, "meanings.json"), encoding="utf-8") as fh:
        return json.load(fh)


def row(name, **kw):
    base = {"name": name, "target_rule": "hold", "interp": {"code": "plan_hold", "params": {}}, "step_pct": 0.0}
    base.update(kw)
    return base


class SideLines(unittest.TestCase):
    """Which line the column shows - the priority of the situations, checked on hand-built plan / session dicts."""
    def next_code(self, plan, ls=None, cfg=CFG):
        it = hist.side_lines(plan, ls, cfg)["next"]
        return it and it["code"], it

    def test_next_session_situations_in_their_order(self):
        s = {"date": "2026-09-26", "est_minutes": 31, "exercises": [row("Row"), row("Belt Squat")], "why_this_date": []}
        self.assertEqual(self.next_code({"next_session": s})[0], "side_next_ready")
        self.assertEqual(self.next_code({"next_session": s, "today": {"rest_today": True}})[0], "side_next_rest")
        self.assertEqual(self.next_code({"next_session": s, "training_break": {"days": 19, "tier": "short"}})[0], "side_next_break")
        s2 = dict(s, repeat_note={"code": "plan_repeat_after_miss", "params": {"exercises": ["Row"], "k": 1}})
        code, it = self.next_code({"next_session": s2})
        self.assertEqual((code, it["text"]["meaning"]), ("side_next_repeat", "Row: noch offen - ab Wiederholung eins alles."))
        eff = row("Row", target_rule="hold_reach_effort", interp={"code": "plan_hold_effort", "params": {"last_pct": 14, "effort_pct": 20}})
        code, it = self.next_code({"next_session": dict(s, exercises=[eff, row("Belt Squat")])})
        self.assertEqual((code, it["params"]["gap_pct"], it["text"]["meaning"]), ("side_next_effort", 6, "Row: das Ziel liegt 6 % höher - ab Wiederholung eins alles."))
        code, it = self.next_code({"next_session": dict(s, exercises=[row("Row", target_rule="step", step_pct=2.0), row("Belt Squat")])})
        self.assertEqual((code, it["text"]["meaning"]), ("side_next_step", "2 Übungen, 2 % über letztes Mal - der Schritt, der wirkt."))
        self.assertEqual(self.next_code({"next_session": dict(s, exercises=[row("Row", target_rule="plateau_add_set", sets=2)])})[0], "side_next_second_set")
        self.assertEqual(self.next_code({"next_session": dict(s, why_this_date=[{"code": "date_week_last_chance"}])})[0], "side_next_last_chance")
        win = dict(s, time_window={"minutes": 15, "interp": {"code": "window_applied"}})
        code, it = self.next_code({"next_session": win})
        self.assertEqual((code, it["text"]["meaning"]), ("side_next_window", "15 Minuten reichen: Row, Belt Squat."))
        self.assertEqual(self.next_code({"next_session": s}, {"exercises": [{"name": "Row", "is_pb": True}]})[0], "side_next_pb_hold")
        self.assertIsNone(self.next_code({"next_session": None})[0])
        en = hist.side_lines({"next_session": s}, None, CFG_EN)["next"]["text"]["meaning"]
        self.assertEqual(en, "2 exercises, ≈ 31 min - your plan is ready.")

    def test_last_session_situations_in_their_order(self):
        def last(ls, cfg=CFG):
            it = hist.side_lines({}, ls, cfg)["last"]
            return it and it["code"], it
        hit = {"name": "Row", "inroad": 24, "inroad_target": 20}
        miss = {"name": "Belt Squat", "inroad": 12, "inroad_target": 20}
        border = {"name": "Press", "inroad": 18, "inroad_target": 20}          # within the borderline = a hit
        self.assertIsNone(last(None)[0])
        self.assertEqual(last({"date": "2026-09-23", "exercises": [hit, border]})[0], "side_last_full")
        code, it = last({"date": "2026-09-23", "exercises": [hit, miss, border]})
        self.assertEqual((code, it["text"]["meaning"]), ("side_last_part", "2 von 3 am Ermüdungsziel - der Rest holt's nächstes Mal."))
        self.assertEqual(last({"date": "2026-09-23", "exercises": [dict(hit, is_pb=True), miss]})[0], "side_last_pb")
        self.assertEqual(last({"date": "2026-09-23", "exercises": [{"name": "Row", "is_pb": True, "inroad": None}]})[0], "side_last_pb_only")
        self.assertEqual(last({"date": "2026-09-23", "gap_days": 21, "exercises": [hit]})[0], "side_last_back")
        code, it = last({"date": "2026-09-23", "open": True, "repeat_now": ["Belt Squat"], "exercises": [hit, miss]})
        self.assertEqual((code, it["text"]["meaning"]), ("side_last_open", "Jetzt gleich nochmal? Belt Squat - deine Wahl."))
        self.assertEqual(last({"date": "2026-09-23", "exercises": [dict(hit, restriction="careful"), dict(miss, effort_capped="low_force")]})[0], "side_last_careful")
        self.assertEqual(last({"date": "2026-09-23", "exercises": [{"name": "Row", "inroad": None}]})[0], "side_last_logged")
        self.assertEqual(last({"date": "2026-09-23", "exercises": [hit, miss, border]}, CFG_EN)[1]["text"]["meaning"],
                         "2 of 3 at the fatigue target - the rest catches up next time.")

    def test_a_real_report_carries_both_lines_in_both_languages(self):
        start = datetime(2026, 8, 1, 10)
        rows = series(ROW, start, [1, 1.02, 1.04, 1.06], every=4) + series(PRESS, start, [1, 1, 1.01, 1.02], every=4)
        for lang, units in (("de", "metric"), ("en", "imperial")):
            r = report(rows, "2026-08-14", language=lang, units=units)
            for key in ("next", "last"):
                it = r["side"][key]
                self.assertTrue(it and it["code"].startswith(f"side_{key}_"), (lang, key, it))
                text = it["text"]["meaning"]
                self.assertTrue(text and "{" not in text and "_" not in text, (lang, key, text))
                self.assertFalse(re.search(LOSS_WORDS[lang], text, re.IGNORECASE), (lang, key, text))
            self.assertEqual(r["features"], planner.FEATURES)


class GainFrame(unittest.TestCase):
    def test_every_side_line_is_short_and_gain_framed(self):
        table = meanings()
        codes = [c for c in table if c.startswith("side_")]
        self.assertGreaterEqual(len(codes), 16)
        for code in codes:
            for lang in ("de", "en"):
                text = table[code]["meaning"][lang]
                self.assertTrue(text, (code, lang))
                self.assertLessEqual(len(text.split()), 14, (code, lang, text))
                self.assertFalse(re.search(LOSS_WORDS[lang], text, re.IGNORECASE), (code, lang, text))

    def test_the_old_loss_frames_are_gone(self):
        with open(os.path.join(HERE, "meanings.json"), encoding="utf-8") as fh:
            raw = fh.read()
        for phrase in ("halber Reiz", "halbe Reiz", "half the stimulus", "Fehlversuch", "Nochmal, richtig", "Once more, properly", "Once more, now, properly",
                       "war für {exercises} ein Krafttest", "Nur {hits} von", "Only {hits} of"):
            self.assertNotIn(phrase, raw, phrase)
        with open(os.path.join(HERE, "web", "index.html"), encoding="utf-8") as fh:
            page = fh.read()
        for phrase in ("halber Reiz", "half the stimulus", "Nochmal, richtig", "Once more, properly"):
            self.assertNotIn(phrase, page, phrase)

    def test_the_gap_to_the_target_is_a_number(self):
        it = hist.item("repeat_now", {"last_pct": 14, "target_pct": 20, "gap_pct": 6}, CFG)
        self.assertEqual(it["text"]["meaning"], "14 % Ermüdung im Satz - eine echte Belastung, das Ziel liegt 6 % höher.")
        self.assertTrue(it["text"]["action"].startswith("Wenn du magst: nochmal"))
        it = hist.item("plan_effort_tempo", {"misses": 2, "effort_pct": 20, "last_pct": 11, "gap_pct": 9, "target_kg": 100, "tempo_from": 4, "tempo_to": 5, "pause_to": 2}, CFG_EN)
        self.assertIn("the target sits 9 % higher", it["text"]["meaning"])

    def test_a_missed_week_is_judged_against_the_norm(self):
        it = hist.item("finding_missed_weekly_target", {"week": "W37", "sessions": 2, "target": 3, "weeks": 6, "rate_pct": 72}, CFG)
        self.assertEqual(it["text"]["meaning"], "Woche W37: 2 von 3 geplanten Einheiten - in den letzten 6 Wochen 72 % des Plans; 60-80 % sind bei Erwachsenen normal.")
        self.assertEqual((planner.ADHERENCE_NORM_LOW, planner.ADHERENCE_NORM_HIGH), (60, 80))
        start = datetime(2026, 8, 3, 10)                                   # Monday; two sessions a week wanted, one done in the last full week
        rows = series(ROW, start, [1] * 6, every=7) + [fx.make_set(900, PRESS, start + timedelta(days=1))]
        r = report(rows, "2026-09-16", sessions_per_week=2)
        found = [f for f in r["history"]["findings"]["all"] if f["type"] == "missed_weekly_target"]
        self.assertTrue(found)
        self.assertIn("60-80 %", found[0]["interp"]["text"]["meaning"])
        self.assertTrue(0 < found[0]["interp"]["params"]["rate_pct"] <= 100)


class FeatureStamp(unittest.TestCase):
    def test_the_ledger_carries_the_text_generation_and_refreshes_it(self):
        rows = series(ROW, datetime(2026, 8, 1, 10), [1, 1.02, 1.04], every=4)
        r = report(rows, "2026-08-12")
        today = datetime(2026, 8, 12).date()
        entry = planner.ledger_entry(r["plan"], today)
        self.assertEqual(entry["features"], planner.FEATURES)
        old = [dict(entry, features={"side_lines": 0})]                   # an entry made by an older version
        entries, changed = planner.update_ledger(old, r["plan"], today)
        self.assertTrue(changed)
        self.assertEqual(entries[-1]["features"], planner.FEATURES)
        self.assertEqual(planner.update_ledger(entries, r["plan"], today)[1], False)   # nothing changed: nothing written
        self.assertEqual(planner.SCIENCE["FEATURES"], "adherence_and_motivation")


if __name__ == "__main__":
    unittest.main()


class CoachEffects(unittest.TestCase):
    """v0.27.0: what a sentence of the live coach did to the force - cued repetition vs the same transition without a cue."""
    @staticmethod
    def a_set(exercise, deltas, cues=None):
        reps, mean = [], 100.0
        for i, d in enumerate(deltas, start=1):
            mean = mean * (1 + d / 100.0) if i > 1 else mean
            reps.append({"i": i, "con_mean": mean, "ecc_mean": mean * 1.4})
        return {"exercise": exercise, "detail": {"reps": reps}, "coach": {"cues": cues or []} if cues is not None else None}

    def test_the_cued_repetition_is_judged_against_the_uncued_transition(self):
        quiet = [self.a_set(3, [0, -3, -3, -3, -3, -3], cues=[]) for _ in range(6)]           # 3 % lost per repetition, no cue
        loud = [self.a_set(3, [0, -3, -3, +1, -3, -3], cues=[{"id": "e_resist", "group": "eccentric", "kind": "general", "rep": 4, "t": 30.0}]) for _ in range(5)]
        safety = [self.a_set(3, [0, -3, -3, -3, -3, -3], cues=[{"id": "m_breathe", "group": "breathe", "kind": "safety", "rep": 4, "t": 30.0}])]
        out = hist.coach_effects(quiet + loud + safety, CFG)
        self.assertEqual((out["available"], out["sets_with_notes"], len(out["groups"])), (True, 12, 1))
        g = out["groups"][0]
        self.assertEqual((g["group"], g["n"], g["control_n"]), ("eccentric", 5, 5))
        self.assertAlmostEqual(g["delta_pct"], 4.0, places=1)                                   # +1 % after the cue vs -3 % without = +4 points
        self.assertEqual(out["interp"]["code"], "coach_effects_ready")
        self.assertIn("+4", g["interp"]["text"]["meaning"])
        few = hist.coach_effects(quiet + loud[:2], CFG)
        self.assertEqual((few["groups"], few["interp"]["code"]), ([], "coach_effects_pending"))
        self.assertEqual(hist.coach_effects([self.a_set(3, [0, -3])], CFG)["available"], False)  # no notes at all


class HowDoYouTick(unittest.TestCase):
    """v0.26.0: the three-question motivational profile - vocabulary only, the answers first, then the age band;
    the rules reach the one-liners, the AI's wording rules and the export's athlete line."""
    def test_the_profile_is_vocabulary_only(self):
        import arx_app as app
        got = app.clean_profile({"coaching": {"compare": "others", "tone": "calm", "drive": "keep", "extra": "x", "note": "free text"}}, fx.CATALOG, datetime(2026, 9, 24).date())
        self.assertEqual(got["coaching"], {"compare": "others", "tone": "calm", "drive": "keep"})
        self.assertIsNone(app.clean_profile({"coaching": {"compare": "louder", "tone": 5}}, fx.CATALOG, datetime(2026, 9, 24).date())["coaching"])
        self.assertIsNone(app.clean_profile({"coaching": "yes"}, fx.CATALOG, datetime(2026, 9, 24).date())["coaching"])
        self.assertIn("coaching", app.PROFILE_KEYS)

    def test_the_answers_win_then_the_age_band_decides(self):
        rules = planner.coaching_rules({}, {"age_band": "40-49", "sex": "male"})
        self.assertEqual((rules["frame"], rules["compare"], rules["tone"], rules["answered"]), ("gain", "self", "push", []))
        self.assertEqual(planner.coaching_rules({}, {"age_band": "60-69"})["frame"], "keep")                     # what a set KEEPS, at 60+
        self.assertEqual(planner.coaching_rules({}, {"age_band": "70+", "sex": "male"})["compare"], "self")     # competition never a default
        rules = planner.coaching_rules({"coaching": {"compare": "others", "tone": "numbers", "drive": "gain"}}, {"age_band": "70+"})
        self.assertEqual((rules["frame"], rules["compare"], rules["tone"]), ("gain", "others", "numbers"))        # the athlete's word beats the band
        self.assertEqual(planner.coaching_rules({"coaching": {"drive": "nudge"}}, {"age_band": "60-69"})["frame"], "gain")
        self.assertEqual(planner.coaching_rules({"coaching": {"compare": "no"}}, None)["compare"], "self")       # an unknown word is no answer

    def test_the_rules_swap_the_one_liners(self):
        s = {"date": "2026-09-26", "est_minutes": 31, "exercises": [row("Row", target_rule="step", step_pct=2.0), row("Belt Squat")], "why_this_date": []}
        keep = hist.side_lines({"next_session": s}, None, CFG, {"frame": "keep", "compare": "self"})["next"]
        self.assertEqual((keep["code"], keep["text"]["meaning"]), ("side_next_step_keep", "2 Übungen, 2 % mehr - so bleibt die Kraft, die du hast."))
        none = hist.side_lines({"next_session": s}, None, CFG, {"frame": "gain", "compare": "none"})["next"]
        self.assertEqual(none["code"], "side_next_step_none")
        self.assertNotIn("letztes Mal", none["text"]["meaning"])
        last = hist.side_lines({}, {"date": "2026-09-23", "exercises": [{"name": "Row", "inroad": 24, "inroad_target": 20}]}, CFG, {"frame": "keep"})["last"]
        self.assertEqual(last["code"], "side_last_full_keep")
        plain = hist.side_lines({"next_session": s}, None, CFG, {"frame": "gain", "compare": "self"})["next"]
        self.assertEqual(plain["code"], "side_next_step")
        table = meanings()
        for code in (c for c in table if c.startswith("side_") and (c.endswith("_keep") or c.endswith("_none"))):
            for lang in ("de", "en"):
                self.assertFalse(re.search(LOSS_WORDS[lang], table[code]["meaning"][lang], re.IGNORECASE), code)

    def test_a_report_carries_the_answers_and_the_rules_and_the_ai_gets_rules_only(self):
        import arx_ai as ai
        rows = series(ROW, datetime(2026, 8, 1, 10), [1, 1.02, 1.04], every=4)
        r = report(rows, "2026-08-12", coaching={"compare": "none", "tone": "calm", "drive": "keep"})
        self.assertEqual(r["profile"]["coaching"], {"compare": "none", "tone": "calm", "drive": "keep"})
        self.assertEqual((r["coaching_rules"]["frame"], r["coaching_rules"]["compare"], r["coaching_rules"]["tone"]), ("keep", "none", "calm"))
        self.assertIn(r["side"]["next"]["code"], ("side_next_ready_keep", "side_next_step_keep", "side_next_step_none", "side_next_pb_hold_keep",
                                                   "side_next_effort", "side_next_repeat"))
        payload = ai.build_payload(r, fx.cfg("2026-08-12", coaching={"compare": "none", "tone": "calm", "drive": "keep"}))
        self.assertEqual(payload["profile"]["wording_rules"], {"frame": "keep", "compare": "none", "tone": "calm"})
        self.assertNotIn("coaching", json.dumps(payload["profile"]))                    # the answers never travel as a label
        self.assertEqual(ai.lint_payload(payload), [])

    def test_the_answers_ride_on_the_export_line_only_when_known(self):
        import arx_export as exporter
        person = exporter.person_facts({}, {"1": {"coaching": {"compare": "self", "tone": "loud", "drive": "keep"}}, "2": {"coaching": {}}, "3": {}})
        self.assertEqual(person(1), {"coaching": {"compare": "self", "drive": "keep"}})
        self.assertEqual((person(2), person(3)), ({}, {}))
