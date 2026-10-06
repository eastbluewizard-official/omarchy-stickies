"""Tags (buckets), the All notes view's data and IPC, and the install fixes
that came with them: the plugin-code hash that restarts the shell and
install.sh's key list."""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, SCRIPT, TempState, git_checkout, stickies

# A v4 database as 1.0.0 made it: notes without `tags`, an FTS index over
# the body only, and its triggers.
V4_SCHEMA = """
CREATE TABLE notes (
    id INTEGER PRIMARY KEY, body TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT 'yellow',
    pinned INTEGER NOT NULL DEFAULT 0, workspace INTEGER, monitor TEXT,
    x INTEGER NOT NULL DEFAULT 0, y INTEGER NOT NULL DEFAULT 0, w INTEGER NOT NULL DEFAULT 240,
    h INTEGER NOT NULL DEFAULT 200, z INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, archived_at TEXT, rolled INTEGER NOT NULL DEFAULT 0, remind_at TEXT);
CREATE VIRTUAL TABLE notes_fts USING fts5(body, content='notes', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2', prefix='2 3');
CREATE TABLE changes (seq INTEGER PRIMARY KEY AUTOINCREMENT, note_id INTEGER NOT NULL, kind TEXT NOT NULL);
CREATE TRIGGER notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, body) VALUES (new.id, new.body);
    INSERT INTO changes(note_id, kind) VALUES (new.id, 'add');
END;
CREATE TRIGGER notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO changes(note_id, kind) VALUES (old.id, 'purge');
END;
CREATE TRIGGER notes_au_body AFTER UPDATE OF body ON notes WHEN old.body IS NOT new.body BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, body) VALUES ('delete', old.id, old.body);
    INSERT INTO notes_fts(rowid, body) VALUES (new.id, new.body);
END;
INSERT INTO notes(body, created_at, updated_at) VALUES
    ('Cardmarket fees went up', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z'),
    ('Groceries: milk, bread', '2026-01-02T00:00:00.000Z', '2026-01-02T00:00:00.000Z');
PRAGMA user_version=4;
"""


