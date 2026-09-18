"""Exactly ONE app process (v0.3.1), tested with real processes: a newer version replaces a running
older one, and starting an older / equal version while a newer one runs just points to it.
(The Windows-specific part - the exclusive bind - cannot run here; the takeover logic can.)"""
import json, os, shutil, socket, subprocess, sys, tempfile, time, unittest

import tests                                   # noqa: F401
import arx_app as app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def read_instance(folder) -> dict:
    with open(os.path.join(folder, "instance.json"), encoding="utf-8") as f:
        return json.load(f)


def wait_for(cond, seconds=12.0, step=0.2):
    end = time.time() + seconds
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(step)
    return None


class Takeover(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp(prefix="arx_test_inst_")
        self.newer = tempfile.mkdtemp(prefix="arx_test_newer_")          # a "downloaded update"
        modules = [n for n in os.listdir(ROOT) if n.startswith("arx_") and n.endswith(".py")]
        for name in modules + ["exercises.json"]:                        # every engine module ships
            shutil.copy2(os.path.join(ROOT, name), self.newer)
        os.makedirs(os.path.join(self.newer, "web"))
        shutil.copy2(os.path.join(ROOT, "web", "index.html"), os.path.join(self.newer, "web"))
        with open(os.path.join(self.newer, "VERSION"), "w") as f:
            f.write("9.9.9\n")
        self.port = free_port()
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            if p.stdout and not p.stdout.closed:          # already read for the "stray" start
                p.communicate()
            else:
                p.wait()
        shutil.rmtree(self.data, ignore_errors=True)
        shutil.rmtree(self.newer, ignore_errors=True)

    def start(self, folder):
        env = dict(os.environ, ARX_DATA_DIR=self.data, ARX_NO_UPDATE_CHECK="1")
        p = subprocess.Popen([sys.executable, "arx_app.py", "--db", "no-database-needed.fdb4",
                              "--port", str(self.port), "--no-browser"],
                             cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.procs.append(p)
        return p

    def version_on_port(self):
        info = app.probe(self.port, timeout=1.0)
        return info and info.get("version")

    def test_newer_replaces_older_and_older_defers_to_newer(self):
        mine = app.app_version()
        old = self.start(ROOT)
        self.assertEqual(wait_for(self.version_on_port), mine)
        inst = read_instance(self.data)
        self.assertEqual((inst["pid"], inst["port"], inst["version"]), (old.pid, self.port, mine))

        new = self.start(self.newer)                                       # the update is started
        self.assertIsNotNone(wait_for(lambda: old.poll() is not None), "the older instance must quit")
        self.assertEqual(wait_for(lambda: self.version_on_port() == "9.9.9" and "9.9.9"), "9.9.9")
        self.assertIsNone(new.poll())

        stray = self.start(ROOT)                                           # old Desktop shortcut
        self.assertIsNotNone(wait_for(lambda: stray.poll() is not None), "must not run next to the newer one")
        self.assertEqual(stray.returncode, 0)
        self.assertIn("already running", stray.communicate()[0])
        self.assertIsNone(new.poll())                                      # the newer one keeps running
        self.assertEqual(self.version_on_port(), "9.9.9")

        inst = read_instance(self.data)
        self.assertEqual(inst["pid"], new.pid)
        self.assertFalse(app.request_shutdown(self.port, "wrong-secret"))  # 403 -> still running
        self.assertIsNone(new.poll())
        self.assertTrue(app.request_shutdown(self.port, inst["secret"]))
        self.assertIsNotNone(wait_for(lambda: new.poll() is not None))
        inst = read_instance(self.data)
        self.assertIsNone(inst["pid"])                                     # marked as stopped


if __name__ == "__main__":
    unittest.main()
