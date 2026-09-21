#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ARX Insight - shared base: data directory, units, Firebird access (always a read-only COPY),
and a few tiny helpers. Leaf module: it imports nothing from the other arx_* modules, so the
engine (arx_report), the detail metrics (arx_detail) and later modules can all build on it
without import cycles. Everything here was moved verbatim out of arx_report.py (v0.4.0);
arx_report re-exports the names, so existing callers keep working.

Public domain / CC0. No warranty. Not medical advice.
"""

from __future__ import annotations
import os, time, json, shutil, tempfile, threading, contextlib
from datetime import datetime, date


def data_dir() -> str:
    """Stable per-user location for settings, goals and caches.

    Kept OUTSIDE the program folder so re-downloading the app (a fresh ZIP)
    never overwrites the user's settings, goals, venv or Firebird client.
    Override with the ARX_DATA_DIR environment variable (the Windows launcher
    sets it to %LOCALAPPDATA%\\ARXInsight)."""
    d = os.environ.get("ARX_DATA_DIR")
    if not d:
        if os.name == "nt":
            d = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ARXInsight")
        else:
            d = os.path.join(os.path.expanduser("~"), ".arx-insight")
    os.makedirs(d, exist_ok=True)
    return d

# --- unit conversions (ARX stores imperial internally) -----------------------
LB_TO_KG = 0.45359237          # pounds  -> kilograms
IN_TO_CM = 2.54                # inches  -> centimeters


# =============================================================================
# Firebird access
# =============================================================================
def locate_fbclient() -> str | None:
    """Find the Firebird client library.

    Priority: explicit env var, then the library that ships with the ARX app on
    Windows (embedded mode), then a couple of common install locations. On the
    machine running ARX this library is always present, because ARX itself uses
    Firebird embedded.
    """
    env = os.environ.get("ARX_FBCLIENT")
    if env and os.path.exists(env):
        return env
    candidates = [
        # Windows: next to the ARX executable (embedded fbclient)
        r"C:\Program Files\WindowsApps",  # searched below
        r"C:\Program Files\Firebird\Firebird_5_0\fbclient.dll",
        r"C:\Program Files\Firebird\Firebird_4_0\fbclient.dll",
        # macOS / Linux
        "/Library/Frameworks/Firebird.framework/Firebird",
        "/opt/firebird/lib/libfbclient.so",
        "/usr/lib/libfbclient.so",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # Windows: hunt for fbclient.dll inside the installed ARX package
    wa = r"C:\Program Files\WindowsApps"
    if os.path.isdir(wa):
        for root, _dirs, files in os.walk(wa):
            if "Arx" in root and "fbclient.dll" in files:
                return os.path.join(root, "fbclient.dll")
    return None


TEMP_PREFIX = "arx_ro_"         # temp folders holding a COPY of the (private) ARX database
STALE_COPY_SECONDS = 3600       # a copy older than this belongs to a crashed / killed run


def open_readonly(db_path: str):
    """Return (connection, tempdir). Opens a COPY so the original is untouched.

    The copy contains private training data, so it must never be left behind: when the copy or
    the connection fails (missing client library, locked file, ...) the temp folder is removed
    before the error is passed on. Callers remove it after closing the connection."""
    from firebird.driver import connect, driver_config  # imported late on purpose

    lib = locate_fbclient()
    if lib:
        driver_config.fb_client_library.value = lib
    tmp = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    try:
        copy = os.path.join(tmp, "arx_copy.fdb")
        shutil.copy2(db_path, copy)
        con = connect(copy, user="SYSDBA")   # embedded: no password required
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return con, tmp


# --- one shared snapshot for the requests of a few seconds (the app) --------------------------------------
# Every request used to copy the whole database (the report twice, the user search once per
# keystroke). The app now shares ONE copy: it is reused while it is younger than SNAPSHOT_TTL_S and
# the source file looks unchanged. The TTL is deliberately short - NTFS updates a file's mtime
# lazily while the ARX app keeps it open, so "unchanged" alone must never keep a snapshot alive.
# A snapshot is removed as soon as nobody uses it and a newer one exists or its TTL has passed -
# a copy of private training data must not linger.
SNAPSHOT_TTL_S = 30
_SNAP_LOCK = threading.Lock()
_SNAPS: list[dict] = []          # newest last: {src, sig, dir, path, made, users}


def _signature(path: str) -> tuple:
    st_ = os.stat(path)
    return st_.st_mtime_ns, st_.st_size


def _purge_snapshots(everything: bool = False) -> None:
    """Remove snapshots nobody uses: all but the newest, and the newest once its TTL has passed.
    Call with the lock held."""
    now = time.time()
    for snap in list(_SNAPS):
        newest = snap is _SNAPS[-1]
        if snap["users"] <= 0 and (everything or not newest or now - snap["made"] > SNAPSHOT_TTL_S):
            shutil.rmtree(snap["dir"], ignore_errors=True)
            if everything or not os.path.exists(snap["dir"]):
                _SNAPS.remove(snap)                  # else: still locked (Windows) - the next purge tries again
            else:
                snap["made"] = 0.0                   # never reuse it, keep trying to delete it


def _janitor() -> None:
    with _SNAP_LOCK:
        _purge_snapshots()
        if _SNAPS:                                   # still in use or still fresh: look again later
            _arm_janitor()


def _arm_janitor() -> None:
    t = threading.Timer(SNAPSHOT_TTL_S + 5, _janitor)
    t.daemon = True
    t.start()


def drop_snapshots() -> None:
    """Remove every shared snapshot (app shutdown)."""
    with _SNAP_LOCK:
        for snap in _SNAPS:
            snap["users"] = 0
        _purge_snapshots(everything=True)


@contextlib.contextmanager
def shared_connection(db_path: str):
    """A connection to the shared read-only COPY of the database (see above). Use as
    'with shared_connection(path) as con:' - the connection is closed and the snapshot released
    on exit, whatever happens."""
    from firebird.driver import connect, driver_config  # imported late on purpose

    lib = locate_fbclient()
    if lib:
        driver_config.fb_client_library.value = lib
    with _SNAP_LOCK:
        now, sig = time.time(), _signature(db_path)
        snap = _SNAPS[-1] if _SNAPS else None
        if not (snap and snap["src"] == db_path and snap["sig"] == sig and now - snap["made"] <= SNAPSHOT_TTL_S):
            tmp = tempfile.mkdtemp(prefix=TEMP_PREFIX)
            try:
                copy = os.path.join(tmp, "arx_copy.fdb")
                shutil.copy2(db_path, copy)
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            snap = {"src": db_path, "sig": sig, "dir": tmp, "path": copy, "made": now, "users": 0}
            _SNAPS.append(snap)
            if len(_SNAPS) == 1:
                _arm_janitor()
        snap["users"] += 1
    con = None
    try:
        con = connect(snap["path"], user="SYSDBA")   # embedded: no password required
        yield con
    finally:
        try:
            if con is not None:
                con.close()
        finally:
            with _SNAP_LOCK:
                snap["users"] -= 1
                _purge_snapshots()


def write_json_atomic(path: str, data) -> None:
    """Write JSON via a temp file + rename, so a crash or a second writer never leaves half a
    file behind (settings, goals, check-ins and the plan ledger live in such files)."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        for attempt in range(6):                     # Windows refuses the rename while a reader (or a virus
            try:                                     # scanner) has the target open for a moment - wait it out
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def sweep_stale_copies(max_age_s: float = STALE_COPY_SECONDS) -> int:
    """Remove DB copies that an earlier run left behind (crash, killed console window).
    Only folders with our prefix that are older than max_age_s are touched, so a report that is
    being built right now by another process keeps its copy. Returns the number removed."""
    root, removed, now = tempfile.gettempdir(), 0, time.time()
    try:
        names = os.listdir(root)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(root, name)
        try:
            if name.startswith(TEMP_PREFIX) and os.path.isdir(path) and now - os.path.getmtime(path) > max_age_s:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    return removed