class MigrationTest(TempState):
    def test_v4_database_gets_tags(self):
        path = os.path.join(self.state, "stickies.db")
        db = sqlite3.connect(path)
        db.executescript(V4_SCHEMA)
        db.close()

        s = stickies.Store()
        self.assertEqual(s.db.execute("PRAGMA user_version").fetchone()[0], stickies.SCHEMA_VERSION)
        self.assertEqual([n["tags"] for n in s.list()], [[], []])
        self.assertEqual([h["id"] for h in s.search("cardm", mode="fts")], [1])  # old notes still indexed
        fts = [r[1] for r in s.db.execute("PRAGMA table_info(notes_fts)")]
        self.assertEqual(fts, ["body", "tags"])
        triggers = {r[0] for r in s.db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        self.assertNotIn("notes_au_body", triggers)
        self.assertIn("notes_au_fts", triggers)
        s.tag(2, ["home"])
        self.assertEqual([h["id"] for h in s.search("home", mode="fts")], [2])
        s.edit(2, body="Groceries: cheese")  # body edits keep the tag in the index
        self.assertEqual([h["id"] for h in s.search("home", mode="fts")], [2])
        self.assertEqual([h["id"] for h in s.search("chees", mode="fts")], [2])
        s.close()
        s = stickies.Store()  # opening again changes nothing
        self.assertEqual(s.get(2)["tags"], ["home"])
        self.assertEqual(s.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        s.db.execute("INSERT INTO notes_fts(notes_fts) VALUES ('integrity-check')")
        s.close()


class StoreTagsTest(TempState):
    def setUp(self):
        super().setUp()
        self.s = stickies.Store()

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def test_rules(self):
        self.assertEqual(stickies.normalize_tags(["#Work", "work", "op-09", " Btw "]), ["work", "op-09", "btw"])
        self.assertEqual(stickies.normalize_tags("a, b c"), ["a", "b", "c"])
        for bad in ("two words", "x" * 33, "ü", "", "a_b", "a.b"):
            with self.assertRaises(stickies.StickiesError, msg=bad):
                stickies.normalize_tag(bad)
        self.assertEqual(stickies.normalize_tag("x" * 32), "x" * 32)
        with self.assertRaises(stickies.StickiesError):
            self.s.add("x", tags=[f"t{i}" for i in range(stickies.TAGS_PER_NOTE + 1)])

    def test_add_tag_untag_set(self):
        n = self.s.add("Fees", tags=["work", "cardmarket"])
        self.assertEqual(n["tags"], ["work", "cardmarket"])
        self.assertEqual(self.s.tag(n["id"], ["home", "work"])["tags"], ["work", "cardmarket", "home"])
        self.assertEqual(self.s.untag(n["id"], ["work", "nope"])["tags"], ["cardmarket", "home"])
        self.assertEqual(self.s.set_tags(n["id"], ["b", "a"])["tags"], ["b", "a"])
        rows = self.s.db.execute("SELECT tag FROM note_tags WHERE note_id=? ORDER BY tag", (n["id"],)).fetchall()
        self.assertEqual([r[0] for r in rows], ["a", "b"])  # note_tags and the mirror agree
        self.assertEqual(self.s.add("no tags")["tags"], [])

    def test_tagging_is_a_change_serve_pushes(self):
        n = self.s.add("x")
        seq = self.s.last_seq()
        self.s.tag(n["id"], ["work"])
        _, events = self.s.changes_since(seq)
        self.assertEqual([(e["kind"], e["note"]["tags"]) for e in events], [("update", ["work"])])
        seq = self.s.last_seq()
        self.s.tag(n["id"], ["work"])  # nothing new: nothing written
        self.assertEqual(self.s.changes_since(seq)[1], [])

    def test_all_tags_most_used_first(self):
        a = self.s.add("a", tags=["work", "home"])["id"]
        self.s.add("b", tags=["work"])
        self.s.add("c", tags=["work", "btw"])
        self.assertEqual(self.s.all_tags(), [{"tag": "work", "count": 3}, {"tag": "btw", "count": 1},
                                             {"tag": "home", "count": 1}])
        self.s.archive(a)
        self.assertEqual([t["tag"] for t in self.s.all_tags()], ["work", "btw"])
        self.assertEqual(self.s.all_tags(archived=True)[0], {"tag": "work", "count": 3})

    def test_list_filter_needs_every_tag(self):
        a = self.s.add("a", tags=["work", "cardmarket"])["id"]
        b = self.s.add("b", tags=["work"])["id"]
        self.s.add("c")
        self.assertEqual({n["id"] for n in self.s.list(tags=["work"])}, {a, b})
        self.assertEqual([n["id"] for n in self.s.list(tags=["work", "cardmarket"])], [a])
        self.assertEqual(self.s.list(tags=["nope"]), [])

    def test_search_finds_tags_and_narrows(self):
        a = self.s.add("Call the bank", tags=["work"])["id"]
        b = self.s.add("Work out the fees")["id"]
        c = self.s.add("The bank fees", tags=["home"])["id"]
        self.assertEqual({h["id"] for h in self.s.search("work", mode="fts")}, {a, b})
        self.assertEqual([h["id"] for h in self.s.search("bank", mode="fts", tags=["home"])], [c])
        self.assertEqual([h["id"] for h in self.s.search("fees", mode="hybrid", tags=["home"])], [c])
        self.assertEqual(self.s.search("bank", mode="fts", tags=["home", "work"]), [])

    def test_purge_cascades_and_tagged_empty_notes_stay(self):
        n = self.s.add("", tags=["inbox"])
        self.assertEqual(self.s.discard_empty(), [])  # tagged on purpose: kept
        empty = self.s.add("")
        self.assertEqual(self.s.discard_empty(), [empty["id"]])
        self.s.purge(n["id"], force=True)
        self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM note_tags").fetchone()[0], 0)

    def test_brief_rows_for_the_list_view(self):
        self.s.add("Groceries\n[x] milk\n- [ ] bread\n[X] eggs", tags=["home"])
        self.s.add("[ ] first\nsecond")
        self.s.add("Fees\n" + "x" * 900)
        rows = stickies.dispatch(self.s, "list", {"brief": True})
        by = {r["title"]: r for r in rows}
        self.assertEqual(by["Groceries"]["checks"], [2, 3])
        self.assertEqual(by["Groceries"]["tags"], ["home"])
        self.assertTrue(by["Groceries"]["hay"].endswith("\nhome"))
        self.assertEqual(by["first"]["checks"], [0, 1])  # title: the checkbox is not part of it
        self.assertIsNone(by["Fees"]["checks"])
        self.assertLessEqual(len(by["Fees"]["hay"]), stickies.BRIEF_TEXT + 1)
        self.assertNotIn("body", by["Fees"])
        self.assertEqual(set(rows[0]), {"id", "title", "hay", "color", "pinned", "workspace", "tags",
                                        "remind_at", "updated_at", "checks"})
        self.assertEqual(len(stickies.dispatch(self.s, "list", {"brief": True, "tags": ["home"]})), 1)

    def test_serve_ops(self):
        n = stickies.dispatch(self.s, "add", {"body": "x", "tags": ["a"]})
        self.assertEqual(stickies.dispatch(self.s, "tag", {"id": n["id"], "tags": ["b"]})["tags"], ["a", "b"])
        self.assertEqual(stickies.dispatch(self.s, "untag", {"id": n["id"], "tags": "a"})["tags"], ["b"])
        self.assertEqual(stickies.dispatch(self.s, "set_tags", {"id": n["id"], "tags": []})["tags"], [])
        self.assertEqual(stickies.dispatch(self.s, "tags", {}), [])

    def test_chat_may_propose_tags(self):
        p = stickies.validate_proposal({"action": "new_note", "body": "Recheck margins",
                                        "tags": ["#Work", "cardmarket"]})
        self.assertEqual(p["tags"], ["work", "cardmarket"])
        self.assertIn("#work #cardmarket", p["summary"])
        for bad in ({"tags": "work"}, {"tags": ["two words"]}, {"tags": [1]}):
            with self.assertRaises(stickies.StickiesError):
                stickies.validate_proposal(dict({"action": "new_note", "body": "x"}, **bad))
        r = stickies.apply_proposal(self.s, {"action": "new_note", "body": "Recheck", "tags": ["work"]})
        self.assertEqual(r["note"]["tags"], ["work"])
        self.assertIn('"tags"', stickies.CHAT_SYSTEM)
        sent = stickies.build_prompt("q", [self.s.get(r["note"]["id"])])
        self.assertIn('tags="work"', sent)


class TagCliTest(TempState):
    def run_cli(self, *args, ok=True, env=None):
        p = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True,
                           env=dict(os.environ, **(env or {})))
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return p

    def j(self, *args, **kw):
        return json.loads(self.run_cli(*args, "--json", **kw).stdout)

    def test_tag_commands(self):
        a = self.j("add", "--tag", "work", "--tag", "Cardmarket", "Fees went up")
        self.assertEqual(a["tags"], ["work", "cardmarket"])
        b = self.j("add", "-t", "#work", "Call the bank")
        c = self.j("add", "Groceries")
        self.assertEqual(c["tags"], [])  # [] when none, never null
        self.assertEqual(self.j("tag", str(c["id"]), "home", "later")["tags"], ["home", "later"])
        self.assertEqual(self.j("untag", str(c["id"]), "later")["tags"], ["home"])
        self.assertEqual(self.j("tags"), [{"tag": "work", "count": 2}, {"tag": "cardmarket", "count": 1},
                                          {"tag": "home", "count": 1}])
        self.assertIn("2  work", self.run_cli("tags").stdout)
        self.assertEqual([n["id"] for n in self.j("list", "--tag", "work")], [b["id"], a["id"]])
        self.assertEqual([n["id"] for n in self.j("list", "--tag", "work", "--tag", "cardmarket")], [a["id"]])
        self.assertIn("#work", self.run_cli("list").stdout)
        self.assertEqual(self.run_cli("list", "--tag", "nope").stdout.strip(), "no notes")
        env = {"STICKIES_SEMANTIC": "0"}
        self.assertEqual({h["id"] for h in self.j("search", "work", env=env)}, {a["id"], b["id"]})
        self.assertEqual([h["id"] for h in self.j("search", "bank", "--tag", "work", env=env)], [b["id"]])
        self.assertEqual(self.j("search", "bank", "--tag", "home", env=env), [])
        self.assertIn("tags work cardmarket", self.run_cli("show", str(a["id"])).stdout)

    def test_bad_tag_is_an_error(self):
        n = self.j("add", "x")
        p = self.run_cli("tag", str(n["id"]), "Two Words", "--json", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("bad tag", json.loads(p.stdout)["error"])
        self.assertNotEqual(self.run_cli("add", "--tag", "a_b", "x", ok=False).returncode, 0)
        self.assertEqual(self.j("list")[0]["tags"], [])

    def test_list_open_calls_the_plugin(self):
        log = os.path.join(self.state, "shell.log")
        fake = os.path.join(self.state, "omarchy-shell")
        with open(fake, "w") as f:
            f.write(f'#!/bin/sh\necho "$*" >> "{log}"\n')
        os.chmod(fake, 0o755)
        env = {"STICKIES_OMARCHY_SHELL": fake}
        r = self.j("list", "--open", env=env)
        self.assertTrue(r["ok"])
        self.assertEqual(r["tags"], [])
        self.assertEqual(self.j("list", "--open", "--tag", "Work", "-t", "op-09", env=env)["tags"], ["work", "op-09"])
        with open(log) as f:
            self.assertEqual(f.read().splitlines(), ["stickies list", "stickies listTag work op-09"])
        self.assertNotEqual(self.run_cli("list", "--open", "--archived", ok=False, env=env).returncode, 0)
        # a terminal `list` keeps printing (agents rely on it)
        self.assertEqual(self.j("list"), [])


class ListIpcTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, "Service.qml")) as f:
            self.service = f.read()
        with open(os.path.join(ROOT, "List.qml")) as f:
            self.list = f.read()

    def test_ipc_methods(self):
        ipc = re.search(r'IpcHandler \{\s*target: "stickies"(.*?)\n  \}', self.service, re.S).group(1)
        self.assertRegex(ipc, r"function list\(\): void \{ listOverlay\.toggle\(\[\]\) \}")
        self.assertRegex(ipc, r"function listTag\(tags: string\): void")
        self.assertIn("list", stickies.SHELL_METHODS)
        self.assertIn(("SUPER + ALT + O", "All sticky notes", "list"), stickies.KEYS)

    def test_same_look_and_focus_as_search(self):
        self.assertIn("OverlayFocus {", self.list)
        self.assertIn("WlrLayershell.keyboardFocus: keeper.keyboardFocus", self.list)
        self.assertIn("WlrLayer.Overlay", self.list)
        self.assertIn("service.goTo(nid, true)", self.list)  # Enter: jump + flash, as search does
        self.assertIn("service.archive(nid", self.list)      # Delete: the archive with the Undo toast
        self.assertIn('"list", { brief: true }', self.list)
        # user text is never markup
        for m in re.finditer(r"Text \{(.*?)\n          \}", self.list, re.S):
            if "modelData.title" in m.group(1):
                self.assertIn("Text.PlainText", m.group(1))

    def test_bar_panel_opens_it(self):
        with open(os.path.join(ROOT, "BarPanel.qml")) as f:
            src = f.read()
        self.assertIn('text: "All notes"', src)
        self.assertIn("service.openList([])", src)


