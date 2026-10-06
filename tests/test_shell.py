"""The desktop plugin (the repo root): manifest, install, and the serve contract it
relies on. Rendering, drag and typing need a live Wayland session, so they
are measured there, not in the unit tests."""

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helpers import (LAUNCHER, ROOT, VALIDATE, TempState, git_checkout, has_symlink, plugin_copy,
                     shipped_files, stickies)
from test_privacy import fake_venv

SHELL = ROOT  # the plugin is the repo root


def node_lib(name):
    """A QML .js library as plain JS for node: its `.import "markup.js" as
    Markup` becomes a Markup object holding markup.js's functions."""
    def read(n):
        with open(os.path.join(SHELL, n)) as f:
            return "\n".join(l for l in f.read().splitlines() if not l.startswith((".pragma", ".import")))
    return ("var Markup = (function() {\n" + read("markup.js")
            + "\nreturn { escapeHtml: escapeHtml, safeColor: safeColor } })();\n" + read(name))


def qml_sources():
    out = {}
    for path in glob.glob(os.path.join(SHELL, "*.qml")):
        with open(path) as f:
            out[os.path.basename(path)] = f.read()
    return out


class ManifestTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(SHELL, "manifest.json")) as f:
            self.m = json.load(f)

    def test_shape(self):
        self.assertEqual(self.m["schemaVersion"], 1)
        self.assertEqual(self.m["id"], "eastbluewizard.stickies")
        self.assertEqual(self.m["kinds"], ["service", "bar-widget"])
        for name in ("name", "version", "description"):
            self.assertTrue(self.m[name])

    def test_entry_points_exist(self):
        self.assertEqual(set(self.m["entryPoints"]), {"service", "barWidget"})
        self.assertFalse(self.m["barWidget"]["allowMultiple"])
        for ep in self.m["entryPoints"].values():
            self.assertFalse(os.path.isabs(ep) or ".." in ep, ep)
            self.assertTrue(os.path.isfile(os.path.join(SHELL, ep)), ep)

    def test_no_symlinks_shipped(self):
        # omarchy-plugin-validate refuses any symlink in the plugin folder
        for f in shipped_files():
            self.assertFalse(os.path.islink(os.path.join(ROOT, f)), f)

    def test_launcher_is_executable(self):
        self.assertTrue(os.access(LAUNCHER, os.X_OK))
        self.assertTrue(os.access(os.path.join(ROOT, "stickies.py"), os.X_OK))

    @unittest.skipUnless(os.path.exists(VALIDATE), "omarchy-plugin-validate not installed")
    def test_omarchy_plugin_validate(self):
        # what `omarchy plugin add` clones: the repo root, as shipped
        tmp = tempfile.mkdtemp(prefix="stickies-plugin-")
        try:
            p = subprocess.run([VALIDATE, plugin_copy(os.path.join(tmp, "eastbluewizard.stickies"))],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class QmlBoundaryTest(unittest.TestCase):
    """QML never touches the DB; it only talks to `stickies serve`."""

    def setUp(self):
        self.src = qml_sources()

    def test_no_direct_db_access(self):
        for name, text in self.src.items():
            for bad in ("stickies.db", "sqlite", "LocalStorage", "STICKIES_STATE"):
                self.assertNotIn(bad, text, f"{name} mentions {bad}")

    def test_only_process_is_serve(self):
        commands = []
        for name, text in self.src.items():
            commands += re.findall(r"command:\s*(\[.*?\])\s*\n", text, re.S)
        self.assertEqual(len(commands), 1, commands)
        self.assertIn("serve", commands[0])

    def test_every_requested_op_exists(self):
        ops = set()
        for text in self.src.values():
            ops |= set(re.findall(r'request\("(\w+)"', text))
        self.assertTrue({"list", "add", "edit", "move", "color", "pin", "rm", "restore"} <= ops, ops)
        state = tempfile.mkdtemp(prefix="stickies-test-")
        # `integrate` writes integration.json to the state dir: never the real one.
        old = os.environ.get("STICKIES_STATE")
        os.environ["STICKIES_STATE"] = state
        try:
            store = stickies.Store(os.path.join(state, "stickies.db"))
            for op in ops:
                try:
                    stickies.dispatch(store, op, {})
                except stickies.StickiesError as e:
                    self.assertNotIn("unknown op", str(e))
            store.close()
        finally:
            if old is None:
                os.environ.pop("STICKIES_STATE", None)
            else:
                os.environ["STICKIES_STATE"] = old
            shutil.rmtree(state, ignore_errors=True)

    def test_six_palette_colors_known_to_cli(self):
        text = self.src["Service.qml"]
        names = re.search(r"paletteNames: \[(.*?)\]", text).group(1)
        names = re.findall(r'"(\w+)"', names)
        self.assertEqual(len(names), 6)
        hues = re.search(r"hues: \(\{(.*?)\}\)", text, re.S).group(1)
        for n in names:
            self.assertIn(n, stickies.COLORS)
            self.assertRegex(hues, rf"\b{n}: \"#[0-9a-f]{{6}}\"")

    def test_palette_follows_the_theme(self):
        # Named colours stay names in the DB; how they look comes from the
        # theme's background, which omarchy-shell swaps live.
        fill = re.search(r"function fillFor\(name\) \{(.*?)\n  \}", self.src["Service.qml"], re.S).group(1)
        self.assertIn("Color.background", fill)
        self.assertIn("Qt.tint(", fill)
        self.assertIn("readonly property color fill: service.fillFor(color)", self.src["NoteCard.qml"])


class PluginServeContractTest(TempState):
    """The exact requests Service.qml sends, and what it expects back."""

    def setUp(self):
        super().setUp()
        self.store = stickies.Store()

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def d(self, op, **a):
        return stickies.dispatch(self.store, op, a)

    def test_new_note_on_monitor_and_workspace(self):
        n = self.d("add", body="", monitor="eDP-1", workspace=3, x=1600, y=760)
        self.assertEqual((n["monitor"], n["workspace"], n["x"], n["y"]), ("eDP-1", 3, 1600, 760))
        n = self.d("add", body="", monitor=None, workspace=None, x=None, y=None)
        self.assertIsNone(n["workspace"])
        self.assertIsNone(n["monitor"])

    def test_drag_resize_raise_colour_pin(self):
        a = self.d("add", body="a")
        b = self.d("add", body="b")
        self.assertGreater(b["z"], a["z"])
        self.assertEqual(self.d("move", id=a["id"], x=10, y=20, monitor="HDMI-A-1")["monitor"], "HDMI-A-1")
        self.assertEqual(self.d("move", id=a["id"], w=300, h=150)["w"], 300)
        self.assertGreater(self.d("move", id=a["id"], **{"raise": True})["z"], b["z"])
        self.assertEqual(self.d("color", id=a["id"], color="purple")["color"], "purple")
        self.assertIs(self.d("pin", id=a["id"], pinned=True)["pinned"], True)
        self.assertIs(self.d("pin", id=a["id"], pinned=False)["pinned"], False)

    def test_archive_then_undo_returns_the_note(self):
        n = self.d("add", body="keep me")
        self.assertIsNotNone(self.d("rm", id=n["id"])["archived_at"])
        self.assertEqual([x["id"] for x in self.d("list")], [])
        r = self.d("restore", id=n["id"])
        # Service.qml upserts the restore result directly.
        self.assertIsInstance(r, dict)
        self.assertEqual((r["id"], r["archived_at"]), (n["id"], None))

    def test_debounced_edit_events(self):
        n = self.d("add", body="")
        seq = self.store.last_seq()
        for text in ("h", "he", "hello"):
            self.d("edit", id=n["id"], body=text)
        _, events = self.store.changes_since(seq)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["note"]["body"], "hello")


