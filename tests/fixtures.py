"""Synthetic ARX data for the tests - no private data, no Firebird needed.

make_set() builds ONE recorded set the way the ARX app stores it: the gzip'd UTF-16 JSON sample
blob (force lb, position inch, timestamps), the JSON event stream with the machine's phase markers
and the rep-scheme JSON. FakeDB serves these rows through the tiny part of the DB-API that
arx_report uses, so build_report() runs end to end against generated data.

Phase orientation follows the real machine: encoder value RISING = concentric. So a set whose
EndPosition is larger than its StartPosition has a concentric first half (Row, presses, curl),
otherwise the first half is eccentric (Pull Down, Dead Lift, Belt Squat).
"""
from __future__ import annotations
import gzip, json
from datetime import datetime, timedelta, timezone

TZ = timezone(timedelta(hours=2))
HZ = 20                                   # samples per second


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="microseconds")


def make_set(set_id: int, exercise: int, start: datetime, *, reps: int = 8, sec_per_dir: float = 5.0,
             pause_end: float = 0.0, pause_start: float = 3.0, start_pos: float = 8.4, end_pos: float = 20.0,
             con: tuple = (150.0, 0.03), ecc: tuple = (240.0, 0.02), hold: float = 60.0,
             hidden: bool = False, finished: bool = True, user: int = 1, protocol: int = 3,
             unfinished_tail: bool = False) -> dict:
    """One set. con / ecc = (force in lb at rep 1, relative decline per rep): (150, 0.03) means the
    concentric force starts at 150 lb and loses 3 % of that per rep. hold = force during pauses.
    unfinished_tail adds a started but never finished rep (first half only), like a countdown set."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    rising_first = end_pos > start_pos                       # first half concentric?
    samples, events = [], []
    t = start
    events.append({"Time": _iso(t), "Type": "BeginSequence"})

    def run(seconds: float, force: float, p0: float, p1: float):
        nonlocal t
        n = max(1, int(round(seconds * HZ)))
        for i in range(n):
            pos = p0 + (p1 - p0) * (i / n)
            samples.append({"Value": round(force, 1), "RawValue": int(force), "HasRawValue": True, "TaredValue": 3,
                            "HasTaredValue": True, "Time": _iso(t), "HasEncoder": True,
                            "EncoderValue": round(pos, 3), "HasSpeed": True, "SpeedValue": 80})
            t += timedelta(seconds=1.0 / HZ)

    c_max = e_max = 0.0
    for r in range(reps):
        c = con[0] * (1 - con[1] * r)
        e = ecc[0] * (1 - ecc[1] * r)
        c_max, e_max = max(c_max, c), max(e_max, e)
        first, second = (c, e) if rising_first else (e, c)
        events.append({"Time": _iso(t), "Type": "BeginRep"})
        events.append({"Time": _iso(t), "Type": "BeginFirstHalf"})
        run(sec_per_dir, first, start_pos, end_pos)
        events.append({"Time": _iso(t), "Type": "BeginPauseAfterFirstHalf"})
        if pause_end:
            run(pause_end, hold, end_pos, end_pos)
        events.append({"Time": _iso(t), "Type": "BeginSecondHalf"})
        run(sec_per_dir, second, end_pos, start_pos)
        events.append({"Time": _iso(t), "Type": "BeginPauseAfterSecondHalf"})
        if pause_start:
            run(pause_start, hold * 0.4, start_pos, start_pos)
        events.append({"Time": _iso(t), "Type": "EndRep"})
    if unfinished_tail:
        events.append({"Time": _iso(t), "Type": "BeginRep"})
        events.append({"Time": _iso(t), "Type": "BeginFirstHalf"})
        level = (con if rising_first else ecc)[0] * 0.5
        run(sec_per_dir * 0.6, level, start_pos, start_pos + (end_pos - start_pos) * 0.6)
        finished = False
    events.append({"Time": _iso(t), "Type": "EndSequence" if finished else "SequenceEndedBeforeCompletion"})

    seconds = (t - start).total_seconds()
    forces = [s["Value"] for s in samples]
    scheme = {"StartPosition": start_pos, "EndPosition": end_pos,
              "StartToEndSpeed": {"InchesPerSecond": abs(end_pos - start_pos) / sec_per_dir, "MaxInchesPerSecond": 3.15},
              "EndToStartSpeed": {"InchesPerSecond": abs(end_pos - start_pos) / sec_per_dir, "MaxInchesPerSecond": 3.15},
              "AccelerationTime": 0.25, "DecelerationTime": 0.25, "PauseAfterEndPosition": pause_end,
              "PauseAfterStartPosition": pause_start, "PreExerciseTimer": 5}
    return {
        "ID": set_id, "USER_ID": user, "EXERCISEDATE": start.replace(tzinfo=None), "SESSION": 1000 + set_id,
        "EXERCISE": exercise, "PROTOCOL": protocol, "MAXLOAD": max(forces), "CONCENTRICMAX": c_max,
        "ECCENTRICMAX": e_max, "INTENSITY": sum(forces) / len(forces), "ELAPSEDSECONDS": seconds,
        "REPSCHEMEDATA": json.dumps(scheme).encode("latin1"),
        "EVENTSTREAMDATA": json.dumps(events).encode("latin1"),
        "SERIALIZEDDETAILEDDATA": gzip.compress(json.dumps(samples).encode("utf-16")),
        "HIDEFROMSTATS": hidden, "REPSCHEME": "LoopingRepSequence",
    }


def make_static_set(set_id: int, exercise: int, start: datetime, *, seconds: float = 60.0,
                    start_force: float = 300.0, end_force: float = 200.0, user: int = 1) -> dict:
    """An isometric set (ARX "Static" mode): no reps, no phases, force fading from start_force to
    end_force (lb) at one position."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    n = int(seconds * HZ)
    samples = [{"Value": round(start_force + (end_force - start_force) * i / n, 1), "Time": _iso(start + timedelta(seconds=i / HZ)),
                "HasEncoder": True, "EncoderValue": 12.0, "HasSpeed": True, "SpeedValue": 0} for i in range(n + 1)]
    events = [{"Time": _iso(start), "Type": "BeginSequence"},
              {"Time": _iso(start + timedelta(seconds=seconds)), "Type": "EndSequence"}]
    scheme = {"StartPosition": 12.0, "EndPosition": 12.0, "PauseAfterEndPosition": 0, "PauseAfterStartPosition": 0}
    forces = [x["Value"] for x in samples]
    return {"ID": set_id, "USER_ID": user, "EXERCISEDATE": start.replace(tzinfo=None), "SESSION": 1000 + set_id,
            "EXERCISE": exercise, "PROTOCOL": 1, "MAXLOAD": max(forces), "CONCENTRICMAX": max(forces),
            "ECCENTRICMAX": max(forces), "INTENSITY": sum(forces) / len(forces), "ELAPSEDSECONDS": seconds,
            "REPSCHEMEDATA": json.dumps(scheme).encode("latin1"), "EVENTSTREAMDATA": json.dumps(events).encode("latin1"),
            "SERIALIZEDDETAILEDDATA": gzip.compress(json.dumps(samples).encode("utf-16")),
            "HIDEFROMSTATS": False, "REPSCHEME": "StaticModeData"}


