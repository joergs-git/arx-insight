"""v0.24.0 - where the sets come from: the original's database, arx-free's recordings, or both (arx_sources).

A temp SQLite in arx-free's layout (contract arx-free-sets-1) stands in for the kiosk's file, the FakeDB for the
original. What must hold: no set is read twice, a copy keeps the original's id, a set is judged by the same code
whichever software recorded it, the e-mail decides who is who, an unusable file never breaks a report, a new
recording is new data - and the WAL (where arx-free's newest sets live) is seen."""
import gzip, json, os, sqlite3, tempfile, unittest, uuid
from datetime import datetime

import tests                                   # noqa: F401  (points ARX_DATA_DIR at a temp folder first)
from tests import fixtures as fx
from tests.fixtures import make_set, FakeDB
import arx_sources as sources
import arx_detail as detail
import arx_report as core

SCHEMA = """
CREATE TABLE athletes (id TEXT PRIMARY KEY, first_name TEXT NOT NULL, last_name TEXT NOT NULL, email TEXT, gender TEXT,
  birth_date TEXT, display_units TEXT NOT NULL DEFAULT 'metric', presets_json TEXT NOT NULL DEFAULT '{}', source TEXT,
  source_user_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT, UNIQUE (source, source_user_id));
CREATE TABLE sets (id TEXT PRIMARY KEY, athlete_id TEXT NOT NULL, session_id TEXT, exercise_code INTEGER NOT NULL,
  started_at TEXT NOT NULL, mode TEXT NOT NULL, protocol TEXT NOT NULL, protocol_value REAL, config_json TEXT NOT NULL,
  elapsed_s REAL NOT NULL, reps_completed INTEGER NOT NULL, ended_early INTEGER NOT NULL, counts INTEGER NOT NULL,
  junk INTEGER NOT NULL DEFAULT 0, intensity_lb REAL, output REAL, max_lb REAL, max_c_lb REAL, max_e_lb REAL,
  inroad_c_pct REAL, inroad_e_pct REAL, metrics_algo_version INTEGER NOT NULL, reference_set_id TEXT, rest_s REAL,
  note TEXT, source TEXT, source_set_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT,
  UNIQUE (source, source_set_id));
CREATE TABLE set_curves (set_id TEXT PRIMARY KEY, samples_gz BLOB NOT NULL, events_json TEXT NOT NULL, phases_json TEXT NOT NULL);
PRAGMA user_version = 4;
"""
NOW = "2026-09-23T10:00:00+00:00"


def free_record(row: dict, *, athlete: str, source: str | None = None, protocol: str = "Reps", mode: str = "Automatic") -> dict:
    """An arx-free set record made from the SAME synthetic set a FakeDB row carries: columnar samples and events with
    seconds since BeginSequence, the configuration verbatim, the scalars as they are (contract arx-free-sets-1)."""
    samples = json.loads(gzip.decompress(row["SERIALIZEDDETAILEDDATA"]).decode("utf-16"))
    events = json.loads(row["EVENTSTREAMDATA"].decode("latin1"))
    scheme = json.loads(row["REPSCHEMEDATA"].decode("latin1"))
    t0 = datetime.fromisoformat(events[0]["Time"])                          # BeginSequence
    rel = lambda iso: round((datetime.fromisoformat(iso) - t0).total_seconds(), 3)     # noqa: E731
    columns = {"t": [rel(s["Time"]) for s in samples], "force_lb": [s["Value"] for s in samples],
               "raw_lb": [None] * len(samples), "pos_in": [s["EncoderValue"] for s in samples]}
    return {"id": str(uuid.uuid4()), "athlete_id": athlete, "session_id": str(uuid.uuid4()), "exercise_code": row["EXERCISE"],
            "started_at": row["EXERCISEDATE"].isoformat(timespec="seconds"), "mode": mode, "protocol": protocol, "protocol_value": 8,
            "config_json": json.dumps(scheme), "elapsed_s": row["ELAPSEDSECONDS"], "reps_completed": 8, "ended_early": 0, "counts": 1,
            "junk": int(bool(row["HIDEFROMSTATS"])), "intensity_lb": row["INTENSITY"], "output": None, "max_lb": row["MAXLOAD"],
            "max_c_lb": row["CONCENTRICMAX"], "max_e_lb": row["ECCENTRICMAX"], "inroad_c_pct": None, "inroad_e_pct": None,
            "metrics_algo_version": 1, "reference_set_id": None, "rest_s": None, "note": None, "source": source,
            "source_set_id": str(row["ID"]) if source else None, "created_at": NOW, "updated_at": NOW, "deleted_at": None,
            "_samples": columns, "_events": [{"Time": rel(e["Time"]), "Type": e["Type"], "AdditionalData": None} for e in events]}