def blob_bytes(v) -> bytes:
    """Normalize a Firebird blob value to bytes (text blobs come back as str)."""
    if v is None:
        return b""
    if hasattr(v, "read"):
        v = v.read()
    return v.encode("latin1") if isinstance(v, str) else bytes(v)


def _ts(s: str):
    """ISO timestamp string -> epoch seconds (None if unparsable)."""
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


def _now(cfg: dict) -> datetime:
    """The clock - overridable via cfg['_now'] (ISO datetime) for tests; with a fixed 'today' (cfg / ARX_TODAY)
    it is the end of that day, so an "open session" never depends on the real clock in a replay."""
    t = (cfg or {}).get("_now") or os.environ.get("ARX_NOW")     # ARX_NOW: screenshots of an "open session"
    try:
        if t:
            return datetime.fromisoformat(str(t))
    except Exception:
        pass
    if (cfg or {}).get("_today") or os.environ.get("ARX_TODAY"):
        return datetime.combine(_today(cfg), datetime.max.time().replace(microsecond=0))
    return datetime.now()


def _today(cfg: dict) -> date:
    """Today - overridable via cfg['_today'] or the ARX_TODAY environment
    variable (ISO date) for reproducible tests and screenshots."""
    t = (cfg or {}).get("_today") or os.environ.get("ARX_TODAY")
    try:
        return date.fromisoformat(t) if t else date.today()
    except Exception:
        return date.today()