class BarWidgetTest(unittest.TestCase):
    """The bar widget reaches data only through its own plugin's service."""

    def setUp(self):
        self.src = qml_sources()
        with open(os.path.join(SHELL, "manifest.json")) as f:
            self.m = json.load(f)

    def test_widget_uses_own_service(self):
        w = self.src[self.m["entryPoints"]["barWidget"]]
        self.assertIn(f'moduleName: "{self.m["id"]}"', w)
        self.assertIn("serviceFor(moduleName)", w)
        self.assertNotIn("Process", w)

    def test_panel_does_not_claim_a_per_monitor_ipc_target(self):
        # One bar per monitor; an IPC target only routes to one handler.
        self.assertIn("manageIpc: false", self.src["BarPanel.qml"])
        self.assertNotIn("ipcTarget", self.src["BarPanel.qml"])

    def test_user_text_is_plain(self):
        self.assertIn("textFormat: Text.PlainText", self.src["NoteList.qml"])

    def test_ipc_methods_used_outside_exist(self):
        ipc = re.search(r'IpcHandler \{\s*target: "stickies"(.*?)\n  \}', self.src["Service.qml"], re.S).group(1)
        methods = set(re.findall(r"function (\w+)\(\): void", ipc))
        with open(os.path.join(ROOT, "hypr", "stickies.lua")) as f:
            used = set(re.findall(r'call\("(\w+)"\)', f.read()))
        used.add(stickies.HUB_LAUNCH.split()[-1])
        self.assertEqual(used, {"newNote", "front", "paste", "find", "chat", "cycleLayout", "collapse", "list"})
        self.assertTrue(used <= methods, methods)
        # `stickies shell` accepts every IPC method and nothing else.
        self.assertEqual(set(stickies.SHELL_METHODS), methods)


