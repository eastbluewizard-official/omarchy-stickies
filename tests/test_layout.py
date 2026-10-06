"""Layouts: free / waterfall-right / waterfall-left. The setting through the
CLI and serve (the CLI stays the only writer; serve pushes a settings event
to every surface), the pure-JS column geometry (shell/waterfall.js, under
node), and static checks of the column surface. The live behaviour (both
layers, tiled windows, drags, frame times) is measured on a live
session."""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unittest

from helpers import ROOT, SCRIPT, TempState, stickies
from test_hub import Lines
from test_shell import SHELL, qml_sources

NODE = shutil.which("node")


class SettingsStoreTest(TempState):
    def setUp(self):
        super().setUp()
        self.store = stickies.Store()

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def test_defaults(self):
        self.assertEqual(self.store.settings(), {
            "layout": "free", "waterfall_width": 320, "waterfall_reserve": False,
            "waterfall_collapsed": False, "waterfall_order": [], "waterfall_free": [], "idle_unload": 10})

    def test_round_trip_and_validation(self):
        st = self.store.set_settings({"layout": "waterfall-left", "waterfall_width": 280,
                                      "waterfall_reserve": "on", "waterfall_collapsed": True})
        self.assertEqual((st["layout"], st["waterfall_width"], st["waterfall_reserve"], st["waterfall_collapsed"]),
                         ("waterfall-left", 280, True, True))
        self.store.close()
        self.store = stickies.Store()  # persisted
        self.assertEqual(self.store.settings()["layout"], "waterfall-left")
        for bad in ({"layout": "grid"}, {"waterfall_width": 5}, {"waterfall_width": True},
                    {"waterfall_reserve": "maybe"}, {"nope": 1}, {"waterfall_order": "1,2"}, {}):
            with self.assertRaises(stickies.StickiesError, msg=bad):
                self.store.set_settings(bad)
        self.assertEqual(self.store.settings()["waterfall_width"], 280)  # nothing half-written

    def test_id_lists_keep_live_notes_only(self):
        a, b = self.store.add("a")["id"], self.store.add("b")["id"]
        self.store.archive(b)
        st = self.store.set_settings({"waterfall_order": [b, a, 999, a]})
        self.assertEqual(st["waterfall_order"], [a])

    def test_dock_and_undock(self):
        n = self.store.add("n")["id"]
        self.assertEqual(self.store.dock(n, docked=False)["waterfall_free"], [n])
        self.assertEqual(self.store.dock(n, docked=False)["waterfall_free"], [n])
        self.assertEqual(self.store.dock(n)["waterfall_free"], [])
        with self.assertRaises(stickies.StickiesError):
            self.store.dock(42)

    def test_settings_writes_are_one_event_first(self):
        n = self.store.add("n")["id"]
        seq = self.store.last_seq()
        self.store.set_settings({"layout": "waterfall-right"})
        self.store.edit(n, body="changed")
        self.store.set_settings({"waterfall_collapsed": True})
        self.store.set_settings({"waterfall_collapsed": True})  # no change: no event
        _, events = self.store.changes_since(seq)
        self.assertEqual([e["event"] for e in events], ["settings", "changed"])
        self.assertEqual(events[0]["settings"]["layout"], "waterfall-right")
        self.assertIs(events[0]["settings"]["waterfall_collapsed"], True)
        self.assertEqual(self.store.changes_since(self.store.last_seq())[1], [])

    def test_free_positions_untouched(self):
        n = self.store.add("n", x=111, y=222, w=250, h=180)
        self.store.set_settings({"layout": "waterfall-right", "waterfall_order": [n["id"]]})
        self.store.set_settings({"layout": "free"})
        m = self.store.get(n["id"])
        self.assertEqual((m["x"], m["y"], m["w"], m["h"], m["updated_at"]),
                         (111, 222, 250, 180, n["updated_at"]))

    def test_serve_ops(self):
        n = self.store.add("n")["id"]
        d = lambda op, **a: stickies.dispatch(self.store, op, a)  # noqa: E731
        self.assertEqual(d("settings")["layout"], "free")
        self.assertEqual(d("set", values={"layout": "waterfall-left"})["layout"], "waterfall-left")
        self.assertEqual(d("dock", id=n, docked=False)["waterfall_free"], [n])
        self.assertEqual(d("dock", id=n)["waterfall_free"], [])
        with self.assertRaises(stickies.StickiesError):
            d("set", values=None)

    def test_upgrade_from_schema_2(self):
        self.store.close()
        path = os.path.join(self.state, "stickies.db")
        os.remove(path)
        db = sqlite3.connect(path)  # a v2 database: no settings table
        db.executescript(stickies.SCHEMA.split("-- Desktop settings")[0]
                         + stickies.SCHEMA[stickies.SCHEMA.index("CREATE TRIGGER IF NOT EXISTS notes_ai"):])
        db.execute("PRAGMA user_version=2")
        db.close()
        self.store = stickies.Store()
        self.assertEqual(self.store.set_settings({"layout": "waterfall-right"})["layout"], "waterfall-right")


