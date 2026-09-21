#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 joergsflow - ARX Insight. Free software under the GNU GPL v3 or later; see LICENSE. No warranty.
# -*- coding: utf-8 -*-
"""
ARX Insight - one-click update (v0.4.1).

The app already tells the athlete when a newer VERSION is on GitHub. This module does what the
README's update procedure asks the user to do by hand - and nothing else:

  1. download the ZIP of the main branch from the project's GitHub page (HTTPS, fixed address),
  2. check it (one top folder, the files an ARX Insight release has, a VERSION newer than the
     running one, no path that leaves the target folder, size limits),
  3. unpack it into <data dir>/app/arx-insight-<version>/ - the data dir survives every update,
  4. on Windows: start that folder's "Install ARX Insight.bat". The installer refreshes the
     packages, points the shortcuts at the new folder and starts the new version, which replaces
     the running one (single-instance takeover, v0.3.1).

Deliberately NOT automatic: code is only ever downloaded after a click in the app on this machine
(the route is loopback-only), never silently and never from a phone. The trust is the same as for
the manual download: GitHub over HTTPS. Older staged versions are removed, the previous one stays
as a fallback. Standard library only. GPL-3.0-or-later (see LICENSE). No warranty.
"""

from __future__ import annotations
import os, re, shutil, subprocess, sys, tempfile, threading, urllib.request, zipfile

ARCHIVE_URL = "https://github.com/joergs-git/arx-insight/archive/refs/heads/main.zip"
MAX_ZIP_BYTES = 60 * 1024 * 1024          # the repository is a few MB; anything huge is not ours
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_FILES = 2000
REQUIRED = ("VERSION", "arx_app.py", "arx_report.py", "web/index.html", "windows/install.ps1",
            "Install ARX Insight.bat", "Start ARX Insight.bat")
KEEP_VERSIONS = 2                          # the new one + the one before it
TIMEOUT_S = 30


# Windows security (Defender or another scanner) judged the download: ERROR_VIRUS_INFECTED / ERROR_VIRUS_DELETED -
# or the file we had just written is gone. Heuristic scanners sometimes misjudge archives with installer scripts;
# the app cannot and does not work around that - it says what happened and where the details are (v0.8.3).
AV_WINERRORS = (225, 226)
BLOCKED_DETAIL = ("Windows security blocked the update package - nothing was installed. "
                  "Windows Security > Protection history names the file and the reason.")


def blocked_by_security(err: BaseException, path: str | None = None) -> bool:
    """True when an error looks like the virus scanner's doing: its own error codes, or a file that
    was written a moment ago and no longer exists."""
    if getattr(err, "winerror", None) in AV_WINERRORS:
        return True
    return isinstance(err, (FileNotFoundError, PermissionError)) and bool(path) and not os.path.exists(path)


class UpdateError(Exception):
    """A failed update with a short machine-readable code for the UI."""
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code, self.detail = code, detail or code