NODE = shutil.which("node")


class SearchOverlayTest(unittest.TestCase):
    """The overlay is part of the service (one serve connection), opened by
    `find`, and never renders note text as markup."""

    def setUp(self):
        self.src = qml_sources()

    def test_hosted_by_service_and_opened_by_find(self):
        svc = self.src["Service.qml"]
        self.assertIn("SearchOverlay {", svc)
        self.assertIn("function find(): void { searchOverlay.toggle() }", svc)
        ov = self.src["SearchOverlay.qml"]
        self.assertNotIn("Process", ov)
        self.assertIn("service.search(", ov)
        self.assertIn("service.goTo(nid, true)", ov)
        self.assertIn("WlrLayershell.keyboardFocus: keeper.keyboardFocus", ov)  # Exclusive, see test_overlay_focus

    def test_note_text_goes_through_the_escaping_helper(self):
        ov = self.src["SearchOverlay.qml"]
        self.assertIn('import "highlight.js" as Highlight', ov)
        styled = re.findall(r"textFormat: Text\.(\w+)", ov)
        self.assertEqual(styled.count("StyledText"), 1)
        self.assertIn("Highlight.styled(row.modelData.snippet", ov)

    def test_card_flashes_on_request(self):
        card = self.src["NoteCard.qml"]
        self.assertIn("onFlashRequestChanged", card)
        self.assertIn("service.flashRequest === nid", card)

    @unittest.skipUnless(NODE, "node not installed")
    def test_highlight_js(self):
        js = node_lib("highlight.js")
        cases = [
            ("<b>x</b> & fees", [[11, 15]], None),
            ("Café fees", [[0, 4]], None),
            ("😀 fees", [[2, 6]], None),
            ("a\nb fees", [[4, 8]], None),
            ("fees", [[-3, 99]], None),
            ("fees", [[0, 3], [1, 4]], None),
        ]
        script = js + "\nconst cases = " + json.dumps([[s, h] for s, h, _ in cases]) + ";\n" \
            "console.log(JSON.stringify(cases.map(c => styled(c[0], c[1], '#ff0000'))));\n" \
            "console.log(JSON.stringify(styled('x', [[0, 1]], 'red\" onload=\"')))"
        p = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        out, bad_color = [json.loads(line) for line in p.stdout.splitlines()]
        hl = '<b><font color="#ff0000">{}</font></b>'
        self.assertEqual(out[0], "&lt;b&gt;x&lt;/b&gt; &amp; " + hl.format("fees"))
        self.assertEqual(out[1], hl.format("Café") + " fees")
        self.assertEqual(out[2], "😀 " + hl.format("fees"))  # code points, not UTF-16 units
        self.assertEqual(out[3], "a b " + hl.format("fees"))
        self.assertEqual(out[4], hl.format("fees"))  # clamped to the text
        self.assertEqual(out[5], hl.format("fee") + hl.format("s"))  # overlap: no letter twice
        self.assertNotIn("onload", bad_color)


