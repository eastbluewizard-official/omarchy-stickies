"""After an update the running shell keeps the QML it loaded (its reload
does not clear Quickshell's component cache), while `serve` restarts from
the new stickies.py. serve notices and says so once; a key that hits
"Function not found" says how to fix it. `stickies --version` reads
manifest.json."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, SCRIPT, TempState, plugin_copy, stickies


class LoadedCodeTest(TempState):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="stickies-upd-")
        self.sent = os.path.join(self.tmp, "sent.log")
        notify = os.path.join(self.tmp, "notify-send")
        with open(notify, "w") as f:
            f.write(f'#!/bin/sh\ntr "\\n" " " >> "{self.sent}"; echo >> "{self.sent}"\n')
        os.chmod(notify, 0o755)
        self.plugin = plugin_copy(os.path.join(self.tmp, "plugin"))
        self._old_env = {k: os.environ.get(k) for k in ("STICKIES_NOTIFY", "STICKIES_SHELL_ID")}
        os.environ["STICKIES_NOTIFY"] = notify
        self._here = stickies.here_dir
        stickies.here_dir = lambda: self.plugin

    def tearDown(self):
        stickies.here_dir = self._here
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def notifications(self):
        try:
            with open(self.sent) as f:
                return f.read().splitlines()
        except OSError:
            return []

    def test_outside_a_shell_nothing_is_checked(self):
        os.environ.pop("STICKIES_SHELL_ID", None)
        self.assertIsNone(stickies.check_loaded_code())

    def test_new_code_under_the_same_shell_is_reported_once(self):
        os.environ["STICKIES_SHELL_ID"] = "100:1"
        self.assertFalse(stickies.check_loaded_code()["stale"])  # the shell loaded this
        self.assertFalse(stickies.check_loaded_code()["stale"])  # a reload, same code
        with open(os.path.join(self.plugin, "Service.qml"), "a") as f:
            f.write("// an update\n")
        self.assertTrue(stickies.check_loaded_code()["stale"])
        self.assertTrue(stickies.check_loaded_code()["stale"])
        sent = self.notifications()
        self.assertEqual(len(sent), 1, sent)  # once per version
        self.assertIn("omarchy-restart-shell", sent[0])
        # a restarted shell loads it: fine again, and it is the new baseline
        os.environ["STICKIES_SHELL_ID"] = "200:5"
        self.assertFalse(stickies.check_loaded_code()["stale"])
        self.assertFalse(stickies.check_loaded_code()["stale"])
        self.assertEqual(len(self.notifications()), 1)

    def test_python_only_changes_need_no_restart(self):
        os.environ["STICKIES_SHELL_ID"] = "100:1"
        stickies.check_loaded_code()
        with open(os.path.join(self.plugin, "stickies.py"), "a") as f:
            f.write("# serve restarts with the shell's reload and runs this\n")
        self.assertFalse(stickies.check_loaded_code()["stale"])

    def test_function_not_found_names_the_fix(self):
        fake = os.path.join(self.tmp, "omarchy-shell")
        with open(fake, "w") as f:
            f.write('#!/bin/sh\necho "Function not found." >&2\nexit 1\n')
        os.chmod(fake, 0o755)
        env = dict(os.environ, STICKIES_OMARCHY_SHELL=fake)
        p = subprocess.run([sys.executable, SCRIPT, "shell", "list"], env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("omarchy-restart-shell", p.stderr)
        self.assertEqual(len(self.notifications()), 1)  # a key's failure is otherwise invisible


class VersionTest(unittest.TestCase):
    def test_version_comes_from_the_manifest(self):
        with open(os.path.join(ROOT, "manifest.json")) as f:
            v = json.load(f)["version"]
        out = subprocess.run([sys.executable, SCRIPT, "--version"], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout, f"stickies {v} ({ROOT})\n")
        out = subprocess.run([sys.executable, SCRIPT, "--json", "--version"], capture_output=True, text=True,
                             check=True)
        self.assertEqual(json.loads(out.stdout), {"version": v, "plugin_dir": ROOT})


if __name__ == "__main__":
    unittest.main()
