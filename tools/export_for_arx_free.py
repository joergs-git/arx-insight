#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 joergsflow - ARX Insight. Free software under the GNU GPL v3 or later; see LICENSE. No warranty.
"""Export the ARX app's history for arx-free as a file - contract ``arx-export`` (ARX Insight tool).

    python tools/export_for_arx_free.py --db "<...>/Resources/DB.FDB4" --out arx-export.ndjson.gz [--since 2026-09-13T18:04:11]

The lines are built by ``arx_export`` (the app module the route ``GET /api/export?since=`` uses as well, v0.22.0);
this file is the command line around it: copy the database, export, remove the copy. What leaves the database and
what the file looks like: ``arx_export`` and ``contracts/arx-export-2.md``. The file contains names and training data
of real people: keep it on your own machines.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arx_base as core  # noqa: E402
import arx_sources as sources  # noqa: E402
from arx_export import (FORMAT, SOURCE, USER_SQL, SET_SQL, RANGE_SQL, _iso, _number, _json_blob, samples_of, range_line, fatigue_target_of,  # noqa: E402,F401  (the names the tests and older scripts use)
                        athlete_line, person_line, set_line, parse_since, person_facts, export)


def _settings(name: str) -> dict:
    """config.json / goals.json of this installation (the same folder the app uses) - what ARX Insight knows about a
    person rides on the athlete line; a missing or unreadable file simply means "not known"."""
    try:
        with open(os.path.join(core.data_dir(), name), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True, help="path of the ARX database (DB.FDB4) - it is copied, never opened in place")
    parser.add_argument("--out", default=f"arx-export-{datetime.now():%Y%m%d-%H%M%S}.ndjson.gz")
    parser.add_argument("--since", default=None, help="only sets that began after this `started_at` (e.g. 2026-09-13T18:04:11); default: all")
    args = parser.parse_args(argv)
    since = None
    if args.since:
        since = parse_since(args.since)
        if since is None:
            parser.error("--since must be a date and time with seconds, like 2026-09-13T18:04:11")
    try:
        version = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION"), encoding="utf-8").read().strip()
    except OSError:
        version = ""
    con, tmp = core.open_readonly(args.db)
    try:
        free = sources.attach(con, _settings("config.json"), sources.emails_from_goals())     # the persons known from arx-free alone (v0.28.0)
        counts = export(con, args.out, version, since=since, person=person_facts(_settings("config.json"), _settings("goals.json")),
                        persons=sources.people_of(free))
    finally:
        try:
            con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)                                # the copy holds private data: never leave it behind
    print(f"{args.out}: {counts['athletes']} athletes, {counts['persons']} persons from arx-free, {counts['ranges']} ranges, {counts['sets']} sets"
          + (f", {counts['skipped']} skipped" if counts["skipped"] else "")
          + (f" (after {since})" if since else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
