"""The hub card: what `hub module set` gets, and when it is called. A fake
`hub` script records its argv; the real hub is never called."""

import json
import os
import subprocess
import sys
import threading
import time

from helpers import SCRIPT, TempState, stickies


class HubTest(TempState):
    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.state, "hub-calls.jsonl")
        self.fake = os.path.join(self.state, "fake-hub")
        with open(self.fake, "w") as f:
            f.write(f"#!{sys.executable}\nimport json, sys\n"
                    f"open({self.log!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
        os.chmod(self.fake, 0o755)
        self._hub = os.environ.pop("STICKIES_HUB", None)

    def tearDown(self):
        os.environ.pop("STICKIES_HUB", None)
        if self._hub is not None:
            os.environ["STICKIES_HUB"] = self._hub
        super().tearDown()

    def calls(self, wait_for=0, timeout=5.0):
        end = time.monotonic() + timeout
        while True:
            out = []
            if os.path.exists(self.log):
                with open(self.log) as f:
                    out = [json.loads(line) for line in f if line.strip()]
            if len(out) >= wait_for or time.monotonic() > end:
                return out
            time.sleep(0.05)

    def cli(self, *args, env=None):
        p = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True,
                           env=dict(os.environ, **(env or {})))
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return p.stdout

    @staticmethod
    def stats(argv):
        return [(argv[i + 1], argv[i + 2]) for i, a in enumerate(argv) if a == "--stat"]

    def test_summary_counts_live_notes_only(self):
        store = stickies.Store()
        a = store.add("Cardmarket fees went up\nsecond line", pinned=True)
        store.add("b")
        c = store.add("archived")
        store.archive(c["id"])
        time.sleep(0.01)  # updated_at has millisecond resolution
        store.edit(a["id"], body="Cardmarket fees went up again")
        s = stickies.hub_summary(store)
        store.close()
        self.assertEqual((s["count"], s["pinned"], s["last_edited_id"]), (2, 1, a["id"]))
        self.assertNotIn("Cardmarket", json.dumps(s))  # counts and when, no note text

    def test_card_holds_no_note_text(self):
        store = stickies.Store()
        n = store.add("Cardmarket fees went up")
        args = stickies.hub_args(stickies.hub_summary(store))
        store.close()
        self.assertNotIn("Cardmarket", " ".join(args))
        self.assertRegex(args[args.index("--line", args.index("--line") + 1) + 1],
                         rf"^last edited \d\d:\d\d \(#{n['id']}\)$")

    def test_card_shape(self):
        store = stickies.Store()
        store.add("one", pinned=True)
        args = stickies.hub_args(stickies.hub_summary(store))
        store.close()
        self.assertEqual(args[:4], ["module", "set", "--name", "stickies"])
        self.assertEqual(args[args.index("--launch") + 1], "omarchy-shell -q stickies find")
        stats = self.stats(args)
        self.assertEqual([label for _, label in stats], ["notes", "pinned", "last edited"])
        self.assertEqual(stats[0][0], "1")
        self.assertRegex(stats[2][0], r"^\d\d:\d\d$")  # edited today -> local HH:MM

    def test_empty_store(self):
        store = stickies.Store()
        args = stickies.hub_args(stickies.hub_summary(store))
        store.close()
        self.assertEqual(self.stats(args), [("0", "notes"), ("0", "pinned"), ("-", "last edited")])

    def test_local_when(self):
        self.assertEqual(stickies._local_when("2020-01-02T10:00:00.000Z")[:4], "2020")
        self.assertIsNone(stickies._local_when("nonsense"))

    def test_auto_publish_off_for_temp_state_unless_asked(self):
        self.assertIsNone(stickies.hub_bin())
        self.assertEqual(stickies.hub_bin(explicit=True), "hub")
        os.environ["STICKIES_HUB"] = ""
        self.assertIsNone(stickies.hub_bin(explicit=True))
        os.environ["STICKIES_HUB"] = self.fake
        self.assertEqual(stickies.hub_bin(), self.fake)
        state = os.environ.pop("STICKIES_STATE")
        try:
            os.environ.pop("STICKIES_HUB")
            self.assertEqual(stickies.hub_bin(), "hub")  # the live notes publish
        finally:
            os.environ["STICKIES_STATE"] = state

    def test_cli_hub_command(self):
        self.cli("add", "x", "--pin")
        out = json.loads(self.cli("hub", "--json", env={"STICKIES_HUB": self.fake}))
        self.assertTrue(out["published"])
        self.assertEqual((out["count"], out["pinned"]), (1, 1))
        self.assertEqual(self.calls(1)[-1][:4], ["module", "set", "--name", "stickies"])

        dry = json.loads(self.cli("hub", "--dry-run", "--json", env={"STICKIES_HUB": self.fake}))
        self.assertFalse(dry["published"])
        self.assertEqual(dry["command"][0], "hub")
        self.assertEqual(len(self.calls()), 1)

    def test_cli_hub_reports_failure(self):
        p = subprocess.run([sys.executable, SCRIPT, "hub", "--json"], capture_output=True, text=True,
                           env=dict(os.environ, STICKIES_HUB="/nonexistent/hub"))
        self.assertEqual(p.returncode, 1)
        self.assertIn("error", json.loads(p.stdout))

    def test_cli_writes_publish_in_background(self):
        self.cli("add", "x")  # temp state, no STICKIES_HUB: nothing
        time.sleep(0.3)
        self.assertEqual(self.calls(), [])
        env = {"STICKIES_HUB": self.fake}
        self.cli("add", "y", "--pin", env=env)
        calls = self.calls(1)
        self.assertEqual(self.stats(calls[-1])[:2], [("2", "notes"), ("1", "pinned")])
        self.cli("list", env=env)  # reads don't publish
        self.cli("rm", "1", env=env)
        self.assertEqual(len(self.calls(2)), 2)
        time.sleep(0.3)
        self.assertEqual(len(self.calls()), 2)

    def test_serve_republishes_after_changes(self):
        os.environ["STICKIES_HUB"] = self.fake
        rfd, wfd = os.pipe()

        def run():  # sqlite connections stay on the thread that made them
            store = stickies.Store()
            try:
                stickies.serve(store, infd=rfd, out=Lines(), poll=0.05, hub_delay=0.2)
            finally:
                store.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        try:
            self.assertEqual(len(self.calls(1)), 1)  # once at start
            for i in range(3):  # a burst coalesces into one publish
                os.write(wfd, (json.dumps({"id": i, "op": "add", "args": {"body": f"n{i}"}}) + "\n").encode())
            calls = self.calls(2)
            self.assertEqual(self.stats(calls[-1])[0], ("3", "notes"))
            time.sleep(0.5)
            self.assertEqual(len(self.calls()), 2)
            # a write from another process (an agent) is picked up too
            self.cli("add", "from an agent", env={"STICKIES_HUB": ""})
            self.assertEqual(self.stats(self.calls(3)[-1])[0], ("4", "notes"))
        finally:
            os.close(wfd)
            t.join(timeout=5)
            os.close(rfd)


class Lines:
    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines.append(s)

    def flush(self):
        pass
