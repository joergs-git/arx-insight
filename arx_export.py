# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 joergsflow - ARX Insight. Free software under the GNU GPL v3 or later; see LICENSE. No warranty.
"""The ARX app's history for arx-free - contract ``arx-export`` (ARX Insight owns it; v0.22.0 moved the code here,
v0.24.0 added the e-mail to the athlete line - contract arx-export-3).

arx-free is the owner's independent control software for the same machine; ARX Insight already owns the safe way to
read the original's database (always a COPY, read-only), so the export lives here and arx-free never needs a Firebird
client. Two ways deliver the same lines (contract ``contracts/arx-export-2.md``):

  * the file: ``python tools/export_for_arx_free.py --db "<...>/Resources/DB.FDB4" --out arx-export.ndjson.gz``
  * the route: ``GET /api/export?since=<started_at>`` on the app's local listener (arx_app) - every athlete, and only
    the sets that began after ``since``; arx-free asks at its start and imports what is new by itself.

What leaves the database - and nothing else:
  * athletes: id, first name, last name, gender, birth date, create date (no password / token, no cloud ids);
    plus, from ARX Insight's own settings, the person's language, the display units and - since contract
    arx-export-3 (v0.24.0) - the e-mail address typed into the profile, each only when known (person_facts)
  * sets that are not deleted: the scalar columns, and **verbatim** the three blobs (configuration, events without the
    "WaitingTimeLeft" countdown ticks, samples). Nothing is converted or rounded; units stay lb and inch.

The result contains names and training data of real people: it stays on the owner's machines.
"""

from __future__ import annotations

import gzip
import json
import sys
from datetime import date, datetime, timedelta

import arx_base as core

FORMAT = "arx-export-1"        # names the LINE format - unchanged by contract v2 (it added the route and `since`)
SOURCE = "arx-original"
USER_SQL = 'select id, trim(firstname), trim(lastname), gender, birthdate, createdate from "User" where deleted is false order by id'
SET_COLUMNS = '''id, user_id, exercisedate, exercise, protocol, protocolparameter, repscheme, elapsedseconds, intensity,
                 maxload, concentricmax, eccentricmax, hidefromstats, notes, resttimer, resttimerused, comparisonset_id,
                 repschemedata, eventstreamdata, serializeddetaileddata'''
SET_SQL = f'select {SET_COLUMNS} from "ExerciseSet" where deleted is false order by id'
# `since` is a `started_at` text of the export itself (seconds); the column keeps fractions of a second, so "began
# after 18:04:11" = "at or after 18:04:12" in the database. The guard in export() applies the same rule to the line.
SET_SQL_SINCE = f'select {SET_COLUMNS} from "ExerciseSet" where deleted is false and exercisedate >= ? order by id'
# The original's CURRENT range of motion per athlete, exercise and range type (contract arx-export-5, v0.27.0): the
# table "RangeOfMotion2" is append-only - the row with the highest id per user + exercise + type is the one in force
# (no DELETED column). Always all of them: a state, not a history - `since` filters sets only.
RANGE_SQL = '''select r.id, r.user_id, r.exercise, r.rangetype, r.startposition, r.endposition, r.confirmed, r.datecreated
               from "RangeOfMotion2" r
               where r.id = (select max(x.id) from "RangeOfMotion2" x
                             where x.user_id = r.user_id and x.exercise = r.exercise and x.rangetype = r.rangetype)
               order by r.id'''


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


def parse_since(text) -> str | None:
    """`since` as the contract wants it: a naive local date and time with seconds, the way `started_at` is exported
    (`2026-09-13T18:04:11`). Returns that normalised text, or None when it is not one (the route answers 400)."""
    if not isinstance(text, str):
        return None
    try:
        value = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if value.tzinfo is not None or value.year <= 1:
        return None
    return value.replace(microsecond=0).isoformat(timespec="seconds")


LANGUAGES, DISPLAY_UNITS = ("en", "de"), ("metric", "imperial")


def person_facts(config: dict, goals: dict):
    """What ARX Insight knows about a person, for the athlete line (contract arx-export-2, owner's decision 2026-09-23:
    "Insight wins for the person, arx-free wins for the machine"). `language` = the person's own choice in the
    profile (goals.json), else the device's language when it is set; `display_units` = the device's units when they
    are set; `email` (contract arx-export-3, v0.24.0) = the address typed into the profile - THE key of a person across
    arx-free, ARX Insight and a future cloud (owner 2026-09-24), lower-cased. Only known values leave - never a name,
    a birth date, a note, a body value, a photo flag (that one is arx-free's). Returns a callable uid -> dict for export()."""
    device_language = config.get("language") if config.get("language") in LANGUAGES else None
    units = config.get("units") if config.get("units") in DISPLAY_UNITS else None

    def facts(uid) -> dict:
        rec = goals.get(str(uid)) if isinstance(goals.get(str(uid)), dict) else {}
        own = rec.get("language")
        language = own if own in LANGUAGES else device_language
        out = {}
        if language:
            out["language"] = language
        if units:
            out["display_units"] = units
        mail = core.clean_email(rec.get("email"))        # the person's key across products (arx-export-3, v0.24.0)
        if mail:
            out["email"] = mail
        raw = rec.get("coaching") if isinstance(rec.get("coaching"), dict) else {}   # "How do you tick?" (arx-export-4, v0.26.0)
        coaching = {k: raw[k] for k, allowed in core.COACHING.items() if raw.get(k) in allowed}
        if coaching:
            out["coaching"] = coaching
        target = fatigue_target_of(rec.get("goal"))       # the goal's fatigue target (arx-export-5, v0.27.0)
        if target is not None:
            out["fatigue_target_pct"] = target
        return out
    return facts