class _Cursor:
    """The queries the engine issues (the set list of one user, single sets by id), answered from
    the generated rows."""
    LIST_COLS = ["ID", "EXERCISEDATE", "SESSION", "EXERCISE", "PROTOCOL", "MAXLOAD", "CONCENTRICMAX", "ECCENTRICMAX",
                 "INTENSITY", "ELAPSEDSECONDS", "REPSCHEMEDATA", "EVENTSTREAMDATA", "HIDEFROMSTATS", "REPSCHEME"]

    def __init__(self, rows, users=None):
        self._rows, self._users, self._result, self.description = rows, users or {}, [], []

    def execute(self, sql: str, params=()):
        q = " ".join(sql.lower().split())
        if 'from "user"' in q:                                          # name-free profile: gender, birthdate
            self._result = [self._users[params[0]]] if params[0] in self._users else []
        elif "where user_id" in q:                                      # load_sets
            rows = sorted((r for r in self._rows if r["USER_ID"] == params[0]), key=lambda r: r["EXERCISEDATE"])
            self.description = [(c,) for c in self.LIST_COLS]
            self._result = [tuple(r[c] for c in self.LIST_COLS) for r in rows]
        elif "where id = ?" in q:                                       # one set by id: any column list
            r = next(r for r in self._rows if r["ID"] == params[0])
            cols = [c.strip().strip('"').upper() for c in q[len("select "):q.index(" from ")].split(",")]
            self._result = [tuple(r[c] for c in cols)]
        else:
            raise AssertionError(f"unexpected query: {sql}")

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        return self._result[0] if self._result else None


class FakeDB:
    """users = {user id: (gender, birthdate)} answers the profile query (no names exist here at all)."""
    def __init__(self, rows, users=None):
        self.rows, self.users = rows, users or {}

    def cursor(self):
        return _Cursor(self.rows, self.users)

    def close(self):
        pass


# a small catalog in the shape of exercises.json
CATALOG = {
    "3":  {"name": "Row", "group": "Pull", "kind": "compound", "targets": ["upper_back", "lats"],
           "limiters": ["grip", "elbow_flexors"], "joints": ["shoulder", "elbow", "wrist", "lower_back"]},
    "4":  {"name": "Pull Down", "group": "Pull", "kind": "compound", "targets": ["lats", "upper_back"],
           "limiters": ["grip", "elbow_flexors"], "joints": ["shoulder", "elbow", "wrist"]},
    "10": {"name": "Dead Lift", "group": "Drive", "kind": "compound", "targets": ["glutes", "hamstrings", "quads"],
           "limiters": ["grip", "lower_back"], "joints": ["hip", "knee", "lower_back", "wrist"]},
    "11": {"name": "Biceps Curl", "group": "Pull", "kind": "isolation", "targets": ["elbow_flexors"],
           "limiters": ["grip"], "joints": ["elbow", "wrist"]},
    "19": {"name": "Belt Squat", "group": "Drive", "kind": "compound", "targets": ["quads", "glutes"],
           "limiters": [], "joints": ["knee", "hip", "lower_back"]},
    "23": {"name": "Horizontal Press", "group": "Push", "kind": "compound", "targets": ["chest"],
           "limiters": ["triceps", "shoulders"], "joints": ["shoulder", "elbow", "wrist"]},
}


def cfg(today: str, **extra) -> dict:
    """A report config like arx_app.make_report builds it (fixed 'today' for reproducible results)."""
    base = {"user_id": 1, "alias": "Athlete", "goal": {"muscle": 0.6, "strength": 0.3, "conditioning": 0.1},
            "sessions_per_week": 2, "units": "metric", "language": "en", "_today": today, "_catalog": CATALOG,
            "_no_detail_cache": True}      # generated sets reuse ids and dates - never cache their detail
    base.update(extra)
    return base
