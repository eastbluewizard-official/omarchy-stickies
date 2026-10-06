"""Nothing outside $STICKIES_STATE / $STICKIES_CACHE after using search.

onnxruntime's PyPI builds write a device id and a telemetry queue to
~/.cache/Microsoft/DeveloperTools/.onnxruntime as soon as they are
imported, and upload events, unless ORT_DISABLE_TELEMETRY=1 is set first
(docs/search.md has the measurements). FakeOnnxTest checks stickies sets it
on every process that imports onnxruntime, with a stand-in package that
behaves the same way; RealOnnxTest repeats it with the installed venv and
model when they are present."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

from helpers import SCRIPT, stickies

FAKE_ORT = textwrap.dedent('''\
    # Stand-in for onnxruntime's import-time telemetry set-up (1.30.0).
    import os
    if os.environ.get("ORT_DISABLE_TELEMETRY", "").strip().lower() not in ("1", "true", "yes", "on", "y"):
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
        d = os.path.join(base, "Microsoft", "DeveloperTools", ".onnxruntime")
        os.makedirs(d, exist_ok=True)
        for name in ("deviceid", "onnxruntime.db"):
            open(os.path.join(d, name), "w").close()

    def disable_telemetry_events():
        pass
''')


def fake_venv(root):
    """A 'venv' whose python sees stand-in onnxruntime, numpy, tokenizers
    and sentencepiece packages (the Embedder then fails to build and search
    falls back to words, after onnxruntime was imported)."""
    site = os.path.join(root, "site")
    for mod in stickies.VENV_PACKAGES:
        os.makedirs(os.path.join(site, mod))
        with open(os.path.join(site, mod, "__init__.py"), "w") as f:
            f.write(FAKE_ORT if mod == "onnxruntime" else "")
    os.makedirs(os.path.join(root, "bin"))
    py = os.path.join(root, "bin", "python")
    with open(py, "w") as f:
        f.write(f'#!/bin/sh\nPYTHONPATH="{site}" exec "{sys.executable}" "$@"\n')
    os.chmod(py, 0o755)
    return root


def fake_model(cache, name):
    """The model's files at their pinned sizes (sparse: only sizes are checked
    before loading)."""
    d = os.path.join(cache, "models", name)
    os.makedirs(d)
    for local, (_, size, _) in stickies.MODELS[name]["files"].items():
        with open(os.path.join(d, local), "wb") as f:
            f.truncate(size)


def strays(home, keep):
    """Files and dirs under home, minus the kept trees and their parents."""
    keep = [os.path.abspath(k) for k in keep]
    out = []
    for d, dirs, files in os.walk(home):
        for n in dirs + files:
            p = os.path.join(d, n)
            if not any(p == k or p.startswith(k + os.sep) or k.startswith(p + os.sep) for k in keep):
                out.append(os.path.relpath(p, home))
    return sorted(out)


class Base(unittest.TestCase):
    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="stickies-home-"))
        self.state = os.path.join(self.home, ".local", "state", "stickies")
        self.cache = os.path.join(self.home, ".cache", "stickies")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("STICKIES_", "XDG_", "ORT_"))}
        self.env.update(HOME=self.home, STICKIES_STATE=self.state, STICKIES_CACHE=self.cache,
                        STICKIES_HUB="", STICKIES_NO_LIVE="1")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def cli(self, *args, stdin=None, ok=True):
        p = subprocess.run([sys.executable, SCRIPT, *args], env=self.env, capture_output=True,
                           text=True, input=stdin, timeout=120)
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return p.stdout

    def use_search(self, serve=False):
        self.cli("add", "Tandarts afspraak vrijdag", "--json")
        self.cli("setup", "--status", "--json")
        self.cli("search", "dentist appointment", "--json")
        # The stand-in imports but can't embed: backfill fails after the import.
        self.cli("backfill", "--json", ok=not isinstance(self, FakeOnnxTest))
        if serve:
            out = self.cli("serve", stdin=json.dumps(
                {"id": 1, "op": "search", "args": {"query": "dentist appointment"}}) + "\n")
            self.assertIn('"ok": true', out)

    def assert_nothing_outside(self):
        self.assertEqual(strays(self.home, [self.state, self.cache]), [])


class FakeOnnxTest(Base):
    def setUp(self):
        super().setUp()
        self.venv = fake_venv(tempfile.mkdtemp(prefix="stickies-venv-"))
        self.addCleanup(shutil.rmtree, self.venv, True)
        fake_model(self.cache, stickies.DEFAULT_MODEL)
        self.env["STICKIES_VENV"] = self.venv

    def test_the_stand_in_writes_like_onnxruntime(self):
        # Without the switch the stand-in leaves the files: the test below
        # would catch a process that imports onnxruntime without it.
        subprocess.run([os.path.join(self.venv, "bin", "python"), "-c", "import onnxruntime"],
                       env=self.env, check=True)
        self.assertEqual(strays(self.home, [self.state, self.cache])[-1],
                         ".cache/Microsoft/DeveloperTools/.onnxruntime/onnxruntime.db")

    def test_search_leaves_nothing_outside_state_and_cache(self):
        self.use_search(serve=True)
        self.assert_nothing_outside()

    def test_purge_leaves_a_shared_onnxruntime_dir_and_says_why(self):
        shared = os.path.join(self.home, ".cache", "Microsoft", "DeveloperTools", ".onnxruntime")
        os.makedirs(shared)
        open(os.path.join(shared, "deviceid"), "w").close()
        r = json.loads(self.cli("uninstall", "--purge", "--json"))
        self.assertTrue(os.path.exists(os.path.join(shared, "deviceid")))
        self.assertEqual(r["left"][0]["path"], shared)
        self.assertIn("other programs", r["left"][0]["why"])
        self.assertIn(shared, self.cli("uninstall", "--purge"))


def real_problem():
    try:
        name = stickies.model_name()
    except stickies.StickiesError as e:
        return str(e)
    if stickies.missing_model_files(name):
        return f"embedding model {name} not downloaded to {stickies.cache_dir()} (run `stickies setup`)"
    if not stickies.venv_python():
        return "no venv with onnxruntime (run `stickies setup`, or set STICKIES_VENV)"
    return None


_REAL = real_problem()


@unittest.skipIf(_REAL, _REAL)
class RealOnnxTest(Base):
    """The installed onnxruntime and model, in a temp HOME."""

    def test_search_leaves_nothing_outside_state_and_cache(self):
        name = stickies.model_name()
        os.makedirs(os.path.join(self.cache, "models"))
        os.symlink(stickies.model_dir(name), os.path.join(self.cache, "models", name))
        with open(os.path.join(self.cache, "model"), "w") as f:
            f.write(name + "\n")
        self.env["STICKIES_VENV"] = stickies.venv_dir()
        self.use_search(serve=True)
        hits = json.loads(self.cli("search", "dentist appointment", "--json"))
        self.assertEqual(hits[0]["match"], "semantic")
        self.assert_nothing_outside()


if __name__ == "__main__":
    unittest.main()
