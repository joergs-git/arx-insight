"""v0.5.0 AI coach without a single billed call: the payload (name-free, athlete's units, deterministic),
the closed schema, one test per validation rule, repair and engine fallback, the memory, background
jobs, the chat (cached prefix, a failed turn commits nothing, limits, name scrubbing) and the routes."""
import copy, json, os, tempfile, threading, time, unittest
from datetime import datetime, timedelta

import tests                                   # noqa: F401  (points ARX_DATA_DIR at a temp folder first)
from tests import fixtures as fx
from tests.test_app_safety import ServerCase, call, HDR, JSON
from tests.test_plan import CATALOG, history, ROW, PRESS, SQUAT, CURL, PULLDOWN, DEADLIFT
import arx_ai as ai
import arx_app as app
import arx_report as core

OK = {**HDR, **JSON}


def make(today="2026-09-18", rows=None, **extra):
    extra.setdefault("_catalog", CATALOG)
    extra.setdefault("session_minutes", 40)
    cfg = fx.cfg(today, **extra)
    report = core.build_report(fx.FakeDB(rows if rows is not None else history([ROW, CURL, PRESS, SQUAT, DEADLIFT])), cfg)
    return report, cfg


def good_board(report, cfg):
    return ai.fake_board(ai.build_payload(report, cfg))


class FakeEnv(unittest.TestCase):
    scenario = "ok"

    def setUp(self):
        os.environ["ARX_AI_FAKE"] = self.scenario
        self.addCleanup(os.environ.pop, "ARX_AI_FAKE", None)


