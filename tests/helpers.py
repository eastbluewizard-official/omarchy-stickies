import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "stickies.py")
LAUNCHER = os.path.join(ROOT, "stickies")
VALIDATE = shutil.which("omarchy-plugin-validate") or "/usr/share/omarchy/bin/omarchy-plugin-validate"

_spec = importlib.util.spec_from_file_location("stickies", SCRIPT)
stickies = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stickies)

# Never part of a release (local state of a checkout, not the plugin).
_LOCAL = {".git", ".venv", "dist", ".claude", "__pycache__"}


def shipped_files():
    """Repo-relative paths of what a release holds: tracked and untracked
    files minus .gitignore'd ones (what tools/export.sh copies, before its
    own exclusions); without git, a walk that skips local-only folders."""
    try:
        out = subprocess.run(["git", "-C", ROOT, "ls-files", "-z", "-co", "--exclude-standard"],
                             capture_output=True, check=True).stdout.decode()
        return sorted(f for f in out.split("\0") if f and os.path.lexists(os.path.join(ROOT, f)))
    except (OSError, subprocess.CalledProcessError):
        files = []
        for d, dirs, names in os.walk(ROOT):
            dirs[:] = [x for x in dirs if x not in _LOCAL]
            files += [os.path.relpath(os.path.join(d, n), ROOT) for n in names]
        return sorted(files)


def plugin_copy(dest):
    """The plugin as `omarchy plugin add` would clone it, copied into dest."""
    for f in shipped_files():
        os.makedirs(os.path.join(dest, os.path.dirname(f)), exist_ok=True)
        shutil.copy2(os.path.join(ROOT, f), os.path.join(dest, f), follow_symlinks=False)
    return dest


def git_checkout(dest, origin=None):
    """The plugin as a git checkout at dest, one commit (what `omarchy plugin
    add` and the git route both leave in the plugins folder); with origin, a
    clone of that checkout instead."""
    if origin:
        subprocess.run(["git", "clone", "-q", "--", origin, dest], check=True)
        return dest
    plugin_copy(dest)
    git = ["git", "-C", dest, "-c", "user.name=test", "-c", "user.email=test@example.invalid",
           "-c", "commit.gpgsign=false"]
    subprocess.run(git + ["init", "-q", "-b", "main"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "plugin"], check=True)
    return dest


def has_symlink(d):
    """The first symlink inside d (skipping .git), as omarchy-plugin-validate
    looks for them, or None."""
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x != ".git"]
        for n in dirs + files:
            if os.path.islink(os.path.join(root, n)):
                return os.path.join(root, n)
    return None


class TempState(unittest.TestCase):
    """Every test gets its own STICKIES_STATE and an empty STICKIES_CACHE
    (no embedding model, so search is FTS-only unless a test injects an
    embedder); the live notes and model cache are never touched."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="stickies-test-")
        self._old = {k: os.environ.get(k) for k in ("STICKIES_STATE", "STICKIES_CACHE")}
        os.environ["STICKIES_STATE"] = self.state
        os.environ["STICKIES_CACHE"] = os.path.join(self.state, "cache")

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.state, ignore_errors=True)
