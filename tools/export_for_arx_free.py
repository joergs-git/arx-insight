#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 joergsflow - ARX Insight. Free software under the GNU GPL v3 or later; see LICENSE. No warranty.
"""Export the ARX app's history for arx-free - contract ``arx-export-1`` (ARX Insight tool).

arx-free is the owner's independent control software for the same machine; its contract lives in its repository
(``contracts/arx-export-1.md``). This tool is the ARX Insight side of it: ARX Insight already owns the safe way to read
the ARX database (always a COPY, read-only), so the export lives here and arx-free never needs a Firebird client.

    python tools/export_for_arx_free.py --db "<...>/Resources/DB.FDB4" --out arx-export.ndjson.gz

What leaves the database - and nothing else:
  * athletes: id, first name, last name, gender, birth date, create date (no e-mail, no password / token, no cloud ids)
  * sets that are not deleted: the scalar columns, and **verbatim** the three blobs (configuration, events without the
    "WaitingTimeLeft" countdown ticks, samples). Nothing is converted or rounded; units stay lb and inch.

The file contains names and training data of real people: keep it on your own machines.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import arx_base as core  # noqa: E402

FORMAT = "arx-export-1"
SOURCE = "arx-original"
USER_SQL = 'select id, trim(firstname), trim(lastname), gender, birthdate, createdate from "User" where deleted is false order by id'
SET_SQL = '''select id, user_id, exercisedate, exercise, protocol, protocolparameter, repscheme, elapsedseconds, intensity,
                    maxload, concentricmax, eccentricmax, hidefromstats, notes, resttimer, resttimerused, comparisonset_id,
                    repschemedata, eventstreamdata, serializeddetaileddata
             from "ExerciseSet" where deleted is false order by id'''


def _iso(value) -> str | None:
    """Dates as ISO text; the ARX app's placeholder 0001-01-01 means "never"."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        if value.year <= 1:
            return None
        return value.isoformat(timespec="seconds") if isinstance(value, datetime) else value.isoformat()
    return str(value)


def _number(value):
    return None if value is None else float(value)


def _json_blob(value, encoding: str):
    raw = core.blob_bytes(value)
    if not raw:
        return None
    return json.loads(raw.decode(encoding))


def samples_of(value) -> list:
    """SERIALIZEDDETAILEDDATA: gzip + UTF-16 JSON list of samples."""
    raw = core.blob_bytes(value)
    if not raw:
        return []
    return json.loads(gzip.decompress(raw).decode("utf-16"))


def athlete_line(row) -> dict:
    uid, first, last, gender, born, created = row
    sex = {"m": "m", "f": "f"}.get((gender or "").strip().lower()[:1])
    return {"kind": "athlete", "source_user_id": str(uid), "first_name": first or "", "last_name": last or "", "gender": sex,
            "birth_date": (_iso(born) or "")[:10] or None, "created_at": _iso(created)}


def set_line(row: dict) -> dict:
    """One "ExerciseSet" row (column name -> value, upper-case names like the driver delivers them)."""
    events = _json_blob(row.get("EVENTSTREAMDATA"), "latin1") or []
    return {
        "kind": "set", "source_set_id": str(row["ID"]), "source_user_id": str(row["USER_ID"]), "exercise_code": int(row["EXERCISE"]),
        "started_at": _iso(row["EXERCISEDATE"]), "protocol": row.get("PROTOCOL"), "protocol_parameter": _number(row.get("PROTOCOLPARAMETER")),
        "rep_scheme": (row.get("REPSCHEME") or "").strip() or None, "elapsed_s": _number(row.get("ELAPSEDSECONDS")),
        "intensity_lb": _number(row.get("INTENSITY")), "max_lb": _number(row.get("MAXLOAD")), "max_c_lb": _number(row.get("CONCENTRICMAX")),
        "max_e_lb": _number(row.get("ECCENTRICMAX")), "hide_from_stats": bool(row.get("HIDEFROMSTATS")),
        "notes": (core.blob_bytes(row.get("NOTES")).decode("latin1").strip() or None) if row.get("NOTES") is not None else None,
        "rest_timer_s": _number(row.get("RESTTIMER")), "rest_timer_used_s": _number(row.get("RESTTIMERUSED")),
        "comparison_source_set_id": str(row["COMPARISONSET_ID"]) if row.get("COMPARISONSET_ID") is not None else None,
        "config": _json_blob(row.get("REPSCHEMEDATA"), "latin1") or {},
        "events": [e for e in events if e.get("Type") != "WaitingTimeLeft"],
        "samples": samples_of(row.get("SERIALIZEDDETAILEDDATA")),
    }


def export(con, out_path: str, version: str = "") -> dict:
    """Write the export from an open connection. Returns the counts. Sets are streamed row by row - the blobs of a
    long history do not fit into memory comfortably on the machine PC."""
    counts = {"athletes": 0, "sets": 0, "skipped": 0}
    cur = con.cursor()
    with gzip.open(out_path, "wt", encoding="utf-8", compresslevel=6) as out:
        def write(line: dict) -> None:
            out.write(json.dumps(line, separators=(",", ":"), default=_iso) + "\n")

        write({"kind": "header", "format": FORMAT, "source": SOURCE, "created_at": datetime.now().isoformat(timespec="seconds"),
               "exporter": f"arx-insight {version}".strip()})
        cur.execute(USER_SQL)
        for row in cur.fetchall():
            write(athlete_line(row))
            counts["athletes"] += 1
        cur.execute(SET_SQL)
        names = [d[0].upper() for d in cur.description]
        while True:
            row = cur.fetchone()
            if row is None:
                break
            try:
                write(set_line(dict(zip(names, row))))
                counts["sets"] += 1
            except (ValueError, OSError, KeyError, TypeError) as exc:            # one unreadable blob must not end the export
                counts["skipped"] += 1
                print(f"set {row[0]} skipped: {type(exc).__name__}", file=sys.stderr)
        write({"kind": "footer", "athletes": counts["athletes"], "sets": counts["sets"]})
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True, help="path of the ARX database (DB.FDB4) - it is copied, never opened in place")
    parser.add_argument("--out", default=f"arx-export-{datetime.now():%Y%m%d-%H%M%S}.ndjson.gz")
    args = parser.parse_args(argv)
    try:
        version = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION"), encoding="utf-8").read().strip()
    except OSError:
        version = ""
    con, tmp = core.open_readonly(args.db)
    try:
        counts = export(con, args.out, version)
    finally:
        try:
            con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)                                # the copy holds private data: never leave it behind
    print(f"{args.out}: {counts['athletes']} athletes, {counts['sets']} sets" + (f", {counts['skipped']} skipped" if counts["skipped"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