class Payload(unittest.TestCase):
    def setUp(self):
        checkin = {"date": "2026-09-18", "sleep": "ok", "energy": "high", "soreness": {}, "note": "ask Anna about it"}
        self.report, self.cfg = make(units="imperial", checkin=checkin, restrictions={"shoulder": "careful"}, alias="Anna Example")
        self.payload = ai.build_payload(self.report, self.cfg)
        self.text = ai.dumps_payload(self.payload)

    def test_nothing_personal_and_nothing_metric_leaves_the_machine(self):
        self.assertEqual(ai.lint_payload(self.payload), [])
        self.assertNotIn("Anna", self.text)                              # neither the alias nor the free-text note
        self.assertEqual(self.report["readiness"]["note"], "ask Anna about it")
        self.assertEqual(self.payload["meta"]["units"], {"force": "lb", "length": "in"})
        row = self.payload["planner"]["proposal"]["rows"][0]
        engine = next(x for x in self.report["plan"]["next_session"]["exercises"] if x["name"] == row["exercise"])
        self.assertAlmostEqual(row["target"], engine["target_peak_kg"] * 2.20462, delta=0.06)
        self.assertTrue(ai.lint_payload({"a": [{"weight_kg": 1}], "name": "x"}))                 # the lint itself works

    def test_same_data_same_bytes_and_memory_does_not_change_the_key(self):
        again = ai.build_payload(*make(units="imperial", checkin=self.cfg["checkin"], restrictions={"shoulder": "careful"}))
        self.assertEqual(ai.dumps_payload(again), self.text)
        with_memory = ai.build_payload(self.report, self.cfg, [{"given_on": "2026-09-12", "focus": "x"}])
        self.assertEqual(ai.payload_key(with_memory, "m", "high"), ai.payload_key(self.payload, "m", "high"))
        self.assertNotEqual(ai.payload_key(self.payload, "m", "low"), ai.payload_key(self.payload, "m", "high"))

    def test_what_the_coach_needs_is_there(self):
        p = self.payload
        ex = p["last_session"]["exercises"][0]
        self.assertTrue(ex["per_rep_concentric"] and ex["per_rep_eccentric"] and ex["context"])
        self.assertEqual(len(p["history"]["exercises"][0]["series"]["date"]), len(p["history"]["exercises"][0]["series"]["comparable"]))
        self.assertTrue(p["planner"]["decision_space"]["dates"][0]["candidates"])
        self.assertIn("effort_cap", p["planner"]["decision_space"])
        self.assertEqual(p["previous_recommendations"], [])

    def test_the_coach_is_told_that_a_muscle_needs_its_weekly_stimulus(self):
        report, cfg = make(sessions_per_week=1, structure="split")
        planner_block = ai.build_payload(report, cfg)["planner"]
        self.assertIn("Ganzkörper" if cfg["language"] == "de" else "full body", planner_block["structure_note"])
        self.assertTrue(planner_block["dose_note"])                       # muscle goal at one session a week
        self.assertEqual(planner_block["frequency_warnings"], [])
        prompt = ai.system_prompt("board")[0]["text"]
        self.assertIn("at least about once a week", prompt)
        self.assertIn("never recommend alternating muscle groups at one session a week", prompt)

    def test_the_coach_is_told_about_a_break_and_may_lower_the_targets(self):
        report, cfg = make(today="2026-10-25")                              # 40 days after the last session
        payload = ai.build_payload(report, cfg)
        self.assertEqual(ai.lint_payload(payload), [])
        brk = payload["planner"]["training_break"]
        self.assertEqual((brk["tier"], brk["days_since_last_session"]), ("long", 40))
        self.assertTrue(brk["engine_says"])
        floors = payload["planner"]["decision_space"]["target_floor_pct"]
        self.assertEqual(set(floors), {r["exercise"] for r in payload["planner"]["proposal"]["rows"]})
        self.assertIsNone(ai.build_payload(*make())["planner"]["training_break"])     # no break, no word about one
        board = good_board(report, cfg)                                     # the engine's own comeback plan passes ...
        self.assertEqual(ai.validate_board(board, report, cfg)[0], [])
        low = copy.deepcopy(board)                                          # ... and so does a target 12 % below the old reference
        row = low["next_training"]["rows"][0]
        row["target"] = round(row["target"] * 0.88, 1)
        row["why"] = "After 40 days away the old reference is only an orientation, so the first set starts lower."
        self.assertEqual(ai.validate_board(low, report, cfg)[0], [])
        prompt = ai.system_prompt("board")[0]["text"]
        self.assertIn('NEVER try to "catch up"', prompt)
        self.assertIn("training_breaks", prompt)                            # the science line is in the prompt as well

    def test_the_coach_knows_what_the_athlete_does_not_do_on_the_machine(self):
        report, cfg = make(excluded_exercises={"11": "elsewhere", "10": "unwanted", "23": "injury"})
        p = ai.build_payload(report, cfg)
        self.assertEqual(p["profile"]["exercises_switched_off"], [{"exercise": "Biceps Curl", "reason": "trained_elsewhere"},
                                                                  {"exercise": "Dead Lift", "reason": "not_wanted"},
                                                                  {"exercise": "Horizontal Press", "reason": "health_not_possible"}])
        self.assertIn("health_not_possible", ai.system_prompt("board")[0]["text"])
        self.assertGreaterEqual(ai.PROMPT_VERSION, 9)
        care = ai.build_payload(*make(excluded_exercises={"23": "careful"}))
        self.assertEqual(care["profile"]["exercises_with_care"], ["Horizontal Press"])
        self.assertEqual(care["profile"]["exercises_switched_off"], [])
        self.assertIn("exercises_with_care", ai.system_prompt("board")[0]["text"])
        day = "2026-09-20"                                                     # today's check-in about single exercises (v0.8.9)
        today = ai.build_payload(*make(today=day, checkin={"date": day, "sleep": "good", "energy": "high", "soreness": {},
                                                             "exercises": {"23": "injury", "3": "careful"}}))
        self.assertEqual((today["readiness_today"]["exercises_not_today"], today["readiness_today"]["exercises_with_care_today"]),
                         (["Horizontal Press"], ["Row"]))
        self.assertEqual(ai.lint_payload(today), [])
        self.assertIn("exercises_not_today", ai.system_prompt("board")[0]["text"])
        self.assertGreaterEqual(ai.PROMPT_VERSION, 19)
        self.assertIn("inroad_calibration", today["history"]); self.assertIn("inroad_mode_pct", today["planner"]["proposal"]["rows"][0])
        self.assertIn("mode_hint", today["planner"]["proposal"]["rows"][0]); self.assertIn("mode_hint", ai.system_prompt("board")[0]["text"])
        self.assertIn("beginner_note", today["planner"]["proposal"]); self.assertIn("BEGINNER", ai.system_prompt("board")[0]["text"])
        self.assertIn("session_open", today["last_session"]); self.assertIn("repeat_now", today["last_session"]["exercises"][0])
        self.assertIn("repeat_note", today["planner"]["proposal"]); self.assertIn("session_open", ai.system_prompt("board")[0]["text"])
        # a missed effort target is acted on (v0.10.0): the coach sees the streak, the rule, and a filler why is refused
        hist = today["history"]["exercises"][0]
        self.assertIn("effort_misses_in_a_row", hist); self.assertIn("inroad_target_pct", hist)
        self.assertIn("effort_misses_in_a_row", ai.system_prompt("board")[0]["text"])
        self.assertIn("settings_change", json.dumps(today["planner"]["proposal"]["rows"][0]))
        self.assertTrue(ai.is_filler("Platzhalter") and ai.is_filler(" - ") and ai.is_filler("") and not ai.is_filler("Der Satz bleibt die saubere Messung."))
        self.assertIn("sore_regions", ai.system_prompt("board")[0]["text"])     # soreness / pain = reasons behind the flags (v0.9.0)
        self.assertEqual(p["profile"]["muscles_trained_elsewhere"], ["elbow_flexors"])
        offered = {c["exercise"] for d in p["planner"]["decision_space"]["dates"] for c in d["candidates"]}
        self.assertFalse(offered & {"Biceps Curl", "Dead Lift"})                       # not in the decision space ...
        schema = json.dumps(ai.board_schema(report))
        self.assertNotIn("Biceps Curl", schema.split('"next_training"')[1].split('"history"')[0])   # ... so the plan rows cannot name it
        self.assertEqual(ai.lint_payload(p), [])
        # an exercise WITH a history goes quiet as well: no series, no effects, no kpi note about it (v0.7.1)
        self.assertNotIn("Biceps Curl", [e["exercise"] for e in p["history"]["exercises"]])
        self.assertIn("Row", [e["exercise"] for e in p["history"]["exercises"]])
        self.assertFalse([x for x in p["history"]["own_evidence"]["order_effects"] if "Biceps Curl" in (x["first"], x["then"])])
        kpi = ai.board_schema(report)["properties"]["history"]["properties"]["kpi_notes"]["items"]["properties"]["exercise"]["enum"]
        self.assertNotIn("Biceps Curl", kpi)
        self.assertNotIn("Dead Lift", kpi)
        self.assertEqual(ai.build_payload(*make())["profile"]["exercises_switched_off"], [])
        prompt = ai.system_prompt("board")[0]["text"]
        self.assertIn("exercises_switched_off is never recommended", prompt)
        self.assertIn("trained_elsewhere", prompt)
        self.assertIn("exercises they do not do on the ARX", ai.system_prompt("chat")[0]["text"])
        self.assertGreaterEqual(ai.PROMPT_VERSION, 4)                                  # a changed prompt is a new cache key

    def test_the_session_size_is_a_time_decision_and_the_coach_knows_its_price(self):
        report, cfg = make(partner=True)
        p = ai.build_payload(report, cfg)
        space, sess = p["planner"]["decision_space"], report["plan"]["next_session"]
        self.assertEqual(space["rows_in_time_budget"], len(sess["exercises"]))
        self.assertGreater(space["bounds"]["rows_max"], space["rows_in_time_budget"])    # more is possible - it costs time, no rule
        self.assertGreater(space["minutes_per_extra_exercise"], 1)
        self.assertIs(p["profile"]["takes_turns_with_partner"], True)
        self.assertEqual(set(p["profile"]["exercises_by_minutes_per_session"]), {"15", "20", "30", "45", "60", "75", "90"})
        self.assertEqual(ai.lint_payload(p), [])
        prompt = ai.system_prompt("board")[0]["text"]
        self.assertIn("TIME decision of the athlete", prompt)
        self.assertIn("never recommend shortening it", prompt)
        self.assertIn("minutes per session decide how many exercises are planned", ai.system_prompt("chat")[0]["text"])
        self.assertGreaterEqual(ai.PROMPT_VERSION, 5)

    def test_the_coach_knows_todays_time_window_and_cannot_blow_it(self):
        day = "2026-09-20"                                                   # the engine plans this day; the athlete has 15 minutes
        report, cfg = make(today=day, checkin={"date": day, "minutes": 15})
        p = ai.build_payload(report, cfg)
        sess = report["plan"]["next_session"]
        self.assertEqual(sess["date"], day)
        self.assertEqual(p["readiness_today"]["minutes_available_today"], 15)
        tw = p["planner"]["proposal"]["time_window"]
        self.assertEqual((tw["minutes"], tw["exercises_kept"]), (15, len(sess["exercises"])))
        self.assertTrue(tw["left_out_for_next_time"] and tw["engine_says"])
        self.assertLessEqual(p["planner"]["decision_space"]["bounds"]["rows_max"], len(sess["exercises"]) + 1)
        self.assertEqual(ai.lint_payload(p), [])
        free = ai.build_payload(*make(today=day))
        self.assertIsNone(free["readiness_today"]["minutes_available_today"])
        self.assertIsNone(free["planner"]["proposal"]["time_window"])
        prompt = ai.system_prompt("board")[0]["text"]
        self.assertIn("minutes_available_today", prompt)
        self.assertIn("never add rows or sets beyond it", prompt)
        self.assertGreaterEqual(ai.PROMPT_VERSION, 6)

    def test_the_coach_knows_that_the_checkin_not_the_clock_made_today_smaller(self):
        day = "2026-09-20"
        report, cfg = make(today=day, checkin={"date": day, "sleep": "poor", "energy": "ok", "soreness": {}, "minutes": 90})
        p = ai.build_payload(report, cfg)
        said = p["planner"]["proposal"]["checkin_adjustment"] or p["planner"]["today_instead_adjustment"]
        self.assertTrue(said and "check-in" in said.lower(), said)
        self.assertEqual(p["readiness_today"]["minutes_available_today"], 90)
        self.assertEqual(ai.lint_payload(p), [])
        prompt = ai.system_prompt("board")[0]["text"]
        self.assertIn("never promise more exercises for today because there is time", prompt)
        self.assertGreaterEqual(ai.PROMPT_VERSION, 7)

    def test_the_system_prompt_is_static_and_carries_the_science(self):
        a, b = ai.system_prompt("board")[0]["text"], ai.system_prompt("board")[0]["text"]
        self.assertEqual(a, b)
        self.assertIn("recovery_between_sessions", a)
        self.assertNotIn("lb", a.split("SCIENCE BASE")[0].split())        # no athlete-specific unit or value: one prompt for everyone
        self.assertIn("LIVE CONVERSATION", ai.system_prompt("chat")[0]["text"])


