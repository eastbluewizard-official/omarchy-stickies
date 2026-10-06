"""The search-by-meaning venv is installed from exact, hash-checked pins
(requirements-search.txt), and every place that names them agrees."""
import os
import re
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, SCRIPT, stickies


class LockTest(unittest.TestCase):
    def lines(self):
        with open(stickies.venv_lock_path()) as f:
            return [ln.split("#", 1)[0].strip() for ln in f if ln.split("#", 1)[0].strip()]

    def test_every_package_pinned_with_one_hash(self):
        names = []
        for ln in self.lines():
            m = re.fullmatch(r"([a-z0-9-]+)==([0-9][0-9a-z.]*) --hash=sha256:[0-9a-f]{64}", ln)
            self.assertTrue(m, ln)
            names.append(m.group(1))
        self.assertEqual(names, list(stickies.VENV_PACKAGES))

    def test_license_names_the_pinned_versions(self):
        with open(os.path.join(ROOT, "LICENSE")) as f:
            text = f.read()
        for name, version in stickies.venv_pins():
            self.assertIn(f"{name} {version} (", text)

    def test_setup_plan_shows_the_pins_and_installs_with_hashes(self):
        home = tempfile.mkdtemp(prefix="stickies-home-")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("STICKIES_", "XDG_"))}
        env.update(HOME=home, STICKIES_HUB="")
        p = subprocess.run([sys.executable, SCRIPT, "setup"], env=env, capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        subprocess.run(["rm", "-rf", home])
        self.assertNotEqual(p.returncode, 0)  # no TTY, no --yes: nothing is downloaded
        if stickies.venv_platform_problem():
            self.assertIn("pinned packages", p.stderr)
            return
        for name, version in stickies.venv_pins():
            self.assertIn(f"{name} {version}", p.stderr)
        with open(SCRIPT) as f:
            src = f.read()
        self.assertIn('"--require-hashes", "--only-binary=:all:", "--no-deps"', src)


if __name__ == "__main__":
    unittest.main()