class ChatOverlayTest(unittest.TestCase):
    """The chat overlay rides the service's serve connection, sends only the
    ticked notes, applies nothing by itself and never renders model or note
    text as markup except through chat.js."""

    def setUp(self):
        self.src = qml_sources()
        self.ov = self.src["ChatOverlay.qml"]

    def test_hosted_by_service_and_opened_by_chat(self):
        svc = self.src["Service.qml"]
        self.assertIn("ChatOverlay {", svc)
        self.assertIn("function chat(): void { chatOverlay.toggle() }", svc)
        self.assertIn('msg.event === "chat"', svc)
        self.assertIn("chatOverlay.serveLost()", svc)
        self.assertNotIn("Process", self.ov)
        self.assertIn("WlrLayershell.keyboardFocus: keeper.keyboardFocus", self.ov)

    def test_sends_only_ticked_and_applies_only_on_click(self):
        self.assertIn("draft.notes.filter(n => draft.ticked[n.id]).map(n => n.id)", self.ov)
        self.assertIn("service.ask(cid, turn.question, ids, hist,", self.ov)
        # the only applyProposal call is in apply(), and apply() only runs from the Apply button
        self.assertEqual(self.ov.count("service.applyProposal("), 1)
        self.assertEqual(re.findall(r"win\.apply\(", self.ov), ["win.apply("])
        self.assertIn("onClicked: win.apply(turn.index, prop.index)", self.ov)
        for bad in ('"rm"', '"purge"', "archive("):
            self.assertNotIn(bad, self.ov)

    def test_markup_only_through_chat_js(self):
        self.assertIn('import "chat.js" as Chat', self.ov)
        styled = re.findall(r"textFormat: Text\.(\w+)", self.ov)
        self.assertEqual(styled.count("StyledText"), 1)
        self.assertIn("text: Chat.answerHtml(", self.ov)

    @unittest.skipUnless(NODE, "node not installed")
    def test_chat_js(self):
        js = node_lib("chat.js")
        script = js + """
console.log(JSON.stringify([
  answerHtml("Fees <b>5%</b> & up [#1], see [#2] and [#9]\\nok", [1, 2], "#ff0000"),
  answerHtml("[#1]", [1], 'red" onload="x'),
  answerHtml(null, [], "#ff0000"),
  answerHtml("due **before the 10th** [#2], 2 * 3 ** x", [2], "#ff0000"),
  noteFromLink("note:12"), noteFromLink("javascript:alert(1)"), noteFromLink("note:12x"),
  title("\\n  first line  \\nsecond", 60), title("abcdef", 4)
]))"""
        p = subprocess.run([NODE, "-e", script], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        html, bad, empty, bold, n1, n2, n3, t1, t2 = json.loads(p.stdout)
        link = '<a href="note:{0}"><font color="#ff0000">#{0}</font></a>'
        self.assertEqual(html, "Fees &lt;b&gt;5%&lt;/b&gt; &amp; up " + link.format(1) + ", see "
                         + link.format(2) + " and #9?<br>ok")
        self.assertNotIn("onload", bad)
        self.assertEqual(empty, "")
        self.assertEqual(bold, "due <b>before the 10th</b> " + link.format(2) + ", 2 * 3 ** x")
        self.assertEqual((n1, n2, n3), (12, -1, -1))
        self.assertEqual((t1, t2), ("first line", "abc…"))


def lua_binds(text):
    """Key combos bound in Omarchy's Lua config, normalised ("ALT+N+SUPER")."""
    return {"+".join(sorted(k.strip().upper() for k in keys.split("+")))
            for keys in re.findall(r'(?:o\.bind|hl\.bind)\(\s*"([^"]+)"', text)}


class KeybindingTest(unittest.TestCase):
    DROPIN = os.path.join(ROOT, "hypr", "stickies.lua")

    def setUp(self):
        with open(self.DROPIN) as f:
            self.text = f.read()
        self.mine = lua_binds(self.text)

    def test_eight_bindings(self):
        self.assertEqual(self.mine, {"ALT+N+SUPER", "ALT+N+SHIFT+SUPER", "ALT+SUPER+V", "ALT+J+SUPER",
                                     "A+ALT+SUPER", "ALT+L+SUPER", "ALT+SUPER+W", "ALT+O+SUPER"})
        self.assertIn("eastbluewizard.stickies keybindings", self.text)
        self.assertNotIn("unbind", self.text)  # adds keys, never replaces any

    @unittest.skipUnless(shutil.which("lua"), "lua not installed")
    def test_dropin_runs_with_omarchy_helpers(self):
        # o.bind as Omarchy's helpers.lua defines it, recording instead of binding.
        stub = ('o = {}; function o.bind(keys, desc, cmd) print(keys .. "\\t" .. desc .. "\\t" .. cmd) end; '
                'hl = {}; dofile(arg[1])')
        p = subprocess.run(["lua", "-e", stub, "-", self.DROPIN], input="", capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = [line.split("\t") for line in p.stdout.splitlines()]

        def call(m):  # no -q: a failed call falls through to the logging wrapper
            return f"omarchy-shell stickies {m} >/dev/null 2>&1 || stickies shell {m}"
        self.assertEqual(rows, [
            ["SUPER + ALT + N", "New sticky note", call("newNote")],
            ["SUPER + ALT + SHIFT + N", "Stickies above windows (toggle)", call("front")],
            ["SUPER + ALT + V", "Sticky note from the clipboard", call("paste")],
            ["SUPER + ALT + J", "Find a sticky note", call("find")],
            ["SUPER + ALT + A", "Ask your sticky notes", call("chat")],
            ["SUPER + ALT + L", "Stickies layout: free / waterfall right / left", call("cycleLayout")],
            ["SUPER + ALT + W", "Collapse the stickies column (toggle)", call("collapse")],
            ["SUPER + ALT + O", "All sticky notes", call("list")]])

    def test_keys_free_in_user_and_omarchy_config(self):
        files = glob.glob(os.path.expanduser("~/.config/hypr/*.lua"))
        files += glob.glob("/usr/share/omarchy/default/hypr/**/*.lua", recursive=True)
        if not files:
            self.skipTest("no Omarchy Hyprland config on this machine")
        taken = set()
        for path in files:
            with open(path, errors="replace") as f:
                taken |= lua_binds(f.read())
        self.assertGreater(len(taken), 20)  # the parser does see the real config
        self.assertFalse(self.mine & taken, self.mine & taken)


class InstallTest(unittest.TestCase):
    """install.sh / uninstall.sh (the git route) against a temp HOME, with
    every live-session command (plugin enable, hyprctl, hub, omarchy-shell)
    stubbed to log, and a stand-in onnxruntime venv: `setup --status`
    imports it, so a leak into ~/.cache/Microsoft shows up whether or not a
    real model is installed on this machine."""

    LIVE = ("omarchy-plugin-enable", "omarchy-plugin-disable", "omarchy-shell", "hyprctl", "hub", "pgrep")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stickies-inst-")
        self.home = os.path.join(self.tmp, "home")
        stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(stubs)
        self.calls = os.path.join(self.tmp, "calls.log")
        for name in self.LIVE:
            path = os.path.join(stubs, name)
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\n')
            os.chmod(path, 0o755)
        venv = fake_venv(os.path.join(self.tmp, "venv"))
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("STICKIES_", "XDG_", "ORT_"))}
        self.env.update(HOME=self.home, PATH=stubs + os.pathsep + os.environ["PATH"],
                        HYPRLAND_INSTANCE_SIGNATURE="fake", STICKIES_VENV=venv)
        self.plugins = os.path.join(self.home, ".config", "omarchy", "plugins")
        self.plugin = os.path.join(self.plugins, "eastbluewizard.stickies")
        self.bin = os.path.join(self.home, ".local", "bin", "stickies")
        self.dropin = os.path.join(self.home, ".local", "state", "omarchy", "toggles", "hypr", "stickies.lua")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sh(self, script, ok=True, where=None):
        p = subprocess.run([os.path.join(where or self.plugin, script)], env=self.env,
                           capture_output=True, text=True)
        if ok:
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p

    def live_calls(self):
        if not os.path.exists(self.calls):
            return ""
        with open(self.calls) as f:
            return f.read()

    def files(self):
        return sorted(os.path.relpath(os.path.join(d, f), self.home)
                      for d, _, fs in os.walk(self.home) for f in fs
                      if not os.path.join(d, f).startswith(self.plugin + os.sep))

    def test_install_and_uninstall_in_temp_home(self):
        git_checkout(self.plugin)
        self.sh("install.sh")
        self.assertEqual(os.readlink(self.bin), os.path.join(self.plugin, "stickies"))
        # a regular-file copy: Omarchy's loader skips symlinks (find -type f)
        self.assertFalse(os.path.islink(self.dropin))
        with open(self.dropin) as f, open(os.path.join(ROOT, "hypr", "stickies.lua")) as g:
            self.assertEqual(f.read(), g.read())
        self.sh("install.sh")  # idempotent
        self.assertIsNone(has_symlink(self.plugins))
        self.assertEqual(self.live_calls(), "", "a temp HOME must not touch the live session")
        self.sh("uninstall.sh")
        for path in (self.bin, self.dropin):
            self.assertFalse(os.path.lexists(path), path)
        self.assertEqual(self.live_calls(), "")
        # nothing left behind but empty dirs and the plugin checkout (omarchy plugin remove's)
        self.assertEqual(self.files(), [])

    @unittest.skipUnless(os.path.exists(VALIDATE), "omarchy-plugin-validate is not installed")
    def test_installed_folder_passes_omarchy_plugin_validate(self):
        git_checkout(self.plugin)
        self.sh("install.sh")
        p = subprocess.run([VALIDATE, self.plugin], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_refuses_to_run_outside_the_plugins_folder(self):
        elsewhere = git_checkout(os.path.join(self.tmp, "checkout"))
        p = self.sh("install.sh", ok=False, where=elsewhere)
        self.assertEqual(p.returncode, 1)
        self.assertIn("omarchy plugin add", p.stderr)
        self.assertFalse(os.path.exists(self.home))  # nothing made, not even the HOME's folders

    def test_explicit_overrides_still_work(self):
        tmp = os.path.join(self.tmp, "elsewhere")
        self.env.update(STICKIES_BIN=os.path.join(tmp, "bin"), STICKIES_PLUGINS=os.path.join(tmp, "plugins"),
                        STICKIES_HYPR_DIR=os.path.join(tmp, "hypr"))
        self.plugin = git_checkout(os.path.join(tmp, "plugins", "eastbluewizard.stickies"))
        self.sh("install.sh")
        self.assertTrue(os.path.isfile(os.path.join(tmp, "hypr", "stickies.lua")))
        self.assertEqual(os.readlink(os.path.join(tmp, "bin", "stickies")), os.path.join(self.plugin, "stickies"))
        self.sh("uninstall.sh")
        self.assertEqual(os.listdir(os.path.join(tmp, "hypr")), [])
        self.assertEqual(self.live_calls(), "")

    def test_xdg_state_home_moves_the_dropin(self):
        git_checkout(self.plugin)
        self.env["XDG_STATE_HOME"] = os.path.join(self.home, "state")
        self.sh("install.sh")
        self.assertTrue(os.path.isfile(os.path.join(self.home, "state", "omarchy", "toggles", "hypr", "stickies.lua")))
        self.assertFalse(os.path.exists(self.dropin))

    def test_foreign_dropin_left_alone(self):
        git_checkout(self.plugin)
        os.makedirs(os.path.dirname(self.dropin))
        with open(self.dropin, "w") as f:
            f.write('o.bind("SUPER + N", "mine", "true")\n')
        self.assertNotEqual(self.sh("install.sh", ok=False).returncode, 0)
        self.sh("uninstall.sh")
        with open(self.dropin) as f:
            self.assertIn("mine", f.read())

    def test_tests_never_migrate_a_plugins_folder_they_did_not_name(self):
        # Only $STICKIES_STATE redirected (as a test or bench does): the
        # plugins folder of that HOME is out of reach, symlink and all.
        checkout = git_checkout(os.path.join(self.tmp, "checkout"))
        os.makedirs(self.plugins)
        os.symlink(checkout, self.plugin)
        env = dict(self.env, STICKIES_STATE=os.path.join(self.tmp, "state"),
                   STICKIES_BIN=os.path.join(self.tmp, "bin"), STICKIES_RESTART_SHELL="true")
        for args in (["integrate", "--no"], ["integrate", "--yes"], ["uninstall"]):
            p = subprocess.run([os.path.join(checkout, "stickies"), *args], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(os.readlink(self.plugin), checkout, args)

    def test_symlink_install_migrates_to_a_clone_and_keeps_the_notes(self):
        # An install from before 1.3.0: the plugins folder links to a checkout.
        checkout = git_checkout(os.path.join(self.tmp, "Work", "stickies"))
        os.makedirs(self.plugins)
        os.symlink(checkout, self.plugin)
        os.makedirs(os.path.dirname(self.bin))
        os.symlink(os.path.join(checkout, "stickies"), self.bin)
        cli = [os.path.join(checkout, "stickies")]
        self.env["STICKIES_HUB"] = ""  # no hub card for the note added here
        nid = json.loads(subprocess.run(cli + ["add", "kept across the move", "--json"], env=self.env,
                                        capture_output=True, text=True, check=True).stdout)["id"]
        state = os.path.join(self.home, ".local", "state", "stickies")
        before = {n: os.path.getsize(os.path.join(state, n)) for n in os.listdir(state) if n.endswith(".db")}

        out = self.sh("install.sh", where=checkout).stdout
        self.assertIn("replaced the symlink", out)
        self.assertFalse(os.path.islink(self.plugin))
        self.assertTrue(os.path.isdir(os.path.join(self.plugin, ".git")))
        origin = subprocess.run(["git", "-C", self.plugin, "remote", "get-url", "origin"],
                                capture_output=True, text=True).stdout.strip()
        self.assertEqual(origin, checkout)  # `omarchy plugin update` pulls from the dev checkout
        self.assertIsNone(has_symlink(self.plugins))
        self.assertEqual(os.readlink(self.bin), os.path.join(self.plugin, "stickies"))
        self.assertEqual({n: os.path.getsize(os.path.join(state, n)) for n in before}, before)
        shown = json.loads(subprocess.run([self.bin, "show", str(nid), "--json"], env=self.env,
                                          capture_output=True, text=True, check=True).stdout)
        self.assertEqual(shown["body"], "kept across the move")
        self.assertEqual(self.live_calls(), "")
        self.assertNotIn("replaced", self.sh("install.sh").stdout)  # once


if __name__ == "__main__":
    unittest.main()