def vparts(v: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", v or "")][:4]


def archive_url() -> str:
    """The fixed GitHub address; ARX_UPDATE_URL overrides it for tests (file:// of a local ZIP)."""
    return os.environ.get("ARX_UPDATE_URL") or ARCHIVE_URL


def download(url: str, dest: str, progress=None) -> int:
    """Stream the archive to dest with a size limit. Returns the number of bytes."""
    total = 0
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ZIP_BYTES:
                    raise UpdateError("too_large", "the download is larger than an ARX Insight release can be")
                f.write(chunk)
                if progress:
                    progress(total)
    except UpdateError:
        raise
    except Exception as e:
        if blocked_by_security(e, dest):
            raise UpdateError("blocked_by_security", BLOCKED_DETAIL)
        raise UpdateError("download_failed", f"{type(e).__name__}: {e}")
    return total


def inspect(zip_path: str, current_version: str) -> tuple[str, str]:
    """(top folder, version) of a release archive - or UpdateError. Nothing is unpacked here."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except Exception as e:
        if blocked_by_security(e, zip_path):
            raise UpdateError("blocked_by_security", BLOCKED_DETAIL)
        raise UpdateError("bad_archive", f"not a ZIP file: {e}")
    with zf:
        infos = zf.infolist()
        if not infos or len(infos) > MAX_FILES:
            raise UpdateError("bad_archive", "unexpected number of files")
        if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
            raise UpdateError("too_large", "the unpacked release would be too large")
        tops = {i.filename.replace("\\", "/").split("/", 1)[0] for i in infos}
        if len(tops) != 1:
            raise UpdateError("bad_archive", "a release has exactly one top folder")
        top = tops.pop()
        for i in infos:
            name = i.filename.replace("\\", "/")
            parts = name.split("/")
            # no absolute paths, no drive letters, no ".." - nothing may land outside the target folder
            if name.startswith("/") or ".." in parts or ":" in name or (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise UpdateError("bad_archive", f"unsafe path in the archive: {name[:80]}")
        names = {i.filename.replace("\\", "/") for i in infos}
        missing = [r for r in REQUIRED if f"{top}/{r}" not in names]
        if missing:
            raise UpdateError("bad_archive", f"not an ARX Insight release (missing {missing[0]})")
        version = zf.read(f"{top}/VERSION").decode("utf-8", "replace").strip()
    if not vparts(version):
        raise UpdateError("bad_archive", "the release has no readable VERSION")
    if vparts(version) <= vparts(current_version):
        raise UpdateError("not_newer", f"the download is v{version}, running is v{current_version}")
    return top, version


def unpack(zip_path: str, top: str, target: str) -> None:
    """Unpack the release so that its files sit directly in target (the top folder is dropped).
    Unpacked next to the target first and renamed at the end, so a half-unpacked folder never
    carries the final name."""
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".unpack-", dir=parent)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            root = os.path.realpath(tmp)
            for i in zf.infolist():
                rel = i.filename.replace("\\", "/")[len(top) + 1:]
                if not rel:
                    continue
                dest = os.path.realpath(os.path.join(tmp, *rel.split("/")))
                if not (dest == root or dest.startswith(root + os.sep)):
                    raise UpdateError("bad_archive", "a file would leave the target folder")
                if rel.endswith("/"):
                    os.makedirs(dest, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(i) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
        if os.path.exists(target):
            shutil.rmtree(target, ignore_errors=True)
        os.replace(tmp, target)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def prune(app_root: str, keep: list[str]) -> None:
    """Remove older staged versions (only folders this module created, never one in 'keep')."""
    try:
        names = sorted((n for n in os.listdir(app_root) if n.startswith("arx-insight-")), key=lambda n: vparts(n), reverse=True)
    except OSError:
        return
    keep_real = {os.path.realpath(k) for k in keep if k}
    for n in names[KEEP_VERSIONS:]:
        path = os.path.join(app_root, n)
        if os.path.realpath(path) not in keep_real:
            shutil.rmtree(path, ignore_errors=True)


def stage(data_dir: str, current_version: str, running_folder: str | None = None, progress=None) -> dict:
    """Download, check and unpack the newest release. -> {folder, version}"""
    app_root = os.path.join(data_dir, "app")
    os.makedirs(app_root, exist_ok=True)
    fd, zip_path = tempfile.mkstemp(prefix="arx-update-", suffix=".zip")
    os.close(fd)
    try:
        if progress:
            progress("downloading", 0)
        download(archive_url(), zip_path, (lambda n: progress("downloading", n)) if progress else None)
        if progress:
            progress("checking", 0)
        top, version = inspect(zip_path, current_version)
        target = os.path.join(app_root, f"arx-insight-{version}")
        if progress:
            progress("unpacking", 0)
        unpack(zip_path, top, target)
    except OSError as e:                          # reading the archive or writing a file of it was stopped by the scanner
        if blocked_by_security(e, zip_path):
            raise UpdateError("blocked_by_security", BLOCKED_DETAIL)
        raise
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass
    prune(app_root, [target, running_folder])
    return {"folder": target, "version": version}


def launch_installer(folder: str) -> bool:
    """Windows: run the new version's installer in its own console window (it refreshes packages,
    re-points the shortcuts and starts the new app, which replaces this one). Elsewhere there is
    no installer - the caller tells the user where the new version lies."""
    if os.name != "nt" or os.environ.get("ARX_UPDATE_STAGE_ONLY"):     # (the switch is for tests)
        return False
    bat = os.path.join(folder, "Install ARX Insight.bat")
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    # the page that started the update reloads itself once the new version answers - no second browser tab
    env = dict(os.environ, ARX_NO_BROWSER="1")
    subprocess.Popen(["cmd.exe", "/c", bat], cwd=folder, creationflags=flags, close_fds=True, env=env)
    return True


class UpdateJob:
    """One update at a time, run in a background thread; the UI polls status()."""
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {"state": "idle", "detail": "", "version": None, "bytes": 0, "folder": None, "installer": False}

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **kw):
        with self._lock:
            self._state.update(kw)

    def start(self, data_dir: str, current_version: str, running_folder: str | None) -> bool:
        with self._lock:
            if self._state["state"] in ("downloading", "checking", "unpacking", "installing"):
                return False
            self._state.update(state="downloading", detail="", version=None, bytes=0, folder=None, installer=False)
        threading.Thread(target=self._run, args=(data_dir, current_version, running_folder), daemon=True).start()
        return True

    def _run(self, data_dir, current_version, running_folder):
        try:
            got = stage(data_dir, current_version, running_folder, lambda st, n: self._set(state=st, bytes=n))
            self._set(state="installing", version=got["version"], folder=got["folder"])
            started = launch_installer(got["folder"])
            # on Windows the installer starts the new version, which asks this process to quit
            self._set(state="done", installer=started)
        except UpdateError as e:
            self._set(state="error", detail=f"{e.code}: {e.detail}"[:300])
        except Exception as e:                      # never take the app down because an update failed
            self._set(state="error", detail=f"unexpected: {type(e).__name__}: {e}"[:300])


if __name__ == "__main__":                           # manual use: python arx_update.py <data dir> <current version>
    info = stage(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "0.0.0")
    print(f"staged v{info['version']} in {info['folder']}")
