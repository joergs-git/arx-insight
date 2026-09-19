"""v0.4.1 one-click update: what is downloaded is checked before anything is unpacked, nothing can
land outside the target folder, only a NEWER release is accepted - and the route only starts a job."""
import os, shutil, tempfile, time, unittest, zipfile
from pathlib import Path

import tests                                   # noqa: F401  (points ARX_DATA_DIR at a temp folder first)
from tests.test_app_safety import ServerCase, call, HDR, JSON
import arx_app as app
import arx_update as upd

OK = {**HDR, **JSON}


def release(folder: str, version: str = "9.9.9", *, top: str = "arx-insight-main", drop: str | None = None,
            extra: dict | None = None) -> str:
    """Build a ZIP that looks like GitHub's archive of the main branch."""
    path = os.path.join(folder, f"release-{version}-{len(os.listdir(folder))}.zip")
    with zipfile.ZipFile(path, "w") as zf:
        for name in upd.REQUIRED:
            if name != drop:
                zf.writestr(f"{top}/{name}", version if name == "VERSION" else f"# {name}\n")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return path


class Staging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "ARX_UPDATE_URL", None)

    def use(self, zip_path):
        os.environ["ARX_UPDATE_URL"] = Path(zip_path).as_uri()

    def test_a_newer_release_is_unpacked_into_the_data_folder(self):
        self.use(release(self.tmp, "9.9.9"))
        got = upd.stage(self.data, "0.4.0")
        self.assertEqual(got["version"], "9.9.9")
        self.assertEqual(got["folder"], os.path.join(self.data, "app", "arx-insight-9.9.9"))
        for name in upd.REQUIRED:                                       # the top folder of the archive is dropped
            self.assertTrue(os.path.isfile(os.path.join(got["folder"], *name.split("/"))), name)
        self.assertFalse([n for n in os.listdir(os.path.join(self.data, "app")) if n.startswith(".unpack")])

    def test_the_same_or_an_older_version_is_refused_before_unpacking(self):
        self.use(release(self.tmp, "0.4.0"))
        with self.assertRaises(upd.UpdateError) as ctx:
            upd.stage(self.data, "0.4.0")
        self.assertEqual(ctx.exception.code, "not_newer")
        self.assertEqual(os.listdir(os.path.join(self.data, "app")), [])

    def test_archives_that_are_not_a_release_are_refused(self):
        cases = {
            "escape": release(self.tmp, extra={"arx-insight-main/../../evil.txt": "x"}),
            "absolute": release(self.tmp, extra={"/etc/evil": "x"}),
            "two tops": release(self.tmp, extra={"other/readme.txt": "x"}),
            "incomplete": release(self.tmp, drop="windows/install.ps1"),
        }
        for label, path in cases.items():
            self.use(path)
            with self.assertRaises(upd.UpdateError, msg=label) as ctx:
                upd.stage(self.data, "0.4.0")
            self.assertEqual(ctx.exception.code, "bad_archive", label)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))
        not_a_zip = os.path.join(self.tmp, "page.zip")
        Path(not_a_zip).write_text("<html>rate limited</html>")
        self.use(not_a_zip)
        with self.assertRaises(upd.UpdateError) as ctx:
            upd.stage(self.data, "0.4.0")
        self.assertEqual(ctx.exception.code, "bad_archive")

    def test_a_package_the_virus_scanner_took_away_is_named_as_that(self):
        """Windows security answers with its own error codes (225 / 226) or simply removes the file that was just
        written - the owner must read "blocked by security", not "not a ZIP file" (v0.8.3)."""
        self.use(release(self.tmp, "9.9.9"))
        real = zipfile.ZipFile

        def infected(*a, **kw):
            err = OSError("Operation did not complete successfully because the file contains a virus")
            err.winerror = 225
            raise err
        zipfile.ZipFile = infected
        try:
            with self.assertRaises(upd.UpdateError) as ctx:
                upd.stage(self.data, "0.4.0")
        finally:
            zipfile.ZipFile = real
        self.assertEqual(ctx.exception.code, "blocked_by_security")
        self.assertIn("Protection history", ctx.exception.detail)
        self.assertFalse(os.listdir(os.path.join(self.data, "app")))                     # nothing was installed
        gone = os.path.join(self.tmp, "was-here.zip")                                    # quarantined: written, then gone
        self.assertTrue(upd.blocked_by_security(FileNotFoundError(gone), gone))
        self.assertFalse(upd.blocked_by_security(FileNotFoundError("x"), __file__))       # the file is there: another problem
        self.assertFalse(upd.blocked_by_security(ValueError("not a zip"), gone))
        bad = os.path.join(self.tmp, "text.zip")
        Path(bad).write_text("this is not a zip")
        self.use(bad)
        with self.assertRaises(upd.UpdateError) as ctx:
            upd.stage(self.data, "0.4.0")
        self.assertEqual(ctx.exception.code, "bad_archive")                               # an ordinary broken download stays that

    def test_a_missing_download_is_a_clean_error(self):
        os.environ["ARX_UPDATE_URL"] = Path(os.path.join(self.tmp, "nope.zip")).as_uri()
        with self.assertRaises(upd.UpdateError) as ctx:
            upd.stage(self.data, "0.4.0")
        self.assertEqual(ctx.exception.code, "download_failed")

    def test_old_staged_versions_go_but_the_running_one_stays(self):
        root = os.path.join(self.data, "app")
        for v in ("0.4.1", "0.4.2", "0.5.0"):
            os.makedirs(os.path.join(root, f"arx-insight-{v}"))
        self.use(release(self.tmp, "0.6.0"))
        upd.stage(self.data, "0.5.0", running_folder=os.path.join(root, "arx-insight-0.4.1"))
        self.assertEqual(sorted(os.listdir(root)), ["arx-insight-0.4.1", "arx-insight-0.5.0", "arx-insight-0.6.0"])


