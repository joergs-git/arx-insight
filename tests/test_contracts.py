"""The contracts with the sibling project arx-free (v0.13.0): the folder matches its manifest, our own fatigue rule
agrees with the shared vectors (we own it - a change needs a new contract version), and when arx-free is checked out
next to this repository, both carry the same contract files and the same exercise catalogue."""
import os, unittest

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

    @unittest.skipUnless(os.path.isdir(os.path.join(contracts.SIBLING, "contracts")), "arx-free is not checked out next to this repository")
    def test_the_sibling_carries_the_same_contracts_and_catalogue(self):
        self.assertEqual(contracts.sibling_problems(), [])


if __name__ == "__main__":
    unittest.main()