class ShellRefreshTest(TempState):
    """The running shell keeps the plugin QML it loaded: install.sh and
    `stickies integrate` restart it when the code changed since the last
    install (a stub stands in for omarchy-restart-shell)."""

    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.state, "restart.log")
        self.stub = os.path.join(self.state, "restart")
        self.write_stub(0)
        self._old_restart = os.environ.get("STICKIES_RESTART_SHELL")
        os.environ["STICKIES_RESTART_SHELL"] = self.stub

    def tearDown(self):
        if self._old_restart is None:
            os.environ.pop("STICKIES_RESTART_SHELL", None)
        else:
            os.environ["STICKIES_RESTART_SHELL"] = self._old_restart
        super().tearDown()

    def write_stub(self, code):
        with open(self.stub, "w") as f:
            f.write(f'#!/bin/sh\necho restart >> "{self.log}"\nexit {code}\n')
        os.chmod(self.stub, 0o755)

    def restarts(self):
        if not os.path.exists(self.log):
            return 0
        with open(self.log) as f:
            return len(f.read().splitlines())

    def test_hash_covers_qml_and_js_only(self):
        d = tempfile.mkdtemp(prefix="stickies-hash-")
        try:
            for name in ("Service.qml", "waterfall.js", "README.md"):
                with open(os.path.join(d, name), "w") as f:
                    f.write("v1")
            h = stickies.plugin_code_hash(d)
            with open(os.path.join(d, "README.md"), "w") as f:
                f.write("v2")
            self.assertEqual(stickies.plugin_code_hash(d), h)
            with open(os.path.join(d, "waterfall.js"), "w") as f:
                f.write("v2")
            self.assertNotEqual(stickies.plugin_code_hash(d), h)
            h = stickies.plugin_code_hash(d)
            with open(os.path.join(d, "List.qml"), "w") as f:  # a new file counts too
                f.write("v1")
            self.assertNotEqual(stickies.plugin_code_hash(d), h)
        finally:
            shutil.rmtree(d)

    def test_restart_only_when_the_code_changed(self):
        r = stickies.refresh_shell()
        self.assertEqual((r["changed"], r["restarted"]), (True, True))  # nothing recorded yet
        self.assertEqual(self.restarts(), 1)
        self.assertEqual(stickies.read_integration()["code_hash"], stickies.plugin_code_hash())
        r = stickies.refresh_shell()
        self.assertEqual((r["changed"], r["restarted"]), (False, False))
        self.assertEqual(self.restarts(), 1)
        self.assertFalse(stickies.integration_status()["code_changed"])
        # what an install of new QML looks like from here
        stickies.write_integration(dict(stickies.read_integration(), code_hash="old"))
        self.assertTrue(stickies.integration_status()["code_changed"])
        self.assertTrue(stickies.refresh_shell()["restarted"])
        self.assertEqual(self.restarts(), 2)

    def test_failed_restart_is_retried_next_time(self):
        self.write_stub(1)
        r = stickies.refresh_shell()
        self.assertIn("error", r)
        self.assertNotIn("code_hash", stickies.read_integration())
        self.write_stub(0)
        self.assertTrue(stickies.refresh_shell()["restarted"])

    def test_cli_integrate_restarts_the_desktop_does_not(self):
        env = dict(os.environ, STICKIES_HYPRCTL=os.path.join(self.state, "no-hyprctl"),
                   STICKIES_BIN=os.path.join(self.state, "bin"))
        p = subprocess.run([sys.executable, SCRIPT, "integrate", "--no", "--json"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout)["shell"]["restarted"])
        p = subprocess.run([sys.executable, SCRIPT, "integrate", "--refresh"], env=env,
                           capture_output=True, text=True)
        self.assertIn("unchanged", p.stdout)
        self.assertEqual(self.restarts(), 1)
        # the desktop's own offer runs inside the shell: never a restart
        stickies.write_integration(dict(stickies.read_integration(), code_hash="old"))
        s = stickies.Store()
        try:
            r = stickies.dispatch(s, "integrate", {"yes": False})
        finally:
            s.close()
        self.assertFalse(r["shell"]["restarted"])
        self.assertEqual(self.restarts(), 1)

    def test_no_restart_outside_the_live_session(self):
        os.environ.pop("STICKIES_RESTART_SHELL")
        old = os.environ.get("HOME")
        os.environ["HOME"] = self.state  # a temp HOME is never the live session
        try:
            self.assertIsNone(stickies.restart_shell_command())
            r = stickies.refresh_shell()
            self.assertEqual((r["changed"], r["restarted"]), (True, False))
        finally:
            os.environ["HOME"] = old
            os.environ["STICKIES_RESTART_SHELL"] = self.stub


class InstallMessageTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="stickies-home-")
        self.plugin = git_checkout(os.path.join(self.home, ".config", "omarchy", "plugins",
                                                "eastbluewizard.stickies"))
        self.restarts = os.path.join(self.home, "restarts.log")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("STICKIES_", "XDG_"))}
        self.env.update(HOME=self.home, STICKIES_NO_LIVE="1", STICKIES_VENV="",
                        STICKIES_RESTART_SHELL=f"sh -c 'echo restart >> {self.restarts}'")

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def install(self):
        p = subprocess.run([os.path.join(self.plugin, "install.sh")], env=self.env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def restarted(self):
        try:
            with open(self.restarts) as f:
                return len(f.read().splitlines())
        except OSError:
            return 0

    def test_install_lists_every_key_from_the_dropin(self):
        out = self.install()
        with open(os.path.join(ROOT, "hypr", "stickies.lua")) as f:
            binds = re.findall(r'^o\.bind\("([^"]+)", "([^"]+)"', f.read(), re.M)
        self.assertEqual(len(binds), len(stickies.KEYS))
        for keys, desc in binds:
            self.assertIn(f"  {keys}: {desc}\n", out)
        with open(os.path.join(ROOT, "install.sh")) as f:
            self.assertNotIn("SUPER+ALT", f.read())  # the key list comes from the same table

    def test_install_restarts_the_shell_when_the_code_changed(self):
        self.install()
        self.assertEqual(self.restarted(), 1)  # nothing recorded yet: the shell may hold anything
        self.install()
        self.assertEqual(self.restarted(), 1)  # same QML/JS: no restart (git pull of docs only)
        with open(os.path.join(self.plugin, "Service.qml"), "a") as f:
            f.write("// changed\n")
        self.assertIn("restarted the shell", self.install())
        self.assertEqual(self.restarted(), 2)


if __name__ == "__main__":
    unittest.main()