class Schema(unittest.TestCase):
    def test_every_object_is_closed_and_every_property_required(self):
        report, _ = make()
        seen = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    seen.append(node)
                    self.assertIs(node["additionalProperties"], False)
                    self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
                if "enum" in node:
                    self.assertTrue(node["enum"])
                for bad in ("minimum", "maximum", "minLength", "maxLength", "anyOf"):
                    self.assertNotIn(bad, node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(ai.board_schema(report))
        self.assertGreater(len(seen), 5)
        empty = ai.board_schema(core.build_report(fx.FakeDB([]), fx.cfg("2026-09-18", _catalog=CATALOG)))
        walk(empty)                                                       # an athlete without history: still a valid schema


class Validation(unittest.TestCase):
    def setUp(self):
        self.report, self.cfg = make(rows=history([ROW, CURL, PRESS, SQUAT, DEADLIFT], factors=[1.0, 1.04, 1.08, 1.12, 1.16], decline=0.06))
        self.board = good_board(self.report, self.cfg)

    def codes(self, board, report=None, cfg=None):
        return sorted({p["code"] for p in ai.validate_board(board, report or self.report, cfg or self.cfg)[0]})

    def test_the_engines_own_plan_passes(self):
        problems, changes = ai.validate_board(self.board, self.report, self.cfg)
        self.assertEqual((problems, changes), ([], []))

    def test_each_rule(self):
        rows = lambda b: b["next_training"]["rows"]
        b = copy.deepcopy(self.board); b["next_training"]["date"] = "2027-01-01"
        self.assertEqual(self.codes(b), ["date_not_feasible"])
        b = copy.deepcopy(self.board); rows(b)[0]["target"] = rows(b)[0]["target"] * 1.2; rows(b)[0]["why"] = "The last three sessions show clear room for more."
        self.assertEqual(self.codes(b), ["target_out_of_bounds"])
        b = copy.deepcopy(self.board); rows(b)[0]["sets"] = 4; rows(b)[0]["why"] = "More volume because the athlete has a lot of time."
        self.assertEqual(self.codes(b), ["sets_out_of_bounds"])
        b = copy.deepcopy(self.board); rows(b)[1]["rest_before_min"] = 25; rows(b)[1]["why"] = "A very long rest to be completely fresh again."
        self.assertEqual(self.codes(b), ["rest_out_of_bounds"])
        b = copy.deepcopy(self.board); rows(b).append(dict(rows(b)[0]))
        self.assertIn("duplicate_exercise", self.codes(b))
        b = copy.deepcopy(self.board); rows(b)[0]["exercise"] = "Leg Swing"
        self.assertIn("not_trainable_that_day", self.codes(b))
        b = copy.deepcopy(self.board)                                     # the curl before the row: its target is the row's helper
        names = [r["exercise"] for r in rows(b)]
        i, j = names.index("Row"), names.index("Biceps Curl")
        rows(b)[i], rows(b)[j] = rows(b)[j], rows(b)[i]
        self.assertEqual(self.codes(b), ["target_before_its_means"])

    def test_a_change_needs_a_reason_and_a_reasoned_change_passes(self):
        b = copy.deepcopy(self.board)
        r = b["next_training"]["rows"][0]
        r["target"], r["why"] = round(r["target"] * 1.03, 1), "ok"
        self.assertEqual(self.codes(b), ["change_without_reason"])
        r["why"] = "Concentric strength rose on four comparable days, so a slightly higher target is justified."
        problems, changes = ai.validate_board(b, self.report, self.cfg)
        self.assertEqual(problems, [])
        self.assertEqual([(c["exercise"], c["field"]) for c in changes], [(r["exercise"], "target")])

    def test_careful_exercises_and_the_effort_cap(self):
        report, cfg = make(restrictions={"knee": "careful"})
        b = good_board(report, cfg)
        squat = next(r for r in b["next_training"]["rows"] if r["exercise"] == "Belt Squat")
        squat.update(effort="deep", target=300.0, why="The legs are fully recovered, so they can go all out today.")
        self.assertIn("needs_submax", self.codes(b, report, cfg))
        checkin = {"date": "2026-09-19", "sleep": "ok", "energy": "ok", "soreness": {}}               # moderate day: no deep sets
        report, cfg = make(today="2026-09-19", checkin=checkin)
        b = good_board(report, cfg)
        if report["plan"]["next_session"]["date"] == "2026-09-19":
            b["next_training"]["rows"][0].update(effort="deep", why="The athlete looks strong enough for a deep set today.")
            self.assertIn("effort_above_cap", self.codes(b, report, cfg))

    def test_enum_values_may_differ_in_capitalisation(self):
        b = copy.deepcopy(self.board)
        b["next_training"]["rows"][0]["exercise"] = b["next_training"]["rows"][0]["exercise"].upper()
        self.assertEqual(self.codes(b), [])

    def test_force_numbers_that_are_not_in_the_payload_are_reported(self):
        payload_text = ai.dumps_payload(ai.build_payload(self.report, self.cfg))
        b = copy.deepcopy(self.board)
        b["focus"] = "Hold 999 kg on the Row and add +5 kg next time."
        self.assertEqual(ai.unverified_forces(b, payload_text, "kg"), ["999 kg"])


class MakeBoard(FakeEnv):
    def test_ok_repair_and_fallback(self):
        report, cfg = make()
        rec = ai.make_board(report, cfg)
        self.assertEqual((rec["plan_source"], rec["repaired"], rec["problems"], len(rec["usage"])), ("coach", False, [], 1))
        os.environ["ARX_AI_FAKE"] = "repair"
        client = ai.FakeAnthropic("repair")
        seen = []
        orig = client.stream
        client.stream = lambda **kw: (seen.append(copy.deepcopy(kw["messages"])), orig(**kw))[1]
        rec = ai.make_board(report, cfg, client=client)
        self.assertEqual((rec["plan_source"], rec["repaired"], len(rec["usage"])), ("coach", True, 2))
        self.assertEqual(seen[1][0], seen[0][0])                          # append-only: the first message is untouched
        self.assertEqual([m["role"] for m in seen[1]], ["user", "assistant", "user"])
        self.assertIn("target_out_of_bounds", seen[1][2]["content"])
        os.environ["ARX_AI_FAKE"] = "fallback"
        rec = ai.make_board(report, cfg)
        self.assertEqual((rec["plan_source"], rec["changes"]), ("engine_fallback", []))
        self.assertTrue(rec["problems"])

    def test_failures_come_back_as_codes(self):
        report, cfg = make()
        for scenario in ("refusal", "truncated", "rate_limit", "network", "invalid_key"):
            os.environ["ARX_AI_FAKE"] = scenario
            with self.assertRaises(ai.AIError) as ctx:
                ai.make_board(report, cfg)
            self.assertEqual(ctx.exception.code, scenario)


class Memory(unittest.TestCase):
    def test_boards_are_kept_and_feed_the_next_payload(self):
        store = ai.BoardStore(os.path.join(tempfile.mkdtemp(), "boards.json"))
        for i in range(ai.BOARDS_KEEP + 3):
            store.put(1, {"key": f"k{i}", "created": "2026-09-01T10:00:00", "today": f"2026-09-{i + 1:02d}", "plan_source": "coach",
                          "board": {"focus": f"focus {i}", "key_recommendations": ["a"], "next_training": {"date": "2026-09-20", "rows": [
                              {"exercise": "Row", "target": 100 + i, "sets": 1, "effort": "deep", "why": "w"}]}}})
        self.assertEqual(len(store.all(1)), ai.BOARDS_KEEP)
        self.assertEqual(store.get(1, "k14")["board"]["focus"], "focus 14")
        self.assertIsNone(store.get(1, "k0"))                             # the oldest ones are gone
        self.assertEqual(store.all(2), [])                                # per athlete
        mem = ai.memory_of(store.all(1))
        self.assertEqual([m["focus"] for m in mem], ["focus 12", "focus 13", "focus 14"])
        self.assertEqual(mem[-1]["rows"][0], {"exercise": "Row", "target": 114, "sets": 1, "effort": "deep"})
        store.put(1, dict(store.get(1, "k14"), plan_source="engine_fallback"))
        self.assertEqual(len(store.all(1)), ai.BOARDS_KEEP)               # same key: replaced, not doubled


class Jobs(unittest.TestCase):
    def test_one_job_per_key_and_errors_are_kept(self):
        jobs, gate, calls = ai.JobManager(), threading.Event(), []
        jobs.start(1, "k", lambda: (calls.append(1), gate.wait(2)))
        jobs.start(1, "k", lambda: calls.append(2))                       # a second device: the same job
        self.assertEqual(jobs.status(1, "k")["state"], "running")
        gate.set()
        for _ in range(40):
            if jobs.status(1, "k")["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual((jobs.status(1, "k")["state"], calls), ("done", [1]))

        def boom():
            raise ai.AIError("rate_limit", "slow down")
        jobs.start(1, "bad", boom)
        for _ in range(40):
            if jobs.status(1, "bad")["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(jobs.status(1, "bad")["error"], {"code": "rate_limit", "message": "slow down"})
        self.assertIsNone(jobs.status(2, "k"))


class Chat(FakeEnv):
    def setUp(self):
        super().setUp()
        self.chat = ai.ChatManager(os.path.join(tempfile.mkdtemp(), "chats.json"))
        self.record = {"key": "board-1", "board": {"focus": "finish the set"}}

    def ask(self, text, mid, record=None, names=("Anna Example",)):
        out = self.chat.send(1, text, mid, cfg={}, payload_text='{"p":1}', board_record=record or self.record, names=list(names))
        for _ in range(60):
            st = self.chat.poll(1, out["turn"])
            if st["state"] != "running":
                return st
            time.sleep(0.05)
        return st

    def test_prefix_is_cached_and_turns_append(self):
        st = self.ask("Why this order?", "m1")
        self.assertEqual(st["state"], "done")
        self.assertIn("Why this order?", st["text"])
        self.ask("And the rests?", "m2")
        with open(self.chat.path, encoding="utf-8") as f:
            conv = json.load(f)["1"]
        first = conv["messages"][0]["content"]
        self.assertEqual([b["text"][:7] for b in first[:2]], ["PAYLOAD", "THE BOA"])
        self.assertEqual(first[1]["cache_control"], {"type": "ephemeral", "ttl": "1h"})
        self.assertEqual([m["role"] for m in conv["messages"]], ["user", "assistant", "user", "assistant"])
        self.assertEqual(conv["messages"][2]["content"], "And the rests?")
        self.assertEqual([t["q"] for t in self.chat.history(1, "board-1")["turns"]], ["Why this order?", "And the rests?"])
        self.assertEqual(self.chat.turns_today(1), 2)

    def test_incremental_polling(self):
        out = self.chat.send(1, "A question", "m1", cfg={}, payload_text="{}", board_record=self.record, names=[])
        time.sleep(0.3)
        full = self.chat.poll(1, out["turn"])
        self.assertEqual(self.chat.poll(1, out["turn"], 10)["text"], full["text"][10:])
        self.assertEqual(self.chat.poll(1, "nope")["state"], "unknown")

    def test_a_failed_turn_commits_nothing_and_the_same_message_is_one_turn(self):
        os.environ["ARX_AI_FAKE"] = "rate_limit"
        st = self.ask("Will this fail?", "m1")
        self.assertEqual((st["state"], st["error"]["code"]), ("error", "rate_limit"))
        self.assertEqual(self.chat.history(1, "board-1")["turns"], [])
        os.environ["ARX_AI_FAKE"] = "ok"
        self.ask("Once", "m2")
        self.ask("Once", "m2")                                            # retry / second device
        self.assertEqual(len(self.chat.history(1, "board-1")["turns"]), 1)

    def test_a_new_board_starts_a_new_conversation_and_limits_hold(self):
        self.ask("Old board", "m1")
        self.ask("New board", "m2", record={"key": "board-2", "board": {}})
        self.assertEqual([t["q"] for t in self.chat.history(1, "board-2")["turns"]], ["New board"])
        self.assertEqual(self.chat.history(1, "board-1")["turns"], [])
        orig = ai.CHAT_TURNS_MAX
        ai.CHAT_TURNS_MAX = 1
        try:
            with self.assertRaises(ai.AIError) as ctx:
                self.chat.send(1, "One too many", "m3", cfg={}, payload_text="{}", board_record={"key": "board-2", "board": {}}, names=[])
            self.assertEqual(ctx.exception.code, "chat_limit")
        finally:
            ai.CHAT_TURNS_MAX = orig
        with self.assertRaises(ai.AIError):
            self.chat.send(1, "   ", "m4", cfg={}, payload_text="{}", board_record=self.record, names=[])

    def test_the_own_name_never_leaves_the_machine(self):
        self.assertEqual(ai.scrub("Is Anna too weak? anna example wants to know.", ["Anna Example"]),
                         "Is the athlete too weak? the athlete the athlete wants to know.")
        st = self.ask("Should Anna train today?", "m1")
        self.assertNotIn("Anna", st["text"])
        self.assertIn("x" * 10, ai.scrub("x" * 10, ["Al"]))              # two-letter fragments are left alone
        long = self.ask("q" * 5000, "m2")
        self.assertLessEqual(len(self.chat.history(1, "board-1")["turns"][-1]["q"]), ai.CHAT_QUESTION_MAX)
        self.assertEqual(long["state"], "done")


class CoachRoutes(ServerCase):
    """The routes on top: no key -> no call; with the fake a board appears; the chat needs the board."""
    def setUp(self):
        super().setUp()
        report, cfg = make()
        self._orig = (app.report_cfg, app.make_report, app.BOARDS, app.JOBS, app.CHATS)
        app.report_cfg = lambda uid: (dict(cfg, user_id=uid), {"name": "Anna Example", "created": None})
        app.make_report = lambda uid, cfg_info=None: report
        tmp = tempfile.mkdtemp()
        app.BOARDS, app.JOBS, app.CHATS = ai.BoardStore(os.path.join(tmp, "b.json")), ai.JobManager(), ai.ChatManager(os.path.join(tmp, "c.json"))
        self.addCleanup(self._restore)
        self.addCleanup(os.environ.pop, "ARX_AI_FAKE", None)

    def _restore(self):
        app.report_cfg, app.make_report, app.BOARDS, app.JOBS, app.CHATS = self._orig

    def wait_done(self):
        for _ in range(60):
            st = call(self.port, "/api/coach/status?user_id=1", headers=HDR)[1]
            if st["state"] != "running":
                return st
            time.sleep(0.05)
        return st

    def test_without_a_key_nothing_is_called(self):
        os.environ.pop("ARX_AI_FAKE", None)
        self.assertEqual(call(self.port, "/api/coach/status?user_id=1", headers=HDR)[1], {"state": "no_key"})
        self.assertEqual(call(self.port, "/api/coach/start", method="POST", body={"user_id": 1}, headers=OK)[1], {"state": "no_key"})

    def test_board_job_then_chat(self):
        os.environ["ARX_AI_FAKE"] = "ok"
        self.assertEqual(call(self.port, "/api/coach/status?user_id=1", headers=HDR)[1], {"state": "none"})
        self.assertEqual(call(self.port, "/api/chat/send", method="POST", body={"user_id": 1, "text": "hi"}, headers=OK)[0], 409)
        self.assertEqual(call(self.port, "/api/coach/start", method="POST", body={"user_id": 1}, headers=OK)[1]["state"], "running")
        st = self.wait_done()
        self.assertEqual((st["state"], st["record"]["plan_source"]), ("done", "coach"))
        again = call(self.port, "/api/coach/start", method="POST", body={"user_id": 1}, headers=OK)[1]
        self.assertEqual(again["record"]["created"], st["record"]["created"])                 # cached: no second call
        status, out = call(self.port, "/api/chat/send", method="POST", body={"user_id": 1, "text": "Why does Anna row first?", "client_msg_id": "c1"}, headers=OK)
        self.assertEqual((status, out["ok"]), (200, True))
        for _ in range(60):
            p = call(self.port, f"/api/chat/poll?user_id=1&turn={out['turn']}&from=0", headers=HDR)[1]
            if p["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(p["state"], "done")
        self.assertNotIn("Anna", p["text"])
        hist = call(self.port, "/api/chat/history?user_id=1", headers=HDR)[1]
        self.assertEqual((len(hist["turns"]), hist["board"], len(hist["suggested"])), (1, True, 3))

    def test_an_api_failure_is_a_state_not_a_crash(self):
        os.environ["ARX_AI_FAKE"] = "invalid_key"
        call(self.port, "/api/coach/start", method="POST", body={"user_id": 1}, headers=OK)
        st = self.wait_done()
        self.assertEqual((st["state"], st["error"]["code"]), ("error", "invalid_key"))
        os.environ["ARX_AI_FAKE"] = "ok"                                   # "try again" starts a fresh job
        call(self.port, "/api/coach/start", method="POST", body={"user_id": 1}, headers=OK)
        self.assertEqual(self.wait_done()["state"], "done")

    def test_settings_accept_only_known_models_and_efforts(self):
        call(self.port, "/api/config", method="POST", body={"model": "gpt-9", "ai_effort_board": "insane", "ai_effort_chat": "low", "ai_auto": False}, headers=OK)
        cfg = app.read_json(app.CONFIG, {})
        self.assertNotIn("model", cfg)
        self.assertNotIn("ai_effort_board", cfg)
        self.assertEqual((cfg["ai_effort_chat"], cfg["ai_auto"]), ("low", False))
        call(self.port, "/api/config", method="POST", body={"model": "claude-fable-5-1"}, headers=OK)
        boot = call(self.port, "/api/bootstrap")[1]["ai"]
        self.assertEqual((boot["model"], boot["effort_board"], boot["effort_chat"], boot["auto"]), ("claude-fable-5-1", "high", "low", False))
        app.write_json(app.CONFIG, {})


if __name__ == "__main__":
    unittest.main()