class UpdateRoute(ServerCase):
    def test_the_route_runs_a_job_and_reports_its_state(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "ARX_UPDATE_URL", None)
        self.addCleanup(os.environ.pop, "ARX_UPDATE_STAGE_ONLY", None)
        os.environ["ARX_UPDATE_URL"] = Path(release(tmp, "9.9.9")).as_uri()
        os.environ["ARX_UPDATE_STAGE_ONLY"] = "1"                        # never run a (fake) installer from a test
        self.assertEqual(call(self.port, "/api/update", method="POST", body={})[0], 403)            # needs the header like every call
        status, body = call(self.port, "/api/update", method="POST", body={}, headers=OK)
        self.assertEqual((status, body["started"]), (200, True))
        for _ in range(50):
            st = call(self.port, "/api/update/status", headers=HDR)[1]
            if st["state"] in ("done", "error"):
                break
            time.sleep(0.1)
        self.assertEqual((st["state"], st["version"]), ("done", "9.9.9"), st)
        self.assertFalse(st["installer"])                                # staged only (see the switch above)
        self.assertTrue(os.path.isfile(os.path.join(st["folder"], "VERSION")))
        shutil.rmtree(os.path.dirname(st["folder"]), ignore_errors=True)

    def test_the_owner_can_look_for_a_new_version_at_once(self):
        """Settings -> "check for updates now": no waiting for the six-hourly check or the banner."""
        asked, real = [], app.check_update
        app.check_update = lambda cur: asked.append(cur) or {"latest": "9.9.9", "update_available": True, "url": app.REPO_URL, "reachable": True}
        try:
            app.STATE["update"] = {}
            code, got = call(self.port, "/api/update/check", method="POST", body={}, headers=OK)
            self.assertEqual((code, got["latest"], got["update_available"], got["version"]), (200, "9.9.9", True, app.STATE["version"]))
            self.assertEqual(asked, [app.STATE["version"]])
            self.assertTrue(app.STATE["update"]["update_available"])                     # the banner everywhere else knows it as well
            self.assertTrue(call(self.port, "/api/bootstrap", headers=HDR)[1]["update"]["update_available"])
            app.check_update = lambda cur: {"latest": cur, "update_available": False, "url": app.REPO_URL, "reachable": False}
            got = call(self.port, "/api/update/check", method="POST", body={}, headers=OK)[1]
            self.assertEqual((got["update_available"], got["reachable"]), (False, False))   # offline is said, not "up to date"
        finally:
            app.check_update = real
            app.STATE["update"] = {}

    def test_bootstrap_says_whether_this_machine_can_update_itself(self):
        boot = call(self.port, "/api/bootstrap")[1]
        self.assertEqual(boot["self_update"], os.name == "nt")


if __name__ == "__main__":
    unittest.main()
