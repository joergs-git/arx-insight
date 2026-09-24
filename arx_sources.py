#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 joergsflow - ARX Insight. Free software under the GNU GPL v3 or later; see LICENSE. No warranty.
# -*- coding: utf-8 -*-
"""
ARX Insight - where the sets come from (v0.24.0): the original software's database, arx-free's recordings, or both.

arx-free (the owner's own control software for the machine, `../arx-free`) records sets itself since 2026-09-22 and
keeps them in its SQLite database (`data/arx-free.sqlite`, WAL mode). Those sets never reach the original's Firebird
database - so a device setting says what ARX Insight reads (config.json `sources`, contract `arx-free-sets-1`):

  * `original`  - the Firebird database only (the default: what every version before 0.24.0 did)
  * `arx-free`  - arx-free's database only: its own recordings AND the copies of the original's sets it imported
                  through our export (the full history from one file; a copy keeps the original's set id)
  * `both`      - the Firebird database + arx-free's OWN recordings; the copies are skipped, they ARE the original's
                  sets. No set is ever read twice: each set from the software that recorded it.

Persons: the original's `User` table, and since v0.28.0 (contract arx-free-sets-3, arx-free's request 9) the athletes
that exist only in arx-free's file - listed like everybody with a STABLE id derived from arx-free's athlete uuid
(`derived_uid`: FREE_UID_BASE + a hash, never an id of the original; nothing is stored, so the CLI, the app and a copy
of the file agree). WHO IS WHO across the two products is decided by the e-mail address first - the only unique world
key (owner 2026-09-24; it will carry into a cloud with the data of many machines) - by the technical link arx-free's
import wrote (`source = 'arx-original'`, `source_user_id` = the original's user id) second, and only then by the
derived id. An original user always wins the same e-mail. Names never identify anyone.

How the file is read: opened read-only and copied INTO MEMORY with SQLite's backup API - one consistent snapshot
that sees the WAL (the recent sets live there; a plain copy of the main file would miss them), the file handle is
released at once, arx-free keeps writing undisturbed, nothing is ever written. The snapshot is shared for
SNAPSHOT_TTL_S like the Firebird copy. A missing / unreadable / unknown-layout file never breaks a report: the
original's data are shown with a note.

arx-free's own recordings are handed to the engine in the ORIGINAL'S shapes (samples with Time / Value /
EncoderValue, events with Time / Type, the rep-scheme configuration) so the one decoding path judges every set the
same way (arx_detail.decode_lists, arx_report.curve_from). Imports arx_base and arx_detail only (leaf).
GPL-3.0-or-later (see LICENSE). No warranty. Not medical advice.
"""
from __future__ import annotations
import os, json, gzip, time, sqlite3, threading, hashlib
from datetime import datetime, timedelta, date
from pathlib import Path

import arx_base as core
import arx_detail as detail

MODES = ("original", "arx-free", "both")
DEFAULT_MODE = "original"
FREE_SOURCE = "arx-original"          # `source` arx-free writes on the copies it imported (contract arx-export)
FREE_NAME = "arx-free"                # `source` of a set on the engine's rows when arx-free recorded it
ORIGINAL_NAME = "original"
SCHEMA_VERSION = 4                    # arx-free's PRAGMA user_version this reader was written against (arx-free-sets-1)
SNAPSHOT_TTL_S = 30                   # a snapshot is reused this long while the file looks unchanged
# the columns the reader relies on - contract arx-free-sets-1 (a file without them has an unknown layout)
NEEDED = {
    "athletes": ("id", "email", "source", "source_user_id", "deleted_at", "first_name", "last_name", "gender", "birth_date", "created_at"),
    "sets": ("id", "athlete_id", "session_id", "exercise_code", "started_at", "mode", "protocol", "protocol_value",
             "config_json", "elapsed_s", "junk", "intensity_lb", "max_lb", "max_c_lb", "max_e_lb", "source",
             "source_set_id", "updated_at", "deleted_at"),
    "set_curves": ("set_id", "samples_gz", "events_json"),
}
# arx-free's protocol label -> the original's ExerciseSet.PROTOCOL code (contract modes-3: 4 = fatigue is arx-free's
# own ending, never the original's). An unknown label stays None -> ending "unknown", never silently "reps".
PROTOCOL_CODES = {"Reps": 3, "Countdown": 1, "Inroad": 0, "FatigueTarget": 4}
DB_NAME = "arx-free.sqlite"
HERE = os.path.dirname(os.path.abspath(__file__))