def _linfit(xs, ys):
    """Least-squares slope/intercept. The slope's unit follows xs: pass the
    occurrence index for 'per training day occurrence', the day offset for 'per
    calendar day' - the two differ by the training frequency (2-3x at 3 sessions
    a week), so never mix them up."""
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs) or 1e-9
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return b, my - b * mx


# --- recovery model constants (shared by arx_report._recovery and the planner) ------------------
# A set loads its target muscles at the effort it reached and its limiters one rank lower; a muscle
# is ready again when the rest its hardest recent load required has elapsed.
EFFORT_RANK = {"deep": 3, "moderate": 2, "submax": 1, "unknown": 2}
RANK_LABEL = {3: "deep", 2: "moderate", 1: "submax"}
# days a muscle needs after a load of that rank (v0.11.0: rest follows the fatigue that was produced - a set below
# the deep line is not failure-like and recovers in about a day; only a deep set keeps its three days, because of
# the eccentric overload; science.json: recovery_between_sessions)
REQUIRED_REST = {3: 3, 2: 1, 1: 1}


def age_band(age: int | None) -> str | None:
    """Name-free age band for guardrails and (if the owner allows it) for the AI."""
    if age is None or age < 0:
        return None
    if age < 16:
        return "13-15"
    if age < 18:
        return "16-17"
    if age < 30:
        return "18-29"
    return "70+" if age >= 70 else f"{age // 10 * 10}-{age // 10 * 10 + 9}"


def user_profile(con, user_id: int, today: date | None = None) -> dict:
    """{sex, age, age_band} of one athlete from the ARX "User" table - never the name. Unknown
    values are None (a birthdate is optional in the ARX app)."""
    out = {"sex": None, "age": None, "age_band": None}
    try:
        cur = con.cursor()
        cur.execute('select gender, birthdate from "User" where id = ?', (user_id,))
        row = cur.fetchone()
    except Exception:
        row = None
    if not row:
        return out
    gender, born = row
    out["sex"] = {"m": "male", "f": "female"}.get((gender or "").strip().lower()[:1])
    if born:
        t = today or date.today()
        b = born.date() if hasattr(born, "date") else born
        try:
            years = t.year - b.year - ((t.month, t.day) < (b.month, b.day))
            if 5 <= years <= 110:                  # the ARX app stores placeholder dates for "unknown"
                out["age"], out["age_band"] = years, age_band(years)
        except Exception:
            pass
    return out
