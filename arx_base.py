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
import os, time, shutil, tempfile
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
REQUIRED_REST = {3: 3, 2: 2, 1: 1}     # days a muscle needs after a load of that rank


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