class FreeFile:
    """A writer that stays open like the running arx-free app: WAL mode, nothing checkpointed - the newest sets sit
    in the -wal file, which a plain copy of the main file would miss."""

    def __init__(self, folder: str):
        self.path = os.path.join(folder, "arx-free.sqlite")
        self.con = sqlite3.connect(self.path)
        self.con.execute("PRAGMA journal_mode = WAL")
        self.con.executescript(SCHEMA)

    def athlete(self, aid: str, *, source=None, source_user_id=None, email=None) -> None:
        self.con.execute("INSERT INTO athletes (id, first_name, last_name, email, source, source_user_id, created_at, updated_at) "
                         "VALUES (?, 'A', 'B', ?, ?, ?, ?, ?)", (aid, email, source, source_user_id, NOW, NOW))
        self.con.commit()

    def add(self, rec: dict) -> str:
        samples, events = rec.pop("_samples"), rec.pop("_events")
        cols = ", ".join(rec)
        self.con.execute(f"INSERT INTO sets ({cols}) VALUES ({', '.join('?' * len(rec))})", tuple(rec.values()))
        self.con.execute("INSERT INTO set_curves (set_id, samples_gz, events_json, phases_json) VALUES (?, ?, ?, '[]')",
                         (rec["id"], gzip.compress(json.dumps(samples).encode("utf-8")), json.dumps(events)))
        self.con.commit()
        return rec["id"]

    def close(self) -> None:
        self.con.close()


