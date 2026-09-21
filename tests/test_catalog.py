"""exercises.json: the shipped catalog is the ground the recovery model, the order rules, the
restrictions and the planner stand on (v0.7.0). One wrong identifier and an exercise silently
drops out of a rule - so every entry is checked against the vocabularies the engine really uses,
and the relationships that follow from the entries (means before the target, twins, fallback map)
are checked for sense."""
import itertools, json, os, re, unittest

import tests                                   # noqa: F401  (points ARX_DATA_DIR at a temp folder first)
import arx_app as app
import arx_plan as planner
import arx_report as core
from arx_history import MUSCLE_NAMES, REGION_OF

HERE = os.path.dirname(os.path.dirname(__file__))
PATH = os.path.join(HERE, "exercises.json")


def muscles(meta: dict) -> dict:
    out = {m: "target" for m in meta.get("targets") or []}
    for m in meta.get("limiters") or []:
        out.setdefault(m, "limiter")
    return out


class ShippedCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(PATH, encoding="utf-8") as f:
            cls.raw = json.load(f)
        cls.catalog = core.load_catalog(PATH)
        cls.mapped = {c: m for c, m in cls.catalog.items() if not c.startswith("lib:")}

    def test_every_entry_is_complete_and_uses_the_engines_vocabulary(self):
        self.assertGreaterEqual(len(self.mapped), 17)
        names = [m["name"] for m in self.catalog.values()]
        self.assertEqual(len(names), len(set(names)), "an exercise name appears twice")
        for code, m in self.catalog.items():
            with self.subTest(exercise=m.get("name"), code=code):
                self.assertTrue(code.startswith("lib:") or code.isdigit())              # DB codes are integers
                self.assertIn(m["group"], ("Push", "Pull", "Drive"))
                self.assertIn(m["kind"], ("compound", "isolation"))
                self.assertTrue(m["targets"], "an exercise without a target loads a pseudo-muscle")
                for t in m["targets"]:
                    self.assertIn(t, MUSCLE_NAMES)
                    self.assertIn(t, REGION_OF)                                           # focus, caps and warnings go by region
                    self.assertNotEqual(REGION_OF[t], "grip")
                for l in m.get("limiters") or []:
                    self.assertIn(l, MUSCLE_NAMES)
                    self.assertNotIn(l, m["targets"], "a muscle is either the goal or a means")
                self.assertTrue(m.get("joints"), "without joints a restriction cannot reach the exercise")
                for j in m["joints"]:
                    self.assertIn(j, app.BODY_PARTS)
                for a in m.get("aids") or []:
                    self.assertIn(a, app.AID_KINDS)
                if m.get("aids"):                                                         # hooks / straps only take the grip out
                    self.assertIn("grip", m.get("limiters") or [])

    def test_the_library_never_repeats_a_mapped_exercise(self):
        mapped = {m["name"] for m in self.mapped.values()}
        for entry in self.raw.get("_library") or []:
            self.assertNotIn(entry.get("name"), mapped)

    def test_means_before_the_target_has_no_cycle(self):
        """'a's target is b's helper -> a after b' must be satisfiable for ANY selection: no cycle."""
        after = {a["name"]: {b["name"] for b in self.catalog.values() if a is not b
                             and planner.means_before_target({"muscles": muscles(a)}, {"muscles": muscles(b)})}
                 for a in self.catalog.values()}
        state: dict = {}

        def visit(n, path):
            if state.get(n) == "done":
                return
            self.assertNotEqual(state.get(n), "open", f"cycle: {' -> '.join(path + [n])}")
            state[n] = "open"
            for nxt in sorted(after[n]):
                visit(nxt, path + [n])
            state[n] = "done"
        for name in sorted(after):
            visit(name, [])
        # the rules a trainer expects are really there
        self.assertIn("Row", after["Biceps Curl"])
        self.assertIn("Pull Down", after["Biceps Curl"])
        for press in ("Horizontal Press", "Incline Press", "Decline Press", "Overhead Press", "Pull Over"):
            self.assertIn(press, after["Triceps Pressdown"])

    def test_twins_are_what_a_trainer_would_call_the_same_slot(self):
        twins = {frozenset((a["name"], b["name"])) for a, b in itertools.combinations(self.catalog.values(), 2)
                 if set(a["targets"]) == set(b["targets"])}
        self.assertEqual(twins, {frozenset(("Row", "Pull Down")), frozenset(("Horizontal Press", "Decline Press")),
                                 frozenset(("Horizontal Press", "Pec Fly")), frozenset(("Decline Press", "Pec Fly"))})

    def test_every_movement_group_has_a_big_exercise_for_its_main_muscles(self):
        """Full body = one big exercise per group first: each group needs compounds, and the big
        muscles (chest, lats, quads ...) need at least one compound that TARGETS them."""
        big = [m for m in self.mapped.values() if m["kind"] == "compound"]
        self.assertEqual({m["group"] for m in big}, {"Push", "Pull", "Drive"})
        reached = {t for m in big for t in m["targets"]}
        for muscle in ("chest", "shoulders", "lats", "upper_back", "quads", "glutes", "hamstrings"):
            self.assertIn(muscle, reached)

    def test_the_readme_lists_every_exercise_with_its_code(self):
        with open(os.path.join(HERE, "README.md"), encoding="utf-8") as f:
            rows = [re.match(r"^\| ([^|]+?) \|.*\| `(\d+)` \|$", line.rstrip()) for line in f.read().splitlines()]
        listed = {m.group(1): m.group(2) for m in rows if m}                # the table rows end with the DB code
        self.assertEqual(listed, {m["name"]: c for c, m in self.mapped.items()})

    def test_body_map_reaches_what_a_region_or_joint_touches(self):
        """v0.9.0: the check-in's shortcuts flag exercises through this map - a region through a target muscle
        (or, one step weaker, a limiter), a joint through the catalog's joints. Pinned per exercise."""
        bm = planner.body_map(self.mapped)
        self.assertEqual(set(bm["regions"]), set(planner.CHECKIN_REGIONS))
        for code, m in self.mapped.items():
            for region, muscles_ in planner.CHECKIN_REGIONS.items():
                expect = ("target" if any(t in muscles_ for t in m["targets"])
                          else "helper" if any(l in muscles_ for l in m.get("limiters") or []) else None)
                self.assertEqual(bm["regions"][region].get(code), expect, (m["name"], region))
            for j in m["joints"]:
                self.assertIn(code, bm["joints"][j])
        self.assertEqual(bm["regions"]["legs"]["19"], "target")               # Belt Squat
        self.assertEqual(bm["regions"]["arms"]["10"], "helper")               # Dead Lift hangs on the grip
        self.assertIn("5", bm["joints"]["shoulder"])                          # Overhead Press

    def test_the_name_fallback_agrees_with_the_catalog(self):
        """BODYPART_EXERCISES only serves entries without 'joints' - it must never say something else."""
        for part in app.BODY_PARTS:
            from_catalog = {m["name"] for m in self.mapped.values() if part in m["joints"]}
            self.assertEqual(set(core.BODYPART_EXERCISES.get(part, [])), from_catalog, part)
        for m in self.mapped.values():                       # and both roads give the same verdict
            for part in app.BODY_PARTS:
                self.assertEqual(core.exercise_restriction(m["name"], {part: "avoid"}, m["joints"]),
                                 core.exercise_restriction(m["name"], {part: "avoid"}, None), (m["name"], part))


if __name__ == "__main__":
    unittest.main()
