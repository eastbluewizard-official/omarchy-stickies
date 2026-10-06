"""The polish pass: checklists, undo, theme colours (test_shell), roll-up,
tidy, quick capture from the clipboard, reminders, and plain-word errors.
The live behaviour of each (and its cost) is measured on a live session."""

import io
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
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

from helpers import SCRIPT, TempState, stickies
from test_shell import qml_sources

NODE = shutil.which("node")


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Base(TempState):
    def setUp(self):
        super().setUp()
        self.s = stickies.Store()

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def script(self, name, body):
        path = os.path.join(self.state, name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def cli(self, *args, env=None):
        e = dict(os.environ, STICKIES_HUB="", **(env or {}))
        return subprocess.run([sys.executable, SCRIPT, *args], input="", capture_output=True, text=True, env=e)


# -- 1. checklists -------------------------------------------------------------

class ChecklistTest(unittest.TestCase):
    """Checkboxes are drawn by the card over the note's own "[ ]" / "[x]"
    text; a click flips that character, so the CLI and search see it too."""

    def setUp(self):
        self.card = qml_sources()["NoteCard.qml"]

    def test_toggle_edits_the_text_and_saves(self):
        fn = re.search(r"function toggleCheck\(pos\) \{(.*?)\n  \}", self.card, re.S).group(1)
        self.assertIn("editor.remove(pos + 1, pos + 2)", fn)
        self.assertIn("editor.insert(pos + 1, mark)", fn)
        self.assertIn("flush()", fn)
        self.assertIn('card.checkDone + "/" + card.checks.length', self.card)

    @unittest.skipUnless(NODE, "node not installed")
    def test_checklist_lines(self):
        regex = re.search(r"var re = (/.*?/gm)", self.card).group(1)
        body = ("Shopping\n[ ] milk\n[x] bread\n- [ ] cheese\n  * [X] coffee\n+ [ ] tea\n"
                "not [ ] mid-line\n[] no space\n-[ ] no gap\n[y] other\n")
        script = f"""const re = {regex}; const t = {json.dumps(body)}; const out = []; let m;
while ((m = re.exec(t)) !== null) out.push([m.index + m[1].length, m[2] !== " "]);
console.log(JSON.stringify(out));"""
        p = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        got = [(body[pos:pos + 3], done) for pos, done in json.loads(p.stdout)]
        self.assertEqual(got, [("[ ]", False), ("[x]", True), ("[ ]", False), ("[X]", True), ("[ ]", False)])

    def test_progress_and_roll_title_skip_boxes(self):
        self.assertIn(r'replace(/^[ \t]*(?:[-*+][ \t]+)?\[[ xX]\][ \t]*/, "")', self.card)


# -- 2. undo -------------------------------------------------------------------

class UndoTest(Base):
    def test_undo_restores_the_newest_archive_then_the_one_before(self):
        a, b, c = (self.s.add(t) for t in "abc")
        self.s.archive(a["id"])
        self.s.archive(c["id"])
        self.assertEqual(self.s.undo_archive()["id"], c["id"])
        self.assertEqual(self.s.undo_archive()["id"], a["id"])
        with self.assertRaisesRegex(stickies.StickiesError, "nothing to undo"):
            self.s.undo_archive()
        self.assertEqual(len(self.s.list()), 3)

    def test_cli_and_serve(self):
        n = self.s.add("keep me")
        self.s.archive(n["id"])
        p = self.cli("undo", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((json.loads(p.stdout)["id"], json.loads(p.stdout)["archived_at"]), (n["id"], None))
        p = self.cli("undo")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(p.stderr.strip(), "stickies: nothing to undo: no archived notes")
        self.s.archive(n["id"])
        self.assertEqual(stickies.dispatch(self.s, "undo", {})["id"], n["id"])

    def test_desktop_toast_uses_rm_and_restore(self):
        svc = qml_sources()["Service.qml"]
        self.assertIn('say("Archived", "archive", nid, screenName)', svc)
        self.assertIn('request("rm", { id: nid }', svc)
        self.assertIn('request("restore", { id: nid }', svc)
        desk = qml_sources()["Desktop.qml"]
        self.assertIn("interval: 6000", desk)
        self.assertIn("desktop.service.undoNotice()", desk)


# -- 4. roll-up ----------------------------------------------------------------

class RollTest(Base):
    def test_roll_is_stored_per_note(self):
        a, b = self.s.add("a"), self.s.add("b")
        self.assertIs(a["rolled"], False)
        self.assertIs(self.s.roll(a["id"])["rolled"], True)
        self.assertIs(self.s.get(b["id"])["rolled"], False)
        self.assertIs(self.s.roll(a["id"], False)["rolled"], False)
        self.assertIs(stickies.dispatch(self.s, "roll", {"id": a["id"], "rolled": True})["rolled"], True)

    def test_cli(self):
        n = self.s.add("Groceries\nmilk")
        self.assertIs(json.loads(self.cli("roll", str(n["id"]), "--json").stdout)["rolled"], True)
        self.assertEqual(self.cli("roll", str(n["id"]), "--off").stdout.strip(), f"#{n['id']} unrolled")

    def test_card_double_click_and_height(self):
        card = qml_sources()["NoteCard.qml"]
        self.assertIn("onDoubleClicked: card.toggleRoll()", card)
        self.assertIn("height: resizing ? liveH : rolled ? headerHeight : h", card)
        self.assertIn("height: card.visible ? (card.rolled ? card.headerHeight : card.h) : 0", card)  # input region

    def test_old_database_gains_the_new_columns(self):
        self.s.close()
        db = sqlite3.connect(os.path.join(self.state, "stickies.db"))
        db.executescript("DROP TABLE notes; DROP TABLE reminders; DROP TABLE meta;")
        db.execute("""CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL DEFAULT '',
            color TEXT NOT NULL DEFAULT 'yellow', pinned INTEGER NOT NULL DEFAULT 0, workspace INTEGER,
            monitor TEXT, x INTEGER NOT NULL DEFAULT 0, y INTEGER NOT NULL DEFAULT 0,
            w INTEGER NOT NULL DEFAULT 240, h INTEGER NOT NULL DEFAULT 200, z INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT)""")
        when = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
        db.execute("INSERT INTO notes(body, created_at, updated_at) VALUES (?, 'x', 'x')",
                   (f"old note\n@ {when} 10:00",))
        db.execute("PRAGMA user_version=3")  # main's schema before this pass
        db.commit()
        db.close()
        self.s = stickies.Store()
        n = self.s.get(1)
        self.assertEqual(tuple(n), stickies.COLUMNS)
        self.assertIs(n["rolled"], False)
        self.assertIsNotNone(n["remind_at"])  # existing reminder lines are picked up


# -- 5. tidy -------------------------------------------------------------------

class TidyTest(Base):
    def test_grid_with_gaps_inside_the_reserved_area(self):
        a = self.s.add("a", workspace=2, monitor="eDP-1", x=900, y=500, w=240, h=200)
        b = self.s.add("b", workspace=2, monitor="eDP-1", x=100, y=100, w=300, h=150)
        c = self.s.add("c", workspace=None, monitor=None, x=50, y=700, w=240, h=200)
        d = self.s.add("d", workspace=2, monitor="eDP-1", x=400, y=900, w=240, h=200)
        self.s.roll(d["id"])
        pinned = self.s.add("p", workspace=2, monitor="eDP-1", pinned=True, x=1, y=1)
        other_ws = self.s.add("o", workspace=3, monitor="eDP-1", x=2, y=2)
        other_mon = self.s.add("m", workspace=2, monitor="HDMI-A-1", x=3, y=3)
        gone = self.s.add("g", workspace=2, monitor="eDP-1", x=4, y=4)
        self.s.archive(gone["id"])
        # 820 wide: b (300) + a (240) fit in one row with gaps, then wrap.
        r = self.s.tidy(2, "eDP-1", 820, 1080, gaps=(10, 12, 10, 12), reserved=(0, 26, 0, 0))
        pos = {n["id"]: (n["x"], n["y"]) for n in r["moved"]}
        # reading order (y, x): b, a, c, d
        self.assertEqual(pos, {b["id"]: (12, 36), a["id"]: (324, 36),
                               c["id"]: (12, 36 + 200 + 10), d["id"]: (264, 246)})
        self.assertTrue(r["undo"])
        for n in (pinned, other_ws, other_mon, gone):
            self.assertEqual((self.s.get(n["id"])["x"], self.s.get(n["id"])["y"]), (n["x"], n["y"]))
        self.assertEqual(self.s.get(c["id"])["monitor"], "eDP-1")
        # not the primary monitor: notes without a monitor aren't shown there
        r = self.s.tidy(2, "HDMI-A-1", 1920, 1080, primary=False)
        self.assertEqual([n["id"] for n in r["moved"]], [other_mon["id"]])

    def test_undo_once(self):
        a = self.s.add("a", workspace=1, x=700, y=600)
        self.s.tidy(1, "eDP-1", 1920, 1080)
        self.assertTrue(self.s.can_undo_tidy())
        self.assertNotEqual(self.s.get(a["id"])["x"], 700)
        r = self.s.tidy_undo()
        self.assertEqual([(n["x"], n["y"], n["monitor"]) for n in r["restored"]], [(700, 600, None)])
        with self.assertRaisesRegex(stickies.StickiesError, "nothing to undo"):
            self.s.tidy_undo()
        self.assertEqual(self.s.tidy(9, "eDP-1", 1920, 1080), {"moved": [], "undo": False})

    def test_gaps_out_from_hyprctl(self):
        for out, want in (('{"option": "general:gaps_out", "css": "10 10 10 10", "set": true}', (10,) * 4),
                          ('{"css": "5 20"}', (5, 20, 5, 20)),
                          ('{"css": "1 2 3"}', (1, 2, 3, 2)),
                          ('{"int": 7}', (7,) * 4),
                          ("not json", (10,) * 4)):
            os.environ["STICKIES_HYPRCTL"] = self.script("hyprctl", f"echo '{out}'")
            try:
                self.assertEqual(stickies.hypr_gaps_out(), want, out)
            finally:
                del os.environ["STICKIES_HYPRCTL"]

    def test_cli_on_the_focused_monitor(self):
        mons = [{"id": 0, "name": "eDP-1", "width": 1920, "height": 1080, "scale": 1, "transform": 0,
                 "focused": True, "reserved": [0, 26, 0, 0], "activeWorkspace": {"id": 4}},
                {"id": 1, "name": "HDMI-A-1", "width": 2560, "height": 1440, "scale": 2, "transform": 1,
                 "focused": False, "reserved": [0, 0, 0, 0], "activeWorkspace": {"id": 5}}]
        fake = self.script("hyprctl", f"""case "$*" in
  *monitors*) echo '{json.dumps(mons)}' ;;
  *gaps_out*) echo '{{"css": "8 8 8 8"}}' ;;
esac""")
        n = self.s.add("x", workspace=4, x=999, y=999)
        p = self.cli("tidy", "--json", env={"STICKIES_HYPRCTL": fake})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([(m["x"], m["y"]) for m in json.loads(p.stdout)["moved"]], [(8, 34)])
        self.assertIn("put 1 note back", self.cli("tidy", "--undo", env={"STICKIES_HYPRCTL": fake}).stdout)
        self.assertEqual(self.s.get(n["id"])["x"], 999)
        # the rotated, scaled second screen
        os.environ["STICKIES_HYPRCTL"] = fake
        try:
            scr = stickies.hypr_screen("HDMI-A-1")
        finally:
            del os.environ["STICKIES_HYPRCTL"]
        self.assertEqual((scr["width"], scr["height"], scr["workspace"], scr["primary"]), (720, 1280, 5, False))
        p = self.cli("tidy", env={"STICKIES_HYPRCTL": self.script("nohypr", "exit 1")})
        self.assertEqual(p.returncode, 1)
        self.assertIn("can't see the screens", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_bar_panel_button_and_ipc(self):
        src = qml_sources()
        self.assertIn("root.service.tidyHere()", src["BarPanel.qml"])
        self.assertIn("root.service.undoTidy()", src["BarPanel.qml"])
        self.assertIn("function tidy(): void { root.tidyHere() }", src["Service.qml"])
        self.assertIn('request("tidy", { workspace: d.workspaceId, monitor: d.screenName', src["Service.qml"])

    def test_drag_snaps_unless_shift(self):
        card = qml_sources()["NoteCard.qml"]
        self.assertIn("readonly property int grid: 8", card)
        self.assertIn("(mouse.modifiers & Qt.ShiftModifier) !== 0", card)
        self.assertIn("snapEdges = docked || service.waterfall ? null : desk.snapEdges(nid)", card)
        self.assertIn("desk.guideX = gx", card)
        self.assertIn("if (!free && snapEdges) {", card)  # no edges (waterfall): no grid either


class WaterfallPolishTest(Base):
    """The waterfall column (#136) owns its notes' places: no tidy, no
    snapping there; roll-up, checklists and the bell ride along."""

    def test_tidy_refuses_in_a_waterfall(self):
        self.s.add("a", workspace=1, x=700, y=600)
        self.s.set_settings({"layout": "waterfall-right"})
        with self.assertRaisesRegex(stickies.StickiesError, "free layout"):
            self.s.tidy(1, "eDP-1", 1920, 1080)
        self.assertFalse(self.s.can_undo_tidy())
        self.s.set_settings({"layout": "free"})
        self.assertEqual(len(self.s.tidy(1, "eDP-1", 1920, 1080)["moved"]), 1)

    def test_shell_hides_and_guards_tidy(self):
        src = qml_sources()
        self.assertIn("visible: !!root.service && !root.service.waterfall", src["BarPanel.qml"])
        fn = re.search(r"function tidyHere\(\) \{(.*?)\n  \}", src["Service.qml"], re.S).group(1)
        self.assertIn("if (waterfall) { say(", fn)

    def test_column_cards_get_roll_and_bell(self):
        wf = qml_sources()["Waterfall.qml"]
        self.assertIn("rolled: slot.rolled", wf)
        self.assertIn("remindAt: slot.remindAt", wf)
        # a rolled note takes its header line in the column's layout
        self.assertIn("rolled: rolled, pinned: pinned", wf)
        self.assertIn("onRolledChanged: if (member) sync()", wf)
        self.assertEqual(stickies.TITLE_H, 26)

    @unittest.skipUnless(NODE, "node not installed")
    def test_column_slots_for_rolled_notes(self):
        with open(os.path.join(os.path.dirname(SCRIPT), "waterfall.js")) as f:
            js = f.read().replace(".pragma library", "")
        script = js + """
const g = geometry({screenW: 1920, screenH: 1080, side: "right", width: 320, top: 26});
console.log(JSON.stringify(slots([{nid: 1, h: 200}, {nid: 2, h: 200, rolled: true}, {nid: 3, h: 50}], g)
  .slots.map(s => [s.y, s.h])));"""
        p = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        # rolled: its header (26), no 110 px minimum; the others unchanged
        self.assertEqual(json.loads(p.stdout), [[0, 200], [210, 26], [246, 110]])

    def test_one_position_behavior(self):
        # Two Behaviors on one property are a QML error: the column's glide
        # and tidy's share one.
        card = qml_sources()["NoteCard.qml"]
        self.assertEqual(card.count("Behavior on x"), 1)
        self.assertEqual(card.count("Behavior on y"), 1)


# -- 6. quick capture ------------------------------------------------------------

class ClipboardTest(Base):
    def fake(self, types, text):
        return self.script("wl-paste", f"""if [ "$1" = "--list-types" ]; then printf '{types}'; exit 0; fi
cat "{self.text_file(text)}\"""")

    def text_file(self, text):
        path = os.path.join(self.state, "clip.txt")
        with open(path, "w") as f:
            f.write(text)
        return path

    def clip(self, types, text):
        os.environ["STICKIES_WL_PASTE"] = self.fake(types, text)
        try:
            return stickies.clipboard_text()
        finally:
            del os.environ["STICKIES_WL_PASTE"]

    def test_text_trimmed_and_capped(self):
        self.assertEqual(self.clip("text/plain;charset=utf-8\\nTEXT\\n", "  \n hello  \n"), ("hello", False))
        text, cut = self.clip("UTF8_STRING\\n", "x" * 12000)
        self.assertEqual((len(text), cut), (stickies.CLIP_MAX, True))

    def test_not_text(self):
        with self.assertRaisesRegex(stickies.StickiesError, "^the clipboard holds an image, not text$"):
            self.clip("image/png\\n", "")
        with self.assertRaisesRegex(stickies.StickiesError, "a file, not text"):
            self.clip("text/uri-list\\n", "file:///x")
        with self.assertRaisesRegex(stickies.StickiesError, "^the clipboard is empty$"):
            self.clip("text/plain\\n", "   ")
        os.environ["STICKIES_WL_PASTE"] = os.path.join(self.state, "missing")
        try:
            with self.assertRaisesRegex(stickies.StickiesError, "install wl-clipboard"):
                stickies.clipboard_text()
        finally:
            del os.environ["STICKIES_WL_PASTE"]

    def test_serve_op_and_cli(self):
        os.environ["STICKIES_WL_PASTE"] = self.fake("text/plain\\n", "Cardmarket fees went up")
        try:
            r = stickies.dispatch(self.s, "paste", {"monitor": "eDP-1", "workspace": 2, "x": 10, "y": 20})
            self.assertEqual((r["note"]["body"], r["note"]["workspace"], r["truncated"]),
                             ("Cardmarket fees went up", 2, False))
            p = self.cli("add", "--clipboard", "--json")
            self.assertEqual(json.loads(p.stdout)["body"], "Cardmarket fees went up")
        finally:
            del os.environ["STICKIES_WL_PASTE"]

    def test_keybinding_and_flash(self):
        svc = qml_sources()["Service.qml"]
        self.assertIn("function paste(): void { root.pasteHere() }", svc)
        fn = re.search(r"function pasteHere\(\) \{(.*?)\n  \}", svc, re.S).group(1)
        self.assertIn('request("paste"', fn)
        self.assertIn("root.flashRequest = r.note.id", fn)
        self.assertIn("root.say(root.sentence(r))", fn)  # errors in words, on the desktop


# -- 7. reminders ----------------------------------------------------------------

class ReminderParseTest(unittest.TestCase):
    NOW = datetime(2026, 10, 5, 14, 30).astimezone()

    def specs(self, body):
        return [(s, datetime.fromisoformat(d.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M"))
                for s, d, _ in stickies.find_reminders(body, self.NOW)]

    def test_forms(self):
        self.assertEqual(self.specs("@ 2026-10-07 09:00"), [("2026-10-07 09:00", "2026-10-07 09:00")])
        self.assertEqual(self.specs("x\n@tomorrow 9:00 call"), [("tomorrow 9:00", "2026-10-06 09:00")])
        self.assertEqual(self.specs("@morgen 17.30"), [("morgen 17:30", "2026-10-06 17:30")])
        self.assertEqual(self.specs("@today"), [("today", "2026-10-05 09:00")])
        self.assertEqual(self.specs("- [ ] pay @ 2026-11-01"), [("2026-11-01", "2026-11-01 09:00")])
        self.assertEqual(self.specs("@ 16:00"), [("16:00", "2026-10-05 16:00")])
        self.assertEqual(self.specs("@8:15"), [("8:15", "2026-10-06 08:15")])  # passed today -> tomorrow

    def test_not_reminders(self):
        for body in ("mail jeroen@tomorrow.nl", "@tomorrowland", "@ 2026-13-01", "@today 25:00",
                     "@ 9", "price @ 5", "@2026-10-07T09:00"):
            self.assertEqual(self.specs(body), [], body)

    def test_text(self):
        self.assertEqual(stickies.reminder_text("Payout\n@tomorrow 9:00 call Jeroen", "tomorrow 9:00"), "call Jeroen")
        self.assertEqual(stickies.reminder_text("Call the bank @ 16:00", "16:00"), "Call the bank")
        self.assertEqual(stickies.reminder_text("BTW aangifte\n@ 2026-10-07", "2026-10-07"), "BTW aangifte")


class ReminderStoreTest(Base):
    def soon(self, minutes):
        return (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M")

    def test_remind_at_follows_the_text(self):
        n = self.s.add(f"Payout\n@ {self.soon(60)} pay Jeroen")
        self.assertIsNotNone(n["remind_at"])
        n2 = self.s.edit(n["id"], body=f"Payout\n@ {self.soon(60)} pay Jeroen\n@ {self.soon(30)} prep")
        self.assertLess(n2["remind_at"], n["remind_at"])
        self.assertEqual(len(self.s.reminders()), 2)
        self.assertIsNone(self.s.edit(n["id"], body="Payout")["remind_at"])
        self.assertEqual(self.s.reminders(), [])

    def test_past_when_written_never_fires(self):
        n = self.s.add("old\n@ 2020-01-01 09:00")
        self.assertIsNone(n["remind_at"])
        self.assertEqual(self.s.fire_reminders(lambda *a: self.fail("notified")), [])

    def test_relative_day_counts_from_when_it_was_written(self):
        n = self.s.add("@tomorrow 9:00 dentist")
        due = self.s.reminders()[0]["due"]
        self.s.edit(n["id"], body="@tomorrow 9:00 dentist\nmore text")  # spec unchanged: same due
        self.assertEqual(self.s.reminders()[0]["due"], due)

    def test_fires_once_and_survives_restart(self):
        n = self.s.add(f"Payout\n@ {self.soon(60)} pay Jeroen")
        # make it overdue, as if the laptop was off at the time
        self.s.db.execute("UPDATE reminders SET due=?", (iso(datetime.now() - timedelta(hours=3)),))
        sent = []
        updated = self.s.get(n["id"])["updated_at"]
        fired = self.s.fire_reminders(lambda title, text: sent.append((title, text)))
        self.assertEqual(self.s.get(n["id"])["updated_at"], updated)  # a reminder is not an edit
        self.assertEqual([f["note_id"] for f in fired], [n["id"]])
        self.assertEqual(sent[0][1], "pay Jeroen")
        self.assertIn("missed", sent[0][0])
        self.assertIsNone(self.s.get(n["id"])["remind_at"])
        self.s.close()
        self.s = stickies.Store()
        self.assertEqual(self.s.fire_reminders(lambda *a: self.fail("fired twice")), [])
        with open(os.path.join(self.state, "stickies.log")) as f:
            self.assertIn("sent", f.read())

    def test_archived_notes_wait_and_a_failed_notify_is_logged(self):
        n = self.s.add(f"x @ {self.soon(60)}")
        self.s.db.execute("UPDATE reminders SET due=?", (iso(datetime.now() - timedelta(minutes=1)),))
        self.s.archive(n["id"])
        self.assertEqual(self.s.fire_reminders(lambda *a: self.fail("archived")), [])
        self.s.restore(n["id"])

        def boom(*a):
            raise RuntimeError("no notification daemon")
        self.assertEqual(len(self.s.fire_reminders(boom)), 1)
        with open(os.path.join(self.state, "stickies.log")) as f:
            self.assertIn("notification failed: no notification daemon", f.read())

    def test_notify_send_command(self):
        log = os.path.join(self.state, "notify.log")
        os.environ["STICKIES_NOTIFY"] = self.script("notify-send", f'printf "%s|" "$@" > "{log}"')
        try:
            self.assertTrue(stickies.send_notification("Sticky note reminder", "pay Jeroen"))
        finally:
            del os.environ["STICKIES_NOTIFY"]
        with open(log) as f:
            self.assertEqual(f.read(), "--app-name=Stickies|--icon=accessories-text-editor|"
                                       "Sticky note reminder|pay Jeroen|")
        self.assertIsNone(stickies.notify_bin())  # a temp STICKIES_STATE never notifies by itself

    def test_cli_lists_pending(self):
        self.assertIn("no reminders", self.cli("reminders").stdout)
        self.s.add(f"Payout\n@ {self.soon(90)} pay Jeroen")
        rs = json.loads(self.cli("reminders", "--json").stdout)
        self.assertEqual([r["text"] for r in rs], ["pay Jeroen"])
        self.assertIn("pay Jeroen", self.cli("reminders").stdout)

    def test_serve_fires_missed_reminders_at_start_then_on_time(self):
        n = self.s.add(f"Payout\n@ {self.soon(60)} pay Jeroen")
        self.s.db.execute("UPDATE reminders SET due=?", (iso(datetime.now() - timedelta(hours=1)),))
        m = self.s.add(f"Later\n@ {self.soon(120)} later")
        sent = []
        rfd, wfd = os.pipe()
        out = io.StringIO()

        def run():
            store = stickies.Store()
            try:
                stickies.serve(store, infd=rfd, out=out, poll=0.05, hub_delay=60, embedder=None,
                               notify=lambda title, text: sent.append(text), remind_every=0.05)
            finally:
                store.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        try:
            deadline = time.time() + 5
            while not sent and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(sent, ["pay Jeroen"])  # the missed one, once, at start
            self.s.db.execute("UPDATE reminders SET due=? WHERE note_id=?",
                              (iso(datetime.now() + timedelta(seconds=0.3)), m["id"]))
            deadline = time.time() + 5
            while len(sent) < 2 and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(sent, ["pay Jeroen", "later"])
        finally:
            os.close(wfd)
            t.join(timeout=5)
            os.close(rfd)
        # the card's bell goes: serve pushed the note without remind_at
        events = [json.loads(l) for l in out.getvalue().splitlines()]
        last = [e for e in events if e.get("event") == "changed" and e["id"] == n["id"]][-1]
        self.assertIsNone(last["note"]["remind_at"])

    def test_bell_on_the_card(self):
        src = qml_sources()
        self.assertIn('remindAt: n.remind_at || ""', src["Service.qml"])
        self.assertIn('visible: card.remindLabel !== ""', src["NoteCard.qml"])


# -- 8. plain words, never a traceback ---------------------------------------------

class PlainErrorsTest(Base):
    def test_unexpected_cli_error_is_logged_not_shown(self):
        old = stickies.Store.get
        stickies.Store.get = lambda self, i: 1 / 0
        err, out = io.StringIO(), io.StringIO()
        try:
            with redirect_stderr(err), redirect_stdout(out):
                code = stickies.main(["show", "1"])
                code_json = stickies.main(["show", "1", "--json"])
        finally:
            stickies.Store.get = old
        self.assertEqual((code, code_json), (1, 1))
        self.assertNotIn("Traceback", err.getvalue())
        self.assertIn("something went wrong (ZeroDivisionError", err.getvalue())
        self.assertIn("details in", err.getvalue())
        self.assertIn("error", json.loads(out.getvalue()))
        with open(os.path.join(self.state, "stickies.log")) as f:
            self.assertIn("Traceback", f.read())

    def test_serve_bug_answers_in_words_and_keeps_serving(self):
        old = stickies.dispatch

        def flaky(store, op, a):
            if op == "boom":
                raise KeyError("x")
            return old(store, op, a)
        stickies.dispatch = flaky
        rfd, wfd = os.pipe()
        out = io.StringIO()
        try:
            t = threading.Thread(target=lambda: stickies.serve(stickies.Store(), infd=rfd, out=out, poll=0.05,
                                                               hub_delay=60, embedder=None, notify=None),
                                 daemon=True)
            t.start()
            os.write(wfd, b'{"id": 1, "op": "boom"}\n{"id": 2, "op": "ping"}\n{"id": 3, "op": "show", "args": {"id": "abc"}}\n')
            deadline = time.time() + 5
            while out.getvalue().count('"id": 3') == 0 and time.time() < deadline:
                time.sleep(0.02)
        finally:
            os.close(wfd)
            t.join(timeout=5)
            os.close(rfd)
            stickies.dispatch = old
        replies = {m["id"]: m for m in map(json.loads, out.getvalue().splitlines()) if "id" in m and "event" not in m}
        self.assertFalse(replies[1]["ok"])
        self.assertIn("something went wrong (KeyError); details in stickies.log", replies[1]["error"])
        self.assertTrue(replies[2]["ok"])
        self.assertEqual(replies[3]["error"], "note id must be a number, not 'abc'")

    def test_desktop_turns_errors_into_sentences(self):
        svc = qml_sources()["Service.qml"]
        self.assertIn("function sentence(err)", svc)
        self.assertIn('say("Stickies is still starting; try again in a moment")', svc)
        desk = qml_sources()["Desktop.qml"]
        self.assertIn("textFormat: Text.PlainText", desk)


if __name__ == "__main__":
    unittest.main()
