"""The plugin on its own (`omarchy plugin add` route): the first-run offer's
backend (`stickies integrate`), runtime keys through `hyprctl eval`, and
`stickies uninstall` leaving the HOME as it was. Also: serve learns about
other processes' writes from a poke on its socket, with no polling.

hyprctl is a fake ($STICKIES_HYPRCTL) that keeps its binds in a JSON file
and understands the hl.bind / hl.unbind lines stickies sends; nothing here
reaches the live compositor."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from helpers import ROOT, TempState, plugin_copy, stickies
from test_e2e import snapshot

FAKE_HYPRCTL = r'''#!{py}
import json, re, sys
path = {binds!r}
binds = json.load(open(path))
open({log!r}, "a").write(json.dumps(sys.argv[1:]) + "\n")
MODS = {{"SHIFT": 1, "CTRL": 4, "ALT": 8, "SUPER": 64}}
def combo(keys):
    *mods, key = [k.strip().upper() for k in keys.split("+")]
    return sum(MODS[m] for m in mods), key
cmd = sys.argv[1]
if cmd == "binds":
    print(json.dumps(binds))
elif cmd == "eval":
    lua = sys.argv[2]
    for keys, run, desc in re.findall(r'hl\.bind\("([^"]+)", hl\.dsp\.exec_cmd\("((?:[^"\\]|\\.)*)"\), \{{ description = "([^"]+)" \}}\)', lua):
        m, k = combo(keys)
        binds.append({{"modmask": m, "key": k, "description": desc, "dispatcher": "__lua", "arg": run}})
    for keys in re.findall(r'hl\.unbind\("([^"]+)"\)', lua):
        binds = [b for b in binds if (b["modmask"], b["key"]) != combo(keys)]
    json.dump(binds, open(path, "w"))
    print("ok")
else:
    print("ok")
'''


class FakeHyprland:
    def __init__(self, tmp, binds=()):
        self.binds = os.path.join(tmp, "binds.json")
        self.log = os.path.join(tmp, "hyprctl.log")
        with open(self.binds, "w") as f:
            json.dump(list(binds), f)
        self.exe = os.path.join(tmp, "hyprctl")
        with open(self.exe, "w") as f:
            f.write(FAKE_HYPRCTL.format(py=sys.executable, binds=self.binds, log=self.log))
        os.chmod(self.exe, 0o755)

    def current(self):
        with open(self.binds) as f:
            return json.load(f)

    def ours(self):
        mine = {d for _, d, _ in stickies.KEYS}
        return sorted(b["description"] for b in self.current() if b["description"] in mine)


ALL = sorted(k for k, _, _ in stickies.KEYS)
NOT_J = [k for k in ALL if k != "SUPER + ALT + J"]


class IntegrateTest(TempState):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="stickies-int-")
        self.hypr = FakeHyprland(self.tmp, [
            {"modmask": 64, "key": "RETURN", "description": "Terminal"},
            {"modmask": 72, "key": "J", "description": "Someone else's J"}])
        self._env = {k: os.environ.get(k) for k in ("STICKIES_HYPRCTL", "STICKIES_BIN", "STICKIES_HYPR_DIR",
                                                    "STICKIES_PLUGINS")}
        os.environ.update(STICKIES_HYPRCTL=self.hypr.exe, STICKIES_BIN=os.path.join(self.tmp, "bin"),
                          STICKIES_HYPR_DIR=os.path.join(self.tmp, "hypr"),
                          STICKIES_PLUGINS=os.path.join(self.tmp, "plugins"))

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def test_first_start_offers_once(self):
        st = stickies.integration_status()
        self.assertFalse(st["asked"])
        self.assertEqual(st["keys"], "off")
        self.assertEqual(st["link"]["state"], "missing")
        r = stickies.integrate(False)
        self.assertTrue(r["asked"])
        self.assertEqual(self.hypr.ours(), [])  # "Not now" binds nothing
        self.assertFalse(os.path.lexists(stickies.bin_link_path()))
        self.assertEqual(stickies.dispatch(stickies.Store(), "keys_on", {}), {"keys": "off"})

    def test_yes_binds_free_keys_and_links_the_command(self):
        r = stickies.integrate(True)
        self.assertEqual(r["link"]["state"], "ours")
        self.assertEqual(os.readlink(stickies.bin_link_path()), stickies.launcher_path())
        res = r["result"]
        self.assertEqual(res["keys"], "runtime")
        self.assertEqual(sorted(res["bound"]), NOT_J)
        self.assertEqual(res["taken"], [{"keys": "SUPER + ALT + J", "by": "Someone else's J"}])
        self.assertNotIn("Find a sticky note", self.hypr.ours())  # their J stays theirs
        # the key runs the plugin, then this launcher by path (no PATH needed)
        new = [b for b in self.hypr.current() if b["description"] == "New sticky note"][0]
        self.assertIn("omarchy-shell stickies newNote", new["arg"])
        self.assertIn(stickies.launcher_path() + " shell newNote", new["arg"])
        # idempotent: a second keys_on (shell restart, config reload) adds nothing
        again = stickies.keys_on()
        self.assertEqual(again["bound"], [])
        self.assertEqual(len(again["already"]), len(NOT_J))
        self.assertEqual(len(self.hypr.ours()), len(NOT_J))

    def test_config_reload_drops_binds_and_keys_on_restores_them(self):
        stickies.integrate(True)
        with open(self.hypr.binds, "w") as f:  # what `hyprctl reload` does to runtime binds
            json.dump([], f)
        r = stickies.dispatch(stickies.Store(), "keys_on", {})
        self.assertEqual(sorted(r["bound"]), ALL)
        self.assertIn('hl.unbind("SUPER + ALT + N")', r["unbind_lua"])

    def test_dropin_wins(self):
        os.makedirs(os.path.dirname(stickies.dropin_path()))
        shutil.copy(os.path.join(ROOT, "hypr", "stickies.lua"), stickies.dropin_path())
        self.assertTrue(stickies.integration_status()["asked"])  # install.sh route: no offer
        r = stickies.integrate(True)
        self.assertEqual(r["result"]["keys"], "dropin")
        self.assertFalse(any("eval" in line for line in open(self.hypr.log)) if os.path.exists(self.hypr.log) else False)

    def test_same_keys_as_the_dropin(self):
        # runtime binds and hypr/stickies.lua: the same keys, descriptions, methods
        import re
        with open(os.path.join(ROOT, "hypr", "stickies.lua")) as f:
            lua = re.findall(r'o\.bind\("([^"]+)", "([^"]+)", call\("(\w+)"\)\)', f.read())
        self.assertEqual(sorted(lua), sorted(stickies.KEYS))
        for _, _, method in stickies.KEYS:
            self.assertIn(method, stickies.SHELL_METHODS)

    def test_keys_off_only_removes_ours(self):
        stickies.integrate(True)
        self.assertEqual(sorted(stickies.keys_off()["removed"]), NOT_J)
        self.assertEqual(self.hypr.ours(), [])
        self.assertEqual(sorted(b["description"] for b in self.hypr.current()), ["Someone else's J", "Terminal"])

    def test_foreign_command_left_alone(self):
        os.makedirs(os.path.dirname(stickies.bin_link_path()))
        with open(stickies.bin_link_path(), "w") as f:
            f.write("#!/bin/sh\necho another stickies\n")
        r = stickies.integrate(True)
        self.assertEqual(r["link"]["state"], "taken")
        stickies.uninstall()
        with open(stickies.bin_link_path()) as f:
            self.assertIn("another", f.read())

    def test_stale_link_from_a_moved_checkout_is_replaced(self):
        os.makedirs(os.path.dirname(stickies.bin_link_path()))
        os.symlink(os.path.join(self.tmp, "gone", "stickies.py"), stickies.bin_link_path())
        self.assertEqual(stickies.link_status()["state"], "stale")
        self.assertEqual(stickies.make_link()["state"], "ours")

    def test_no_hyprland_no_calls(self):
        os.environ.pop("STICKIES_HYPRCTL")
        os.environ["HOME"], home = self.tmp, os.environ["HOME"]
        try:
            self.assertIsNone(stickies.hyprctl("binds", "-j"))  # a temp HOME never reaches the compositor
            self.assertEqual(stickies.integrate(True)["result"]["keys"], "unavailable")
        finally:
            os.environ["HOME"] = home


class PluginRouteEndToEnd(unittest.TestCase):
    """`omarchy plugin add`: a real folder in the plugins dir (no install.sh),
    the desktop's Set up, use, then `stickies uninstall --purge` + removing
    the folder leave the HOME exactly as before. Nothing is ever written
    inside the plugin folder (omarchy-shell would reload the plugin)."""

    def test_plugin_add_setup_use_uninstall(self):
        tmp = tempfile.mkdtemp(prefix="stickies-plug-")
        try:
            home = os.path.join(tmp, "home")
            for d in (".local/bin", ".config/omarchy/plugins", ".local/state/omarchy/toggles/hypr", ".cache"):
                os.makedirs(os.path.join(home, d))
            before = snapshot(home)
            plugin = plugin_copy(os.path.join(home, ".config/omarchy/plugins/eastbluewizard.stickies"))
            plugin_before = snapshot(plugin)
            hypr = FakeHyprland(tmp)
            env = {k: v for k, v in os.environ.items() if not k.startswith(("STICKIES_", "XDG_"))}
            env.update(HOME=home, STICKIES_HYPRCTL=hypr.exe, STICKIES_HUB="", STICKIES_VENV="")
            cmd = os.path.join(plugin, "stickies")

            def run(*args):
                p = subprocess.run([cmd, *args, "--json"], env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                return json.loads(p.stdout)

            self.assertFalse(run("integrate")["asked"])
            r = run("integrate", "--yes")
            self.assertEqual(sorted(r["result"]["bound"]), ALL)
            link = os.path.join(home, ".local/bin/stickies")
            self.assertEqual(os.readlink(link), cmd)
            self.assertEqual(len(hypr.ours()), len(ALL))
            p = subprocess.run([link, "add", "via the link", "--json"], env=env, capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(len(run("search", "link")), 1)
            self.assertEqual(snapshot(plugin), plugin_before, "something wrote inside the plugin folder")

            r = run("uninstall", "--purge")
            self.assertEqual(r["next"], ["omarchy plugin remove eastbluewizard.stickies"])
            self.assertEqual(hypr.ours(), [])
            shutil.rmtree(plugin)  # what `omarchy plugin remove` does
            self.assertEqual(snapshot(home), before)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ServeNoPollTest(TempState):
    def test_reminder_fires_when_due_without_a_tick(self):
        # remind_every=3600: serve must wake at the reminder's due time itself
        s = stickies.Store()
        s.add("Dentist\n@ tomorrow 09:00 leave")
        due = (stickies.datetime.now(stickies.timezone.utc) + stickies.timedelta(seconds=0.6)) \
            .isoformat(timespec="milliseconds").replace("+00:00", "Z")
        s.db.execute("UPDATE reminders SET due=?", (due,))
        s.close()
        rfd, wfd = os.pipe()
        sent = []

        class Out:
            def write(self, s):
                pass

            def flush(self):
                pass

        def run():
            store = stickies.Store()
            try:
                stickies.serve(store, infd=rfd, out=Out(), poll=3600, hub_delay=3600, embedder=None,
                               notify=lambda title, text: sent.append((time.monotonic(), text)),
                               remind_every=3600)
            finally:
                store.close()

        start = time.monotonic()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        try:
            while not sent and time.monotonic() - start < 5:
                time.sleep(0.02)
            self.assertEqual([x for _, x in sent], ["leave"])
            self.assertLess(sent[0][0] - start, 2.0)
        finally:
            os.close(wfd)
            t.join(timeout=5)
            os.close(rfd)

    def test_other_process_write_arrives_without_polling(self):
        rfd, wfd = os.pipe()
        lines = []

        class Out:
            def write(self, s):
                lines.extend(json.loads(x) for x in s.splitlines() if x)

            def flush(self):
                pass

        # poll=3600: if serve relied on polling, the event would never come
        def run():
            store = stickies.Store()  # sqlite objects stay on their thread
            try:
                stickies.serve(store, infd=rfd, out=Out(), poll=3600, hub_delay=3600, embedder=None)
            finally:
                store.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        sock = os.path.join(self.state, stickies.SEARCH_SOCKET)
        deadline = time.monotonic() + 5
        while not os.path.exists(sock) and time.monotonic() < deadline:
            time.sleep(0.01)
        start = time.monotonic()
        p = subprocess.run([sys.executable, os.path.join(ROOT, "stickies.py"), "add", "from elsewhere"],
                           env=dict(os.environ, STICKIES_HUB=""), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        while not any(m.get("event") == "changed" for m in lines) and time.monotonic() - start < 5:
            time.sleep(0.01)
        ev = [m for m in lines if m.get("event") == "changed"]
        self.assertEqual(len(ev), 1, lines)
        self.assertEqual(ev[0]["note"]["body"], "from elsewhere")
        os.close(wfd)
        t.join(timeout=5)
        os.close(rfd)


if __name__ == "__main__":
    unittest.main()
