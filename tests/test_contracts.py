"""The contracts with the sibling project arx-free (v0.13.0): the folder matches its manifest, our own fatigue rule
agrees with the shared vectors (we own it - a change needs a new contract version), and when arx-free is checked out
next to this repository, both carry the same contract files and the same exercise catalogue."""
import json, os, tempfile, unittest

import tests                                   # noqa: F401
from tools import contracts


class Contracts(unittest.TestCase):
    def test_the_folder_is_what_the_manifest_says(self):
        self.assertEqual(contracts.own_problems(), [])

    def test_our_fatigue_rule_agrees_with_the_shared_vectors(self):
        self.assertEqual(contracts.rule_problems(), [])
        # the vocabulary contract names what a person reads - the page and the texts use exactly those words
        with open(os.path.join(contracts.FOLDER, "vocabulary-1.md"), encoding="utf-8") as fh:
            vocab = fh.read()
        for word in ("Ermüdung im Satz", "fatigue in the set", "Ermüdungsziel", "tief", "mittel", "leicht"):
            self.assertIn(word, vocab)
        with open(os.path.join(contracts.ROOT, "web", "index.html"), encoding="utf-8") as fh:
            page = fh.read()
        self.assertIn('moderate:de?"mittel":"medium",submax:de?"leicht":"light"', page)

    def test_the_progress_factors_keep_the_shape_arx_free_shows(self):
        # contract insight-progress-1 (arx-free's request 8): a rename of a pinned key is a new version, never a silent break
        from datetime import datetime
        from tests.test_history import series, report, ROW
        r = report(series(ROW, datetime(2026, 8, 1, 10), [1, 1.02, 1.04, 1.06, 1.08], every=4), "2026-08-20")
        pf = r["history"]["progress_factors"]
        overall = pf["overall"]
        for key in ("strength_index", "exercises_in_index", "exercises_total", "interp"):
            self.assertIn(key, overall, key)
        self.assertEqual(set(overall["interp"]) >= {"code", "params", "text"}, True)
        self.assertEqual(set(overall["interp"]["text"]), {"meaning", "action"})
        row = pf["exercises"][0]
        for key in ("name", "ex", "n", "status", "change_pct", "change_4w_pct", "change_quarter_pct", "rate_pct_per_week", "rate_quality",
                    "span_days", "baseline_date", "latest_date", "points", "interp"):
            self.assertIn(key, row, key)
        self.assertIn(row["status"], ("progressing", "progressing_context", "stable", "stable_context", "plateau", "plateau_context",
                                      "regressing", "regressing_context", "familiarisation", "not_comparable", "insufficient"))
        self.assertTrue(row["points"] and all({"date", "index"} <= set(p) for p in row["points"]))
        self.assertTrue(row["interp"]["code"].startswith("progress_"))
        self.assertIn("today", r)

    @unittest.skipUnless(os.path.isdir(os.path.join(contracts.SIBLING, "contracts")), "arx-free is not checked out next to this repository")
    def test_the_sibling_carries_the_same_contracts_and_catalogue(self):
        self.assertEqual(contracts.sibling_problems(), [])

    def test_a_superseded_file_of_our_own_contract_at_the_sibling_is_history_not_drift(self):
        """v0.16.2: arx-free may still carry an older file of a contract ARX Insight owns (modes-1 after modes-2 was
        adopted and modes-1 retired here) - nothing to adopt. A file of a contract arx-free OWNS that we lack is still
        'not adopted here yet'."""
        ours = contracts.load_manifest()
        modes = next(c["files"] for c in ours["contracts"] if c["name"] == "modes")
        theirs = {"contracts": [
            {"name": "modes", "version": 2, "owner": "arx-insight", "files": {"modes-0.md": "0" * 64, **modes}},
            {"name": "exercise-coaching", "version": 9, "owner": "arx-free", "files": {"exercise-coaching-9.json": "0" * 64}},
        ]}
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "contracts"))
            with open(os.path.join(tmp, "contracts", "MANIFEST.json"), "w", encoding="utf-8") as fh:
                json.dump(theirs, fh)
            self.assertEqual(contracts.sibling_problems(sibling=tmp),
                             ["exercise-coaching-9.json: arx-free lists this contract file, it is not adopted here yet (copy it and add it to the manifest)"])


if __name__ == "__main__":
    unittest.main()
