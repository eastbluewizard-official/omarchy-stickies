"""The state is private: dir 0700, db / -wal / -shm / log /
integration.json 0600, under umask 022, and an older 0755 / 0644 state is
repaired on the next open. Nothing that is not ours, or is a symlink, is
chmodded."""
import os
import stat
import subprocess
import sys
import tempfile
import unittest

from helpers import LAUNCHER, TempState, stickies


def mode(p):
    return stat.S_IMODE(os.lstat(p).st_mode)


class PermsTest(TempState):
    def setUp(self):
        super().setUp()
        self._umask = os.umask(0o022)
        self.d = os.path.join(self.state, "s")  # a fresh state dir, made by us
        os.environ["STICKIES_STATE"] = self.d

    def tearDown(self):
        os.umask(self._umask)
        super().tearDown()

    def files(self):
        return [os.path.join(self.d, n) for n in
                ("stickies.db", "stickies.db-wal", "stickies.db-shm", "stickies.log")]

    def test_fresh_state_is_private(self):
        s = stickies.Store(embedder=None)
        s.add("the Cardmarket fees")  # a write: -wal and -shm exist, the log has a line
        stickies.write_integration({"x": 1})
        self.assertEqual(mode(self.d), 0o700)
        for p in self.files() + [stickies.integration_file()]:
            self.assertEqual(mode(p), 0o600, p)
        s.db.close()

    def test_cli_under_umask_022(self):
        env = dict(os.environ)
        subprocess.run([sys.executable, LAUNCHER, "add", "secret"], env=env, check=True,
                       capture_output=True, preexec_fn=lambda: os.umask(0o022))
        self.assertEqual(mode(self.d), 0o700)
        self.assertEqual(mode(os.path.join(self.d, "stickies.db")), 0o600)
        self.assertEqual(mode(os.path.join(self.d, "stickies.log")), 0o600)

    def test_old_state_is_repaired(self):
        s = stickies.Store(embedder=None)
        s.add("one")
        stickies.write_integration({"x": 1})
        # What 1.3.0 left under umask 022 (the -wal / -shm still there: a
        # reader kept them open, or the last writer crashed).
        keep = stickies.Store(embedder=None)
        keep.db.execute("SELECT 1 FROM notes").fetchall()
        paths = self.files() + [stickies.integration_file()]
        for p in paths:
            os.chmod(p, 0o644)
        os.chmod(self.d, 0o755)
        s.db.close()
        s = stickies.Store(embedder=None)
        self.assertEqual(mode(self.d), 0o700)
        for p in paths:
            self.assertEqual(mode(p), 0o600, p)
        s.db.close()
        keep.db.close()

    def test_log_without_a_store_is_repaired(self):
        os.makedirs(self.d)
        log = os.path.join(self.d, "stickies.log")
        open(log, "w").close()
        os.chmod(log, 0o644)
        stickies.Store.log_line("hello")
        self.assertEqual(mode(log), 0o600)

    def test_agent_dir_is_private(self):
        stickies.private_dir(os.path.join(stickies.state_dir(), "agent"))
        self.assertEqual(mode(os.path.join(self.d, "agent")), 0o700)

    def test_symlinked_state_is_not_followed(self):
        real = os.path.join(self.state, "real")
        os.makedirs(real)
        os.chmod(real, 0o755)
        os.symlink(real, self.d)
        stickies.state_dir()
        self.assertEqual(mode(real), 0o755)

    def test_symlinked_file_is_not_followed(self):
        os.makedirs(self.d)
        target = os.path.join(self.state, "elsewhere")
        open(target, "w").close()
        os.chmod(target, 0o644)
        os.symlink(target, os.path.join(self.d, "integration.json"))
        stickies.Store(embedder=None).db.close()
        self.assertEqual(mode(target), 0o644)

    def test_not_ours_is_left_alone(self):
        d = tempfile.gettempdir()  # /tmp: root's, 1777
        if os.stat(d).st_uid == os.getuid():
            self.skipTest(f"{d} is ours here")
        before = mode(d)
        os.environ["STICKIES_STATE"] = d
        self.assertEqual(stickies.state_dir(), d)
        self.assertEqual(mode(d), before)


if __name__ == "__main__":
    unittest.main()
