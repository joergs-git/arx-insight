"""Export for arx-free (contract arx-export-1): synthetic rows in, a complete verbatim file out."""

import gzip
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import export_for_arx_free as exporter  # noqa: E402
from tests.fixtures import make_set  # noqa: E402


class FakeCursor:
    def __init__(self, users, sets):
        self.users, self.sets, self.rows, self.description = users, sets, [], []

    def execute(self, sql, params=()):
        if '"User"' in sql:
            self.rows = list(self.users)
        else:
            columns = ["ID", "USER_ID", "EXERCISEDATE", "EXERCISE", "PROTOCOL", "PROTOCOLPARAMETER", "REPSCHEME", "ELAPSEDSECONDS", "INTENSITY",
                       "MAXLOAD", "CONCENTRICMAX", "ECCENTRICMAX", "HIDEFROMSTATS", "NOTES", "RESTTIMER", "RESTTIMERUSED", "COMPARISONSET_ID",
                       "REPSCHEMEDATA", "EVENTSTREAMDATA", "SERIALIZEDDETAILEDDATA"]
            self.description = [(c,) for c in columns]
            self.rows = [tuple(s.get(c) for c in columns) for s in sorted(self.sets, key=lambda s: s["ID"])]

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeConnection:
    def __init__(self, users, sets):
        self._cursor = FakeCursor(users, sets)

    def cursor(self):
        return self._cursor


