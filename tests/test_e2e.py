"""End to end in a throwaway HOME, the git route: a checkout of the plugin
in ~/.config/omarchy/plugins/eastbluewizard.stickies, its install.sh (the
`stickies` command + keybinding drop-in, no symlink in the plugins folder,
omarchy-plugin-validate passes on it), first run, add / search / ask (a
fake agent) / apply through the installed `stickies`, then uninstall.sh: the
HOME must match a snapshot taken after the clone, except for the notes,
which only --purge deletes; nothing is ever written inside the checkout.

Every live-session command (plugin enable/disable, hyprctl, omarchy-shell,
hub, pgrep) is a stub that logs; the stubs, the fake agent and the fake
.venv live outside the temp HOME so they are not part of the snapshot."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT, VALIDATE, git_checkout, has_symlink
LIVE = ("omarchy-plugin-enable", "omarchy-plugin-disable", "omarchy-shell", "hyprctl", "hub", "pgrep")

# Answers from the notes it was sent: cites the first one, proposes a note.
FAKE_AGENT = r'''#!{py}
import json, os, re, sys
prompt = sys.stdin.read()
open(os.environ["FAKE_LOG"], "a").write(prompt + "\n\0\n")
ids = re.findall(r'<note id="(\d+)"', prompt)
print(f"The seller fee is 5% [#{{ids[0]}}]." if ids else "No notes were sent.")
print("```stickies-actions")
print(json.dumps([{{"action": "new_note", "body": "Recheck singles margins", "color": "green"}}]))
print("```")
'''


def snapshot(root):
    """{relative path: what it is}: dirs, symlinks (target), files (sha1 + mode)."""
    out = {}
    for d, dirs, files in os.walk(root):
        for name in dirs + files:
            p = os.path.join(d, name)
            rel = os.path.relpath(p, root)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = "link -> " + os.readlink(p)
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = "dir"
            else:
                with open(p, "rb") as f:
                    out[rel] = f"file {oct(st.st_mode & 0o777)} {hashlib.sha1(f.read()).hexdigest()}"
    return out


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stickies-e2e-")
        self.home = os.path.join(self.tmp, "home")
        # What any Omarchy HOME already has; install must not need to make them.
        for d in (".local/bin", ".config/omarchy/plugins", ".config/hypr",
                  ".local/state/omarchy/toggles/hypr", ".cache"):
            os.makedirs(os.path.join(self.home, d))
        with open(os.path.join(self.home, ".config/hypr/bindings.lua"), "w") as f:
            f.write('o.bind("SUPER + RETURN", "Terminal", "uwsm app -- $TERMINAL")\n')
        # `git clone <url> ~/.config/omarchy/plugins/eastbluewizard.stickies`
        self.plugin = git_checkout(os.path.join(self.home, ".config/omarchy/plugins/eastbluewizard.stickies"))

        stubs = os.path.join(self.tmp, "stubs")
        os.makedirs(stubs)
        self.calls = os.path.join(self.tmp, "calls.log")
        for name in LIVE:
            path = os.path.join(stubs, name)
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\necho "{name} $*" >> "{self.calls}"\n')
            os.chmod(path, 0o755)
        self.agent_log = os.path.join(self.tmp, "agent.log")
        agent = os.path.join(self.tmp, "fake-agent")
        with open(agent, "w") as f:
            f.write(FAKE_AGENT.format(py=sys.executable))
        os.chmod(agent, 0o755)
        # A stand-in for the .venv `stickies setup` makes (never the repo's own).
        self.venv = os.path.join(self.tmp, "venv")
        os.makedirs(self.venv)
        open(os.path.join(self.venv, "pyvenv.cfg"), "w").close()

        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("STICKIES_", "XDG_"))}
        self.env.update(HOME=self.home, PATH=stubs + os.pathsep + os.environ["PATH"],
                        HYPRLAND_INSTANCE_SIGNATURE="fake", STICKIES_AGENT=agent,
                        STICKIES_VENV=self.venv, FAKE_LOG=self.agent_log)
        self.before = snapshot(self.home)
        self.cli = os.path.join(self.home, ".local/bin/stickies")
        self.state = os.path.join(self.home, ".local/state/stickies")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_(self, *argv, ok=True, stdin=None):
        p = subprocess.run(list(argv), env=self.env, capture_output=True, text=True, input=stdin,
                           cwd=self.tmp, timeout=60)
        if ok:
            self.assertEqual(p.returncode, 0, f"{argv}: {p.stdout}{p.stderr}")
        return p

    def stickies(self, *args):
        p = self.run_(self.cli, *args, "--json")
        return json.loads(p.stdout)

    def live_calls(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return f.read().splitlines()

    def test_install_use_uninstall_leaves_nothing(self):
        # -- install
        out = self.run_(os.path.join(self.plugin, "install.sh")).stdout
        self.assertIn("stickies setup", out)  # points at the optional download, doesn't do it
        self.assertEqual(os.readlink(self.cli), os.path.join(self.plugin, "stickies"))
        self.assertIsNone(has_symlink(os.path.dirname(self.plugin)))
        if os.path.exists(VALIDATE):
            v = self.run_(VALIDATE, self.plugin, ok=False)
            self.assertEqual(v.returncode, 0, v.stderr)
        dropin = os.path.join(self.home, ".local/state/omarchy/toggles/hypr/stickies.lua")
        self.assertTrue(os.path.isfile(dropin) and not os.path.islink(dropin))
        with open(os.path.join(self.home, ".config/hypr/bindings.lua")) as f:
            self.assertNotIn("stickies", f.read())  # hand-written config untouched
        self.assertEqual(self.live_calls(), [], "a temp HOME must not touch the live session")

        # -- first run: empty, with a hint; JSON stays a plain empty list
        self.assertEqual(self.stickies("list"), [])
        hint = self.run_(self.cli, "list").stdout
        self.assertIn("no notes yet", hint)
        self.assertIn("stickies add", hint)
        self.assertIn("stickies setup", self.run_(self.cli, "--help").stdout)
        status = self.stickies("setup", "--status")
        self.assertTrue(status["model_missing"])  # nothing downloaded by install
        self.assertTrue(status["model_dir"].startswith(self.home))

        # -- add / search / ask / apply through the installed CLI
        fees = self.stickies("add", "Cardmarket fees went up: 5% seller fee", "-c", "pink")["id"]
        self.stickies("add", "Boodschappen: melk, brood, kaas")
        self.run_(self.cli, "add", "--stdin", stdin="OP-09 preorder [2 displays]\n")
        self.assertEqual(len(self.stickies("list")), 3)
        hits = self.stickies("search", "cardm", "fee")
        self.assertEqual([h["id"] for h in hits], [fees])
        self.assertEqual(hits[0]["match"], "fts")  # no model: words only, no error
        self.assertEqual(len(self.stickies("search", "[2")), 1)

        r = self.stickies("ask", "what did I write about the Cardmarket fees?")
        self.assertEqual(r["sent"][0]["id"], fees)
        self.assertEqual(r["citations"], [fees])
        self.assertEqual(len(r["proposals"]), 1)
        with open(self.agent_log) as f:
            sent = f.read()
        self.assertIn("5% seller fee", sent)
        self.assertNotIn("Boodschappen", sent)  # only the notes that match went out
        self.assertEqual(len(self.stickies("list")), 3)  # nothing applied yet
        p = {k: v for k, v in r["proposals"][0].items() if k != "summary"}
        self.assertEqual(self.stickies("apply", json.dumps(p))["note"]["color"], "green")
        self.assertEqual(len(self.stickies("list")), 4)
        self.assertTrue(os.path.isfile(os.path.join(self.state, "stickies.db")))
        self.assertEqual(self.stickies("hub")["count"], 4)  # the card goes to the stubbed hub
        self.assertIn("hub module set --name stickies", "\n".join(self.live_calls()))

        # -- uninstall: everything back as it was, except the notes
        out = self.run_(os.path.join(self.plugin, "uninstall.sh")).stdout
        self.assertIn("kept your notes", out)
        self.assertIn("omarchy plugin remove eastbluewizard.stickies", out)  # the checkout is the plugin
        after = snapshot(self.home)
        extra = {k for k in after if k not in self.before}
        self.assertTrue(extra, "the notes should still be there")
        self.assertTrue(all(k.startswith(".local/state/stickies") for k in extra), sorted(extra))
        self.assertEqual({k: v for k, v in after.items() if k in self.before}, self.before)
        self.assertEqual(set(self.before) - set(after), set())
        self.assertTrue(os.path.isdir(self.venv))

        # -- --purge: byte-for-byte the HOME from before install
        self.run_(os.path.join(self.plugin, "uninstall.sh"), "--purge")
        self.assertEqual(snapshot(self.home), self.before)
        self.assertFalse(os.path.exists(self.venv))
        shutil.rmtree(self.plugin)  # what `omarchy plugin remove` does to a git checkout
        self.assertFalse(any(c.split()[0] in ("omarchy-plugin-enable", "omarchy-plugin-disable",
                                              "omarchy-shell", "hyprctl", "pgrep")
                             for c in self.live_calls()), self.live_calls())

    def test_install_refuses_outside_the_plugins_folder(self):
        p = self.run_(os.path.join(ROOT, "install.sh"), ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("omarchy plugin add", p.stderr)
        self.assertEqual(snapshot(self.home), self.before)

    def test_purge_never_touches_home_itself(self):
        self.env["STICKIES_STATE"] = self.home
        self.env["STICKIES_CACHE"] = self.home + "/"
        self.run_(os.path.join(ROOT, "uninstall.sh"), "--purge")
        self.assertEqual(snapshot(self.home), self.before)

    def test_unknown_argument_is_refused(self):
        p = self.run_(os.path.join(ROOT, "uninstall.sh"), "--purg", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(snapshot(self.home), self.before)


if __name__ == "__main__":
    unittest.main()
