# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 joergsflow - ARX Insight. Free software under the GNU GPL v3 or later; see LICENSE. No warranty.
"""Keep what ARX Insight shares with arx-free from drifting apart (v0.13.0).

Everything the two products have to agree on is a **contract**: a file in ``contracts/`` with a version and ONE owner
(the project where a change starts). ``contracts/MANIFEST.json`` lists every contract file with its SHA-256 - the
copy here is byte-identical to ``arx-free/contracts/``; a change of a contract happens in its owner's project first
(new version), then ``--update`` there and the identical copy in the other project. The test suite checks:

1. what lies in ``contracts/`` is what the manifest says (nobody edits a contract in passing),
2. our own rule agrees with the shared test vectors (``arx_detail.effort_v3`` against ``effort-v3-vectors.json`` -
   ARX Insight OWNS that rule, so this is the guard against changing it without a new contract version),
3. when the sibling is checked out next to this one (``../arx-free``): every contract file both carry is identical,
   every contract the sibling lists is adopted here - except an older file of a contract ARX Insight OWNS that arx-free
   still carries after we retired it (history, not drift: the owner decides what the current version contains) -,
   and the exercise catalogue agrees code by code.

    ./.venv/bin/python tools/contracts.py            check (exit code 1 on a difference)
    ./.venv/bin/python tools/contracts.py --update   after a DELIBERATE change of a contract: write the new hashes
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLDER = os.path.join(ROOT, "contracts")
MANIFEST = os.path.join(FOLDER, "MANIFEST.json")
SIBLING = os.path.join(os.path.dirname(ROOT), "arx-free")
VECTORS = os.path.join(FOLDER, "effort-v3-vectors.json")


def sha256(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def contract_files(folder: str = FOLDER) -> list[str]:
    return sorted(n for n in os.listdir(folder) if n not in ("MANIFEST.json", "README.md") and not n.startswith("."))


def load_manifest(path: str = MANIFEST) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def own_problems(folder: str = FOLDER) -> list[str]:
    """Differences between the contract folder and its manifest."""
    manifest = load_manifest(os.path.join(folder, "MANIFEST.json"))
    listed = {name: digest for c in manifest["contracts"] for name, digest in c["files"].items()}
    problems = [f"{name}: not in the manifest" for name in contract_files(folder) if name not in listed]
    for name, digest in listed.items():
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            problems.append(f"{name}: listed in the manifest, but the file is missing")
        elif sha256(path) != digest:
            problems.append(f"{name}: changed without the manifest - a contract changes with its version, in its owner's project first")
    return problems


def rule_problems() -> list[str]:
    """Our fatigue rule against the shared vectors and the shared constants (we own the rule: a change needs a new
    contract version and new vectors, never a silent edit)."""
    sys.path.insert(0, ROOT)
    import arx_detail as detail                                          # noqa: E402  (stdlib project, no side effects)
    with open(VECTORS, encoding="utf-8") as fh:
        agreed = json.load(fh)
    problems = [f"{k}: contract effort-v3 says {v}, arx_detail says {getattr(detail, k, None)}"
                for k, v in agreed["constants"].items() if getattr(detail, k, None) != v]
    for case in agreed["cases"]:
        got = detail.effort_v3(case["con"], case["ecc"])
        for key, expected in case["expected"].items():
            if got.get(key) != expected:
                problems.append(f"vector '{case['name']}' / {key}: expected {expected}, got {got.get(key)}")
    return problems


def _catalogue(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return {int(code): (str(e.get("name")), str(e.get("group"))) for code, e in data.items() if str(code).isdigit() and isinstance(e, dict)}


def sibling_problems(sibling: str = SIBLING, root: str = ROOT) -> list[str] | None:
    """Differences against the sibling checkout; None when it is not there (Windows PC, fresh clone)."""
    if not os.path.isdir(os.path.join(sibling, "contracts")):
        return None
    problems = []
    manifest = load_manifest()
    ours = {name: digest for c in manifest["contracts"] for name, digest in c["files"].items()}
    owned = {c["name"] for c in manifest["contracts"] if c.get("owner") == "arx-insight"}
    theirs_manifest = load_manifest(os.path.join(sibling, "contracts", "MANIFEST.json"))
    for contract in theirs_manifest["contracts"]:
        for name, digest in contract["files"].items():
            if name not in ours:
                if contract["name"] in owned:
                    continue                # a superseded version of OUR contract that arx-free still carries (modes-1 after modes-2)
                problems.append(f"{name}: arx-free lists this contract file, it is not adopted here yet (copy it and add it to the manifest)")
            elif ours[name] != digest:
                problems.append(f"{name}: the two projects carry different versions of this contract file")
    theirs_catalogue = os.path.join(sibling, "config", "exercises.json")
    if os.path.isfile(theirs_catalogue):
        mine, other = _catalogue(os.path.join(root, "exercises.json")), _catalogue(theirs_catalogue)
        for code in sorted(set(mine) | set(other)):
            if mine.get(code) != other.get(code):
                problems.append(f"exercise {code}: ARX Insight {mine.get(code)} - arx-free {other.get(code)}")
    return problems


def update() -> None:
    manifest = load_manifest()
    known = {name for c in manifest["contracts"] for name in c["files"]}
    for c in manifest["contracts"]:
        c["files"] = {name: sha256(os.path.join(FOLDER, name)) for name in c["files"]}
    new = [n for n in contract_files() if n not in known]
    with open(MANIFEST, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
        fh.write("\n")
    print("contracts/MANIFEST.json written" + (f" - NOT listed yet (add them to a contract entry by hand): {new}" if new else ""))


def main(argv: list[str] | None = None) -> int:
    if "--update" in (sys.argv[1:] if argv is None else argv):
        update()
        return 0
    own, rule, sibling = own_problems(), rule_problems(), sibling_problems()
    for line in own:
        print("contract:", line)
    for line in rule:
        print("rule:    ", line)
    for line in sibling or []:
        print("sibling: ", line)
    print("contracts:", "ok" if not own else "DIFFERENT", "| effort-v3 rule:", "ok" if not rule else "DIFFERENT", "|",
          "arx-free not next to this checkout - not compared" if sibling is None else ("arx-free: in step" if not sibling else "arx-free: DIFFERENT"))
    return 1 if (own or rule or sibling) else 0


if __name__ == "__main__":
    sys.exit(main())