def fatigue_target_of(goal) -> int | None:
    """The fatigue target the athlete's goal asks for on the effort-v3 scale (10 = medium, 20 = deep; contract
    vocabulary-2), from the profile's goal mix - None without a goal. The rule is the planner's (arx_plan.goal_effort,
    EFFORT_TARGETS); imported here on demand so the exporter stays light to load."""
    if not isinstance(goal, dict) or not any(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in goal.values()):
        return None
    import arx_plan as planner                    # noqa: WPS433 - the one rule, not a copy of it
    try:
        return int(planner.EFFORT_TARGETS[planner.goal_effort(goal)]["inroad_min"])
    except (KeyError, TypeError, ValueError):
        return None


def range_line(row) -> dict:
    """One current row of the original's "RangeOfMotion2" as a `range_of_motion` line (contract arx-export-5):
    positions in inches as stored, the type as the original names it (Automatic | Static)."""
    rid, uid, exercise, rangetype, start, end, confirmed, created = row
    return {"kind": "range_of_motion", "source_user_id": str(uid), "exercise_code": int(exercise),
            "range_type": (str(rangetype or "").strip() or "Automatic"), "start_in": _number(start), "end_in": _number(end),
            "confirmed": bool(confirmed), "source_rom_id": str(rid), "created_at": _iso(created)}


def person_line(row: dict, person=None) -> dict:
    """A person ARX Insight knows from arx-free's file alone (contract arx-export-6, arx-free's request 9) - NEVER an
    `athlete` line (that kind is the original's users; arx-free's importer would create a duplicate athlete from an id
    it does not know). The key is arx-free's own athlete id (plus the e-mail when it is known); `source_user_id` is
    the id ARX Insight answers `/api/report?user_id=` with (derived, contract arx-free-sets-3); with `person`
    (person_facts) what ARX Insight knows about the person - language, units, e-mail, coaching, fatigue target."""
    ids = row.get("arx_free_ids") or []
    line = {"kind": "person", "arx_free_id": str(ids[0]) if ids else None, "source_user_id": str(row["id"])}
    if person is not None:
        line.update(person(row["id"]))
    return line


def athlete_line(row, person=None) -> dict:
    """The athlete as the original knows them - plus, when `person` (see person_facts) is given, what ARX Insight
    knows about the person: language and display units, only when known."""
    uid, first, last, gender, born, created = row
    sex = {"m": "m", "f": "f"}.get((gender or "").strip().lower()[:1])
    line = {"kind": "athlete", "source_user_id": str(uid), "first_name": first or "", "last_name": last or "", "gender": sex,
            "birth_date": (_iso(born) or "")[:10] or None, "created_at": _iso(created)}
    if person is not None:
        line.update(person(uid))
    return line


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


def export(con, out_path: str, version: str = "", since: str | None = None, person=None, persons=None) -> dict:
    """Write the export from an open connection. Returns the counts. Sets are streamed row by row - the blobs of a
    long history do not fit into memory comfortably on the machine PC. With `since` (a normalised `started_at`
    text, see parse_since) only the sets that began strictly after it are written; the athletes always all - each
    with what ARX Insight knows about the person when `person` (person_facts) is given - and, after them, a `person`
    line for everybody ARX Insight knows from arx-free's file alone (`persons` = arx_sources.people_of rows; v0.28.0).
    The SETS stay the original's: what arx-free recorded itself never comes back to it."""
    counts = {"athletes": 0, "persons": 0, "ranges": 0, "sets": 0, "skipped": 0}
    cur = con.cursor()
    with gzip.open(out_path, "wt", encoding="utf-8", compresslevel=6) as out:
        def write(line: dict) -> None:
            out.write(json.dumps(line, separators=(",", ":"), default=_iso) + "\n")

        write({"kind": "header", "format": FORMAT, "source": SOURCE, "created_at": datetime.now().isoformat(timespec="seconds"),
               "exporter": f"arx-insight {version}".strip(), "since": since})
        cur.execute(USER_SQL)
        for row in cur.fetchall():
            write(athlete_line(row, person))
            counts["athletes"] += 1
        for row in persons or []:                 # the persons known from arx-free alone, after the athletes (arx-export-6)
            write(person_line(row, person))
            counts["persons"] += 1
        cur.execute(RANGE_SQL)                    # the current ranges, after the athletes and before the sets (v0.27.0)
        for row in cur.fetchall():
            write(range_line(row))
            counts["ranges"] += 1
        if since:
            cur.execute(SET_SQL_SINCE, (datetime.fromisoformat(since) + timedelta(seconds=1),))
        else:
            cur.execute(SET_SQL)
        names = [d[0].upper() for d in cur.description]
        while True:
            row = cur.fetchone()
            if row is None:
                break
            try:
                line = set_line(dict(zip(names, row)))
                if since and not ((line["started_at"] or "") > since):   # the same rule on the exported text (seconds)
                    continue
                write(line)
                counts["sets"] += 1
            except (ValueError, OSError, KeyError, TypeError) as exc:            # one unreadable blob must not end the export
                counts["skipped"] += 1
                print(f"set {row[0]} skipped: {type(exc).__name__}", file=sys.stderr)
        write({"kind": "footer", "athletes": counts["athletes"], "persons": counts["persons"], "ranges": counts["ranges"], "sets": counts["sets"]})
    return counts