# --- persons that exist only in arx-free (v0.28.0, contract arx-free-sets-3, arx-free's request 9) ------------------
FREE_UID_BASE = 1_000_000             # their Insight ids start here - the original's ids are small, these never collide with them
FREE_UID_SPAN = 1_000_000_000         # ... and stay below FREE_UID_BASE + this (Firebird INTEGER for the queries, 2^53 for the page)


def derived_uid(athlete_id) -> int:
    """The stable Insight id of an arx-free athlete that matches none of the original's users: derived from
    arx-free's uuid, so it is the same on every start, in the CLI and the app, on a copy of the file - nothing is
    stored, nothing can drift, and every file keyed by a user id (profile, ledger, boards, access codes) just works."""
    digest = hashlib.sha256(str(athlete_id).encode("utf-8")).hexdigest()[:8]
    return FREE_UID_BASE + int(digest, 16) % FREE_UID_SPAN


def assign_ids(athletes: list[dict]) -> dict:
    """{athlete id: derived uid} for these athletes - deterministic even when two uuids hash alike (odds about
    n^2 / 2e9): the athletes are walked in the order created_at, id and a later one probes upward from its own value,
    so the earlier athlete keeps the id it always had."""
    taken, out = set(), {}
    for a in sorted(athletes, key=lambda a: (str(a.get("created_at") or ""), str(a["id"]))):
        uid = derived_uid(a["id"])
        while uid in taken:
            uid += 1
        taken.add(uid)
        out[a["id"]] = uid
    return out


def emails_from_goals(path: str | None = None) -> dict:
    """{Insight user id: e-mail} of every profile in goals.json that carries one - the key that tells which arx-free
    athlete is which of our persons (owner 2026-09-24: the e-mail is the only unique key across products). The app
    and the CLI read the same file, so both map the same persons."""
    path = path or os.path.join(core.data_dir(), "goals.json")
    try:
        with open(path, encoding="utf-8") as fh:
            goals = json.load(fh)
    except (OSError, ValueError):
        return {}
    out = {}
    for uid, rec in (goals.items() if isinstance(goals, dict) else []):
        mail = core.clean_email(rec.get("email")) if isinstance(rec, dict) else None
        if mail and str(uid).isdigit():
            out[int(uid)] = mail
    return out


def person_sex(gender) -> str | None:
    """arx-free's gender text (its form is free: m / f / male / female / männlich / weiblich, mostly nothing) ->
    male | female | None - the same reading the original's column gets in arx_base.user_profile."""
    g = str(gender or "").strip().lower()
    return {"m": "male", "f": "female", "w": "female"}.get(g[:1]) if g else None


def person_birth(text) -> date | None:
    """arx-free's birth_date TEXT (ISO date) as a date - None for nothing or garbage."""
    try:
        return date.fromisoformat(str(text)[:10]) if text else None
    except ValueError:
        return None


def person_of(a: dict, uid: int) -> dict:
    """The person behind an arx-free-only athlete, in the shapes the app needs: the user-list row's keys (id, name,
    gender, birthdate, created), what user_profile needs (sex, birth) and the link back (arx_free_id)."""
    first, last = str(a.get("first_name") or "").strip(), str(a.get("last_name") or "").strip()
    return {"id": uid, "name": f"{first} {last}".strip() or f"User {uid}", "first": first, "last": last,
            "gender": str(a.get("gender") or "").strip(), "sex": person_sex(a.get("gender")),
            "birth": person_birth(a.get("birth_date")), "birthdate": str(a.get("birth_date") or "")[:10] or None,
            "created": str(a.get("created_at") or "")[:10] or None, "arx_free_id": str(a["id"]), "source": FREE_NAME}


class SourceError(Exception):
    """arx-free's database cannot be used: code = missing | unreadable | unknown_layout (fit for the settings window)."""
    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def mode_of(cfg: dict | None) -> str:
    """The device setting, or the default (the original's database only)."""
    mode = (cfg or {}).get("sources")
    return mode if mode in MODES else DEFAULT_MODE