class Sources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(sources.drop_snapshots)
        self.free = FreeFile(self.tmp.name)
        self.addCleanup(self.free.close)
        # the original: two sets of user 1
        self.fb_rows = [make_set(1, 3, datetime(2026, 9, 13, 18, 0)), make_set(2, 3, datetime(2026, 9, 15, 18, 0))]
        self.db = FakeDB(self.fb_rows, {1: ("m", None), 2: ("f", None)})
        # arx-free: a1 = user 1 by the import link, a2 = by e-mail only, a3 = nobody (no source, no e-mail)
        self.free.athlete("a1", source="arx-original", source_user_id="1")
        self.free.athlete("a2", email="Anna@Example.com")
        self.free.athlete("a3")
        self.copy_id = self.free.add(free_record(self.fb_rows[1], athlete="a1", source="arx-original"))          # a copy of set 2
        self.own1 = self.free.add(free_record(make_set(101, 3, datetime(2026, 9, 20, 18, 0)), athlete="a1"))
        self.own2 = self.free.add(free_record(make_set(102, 3, datetime(2026, 9, 21, 18, 0), user=2), athlete="a2"))
        self.own3 = self.free.add(free_record(make_set(103, 3, datetime(2026, 9, 22, 18, 0)), athlete="a3"))

    def cfg(self, mode=None, **extra):
        base = fx.cfg("2026-09-23", **{"arx_free_db": self.free.path, **extra})
        if mode:
            base["sources"] = mode
        return base

    def test_the_default_reads_the_original_only(self):
        con = sources.attach(self.db, self.cfg())
        self.assertIsNone(con.free)
        sets = core.load_sets(con, 1)
        self.assertEqual(([s["id"] for s in sets], {s["source"] for s in sets}), ([1, 2], {"original"}))
        report = core.build_report(con, self.cfg())
        self.assertEqual((report["sources"]["mode"], report["sources"]["arx_free"]["found"], report["sets_total"]), ("original", False, 2))

    def test_both_adds_arx_frees_own_recordings_and_skips_its_copies(self):
        con = sources.attach(self.db, self.cfg("both"))
        sets = core.load_sets(con, 1)
        self.assertEqual([s["id"] for s in sets], [1, 2, self.own1])                     # the copy of set 2 is not a third set
        self.assertEqual([s["source"] for s in sets], ["original", "original", "arx-free"])
        self.assertEqual([s["date"][:10] for s in sets], ["2026-09-13", "2026-09-15", "2026-09-20"])
        self.assertTrue(all(s["working"] for s in sets))
        report = core.build_report(con, self.cfg("both"))
        self.assertEqual((report["sources"]["mode"], report["sources"]["arx_free"]["own_sets"], report["sets_total"]), ("both", 1, 3))
        self.assertEqual(report["last_session"]["date"], "2026-09-20")
        self.assertEqual([x["source"] for x in report["last_session"]["exercises"]], ["arx-free"])
        self.assertEqual(report["sources"]["arx_free"]["unmapped_athletes"], 2)           # a2 (no e-mail here yet) and a3

    def test_arx_free_alone_reads_its_file_with_the_originals_ids_for_copies(self):
        con = sources.attach(self.db, self.cfg("arx-free"))
        sets = core.load_sets(con, 1)
        self.assertEqual([s["id"] for s in sets], [2, self.own1])                         # the copy carries the original's id, set 1 was never copied
        self.assertEqual([s["source"] for s in sets], ["original", "arx-free"])
        self.assertTrue(con.reads_free(2) and con.reads_free(self.own1))
        report = core.build_report(con, self.cfg("arx-free"))
        self.assertEqual((report["sets_total"], report["sources"]["mode"]), (2, "arx-free"))

    def test_a_set_is_judged_the_same_whichever_software_recorded_it(self):
        # the copy of set 2 carries the same samples as the FakeDB row - decoded through the other path
        plain, both = self.db, sources.attach(self.db, self.cfg("arx-free"))
        a, b = detail.load_raw(plain, 2), detail.load_raw(both, 2)
        self.assertEqual(len(a["t"]), len(b["t"]))
        for x, y in zip(a["t"], b["t"]):
            self.assertAlmostEqual(x, y, places=3)
        self.assertEqual(a["f"], b["f"])
        self.assertEqual(a["pos"], b["pos"])
        self.assertEqual([e[1] for e in a["events"]], [e[1] for e in b["events"]])
        for x, y in zip(a["events"], b["events"]):
            self.assertAlmostEqual(x[0], y[0], places=3)
        self.assertEqual((a["scheme"], a["static"]), (b["scheme"], b["static"]))
        da, db_ = detail.set_detail(a, 100.0, 80.0), detail.set_detail(b, 100.0, 80.0)
        self.assertEqual((da["inroad_v3"], da["n_reps"], da["method"]), (db_["inroad_v3"], db_["n_reps"], db_["method"]))
        ca, cb = core.load_curve(plain, 2), core.load_curve(both, 2)
        self.assertEqual(ca[0], cb[0])                                                    # the curve the page draws
        self.assertEqual(core.phase_spans(ca[1], "con"), core.phase_spans(cb[1], "con"))
        self.assertEqual(core.featured_settings(plain, 2, 8), core.featured_settings(both, 2, 8))

    def test_the_email_decides_who_is_who_before_the_import_link(self):
        con = sources.attach(self.db, self.cfg("both"), {2: "anna@example.com"})
        self.assertEqual(con.by_user, {1: ["a1"], 2: ["a2"]})
        self.assertEqual(con.unmapped, 1)                                                 # a3: nothing to go by
        self.assertEqual([s["id"] for s in core.load_sets(con, 2)], [self.own2])
        self.free.athlete("a4", source="arx-original", source_user_id="7", email="ANNA@example.com")   # the address wins over the link (user 7 by the link)
        sources.drop_snapshots()
        con = sources.attach(self.db, self.cfg("both"), {2: "anna@example.com"})
        self.assertEqual(sorted(con.by_user[2]), ["a2", "a4"])
        self.assertEqual((con.by_user[1], con.by_user.get(7)), (["a1"], None))

    def test_an_unusable_file_never_breaks_the_report(self):
        for path, code in ((os.path.join(self.tmp.name, "nowhere.sqlite"), "missing"), (self.free.path + ".txt", "unreadable"),
                           (os.path.join(self.tmp.name, "other.sqlite"), "unknown_layout")):
            if code == "unreadable":
                with open(path, "wb") as fh:
                    fh.write(b"this is not a database at all, just bytes")
            elif code == "unknown_layout":
                other = sqlite3.connect(path)
                other.execute("CREATE TABLE sets (id TEXT)")
                other.commit()
                other.close()
            con = sources.attach(self.db, self.cfg("both", arx_free_db=path))
            self.assertIsNone(con.free)
            self.assertEqual((con.mode, con.wanted, con.note["code"]), ("original", "both", code), code)
            report = core.build_report(con, self.cfg("both", arx_free_db=path))
            self.assertEqual((report["sets_total"], report["sources"]["arx_free"]["note"]["code"]), (2, code), code)
        con = sources.attach(self.db, {"sources": "arx-free", "arx_free_db": ""}, None)       # nothing configured, nothing found
        self.assertIn(con.note["code"], ("missing",)) if con.free is None else self.assertTrue(con.free)   # a dev checkout next door may exist

    def test_a_new_recording_is_new_data_even_before_a_checkpoint(self):
        con = sources.attach(self.db, self.cfg("both"))
        before = con.free.signature(con.free_ids(1), "both")
        self.assertEqual(before[0], "1")
        self.assertTrue(os.path.exists(self.free.path + "-wal"))                          # the writer is open, nothing checkpointed
        self.free.add(free_record(make_set(104, 3, datetime(2026, 9, 23, 8, 0)), athlete="a1"))
        con = sources.attach(self.db, self.cfg("both"))                                   # the file changed: a fresh snapshot
        after = con.free.signature(con.free_ids(1), "both")
        self.assertNotEqual(before, after)
        self.assertEqual(len(core.load_sets(con, 1)), 4)

    def test_the_status_for_the_settings_window(self):
        st = sources.status(self.cfg("both"), {2: "anna@example.com"})
        self.assertEqual((st["mode"], st["found"], st["configured"], st["own_sets"], st["unmapped_athletes"], st["note"]),
                         ("both", True, True, 3, 1, None))
        st = sources.status({"sources": "both", "arx_free_db": os.path.join(self.tmp.name, "gone.sqlite")})
        self.assertEqual((st["found"], st["note"]["code"]), (False, "missing"))
        self.assertEqual(sources.mode_of({"sources": "nonsense"}), "original")

    def test_labels_the_original_never_uses_stay_visible(self):
        rec = free_record(make_set(105, 3, datetime(2026, 9, 22, 19, 0)), athlete="a1", protocol="FatigueTarget")
        fid = self.free.add(rec)
        weird = free_record(make_set(106, 3, datetime(2026, 9, 22, 19, 30)), athlete="a1", protocol="Whatever")
        wid = self.free.add(weird)
        con = sources.attach(self.db, self.cfg("both"))
        by_id = {s["id"]: s for s in core.load_sets(con, 1)}
        self.assertEqual((by_id[fid]["protocol"], by_id[fid]["ending"]), (4, "fatigue"))
        self.assertEqual((by_id[wid]["protocol"], by_id[wid]["ending"]), (None, "unknown"))


if __name__ == "__main__":
    unittest.main()
