"""Stickies you can see on a tiled workspace: front mode (notes above
windows while summoned), a surface on every monitor, empty notes discarded,
and keybinding failures logged instead of swallowed."""
import json
import os
import re
import stat
import subprocess
import sys
import unittest

from helpers import SCRIPT, TempState, stickies
from test_shell import qml_sources


class DiscardEmptyTest(TempState):
    def setUp(self):
        super().setUp()
        self.store = stickies.Store()

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def test_one_note_only_if_empty(self):
        empty = self.store.add("")
        blank = self.store.add("  \n\t ")
        text = self.store.add("keep me")
        self.assertEqual(self.store.discard_empty(text["id"]), [])
        self.assertEqual(self.store.discard_empty(empty["id"]), [empty["id"]])
        self.assertEqual(self.store.discard_empty(blank["id"]), [blank["id"]])
        self.assertEqual([n["id"] for n in self.store.list(all=True)], [text["id"]])
        # gone for good (nothing to undo), and serve sees a purge
        _, events = self.store.changes_since(0)
        kinds = {e["id"]: e["kind"] for e in events}
        self.assertEqual(kinds[empty["id"]], "purge")

    def test_all_skips_archived_and_recent(self):
        old = self.store.add("")
        archived = self.store.add("")
        self.store.archive(archived["id"])
        self.store.db.execute("UPDATE notes SET updated_at='2020-01-01T00:00:00.000Z' WHERE id=?", (old["id"],))
        fresh = self.store.add("")
        self.assertEqual(self.store.discard_empty(older_than=10), [old["id"]])
        live = {n["id"] for n in self.store.list(all=True)}
        self.assertEqual(live, {archived["id"], fresh["id"]})
        self.assertEqual(self.store.discard_empty(), [fresh["id"]])
        with open(os.path.join(self.state, "stickies.log")) as f:
            self.assertIn("discard empty", f.read())

    def test_serve_ops(self):
        n = self.store.add("")
        keep = self.store.add("x")
        self.assertEqual(stickies.dispatch(self.store, "discard", {"id": keep["id"]}),
                         {"id": keep["id"], "discarded": False})
        self.assertEqual(stickies.dispatch(self.store, "discard", {"id": n["id"]}),
                         {"id": n["id"], "discarded": True})
        m = self.store.add("")
        self.assertEqual(stickies.dispatch(self.store, "clean", {"older_than": 60}), {"discarded": []})
        self.assertEqual(stickies.dispatch(self.store, "clean", {}), {"discarded": [m["id"]]})


class CliTest(TempState):
    def cli(self, *args, env=None):
        e = dict(os.environ, STICKIES_HUB="", **(env or {}))
        return subprocess.run([sys.executable, SCRIPT, *args], input="", capture_output=True, text=True, env=e)

    def test_clean(self):
        self.cli("add", "--stdin")  # empty stdin -> empty note
        self.cli("add", "text")
        p = self.cli("clean", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(json.loads(p.stdout)["discarded"]), 1)
        self.assertEqual(self.cli("clean").stdout.strip(), "no empty notes")

    def fake_shell(self, body):
        path = os.path.join(self.state, "omarchy-shell")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        return path

    def log(self):
        try:
            with open(os.path.join(self.state, "stickies.log")) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def test_shell_success_logs_nothing(self):
        fake = self.fake_shell('echo "$@" > "$(dirname "$0")/called"')
        p = self.cli("shell", "newNote", "--json", env={"STICKIES_OMARCHY_SHELL": fake})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {"method": "newNote", "ok": True, "output": None})
        with open(os.path.join(self.state, "called")) as f:
            self.assertEqual(f.read().strip(), "stickies newNote")  # no -q
        self.assertNotIn("shell", self.log())

    def test_shell_failure_is_logged(self):
        fake = self.fake_shell('echo "Target not found." >&2; exit 1')
        p = self.cli("shell", "front", "--json", env={"STICKIES_OMARCHY_SHELL": fake})
        self.assertEqual(p.returncode, 1)
        self.assertIn("Target not found.", json.loads(p.stdout)["error"])
        self.assertRegex(self.log(), r"shell front failed: Target not found\.")

    def test_shell_missing_is_logged(self):
        p = self.cli("shell", "find", env={"STICKIES_OMARCHY_SHELL": os.path.join(self.state, "nope")})
        self.assertEqual(p.returncode, 1)
        self.assertIn("not found", p.stderr)
        self.assertIn("shell find failed", self.log())

    def test_shell_only_known_methods(self):
        self.assertEqual(self.cli("shell", "rm").returncode, 2)


class FrontModeQmlTest(unittest.TestCase):
    """Static checks of the shell plugin; the live behaviour (hyprctl layers
    level, keyboard, click-outside) was measured on a live session."""

    def setUp(self):
        self.src = qml_sources()
        self.svc = self.src["Service.qml"]

    def test_layer_follows_front(self):
        self.assertRegex(self.svc, r"WlrLayershell\.layer: root\.front \|\| root\.peeking \|\| root\.reminding \? WlrLayer\.Top")
        self.assertIn("WlrLayer.Bottom", self.svc)  # the desktop layer otherwise

    def test_focus_grab_ends_front_mode(self):
        grab = re.search(r"HyprlandFocusGrab \{(.*?)\n  \}", self.svc, re.S).group(1)
        self.assertIn("active: root.front", grab)
        self.assertIn("onCleared: root.leaveFront()", grab)
        self.assertIn("d.window", grab)

    def test_input_region_stays_the_notes(self):
        # front mode must not widen the mask: clicks off the notes reach windows
        self.assertIn("mask: Region { regions: desktop.regions }", self.svc)
        self.assertNotRegex(self.svc, r"mask:.*front")

    def test_new_note_and_key_enter_front(self):
        # (in a waterfall layout the new note lands in the column instead)
        self.assertRegex(self.svc, r"function newNote\([^)]*\) \{\s*hidden = false\s*if \(notReady\(\)\) return\s*"
                                   r"(//[^\n]*\s*)*if \(!body && !waterfall\) enterFront\(\)")
        self.assertIn("function front(): void { root.toggleFront() }", self.svc)
        self.assertIn("function onFocusedWorkspaceChanged() { root.leaveFront(true) }", self.svc)
        card = self.src["NoteCard.qml"]
        self.assertRegex(card, r"Keys\.onEscapePressed: \{[^}]*leaveFront\(true\)")

    def test_surface_per_screen_with_remap(self):
        self.assertRegex(self.svc, r"Variants \{\s*model: Quickshell\.screens")
        self.assertIn("ScreenMoveRemap", self.svc)
        self.assertIn("!remapGuard.remapping", self.svc)
        self.assertIn("import qs.Ui", self.svc)

    def test_empty_note_discarded_on_blur(self):
        card = self.src["NoteCard.qml"]
        self.assertIn('styler.plain.trim() === ""', card)
        self.assertIn("discardIfEmpty", card)
        self.assertIn('request("discard"', self.svc)
        self.assertIn('request("clean"', self.svc)


if __name__ == "__main__":
    unittest.main()