def find_arx_free_db(cfg: dict | None) -> tuple[str | None, bool]:
    """(path, configured): the path config.json / ARX_FREE_DB names (configured = True, whether or not the file
    exists - the settings window says so), else the first of the usual places that exists: the kiosk's
    `Documents\\arx-free\\data\\arx-free.sqlite`, or the sibling checkout next to this folder (development)."""
    configured = str((cfg or {}).get("arx_free_db") or "").strip() or os.environ.get("ARX_FREE_DB", "").strip()
    if configured:
        return configured, True
    home = os.environ.get("USERPROFILE") if os.name == "nt" else None
    for candidate in (os.path.join(home or os.path.expanduser("~"), "Documents", "arx-free", "data", DB_NAME),
                      os.path.join(HERE, "..", "arx-free", "data", DB_NAME)):
        if os.path.isfile(candidate):
            return os.path.normpath(candidate), False
    return None, False


def _signature(path: str) -> tuple:
    """What "unchanged" means for a WAL database: the main file AND the -wal file (the new sets sit there)."""
    out = []
    for name in (path, path + "-wal"):
        try:
            st = os.stat(name)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _protocol_code(label) -> int | None:
    return PROTOCOL_CODES.get(str(label or "").strip())


class FreeSnapshot:
    """One consistent in-memory copy of arx-free's database (read-only, see the module docstring)."""

    def __init__(self, path: str, con: sqlite3.Connection, sig: tuple):
        self.path, self._con, self.sig, self.made = path, con, sig, time.time()
        self._lock = threading.Lock()
        self.user_version = self._con.execute("PRAGMA user_version").fetchone()[0]

    @classmethod
    def open(cls, path: str) -> "FreeSnapshot":
        if not os.path.isfile(path):
            raise SourceError("missing", path)
        sig = _signature(path)
        src = None
        try:
            try:                                                    # a real read-only open (the file is arx-free's)
                src = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
                src.execute("select 1 from sqlite_master limit 1").fetchall()
            except sqlite3.Error:                                   # a WAL file without its -shm: fall back to a plain open, SELECT only
                if src is not None:
                    src.close()
                src = sqlite3.connect(path, timeout=5)
            mem = sqlite3.connect(":memory:", check_same_thread=False)
            src.backup(mem)                                         # one consistent snapshot incl. the WAL
        except sqlite3.Error as exc:
            raise SourceError("unreadable", str(exc)) from None
        finally:
            if src is not None:
                src.close()                                         # the handle is released at once - arx-free writes on
        snap = cls(path, mem, sig)
        missing = snap._missing_columns()
        if missing:
            mem.close()
            raise SourceError("unknown_layout", f"schema {snap.user_version}: missing {', '.join(missing)}")
        return snap

    def _missing_columns(self) -> list[str]:
        out = []
        for table, cols in NEEDED.items():
            have = {row[1] for row in self._con.execute(f"PRAGMA table_info({table})").fetchall()}
            out += [f"{table}.{c}" for c in cols if c not in have]
        return out

    def _rows(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            cur = self._con.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def close(self) -> None:
        try:
            self._con.close()
        except sqlite3.Error:
            pass

    # --- who is who --------------------------------------------------------------------------------------------
    def athletes(self) -> list[dict]:
        return self._rows("select id, email, source, source_user_id, first_name, last_name, gender, birth_date, created_at "
                          "from athletes where deleted_at is null")

    def user_map(self, emails: dict | None, derive: bool = True) -> tuple[dict, int, dict]:
        """({arx-free athlete id: Insight user id}, athletes that map to nobody, {derived id: person}). Order of the
        rule (contract arx-free-sets-3): the e-mail first (case-insensitive, `emails` = {Insight user id: e-mail} from
        the profiles; an original user wins over a derived one carrying the same address), then arx-free's import link
        (source = the original, source_user_id = the original's id), then - with `derive` - an id of its own derived
        from the athlete's uuid: the person exists only in arx-free and is listed like everybody (v0.28.0). Without
        `derive` (the original-only mode's status line) such athletes are only counted."""
        pairs = sorted((int(uid), core.clean_email(mail)) for uid, mail in (emails or {}).items() if str(uid).isdigit())
        by_email = {}
        for uid, mail in pairs:                      # ascending: a Firebird id beats a derived one with the same e-mail
            if mail:
                by_email.setdefault(mail, uid)
        mapped, own, persons = {}, [], {}
        for a in self.athletes():
            uid = by_email.get(core.clean_email(a.get("email")) or "")
            if uid is None and a.get("source") == FREE_SOURCE and str(a.get("source_user_id") or "").isdigit():
                uid = int(a["source_user_id"])
            if uid is None:
                own.append(a)
            else:
                mapped[a["id"]] = uid
        if not derive:
            return mapped, len(own), persons
        for athlete_id, uid in assign_ids(own).items():
            mapped[athlete_id] = uid
        for a in own:
            persons[mapped[a["id"]]] = person_of(a, mapped[a["id"]])
        return mapped, 0, persons

    # --- the sets ----------------------------------------------------------------------------------------------
    @staticmethod
    def _filter(athlete_ids: list, mode: str) -> tuple[str, tuple]:
        marks = ",".join("?" * len(athlete_ids))
        where = f"athlete_id in ({marks}) and deleted_at is null"
        if mode != "arx-free":                       # both: arx-free's OWN recordings only - the copies are the original's sets
            where += " and source is null"
        return where, tuple(athlete_ids)

    @staticmethod
    def set_key(row: dict):
        """The engine's id of a set: the original's integer id for a copy (so the detail cache, the ledger and every
        chart key are the same whichever file it was read from), arx-free's uuid text for its own recording."""
        if row.get("source") == FREE_SOURCE and str(row.get("source_set_id") or "").isdigit():
            return int(row["source_set_id"])
        return str(row["id"])

    def set_rows(self, athlete_ids: list, mode: str) -> list[dict]:
        """The sets of these athletes as rows in the shape arx_report.load_sets builds from (the original's column
        names, plus the parsed events / configuration and the recorder's name)."""
        if not athlete_ids:
            return []
        where, params = self._filter(athlete_ids, mode)
        out = []
        for r in self._rows(f"select * from sets where {where} order by started_at", params):
            try:
                scheme = json.loads(r.get("config_json") or "{}")
            except ValueError:
                scheme = {}
            try:
                events = json.loads(self._rows("select events_json from set_curves where set_id = ?", (r["id"],))[0]["events_json"])
            except (IndexError, ValueError, TypeError):
                events = []
            static = str(r.get("mode") or "").strip().lower() == "static"
            try:                                     # the live coach's notes (contract arx-free-sets-2, schema 5): optional, own recordings only
                coach = json.loads(r["coach_json"]) if r.get("coach_json") else None
            except (ValueError, TypeError):
                coach = None
            out.append({
                "ID": self.set_key(r), "EXERCISEDATE": str(r["started_at"]).replace("T", " "), "SESSION": r.get("session_id"),
                "EXERCISE": r["exercise_code"], "PROTOCOL": _protocol_code(r.get("protocol")),
                "MAXLOAD": r.get("max_lb"), "CONCENTRICMAX": r.get("max_c_lb"), "ECCENTRICMAX": r.get("max_e_lb"),
                "INTENSITY": r.get("intensity_lb"), "ELAPSEDSECONDS": r.get("elapsed_s"),
                "HIDEFROMSTATS": bool(r.get("junk")), "REPSCHEME": "StaticModeData" if static else "LoopingRepSequence",
                "_events": events if isinstance(events, list) else [], "_scheme": scheme if isinstance(scheme, dict) else {},
                "_source": ORIGINAL_NAME if r.get("source") == FREE_SOURCE else FREE_NAME,
                "_uuid": r["id"], "_coach": coach if isinstance(coach, dict) else None,
            })
        return out

    def signature(self, athlete_ids: list, mode: str) -> list[str]:
        """count, newest start, newest change of the sets this mode reads for these athletes - a new set changes it."""
        if not athlete_ids:
            return ["0", "", ""]
        where, params = self._filter(athlete_ids, mode)
        row = self._rows(f"select count(*) as n, max(started_at) as s, max(updated_at) as u from sets where {where}", params)[0]
        return [str(row["n"]), str(row["s"] or ""), str(row["u"] or "")]

    def _find(self, set_id) -> dict | None:
        if isinstance(set_id, str):
            rows = self._rows("select * from sets where id = ? and deleted_at is null", (set_id,))
        else:
            rows = self._rows("select * from sets where source = ? and source_set_id = ? and deleted_at is null", (FREE_SOURCE, str(set_id)))
        return rows[0] if rows else None

    def parsed(self, set_id) -> tuple[list, list, dict, str]:
        """(samples, events, scheme, repscheme) of one set in the ORIGINAL'S shapes: samples {Time, Value,
        EncoderValue}, events {Time, Type, AdditionalData} with absolute ISO times (set start + the recorder's
        relative seconds), values in lb / inch - exactly what the Firebird blobs decode to, so arx_detail and
        arx_report judge the set with the same code."""
        row = self._find(set_id)
        if row is None:
            raise KeyError(f"no such set in arx-free's database: {set_id}")
        curve = self._rows("select samples_gz, events_json from set_curves where set_id = ?", (row["id"],))
        try:
            columns = json.loads(gzip.decompress(curve[0]["samples_gz"]).decode("utf-8")) if curve else {}
        except (ValueError, OSError, TypeError):
            columns = {}
        try:
            events = json.loads(curve[0]["events_json"]) if curve else []
        except (ValueError, TypeError):
            events = []
        try:
            scheme = json.loads(row.get("config_json") or "{}")
        except ValueError:
            scheme = {}
        start = datetime.fromisoformat(str(row["started_at"]))

        def iso(t) -> str:
            return (start + timedelta(seconds=float(t))).isoformat(timespec="microseconds")

        samples = []
        for t, force, pos in zip(columns.get("t") or [], columns.get("force_lb") or [], columns.get("pos_in") or []):
            if t is None or force is None:
                continue
            samples.append({"Time": iso(t), "Value": float(force), "HasEncoder": pos is not None,
                            "EncoderValue": float(pos) if pos is not None else 0.0})
        evs = [{"Time": iso(e["Time"]), "Type": e.get("Type"), "AdditionalData": e.get("AdditionalData")}
               for e in (events if isinstance(events, list) else []) if isinstance(e, dict) and e.get("Time") is not None]
        static = str(row.get("mode") or "").strip().lower() == "static"
        return samples, evs, scheme if isinstance(scheme, dict) else {}, "StaticModeData" if static else "LoopingRepSequence"


# --- the shared snapshot (like arx_base.shared_connection: reused while the file looks unchanged) ---------------
_LOCK = threading.Lock()
_SNAPS: dict[str, FreeSnapshot] = {}


def shared(path: str) -> FreeSnapshot:
    with _LOCK:
        snap = _SNAPS.get(path)
        if snap is not None and snap.sig == _signature(path) and time.time() - snap.made <= SNAPSHOT_TTL_S:
            return snap
        fresh = FreeSnapshot.open(path)             # raises SourceError
        if snap is not None:
            snap.close()
        _SNAPS[path] = fresh
        return fresh


def drop_snapshots() -> None:
    with _LOCK:
        for snap in _SNAPS.values():
            snap.close()
        _SNAPS.clear()


# --- the connection the engine sees --------------------------------------------------------------------------
class Connection:
    """The Firebird connection (everything it has is passed through) plus, when the setting asks for it, arx-free's
    snapshot: `free` (None when nothing is read from arx-free), `mode` (the EFFECTIVE mode - `arx-free` without a
    usable file falls back to the original with a note), `by_user` {Insight user id: [arx-free athlete ids]},
    `unmapped` (athletes of arx-free that belong to nobody here - 0 since v0.28.0, they become persons), `persons`
    {derived id: person} for the athletes that exist only in arx-free (v0.28.0) and `note` {code, path} when the file
    is not usable."""

    def __init__(self, fb, free: FreeSnapshot | None, mode: str, by_user: dict, unmapped: int, note: dict | None, path: str | None,
                 wanted: str, persons: dict | None = None):
        self._fb, self.free, self.mode, self.by_user, self.unmapped, self.note, self.path, self.wanted = \
            fb, free, mode, by_user, unmapped, note, path, wanted
        self.persons = persons or {}

    def __getattr__(self, name):                 # cursor(), close(), ... of the Firebird connection
        return getattr(self._fb, name)

    def person(self, user_id) -> dict | None:
        """The person behind an id that exists only in arx-free's file (v0.28.0; keys see person_of) - None for the
        original's users, so arx_base.user_profile falls through to the "User" table for them."""
        try:
            return self.persons.get(int(user_id))
        except (TypeError, ValueError):
            return None

    def free_ids(self, user_id) -> list:
        return self.by_user.get(int(user_id), []) if self.free is not None else []

    def reads_free(self, set_id) -> bool:
        """Does this set id come from arx-free's snapshot? Its own recordings carry text ids; when arx-free's file is
        THE source, every set does."""
        return self.free is not None and (self.mode == "arx-free" or isinstance(set_id, str))


def attach(fb, cfg: dict | None, emails: dict | None = None) -> Connection:
    """Wrap the Firebird connection according to the device setting. Never raises for arx-free's sake: a missing or
    unusable file leaves `free` None with a note, and the mode falls back to the original's data."""
    wanted = mode_of(cfg)
    path, configured = find_arx_free_db(cfg)
    if wanted == "original":
        return Connection(fb, None, wanted, {}, 0, None, path, wanted)
    if not path:
        return Connection(fb, None, "original", {}, 0, {"code": "missing", "path": None}, None, wanted)
    try:
        snap = shared(path)
    except SourceError as exc:
        return Connection(fb, None, "original", {}, 0, {"code": exc.code, "path": path, "detail": exc.detail}, path, wanted)
    mapped, unmapped, persons = snap.user_map(emails)
    by_user: dict = {}
    for athlete_id, uid in mapped.items():
        by_user.setdefault(uid, []).append(athlete_id)
    return Connection(fb, snap, wanted, by_user, unmapped, None, path, wanted, persons)


def describe(con, sets: list[dict] | None = None) -> dict:
    """The report's `sources` block: what was read from where (FakeDB and plain connections read the original only).
    No file path here - the report also travels to a phone; the path is the settings window's (bootstrap, local)."""
    free = getattr(con, "free", None)
    own = sum(1 for s in sets or [] if s.get("source") == FREE_NAME)
    note = getattr(con, "note", None)
    return {"mode": getattr(con, "mode", DEFAULT_MODE), "wanted": getattr(con, "wanted", DEFAULT_MODE),
            "arx_free": {"found": free is not None, "own_sets": own, "unmapped_athletes": getattr(con, "unmapped", 0),
                         "own_persons": len(getattr(con, "persons", None) or {}),     # persons known from arx-free alone (v0.28.0)
                         "note": {"code": note["code"]} if note else None,
                         "schema": free.user_version if free is not None else None}}


def status(cfg: dict | None, emails: dict | None = None) -> dict:
    """For the settings window: the setting, the file (found / configured / note) and how many arx-free athletes
    belong to nobody here. Opens the shared snapshot (cheap: one in-memory copy per SNAPSHOT_TTL_S)."""
    mode = mode_of(cfg)
    path, configured = find_arx_free_db(cfg)
    out = {"mode": mode, "path": path, "configured": configured, "found": False, "unmapped_athletes": 0, "note": None,
           "own_sets": 0, "own_persons": 0}
    if not path:
        out["note"] = {"code": "missing", "path": None}
        return out
    try:
        snap = shared(path)
    except SourceError as exc:
        out["note"] = {"code": exc.code, "path": path, "detail": exc.detail}
        return out
    mapped, out["unmapped_athletes"], persons = snap.user_map(emails, derive=mode != "original")   # original-only: counted, not persons
    out["found"] = True
    out["own_persons"] = len(persons)
    out["own_sets"] = int(snap._rows("select count(*) as n from sets where source is null and deleted_at is null")[0]["n"])
    return out


def people_of(con) -> list[dict]:
    """The persons that exist only in arx-free's file as rows of the app's user list (arx_app.search_users, v0.28.0):
    the keys of a row of the original's User table plus `source` and `arx_free_ids` (the link arx-free resolves its
    athlete by, contract arx-free-sets-3). Empty on a plain connection and in the original-only mode."""
    out = []
    for uid, p in sorted((getattr(con, "persons", None) or {}).items()):
        out.append({"id": uid, "name": p["name"], "first": p["first"], "last": p["last"], "gender": p["gender"],
                    "birthdate": p["birthdate"], "created": p["created"], "source": FREE_NAME, "arx_free_ids": [p["arx_free_id"]]})
    return out
