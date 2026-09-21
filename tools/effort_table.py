#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 joergsflow - ARX Insight. Free software under the GNU GPL v3 or later; see LICENSE. No warranty.
# -*- coding: utf-8 -*-
"""
Developer tool: effort v2 (legacy inroad) vs effort v3 (phase-resolved) for every working set.

    python tools/effort_table.py --db <DB.FDB4> [--user 1] [--shuffle]

Prints one line per working set - never a name - plus the change matrix and the recovery-relevant
summary, so a change of the effort definition or of its thresholds can be judged on real data before
it ships (lesson 2026-09-12: check a filter against the data's extremes first). --shuffle adds the
noise check: the same statistic on randomly re-ordered reps must read ~0 (lesson 2026-09-18).
"""
from __future__ import annotations
import os, sys, random, shutil, argparse, statistics as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import arx_report as core                                     # noqa: E402
import arx_detail as detail                                   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--user", type=int, action="append", help="user id (repeatable); default: all with sets")
    ap.add_argument("--shuffle", action="store_true", help="also run the shuffled-reps noise check")
    args = ap.parse_args()
    catalog = core.load_catalog(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exercises.json"))
    con, tmp = core.open_readonly(args.db)
    try:
        users = args.user
        if not users:
            cur = con.cursor()
            cur.execute('select distinct user_id from "ExerciseSet" where deleted is false order by 1')
            users = [r[0] for r in cur.fetchall()]
        rng = random.Random(7)
        for uid in users:
            sets = core.load_sets(con, uid)
            core.flag_false_starts(sets)
            work = [s for s in sets if s["working"]]
            if not work:
                continue
            for s in work:
                s["name"] = catalog.get(str(s["exercise"]), {}).get("name", f"Exercise {s['exercise']}")
                core.attach_detail(s, detail.get_detail(con, s, {}, s["mean_force_kg"] / core.LB_TO_KG))
            core.cap_low_force(work)
            print(f"\n=== user {uid}: {len(work)} working sets ===")
            print(f"{'set':>4} {'date':10} {'exercise':19} {'reps':>4} {'peak':>6} | {'v2':>3} {'effort v2':9} | "
                  f"{'con':>3} {'ecc':>3} {'v3':>3} {'effort v3':9} {'note':12} | change")
            matrix: dict = {}
            shuffled = []
            for s in work:
                v2, v3 = s["effort_legacy"], s["effort"]
                matrix[(v2, v3)] = matrix.get((v2, v3), 0) + 1
                note = ("capped:low" if s.get("effort_capped") else "") + (" ~" if s.get("borderline") else "")
                print(f"{s['id']:>4} {s['date'][:10]} {s['name'][:19]:19} {s['reps']:>4} {s['max_kg']:>6.1f} | "
                      f"{str(s['inroad_legacy']):>3} {v2:9} | {str(s['fatigue_con_pct']):>3} {str(s['fatigue_ecc_pct']):>3} "
                      f"{str(s['inroad']):>3} {v3:9} {note:12} | {'' if v2 == v3 else v2 + ' -> ' + v3}")
                if args.shuffle and s["detail"].get("reps"):
                    con_m = [r["con_mean"] for r in s["detail"]["reps"]]
                    ecc_m = [r["ecc_mean"] for r in s["detail"]["reps"]]
                    vals = []
                    for _ in range(200):
                        idx = list(range(len(con_m)))
                        rng.shuffle(idx)
                        vals.append(detail.effort_v3([con_m[i] for i in idx], [ecc_m[i] for i in idx])["inroad_v3"] or 0)
                    shuffled.append((s["id"], s["inroad"], st.median(vals), sum(1 for v in vals if v >= (s["inroad"] or 0)) / len(vals)))
            changed = sum(n for (a, b), n in matrix.items() if a != b)
            count = lambda key, val: sum(1 for s in work if s[key] == val)
            print(f"\nchanged class: {changed} of {len(work)}   (v2 -> v3: " +
                  ", ".join(f"{a}->{b}: {n}" for (a, b), n in sorted(matrix.items()) if a != b) + ")")
            print("deep / moderate / submax / unknown   v2: " + " / ".join(str(count("effort_legacy", k)) for k in ("deep", "moderate", "submax", "unknown"))
                  + "   v3: " + " / ".join(str(count("effort", k)) for k in ("deep", "moderate", "submax", "unknown")))
            if shuffled:
                print("\nnoise check (200 random rep orders per set): median v3 on shuffled reps, and how often noise")
                print("reaches the set's real value (p) - hard sets should have a small p")
                for sid, real, med, p in shuffled:
                    if (real or 0) >= detail.INROAD_MODERATE:
                        print(f"  set {sid:>4}: real {real:>3}  shuffled median {med:>4.0f}  p = {p:.2f}")
                print(f"  all sets: median of shuffled medians = {st.median(x[2] for x in shuffled):.0f}")
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