def read(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


class ExportTest(unittest.TestCase):
    def test_a_complete_verbatim_export(self):
        first = make_set(10, 10, datetime(2026, 9, 13, 18, 0), reps=2, start_pos=20.35, end_pos=9.07)
        first.update(PROTOCOLPARAMETER=2, NOTES="  grip aid ", RESTTIMER=180, RESTTIMERUSED=201, COMPARISONSET_ID=None)
        events = json.loads(first["EVENTSTREAMDATA"].decode("latin1"))
        events.insert(1, {"Time": events[0]["Time"], "Type": "WaitingTimeLeft"})
        first["EVENTSTREAMDATA"] = json.dumps(events).encode("latin1")
        second = make_set(11, 10, datetime(2026, 9, 20, 18, 0), reps=2, hidden=True)
        second.update(PROTOCOLPARAMETER=2, NOTES=None, RESTTIMER=None, RESTTIMERUSED=None, COMPARISONSET_ID=10)
        users = [(1, "Anna", "Beispiel", "Female", datetime(1985, 5, 17), datetime(2024, 3, 1, 10, 0)),
                 (2, "Ben", "Muster", None, datetime(1, 1, 1), None)]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.ndjson.gz")
            counts = exporter.export(FakeConnection(users, [second, first]), path, "9.9.9")
            lines = read(path)
        self.assertEqual(counts, {"athletes": 2, "sets": 2, "skipped": 0})
        self.assertEqual([line["kind"] for line in lines], ["header", "athlete", "athlete", "set", "set", "footer"])
        self.assertEqual((lines[0]["format"], lines[0]["source"], lines[0]["exporter"]), ("arx-export-1", "arx-original", "arx-insight 9.9.9"))
        self.assertEqual(lines[1], {"kind": "athlete", "source_user_id": "1", "first_name": "Anna", "last_name": "Beispiel", "gender": "f",
                                    "birth_date": "1985-05-17", "created_at": "2024-03-01T10:00:00"})
        self.assertIsNone(lines[2]["birth_date"])                                  # the app's placeholder date means "never"
        one, two = lines[3], lines[4]
        self.assertEqual([one["source_set_id"], two["source_set_id"]], ["10", "11"])        # ordered by id: a set may refer to an earlier one
        self.assertEqual((one["started_at"], one["protocol"], one["protocol_parameter"], one["rep_scheme"]), ("2026-09-13T18:00:00", 3, 2.0, "LoopingRepSequence"))
        self.assertEqual((one["notes"], one["rest_timer_used_s"], two["comparison_source_set_id"], two["hide_from_stats"]), ("grip aid", 201.0, "10", True))
        self.assertEqual(one["config"]["StartPosition"], 20.35)                    # verbatim configuration
        self.assertNotIn("WaitingTimeLeft", [e["Type"] for e in one["events"]])
        self.assertEqual(one["events"][0]["Type"], "BeginSequence")
        self.assertEqual(set(one["samples"][0]) >= {"Time", "Value", "EncoderValue"}, True)
        self.assertEqual(lines[-1], {"kind": "footer", "athletes": 2, "sets": 2})
        self.assertNotIn("email", json.dumps(lines).lower())                       # nothing but name, gender, birth date of a person

    def test_since_keeps_only_the_sets_that_began_after_it(self):
        # contract arx-export-2: `since` = a started_at text of the export; strictly after it; the athletes always all
        rows = [make_set(i, 10, datetime(2026, 9, 13, 18, 0, i), reps=1) for i in (10, 11, 12)]
        for row in rows:
            row.update(PROTOCOLPARAMETER=1, NOTES=None, RESTTIMER=None, RESTTIMERUSED=None, COMPARISONSET_ID=None)
        users = [(1, "A", "B", "m", None, None), (2, "C", "D", "f", None, None)]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.ndjson.gz")
            counts = exporter.export(FakeConnection(users, rows), path, "9.9.9", since="2026-09-13T18:00:11")
            lines = read(path)
            exporter.export(FakeConnection(users, rows), path)
            everything = read(path)
        self.assertEqual(counts, {"athletes": 2, "sets": 1, "skipped": 0})
        self.assertEqual((lines[0]["format"], lines[0]["since"]), ("arx-export-1", "2026-09-13T18:00:11"))    # the line format is unchanged
        self.assertEqual([line["kind"] for line in lines], ["header", "athlete", "athlete", "set", "footer"])
        self.assertEqual((lines[3]["source_set_id"], lines[3]["started_at"]), ("12", "2026-09-13T18:00:12"))    # 18:00:11 itself is not "after"
        self.assertEqual(lines[-1], {"kind": "footer", "athletes": 2, "sets": 1})
        self.assertEqual((everything[0]["since"], everything[-1]["sets"]), (None, 3))                          # no since: null, all sets

    def test_since_is_a_started_at_text_or_nothing(self):
        self.assertEqual(exporter.parse_since("2026-09-13T18:04:11"), "2026-09-13T18:04:11")
        self.assertEqual(exporter.parse_since(" 2026-09-13 18:04:11.437 "), "2026-09-13T18:04:11")   # fractions are cut like the export does
        self.assertEqual(exporter.parse_since("2026-09-13"), "2026-09-13T00:00:00")
        for bad in ("yesterday", "2026-09-13T18:04:11+02:00", "", None, 5, "0001-01-01T00:00:00"):
            self.assertIsNone(exporter.parse_since(bad), bad)

    def test_one_unreadable_set_does_not_end_the_export(self):
        good = make_set(10, 10, datetime(2026, 9, 13, 18, 0), reps=1)
        bad = make_set(11, 10, datetime(2026, 9, 14, 18, 0), reps=1)
        bad["SERIALIZEDDETAILEDDATA"] = b"not gzip"
        for row in (good, bad):
            row.update(PROTOCOLPARAMETER=1, NOTES=None, RESTTIMER=None, RESTTIMERUSED=None, COMPARISONSET_ID=None)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "export.ndjson.gz")
            counts = exporter.export(FakeConnection([(1, "A", "B", "m", None, None)], [good, bad]), path)
            lines = read(path)
        self.assertEqual(counts, {"athletes": 1, "sets": 1, "skipped": 1})
        self.assertEqual(lines[-1], {"kind": "footer", "athletes": 1, "sets": 1})   # the footer counts what is in the file


if __name__ == "__main__":
    unittest.main()
