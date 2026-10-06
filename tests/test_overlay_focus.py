"""Find (SUPER+ALT+J) and Ask (SUPER+ALT+A) open and stay open: the search
and chat overlays keep the keyboard through a spurious focus-out and close
only on Esc, a click outside the card or the key again (OverlayFocus.qml,
overlayfocus.js).

The decisions run under node; the real overlays run in
tests/shell/focus_harness.qml, which maps them on the live compositor for
a few seconds and so only runs with STICKIES_LIVE_TESTS=1."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, SCRIPT
from test_shell import SHELL, qml_sources

NODE = shutil.which("node")
QUICKSHELL = shutil.which("quickshell")
SYSTEM_SHELL = os.path.join(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy", "shell")
LIVE = (os.environ.get("STICKIES_LIVE_TESTS") == "1" and QUICKSHELL
        and os.environ.get("WAYLAND_DISPLAY") and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))


def run_js(body):
    with open(os.path.join(SHELL, "overlayfocus.js")) as f:
        js = f.read().replace(".pragma library", "")
    p = subprocess.run([NODE, "-e", js + "\n" + body], capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError(p.stderr)
    return json.loads(p.stdout)


@unittest.skipUnless(NODE, "node not installed")
class OverlayFocusLogicTest(unittest.TestCase):
    def test_opens_survives_one_spurious_focus_out_closes_on_esc(self):
        out = run_js("""
          var s = create(), log = []
          log.push(toggle(s, 1000))              // SUPER+ALT+J
          log.push(focusLost(s, 1010, true))     // Hyprland: window under the pointer
          log.push(s.opened)
          log.push(focusGained(s, 1080))
          log.push(pressEscape(s))
          log.push(s.opened)
          console.log(JSON.stringify(log))""")
        self.assertEqual(out, ["open", "refocus", True, "ignore", "close", False])

    def test_duplicate_press_ignored_real_second_press_closes(self):
        out = run_js("""
          var s = create()
          console.log(JSON.stringify([toggle(s, 0), toggle(s, 100), s.opened,
                                      toggle(s, REPEAT_MS + 1), s.opened]))""")
        self.assertEqual(out, ["open", "ignore", True, "close", False])

    def test_grab_only_while_mapping(self):
        out = run_js("""
          var s = create()
          open(s, 0)
          var a = grabCleared(s, 50)                 // before the field had focus
          focusGained(s, 60)
          var b = grabCleared(s, 100)                // focused, not settled yet
          var held = wantGrab(s, 60 + SETTLE_MS - 1)
          var dropped = !wantGrab(s, 60 + SETTLE_MS)
          var c = grabCleared(s, 60 + SETTLE_MS)
          console.log(JSON.stringify([a, b, held, dropped, c, s.opened]))""")
        self.assertEqual(out, ["regrab", "regrab", True, True, "ignore", True])

    def test_after_settling_another_surface_is_left_alone(self):
        out = run_js("""
          var s = create()
          open(s, 0)
          var mapping = focusLost(s, 5, true)        // the window under the pointer, during the map
          focusGained(s, 20)
          var later = focusLost(s, 20 + SETTLE_MS, true)    // a menu on top: wait for it
          var inside = focusLost(s, 20 + SETTLE_MS, false)  // our own field: take it back
          console.log(JSON.stringify([mapping, later, inside, s.opened]))""")
        self.assertEqual(out, ["refocus", "wait", "refocus", True])

    def test_focus_loss_never_closes_and_gives_up_after_a_streak(self):
        out = run_js("""
          var s = create(), seen = {}
          open(s, 0)
          for (var i = 0; i < MAX_REFOCUS + 3; i++) seen[focusLost(s, i, true)] = true
          var stillOpen = s.opened
          // the keyboard came back and stayed: a fresh budget
          focusGained(s, 100)
          var after = focusLost(s, 100 + SETTLE_MS, false)
          console.log(JSON.stringify([Object.keys(seen).sort(), stillOpen, after]))""")
        self.assertEqual(out, [["giveup", "refocus"], True, "refocus"])

    def test_click_outside_closes_and_closed_overlay_ignores_focus(self):
        out = run_js("""
          var s = create()
          open(s, 0)
          var a = clickOutside(s)
          console.log(JSON.stringify([a, focusLost(s, 10, true), grabCleared(s, 10), pressEscape(s)]))""")
        self.assertEqual(out, ["close", "ignore", "ignore", "ignore"])


class OverlayWiringTest(unittest.TestCase):
    def setUp(self):
        self.src = qml_sources()

    def test_both_overlays_use_the_keeper(self):
        for name in ("SearchOverlay.qml", "ChatOverlay.qml"):
            ov = self.src[name]
            self.assertIn("OverlayFocus {", ov, name)
            self.assertIn("WlrLayershell.keyboardFocus: keeper.keyboardFocus", ov, name)
            self.assertIn("readonly property bool opened: keeper.opened", ov, name)
            self.assertIn("onClicked: keeper.clickOutside()", ov, name)  # scrim = outside the card
            self.assertRegex(ov, r"Keys\.onEscapePressed: .*keeper\.pressEscape\(\)", name)
            self.assertRegex(ov, r"function toggle\(\) \{[^}]*keeper\.toggle\(\)", name)
            self.assertNotRegex(ov, r"\bopened = ", name)  # only the keeper opens/closes
            # opens where the focus is, whichever monitor (rotated ones too)
            self.assertIn("service.screenWithFocus()", ov, name)
        self.assertRegex(self.src["Service.qml"],
                         r"function screenWithFocus\(\) \{\n    var mon = Hyprland\.focusedMonitor")

    def test_keeper_holds_exclusive_and_a_grab(self):
        k = self.src["OverlayFocus.qml"]
        # Exclusive from the first commit, not switched on at open
        self.assertIn("keyboardFocus: bouncing ? WlrKeyboardFocus.None : WlrKeyboardFocus.Exclusive", k)
        self.assertIn("HyprlandFocusGrab {", k)
        self.assertIn('import "overlayfocus.js" as Focus', k)
        self.assertIn("Window.active", k)

    def test_ipc_methods_toggle(self):
        svc = self.src["Service.qml"]
        self.assertIn("function find(): void { searchOverlay.toggle() }", svc)
        self.assertIn("function chat(): void { chatOverlay.toggle() }", svc)

    def test_binds_call_without_q(self):
        with open(os.path.join(ROOT, "hypr", "stickies.lua")) as f:
            lua = f.read()
        self.assertRegex(lua, r'"SUPER \+ ALT \+ J",.*call\("find"\)')
        self.assertRegex(lua, r'"SUPER \+ ALT \+ A",.*call\("chat"\)')
        self.assertNotIn("omarchy-shell -q", lua)


@unittest.skipUnless(LIVE, "maps the overlays on the live session: set STICKIES_LIVE_TESTS=1 "
                           "(needs quickshell, Wayland and Hyprland)")
class OverlayFocusLiveTest(unittest.TestCase):
    def test_harness(self):
        tmp = tempfile.mkdtemp(prefix="stickies-focus-")
        try:
            state, conf = os.path.join(tmp, "state"), os.path.join(tmp, "shell")
            os.makedirs(state)
            os.makedirs(conf)
            shutil.copy(os.path.join(ROOT, "tests", "shell", "focus_harness.qml"), os.path.join(conf, "shell.qml"))
            for name in ("Commons", "Ui"):
                os.symlink(os.path.join(SYSTEM_SHELL, name), os.path.join(conf, name))
            os.symlink(SHELL, os.path.join(conf, "stickies"))
            env = dict(os.environ, STICKIES_STATE=state, STICKIES_HUB="", STICKIES_SEMANTIC="0",
                       STICKIES_AGENT="cat", STICKIES_CMD=f"{sys.executable} {SCRIPT}")
            p = subprocess.run([QUICKSHELL, "--no-duplicate", "-p", conf], env=env,
                               capture_output=True, text=True, timeout=60)
            lines = [l for l in (p.stdout + p.stderr).splitlines() if "FOCUS {" in l]
            self.assertTrue(lines, (p.stdout + p.stderr)[-3000:])
            result = json.loads(lines[0][lines[0].index("FOCUS {") + 6:])
            sys.stderr.write("\n[focus] " + json.dumps(result) + "\n")
            self.assertNotIn("error", result)
            for name in ("search", "chat"):
                r = result[name]
                self.assertNotIn("error", r, name)
                self.assertTrue(r["duplicate_ignored"], name)
                self.assertTrue(r["survived_map_steal"], name)
                self.assertTrue(r["lost_keyboard"], name)
                self.assertTrue(r["survived_surface_focus_out"], name)
                self.assertTrue(r["survived_field_focus_out"], name)
                self.assertEqual(r["typed"], "abc", name)
                self.assertTrue(r["closed_on_escape"], name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