class LayoutCliTest(TempState):
    def cli(self, *args, ok=True):
        p = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True,
                           env=dict(os.environ, STICKIES_HUB=""))
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return p

    def j(self, *args, ok=True):
        return json.loads(self.cli(*args, "--json", ok=ok).stdout)

    def test_print_set_cycle(self):
        self.assertEqual(self.cli("layout").stdout.strip(), "layout: free")
        self.assertEqual(self.j("layout")["layout"], "free")
        for want in ("waterfall-right", "waterfall-left", "free", "waterfall-right"):
            self.assertEqual(self.j("layout", "next")["layout"], want)
        st = self.j("layout", "waterfall-left", "--width", "300", "--reserve", "on", "--collapsed", "toggle")
        self.assertEqual((st["layout"], st["waterfall_width"], st["waterfall_reserve"], st["waterfall_collapsed"]),
                         ("waterfall-left", 300, True, True))
        self.assertEqual(self.cli("layout").stdout.strip(),
                         "layout: waterfall-left (300 px, reserve on, collapsed)")
        self.assertIs(self.j("layout", "--collapsed", "toggle")["waterfall_collapsed"], False)

    def test_order_dock_undock(self):
        a = self.j("add", "a")["id"]
        b = self.j("add", "b")["id"]
        self.assertEqual(self.j("layout", "--order", f"{b},{a}")["waterfall_order"], [b, a])
        self.assertEqual(self.j("layout", "--undock", str(a))["waterfall_free"], [a])
        self.assertEqual(self.j("layout", "--dock", str(a))["waterfall_free"], [])

    def test_errors_are_json(self):
        p = self.cli("layout", "--width", "5", "--json", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("160-900", json.loads(p.stdout)["error"])
        p = self.cli("layout", "--undock", "77", "--json", ok=False)
        self.assertIn("no note #77", json.loads(p.stdout)["error"])
        self.assertNotEqual(self.cli("layout", "grid", ok=False).returncode, 0)

    def test_shell_methods_for_the_keys(self):
        self.assertIn("cycleLayout", stickies.SHELL_METHODS)
        self.assertIn("collapse", stickies.SHELL_METHODS)


class ServeSettingsEventTest(TempState):
    """A layout change from the CLI (an agent, another terminal) reaches
    every surface as one {"event": "settings"} from serve."""

    def test_cli_write_becomes_a_settings_event(self):
        os.environ["STICKIES_HUB"] = ""
        rfd, wfd = os.pipe()
        out = Lines()

        def run():
            store = stickies.Store()
            try:
                stickies.serve(store, infd=rfd, out=out, poll=0.05, embedder=None)
            finally:
                store.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        try:
            def events(kind):
                return [json.loads(x) for x in list(out.lines) if f'"event": "{kind}"' in x]

            deadline = time.time() + 5
            while not events("ready") and time.time() < deadline:
                time.sleep(0.02)
            subprocess.run([sys.executable, SCRIPT, "layout", "waterfall-left"], check=True,
                           capture_output=True, env=os.environ.copy())
            while not events("settings") and time.time() < deadline:
                time.sleep(0.02)
            ev = events("settings")
            self.assertEqual(len(ev), 1)
            self.assertEqual(ev[0]["settings"]["layout"], "waterfall-left")
            # and the plugin's own write through serve
            os.write(wfd, (json.dumps({"id": 1, "op": "set", "args": {"values": {"waterfall_collapsed": True}}})
                           + "\n").encode())
            while len(events("settings")) < 2 and time.time() < deadline:
                time.sleep(0.02)
            self.assertIs(events("settings")[1]["settings"]["waterfall_collapsed"], True)
            replies = [json.loads(x) for x in list(out.lines) if '"id": 1' in x]
            self.assertTrue(replies and replies[0]["ok"], replies)
        finally:
            os.close(wfd)
            t.join(timeout=5)
            os.close(rfd)


@unittest.skipUnless(NODE, "node not installed")
class WaterfallGeometryTest(unittest.TestCase):
    """shell/waterfall.js, the column's layout maths, run under node."""

    def js(self, expr):
        with open(os.path.join(SHELL, "waterfall.js")) as f:
            src = f.read().replace(".pragma library", "")
        p = subprocess.run([NODE, "-e", src + f"\nconsole.log(JSON.stringify({expr}))"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def test_cycle(self):
        self.assertEqual(self.js('[nextLayout("free"), nextLayout("waterfall-right"), '
                                 'nextLayout("waterfall-left"), nextLayout("bogus")]'),
                         ["waterfall-right", "waterfall-left", "free", "free"])
        self.assertEqual(self.js("LAYOUTS"), list(stickies.LAYOUTS))  # same order as the CLI's `next`

    def test_sort_pinned_then_recent_then_stored_order(self):
        notes = ('[{nid: 1, pinned: false, updated: "2026-10-01"}, {nid: 2, pinned: true, updated: "2026-09-01"},'
                 ' {nid: 3, pinned: false, updated: "2026-10-03"}, {nid: 4, pinned: false, updated: "2026-10-02"},'
                 ' {nid: 5, pinned: true, updated: "2026-10-04"}]')
        ids = lambda order: [n["nid"] for n in self.js(f"sortColumn({notes}, {order})")]  # noqa: E731
        self.assertEqual(ids("[]"), [5, 2, 3, 4, 1])
        # dragged into an order: those keep it, after the notes not in it
        self.assertEqual(ids("[1, 4, 99]"), [5, 2, 3, 1, 4])
        self.assertEqual(ids("[4, 3, 2, 1, 5]"), [4, 3, 2, 1, 5])

    def test_geometry_right_left_collapsed(self):
        g = self.js('geometry({screenW: 1920, screenH: 1080, side: "right", width: 320, top: 26})')
        self.assertEqual(g["column"], {"x": 1920 - 12 - 320, "y": 38, "w": 320, "h": 1080 - 38 - 12})
        self.assertEqual(g["strip"]["x"], 1914)
        self.assertLess(g["handle"]["x"] + g["handle"]["w"], g["column"]["x"])  # on the inner edge
        self.assertGreaterEqual(g["parkX"], 1920)
        self.assertEqual(g["reserve"], 320 + 24 + 12)
        g = self.js('geometry({screenW: 1920, screenH: 1080, side: "left", width: 320, collapsed: true})')
        self.assertEqual((g["column"]["x"], g["strip"]["x"], g["strip"]["w"], g["reserve"]), (12, 0, 6, 6))
        self.assertEqual(g["handle"]["x"], 6)  # beside the strip
        self.assertLessEqual(g["parkX"] + g["column"]["w"], 0)  # off screen

    def test_rotated_monitor(self):
        # a 1920x1080 panel with transform 1: Quickshell's screen is 1080 x 1920
        g = self.js('geometry({screenW: 1080, screenH: 1920, side: "right", width: 320, top: 26})')
        self.assertEqual(g["column"], {"x": 1080 - 12 - 320, "y": 38, "w": 320, "h": 1920 - 50})
        r = self.js('slots([{nid: 1, h: 200}, {nid: 2, h: 3000}], geometry({screenW: 1080, screenH: 1920, '
                    'side: "left", width: 320}))')
        self.assertEqual([s["x"] for s in r["slots"]], [12, 12])
        self.assertEqual(r["slots"][1]["h"], 1920 - 24)  # capped to the column, scrolls inside
        # a narrow screen never gets a column wider than itself
        g = self.js('geometry({screenW: 300, screenH: 800, side: "right", width: 900})')
        self.assertLessEqual(g["column"]["x"] + g["column"]["w"], 300)
        self.assertGreaterEqual(g["column"]["x"], 0)

    def test_slots_own_heights_gap_and_scroll_range(self):
        r = self.js('slots([{nid: 1, h: 200}, {nid: 2, h: 50}, {nid: 3, h: 300}], '
                    'geometry({screenW: 1920, screenH: 500, side: "right", width: 320}))')
        self.assertEqual([(s["nid"], s["y"], s["h"]) for s in r["slots"]], [(1, 0, 200), (2, 210, 110), (3, 330, 300)])
        self.assertEqual(r["contentH"], 630)
        self.assertEqual(r["maxScroll"], 630 - (500 - 24))
        self.assertEqual(r["regionH"], 500 - 24)
        self.assertEqual(self.js("[clampScroll(-5, 630, 476), clampScroll(9999, 630, 476), clampScroll(10, 100, 476)]"),
                         [0, 154, 0])

    def test_collapsed_slots_are_parked(self):
        r = self.js('(function() { var g = geometry({screenW: 1920, screenH: 1080, side: "right", width: 320, '
                    'collapsed: true}); return [g.parkX, slots([{nid: 1, h: 200}], g).slots[0].x] })()')
        self.assertEqual(r[0], r[1])

    def test_drag_gap_drop_index_and_reorder(self):
        notes = "[{nid: 1, h: 200}, {nid: 2, h: 200}, {nid: 3, h: 200}]"
        g = 'geometry({screenW: 1920, screenH: 1080, side: "right", width: 320})'
        r = self.js(f"slots({notes}, {g}, {{nid: 3, index: 0}}).slots.map(s => s.nid)")
        self.assertEqual(r, [3, 1, 2])
        self.assertEqual(self.js(f"[dropIndex({notes}, {g}, 3, -50), dropIndex({notes}, {g}, 3, 150), "
                                 f"dropIndex({notes}, {g}, 1, 5000)]"), [0, 1, 2])
        # other columns' ids stay, this column's follow in the new order
        self.assertEqual(self.js("reorder([9, 1, 2, 8, 3], [1, 2, 3], 3, 0)"), [9, 8, 3, 1, 2])
        self.assertEqual(self.js("reorder([], [1, 2, 3], 1, 99)"), [2, 3, 1])

    def test_dropped_outside_the_column(self):
        g = 'geometry({screenW: 1920, screenH: 1080, side: "right", width: 320})'
        self.assertEqual(self.js(f"[leftColumn({g}, 1700), leftColumn({g}, 1560), leftColumn({g}, 700)]"),
                         [False, False, True])


class WaterfallQmlTest(unittest.TestCase):
    """Static checks; the live behaviour is measured on a live session."""

    def setUp(self):
        self.src = qml_sources()
        self.svc = self.src["Service.qml"]
        self.wf = self.src["Waterfall.qml"]

    def block(self, text, start):
        i = text.index(start)
        depth, j = 0, text.index("{", i)
        while True:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            if depth == 0:
                return text[i:j + 1]
            j += 1

    def test_column_floats_above_windows_and_pushes_nothing(self):
        col = self.block(self.svc, "PanelWindow {\n    id: columnPanel")
        self.assertIn("WlrLayer.Top", col)
        self.assertNotIn("WlrLayer.Bottom", col)
        self.assertIn("exclusionMode: ExclusionMode.Ignore", col)
        self.assertIn("screen: root.columnScreen", col)  # follows the focused monitor
        self.assertIn("mask: Region { regions: waterfallItem.regions }", col)

    def test_input_region_is_only_the_column(self):
        self.assertRegex(self.wf, r"regions: !service\.waterfall \? \[\]\s*: service\.colCollapsed \? "
                                  r"\[stripRegion, handleRegion\]\s*: hasNotes \? \[columnRegion, handleRegion\] : \[\]")

    def test_reserve_is_a_separate_spacer(self):
        sp = self.block(self.svc, "PanelWindow {\n    id: reservePanel")
        self.assertIn("root.colReserve", sp)
        self.assertIn("exclusiveZone: waterfallItem.geo.reserve", sp)
        self.assertIn("mask: Region {}", sp)  # no input at all

    def test_settings_follow_serve_events(self):
        self.assertIn('msg.event === "settings"', self.svc)
        self.assertIn('request("settings"', self.svc)
        self.assertIn('request("set", { values: values })', self.svc)

    def test_keys_and_ipc(self):
        ipc = re.search(r'IpcHandler \{\s*target: "stickies"(.*?)\n  \}', self.svc, re.S).group(1)
        for m in ("cycleLayout", "collapse", "layout"):
            self.assertIn(f"function {m}(", ipc)
        with open(os.path.join(ROOT, "hypr", "stickies.lua")) as f:
            lua = f.read()
        self.assertIn('o.bind("SUPER + ALT + L"', lua)
        self.assertIn('call("cycleLayout")', lua)
        self.assertIn('o.bind("SUPER + ALT + W"', lua)
        self.assertIn('call("collapse")', lua)

    def test_desktop_hides_docked_notes_and_column_never_writes_free_spots(self):
        self.assertIn("desktop.service.docked(nid, workspace, pinned)", self.src["Desktop.qml"])
        # the only geometry write in the column is a drag out (undock)
        self.assertNotIn("moveNote", self.wf)
        self.assertEqual(self.wf.count("service.undock("), 1)

    def test_bar_panel_has_a_picker(self):
        bar = self.src["BarPanel.qml"]
        for name in stickies.LAYOUTS:
            self.assertIn(f'name: "{name}"', bar)
        self.assertIn("root.service.setLayout(modelData.name)", bar)


if __name__ == "__main__":
    unittest.main()
