"""Note text never reaches a child process's argv.

/proc/<pid>/cmdline is readable by every local user, so whatever stickies
passes as arguments to notify-send, hub or anything else leaks to `ps`.
Each call site that used to (reminders, the hub card, chat's hub todo) runs
here against a stand-in that records its argv and stdin, with a note that
holds a known secret, and the secret must never show up in argv.
Reminders go over D-Bus itself; DBusTest checks that client against a
private dbus-daemon (never the session's bus) with a fake notification
server. And a reminder's notification holds no note words at all, on any
path: the notification server keeps what it shows (Omarchy's writes it to
0644 files and a `bash -c` argument); serve hands the words to the plugin."""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from helpers import SCRIPT, TempState, stickies

SECRET = "zebraquartz Jeroen owes 40 EUR"

RECORDER = """#!{py}
import json, os, sys
stdin = sys.stdin.read()
with open({log!r}, "a") as f:
    f.write(json.dumps({{"argv": sys.argv, "stdin": stdin}}) + "\\n")
if sys.argv[1:3] == ["todo", "add"]:
    print(json.dumps({{"id": 1}}))
"""


class ArgvTest(TempState):
    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.state, "calls.jsonl")
        self.bin = os.path.join(self.state, "bin")
        os.mkdir(self.bin)
        self.fake = self.stand_in("recorder")
        self._env = {k: os.environ.get(k) for k in
                     ("STICKIES_NOTIFY", "STICKIES_HUB", "DBUS_SESSION_BUS_ADDRESS", "PATH")}
        self.s = stickies.Store(embedder=None)

    def tearDown(self):
        self.s.close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def stand_in(self, name, body=None):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write(body or RECORDER.format(py=sys.executable, log=self.log))
        os.chmod(path, 0o755)
        return path

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f if line.strip()]

    def assert_argv_clean(self, calls):
        self.assertTrue(calls, "the stand-in was never run")
        for c in calls:
            for a in c["argv"]:
                self.assertNotIn("zebraquartz", a, c["argv"])
                self.assertNotIn("Jeroen", a, c["argv"])

    def overdue_reminder(self):
        n = self.s.add(f"{SECRET}\n@ {(datetime.now() + timedelta(hours=1)):%Y-%m-%d %H:%M} {SECRET}")
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="milliseconds")
        self.s.db.execute("UPDATE reminders SET due=?", (past.replace("+00:00", "Z"),))
        return n

    def test_reminder_stand_in_never_gets_the_note(self):
        n = self.overdue_reminder()
        os.environ["STICKIES_NOTIFY"] = self.fake
        self.assertEqual(len(self.s.fire_reminders(stickies.send_notification)), 1)
        calls = self.calls()
        self.assert_argv_clean(calls)
        self.assertEqual(calls[0]["stdin"], f"Sticky note reminder\nReminder for sticky note #{n['id']}\n")

    def test_serve_hands_the_words_to_the_plugin(self):
        n = self.overdue_reminder()
        os.environ["STICKIES_NOTIFY"] = self.fake
        rfd, wfd = os.pipe()
        out = io.StringIO()

        def run():
            store = stickies.Store()
            try:
                stickies.serve(store, infd=rfd, out=out, poll=0.05, hub_delay=3600, embedder=None,
                               remind_every=3600)
            finally:
                store.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        try:
            deadline = time.time() + 5
            while '"reminder"' not in out.getvalue() and time.time() < deadline:
                time.sleep(0.02)
        finally:
            os.close(wfd)
            t.join(timeout=5)
            os.close(rfd)
        events = [json.loads(line) for line in out.getvalue().splitlines()]
        ev = [e for e in events if e.get("event") == "reminder"]
        self.assertEqual([(e["id"], e["text"]) for e in ev], [(n["id"], SECRET)])
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("zebraquartz", json.dumps(calls))

    def test_without_a_bus_notify_send_never_gets_the_note(self):
        n = self.overdue_reminder()
        self.stand_in("notify-send")
        os.environ.pop("STICKIES_NOTIFY", None)
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={self.state}/no-bus"
        os.environ["PATH"] = self.bin + os.pathsep + os.environ["PATH"]
        real = stickies.notify_bin
        stickies.notify_bin = lambda: "dbus"  # as outside tests
        try:
            self.s.fire_reminders(stickies.send_notification)
        finally:
            stickies.notify_bin = real
        calls = self.calls()
        self.assert_argv_clean(calls)
        self.assertNotIn("zebraquartz", calls[0]["stdin"])
        self.assertEqual(calls[0]["argv"][-1], f"Reminder for sticky note #{n['id']}")
        with open(os.path.join(self.state, "stickies.log")) as f:
            self.assertIn("notification over D-Bus failed", f.read())

    def test_hub_card_holds_no_note_text(self):
        self.s.add(SECRET)
        os.environ["STICKIES_HUB"] = self.fake
        self.assertTrue(stickies.publish_hub(self.s, explicit=True)["published"])
        p = subprocess.run([sys.executable, SCRIPT, "hub", "--json"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("zebraquartz", p.stdout)
        self.assert_argv_clean(self.calls())
        self.assertEqual(len(self.calls()), 2)

    def test_hub_todo_title_goes_on_stdin(self):
        os.environ["STICKIES_HUB"] = self.fake
        r = stickies.apply_proposal(self.s, {"action": "hub_todo", "title": SECRET, "due": "2026-10-12"})
        self.assertEqual(r["todo"], {"id": 1})
        calls = self.calls()
        self.assert_argv_clean(calls)
        self.assertEqual(calls[0]["argv"][1:], ["todo", "add", "--title-stdin", "--json", "--due", "2026-10-12"])
        self.assertEqual(calls[0]["stdin"], SECRET + "\n")

    def test_an_old_hub_is_an_error_not_an_argv_fallback(self):
        os.environ["STICKIES_HUB"] = self.stand_in("old-hub", RECORDER.format(py=sys.executable, log=self.log)
                                                    + "sys.stderr.write('usage: hub todo add [-h] title\\n"
                                                      "hub todo add: error: the following arguments are "
                                                      "required: title\\n'); sys.exit(2)\n")
        with self.assertRaisesRegex(stickies.StickiesError, "update hub"):
            stickies.apply_proposal(self.s, {"action": "hub_todo", "title": SECRET})
        calls = self.calls()
        self.assertEqual(len(calls), 1)  # tried once, never again with the title in argv
        self.assert_argv_clean(calls)


class StdinQueryTest(TempState):
    def cli(self, *args, stdin=None, ok=True):
        p = subprocess.run([sys.executable, SCRIPT, *args], input=stdin, capture_output=True, text=True,
                           env=dict(os.environ, STICKIES_HUB="", STICKIES_SEMANTIC="0"))
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return p

    def test_search_reads_the_query_from_stdin(self):
        self.cli("add", "--stdin", stdin=SECRET)
        for args in (["search", "--json"], ["search", "--stdin", "--json"]):
            hits = json.loads(self.cli(*args, stdin="zebraquartz\n").stdout)
            self.assertEqual([h["body"] for h in hits], [SECRET])

    def test_ask_reads_the_question_from_stdin(self):
        out = json.loads(self.cli("ask", "--dry-run", "--json", stdin="what does Jeroen owe?\n").stdout)
        self.assertEqual(out["question"], "what does Jeroen owe?")

    def test_words_and_stdin_together_or_neither(self):
        p = self.cli("search", "x", "--stdin", "--json", stdin="y", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("not both", json.loads(p.stdout)["error"])
        p = self.cli("ask", "--json", stdin="  \n", ok=False)
        self.assertIn("no question on stdin", json.loads(p.stdout)["error"])

    def test_reexec_hands_stdin_on_not_argv(self):
        # maybe_reexec under the venv's Python: the query read from stdin
        # must reach the new image on fd 0, and argv stays what it was
        code = (f"import json, os, sys; sys.path.insert(0, {os.path.dirname(__file__)!r})\n"
                "from helpers import stickies\n"
                "stickies.venv_python = lambda: '/venv/python'\n"
                "stickies.missing_model_files = lambda m: []\n"
                "stickies.deps_importable = lambda: False\n"
                "stickies.search_via_serve = lambda *a, **k: None\n"
                "def execv(py, argv):\n"
                "    print(json.dumps({'argv': argv, 'stdin': sys.stdin.read()})); sys.exit(0)\n"
                "stickies.os.execv = execv\n"
                "sys.exit(stickies.main(['search', '--json']))\n")
        env = dict(os.environ, STICKIES_HUB="")
        env.pop("STICKIES_SEMANTIC", None)
        env.pop("STICKIES_REEXEC", None)
        p = subprocess.run([sys.executable, "-c", code], input=SECRET, capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["stdin"], SECRET)
        self.assertEqual(out["argv"][2:], ["search", "--json"])


class FakeNotificationServer(threading.Thread):
    """Owns org.freedesktop.Notifications on a private bus, answers one
    Notify with id 42 and keeps its strings."""

    def __init__(self, address):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(address)
        self.sock.sendall(b"\0AUTH EXTERNAL " + str(os.getuid()).encode().hex().encode() + b"\r\nBEGIN\r\n")
        line = b""
        while not line.endswith(b"\r\n"):
            line += self.sock.recv(1)
        assert line.startswith(b"OK "), line
        self.buf = bytearray()
        stickies.dbus_call(self.sock, self.buf, 1, "/org/freedesktop/DBus", "org.freedesktop.DBus",
                           "Hello", "org.freedesktop.DBus")
        w = stickies._DBusWriter()
        w.string("org.freedesktop.Notifications")
        w.u32(4)  # DO_NOT_QUEUE
        reply, _ = stickies.dbus_call(self.sock, self.buf, 2, "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                      "RequestName", "org.freedesktop.DBus", "su", bytes(w.b))
        assert reply[:4] == b"\x01\0\0\0", reply  # PRIMARY_OWNER
        self.got = None

    def run(self):
        import struct
        while True:
            typ, serial, fields, body, e = stickies.dbus_read(self.sock, self.buf)
            if typ == 1 and fields.get(3) == "Notify":
                break
        got = {"signature": fields.get(8), "strings": [], "hints": {}}
        pos = 0

        def string(pos):
            pos += -pos % 4
            n = struct.unpack_from(e + "I", body, pos)[0]
            return body[pos + 4:pos + 4 + n].decode(), pos + 5 + n
        for t in "susss":
            if t == "u":
                pos += -pos % 4
                got["replaces_id"] = struct.unpack_from(e + "I", body, pos)[0]
                pos += 4
            else:
                v, pos = string(pos)
                got["strings"].append(v)
        pos += -pos % 4  # actions (as): skipped
        pos += 4 + struct.unpack_from(e + "I", body, pos)[0]
        pos += -pos % 4  # hints (a{sv}): string variants only
        n = struct.unpack_from(e + "I", body, pos)[0]
        pos += 4
        pos += -pos % 8
        end = pos + n
        while pos < end:
            pos += -pos % 8
            k, pos = string(pos)
            sig = body[pos + 1:pos + 1 + body[pos]].decode()
            pos += 2 + body[pos]
            assert sig == "s", sig
            got["hints"][k], pos = string(pos)
        w = stickies._DBusWriter()  # METHOD_RETURN (u 42) to the caller
        w.b += b"l\x02\x00\x01"
        w.u32(4)
        w.u32(3)

        def header():  # REPLY_SERIAL, DESTINATION (the sender), SIGNATURE
            for code, typ, put, val in ((5, "u", w.u32, serial), (6, "s", w.string, fields[7]),
                                        (8, "g", w.sig, "u")):
                w.align(8)
                w.byte(code)
                w.sig(typ)
                put(val)
        w.array(8, header)
        w.align(8)
        self.sock.sendall(bytes(w.b) + struct.pack("<I", 42))
        self.got = got
        self.sock.close()


def die_with_parent():
    """The private bus goes with the test run, even one killed before its
    clean-ups ran (PR_SET_PDEATHSIG)."""
    import ctypes
    import signal
    try:
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL)
    except (OSError, AttributeError):
        pass


@unittest.skipUnless(shutil.which("dbus-daemon"), "needs dbus-daemon for a private bus")
class DBusTest(unittest.TestCase):
    # Our own config, not --session's: no service directories, so the bus
    # never starts anything (dconf-service and the like) on activation.
    CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={address}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stickies-bus-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.address = os.path.join(self.tmp, "bus")
        conf = os.path.join(self.tmp, "bus.conf")
        with open(conf, "w") as f:
            f.write(self.CONFIG.format(address=self.address))
        self.daemon = subprocess.Popen(["dbus-daemon", f"--config-file={conf}", "--nofork", "--print-address"],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                       preexec_fn=die_with_parent)
        # registered at once, so a setUp or test that fails still stops it
        self.addCleanup(self.stop_daemon)
        self.daemon.stdout.readline()  # listening
        self._env = {k: os.environ.get(k) for k in ("DBUS_SESSION_BUS_ADDRESS", "STICKIES_NOTIFY")}
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={self.address},guid=0123"

    def stop_daemon(self):
        self.daemon.kill()
        self.daemon.wait()
        self.daemon.stdout.close()

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_notify_reaches_the_server_without_a_child_process(self):
        server = FakeNotificationServer(self.address)
        server.start()
        real_bin, real_run = stickies.notify_bin, subprocess.run
        stickies.notify_bin = lambda: "dbus"

        def no_children(*a, **kw):
            raise AssertionError(f"a child process was started: {a}")
        subprocess.run = no_children
        try:
            self.assertTrue(stickies.send_notification("Sticky note reminder", SECRET, public="#1"))
        finally:
            stickies.notify_bin, subprocess.run = real_bin, real_run
        server.join(timeout=5)
        self.assertEqual(server.got, {"signature": "susssasa{sv}i", "replaces_id": 0, "hints": {},
                                      "strings": ["Stickies", "accessories-text-editor",
                                                  "Sticky note reminder", SECRET]})

    def test_a_reminder_tells_the_server_only_which_note(self):
        tmp = tempfile.mkdtemp(prefix="stickies-state-")
        self.addCleanup(shutil.rmtree, tmp, True)
        old_state = os.environ.get("STICKIES_STATE")
        os.environ["STICKIES_STATE"] = tmp
        s = stickies.Store(embedder=None)
        try:
            n = s.add(f"{SECRET}\n@ {(datetime.now() + timedelta(hours=1)):%Y-%m-%d %H:%M} {SECRET}")
            s.db.execute("UPDATE reminders SET due=?", ("2000-01-01T00:00:00.000Z",))
            server = FakeNotificationServer(self.address)
            server.start()
            real_bin = stickies.notify_bin
            stickies.notify_bin = lambda: "dbus"
            try:
                fired = s.fire_reminders(stickies.send_notification)
            finally:
                stickies.notify_bin = real_bin
            server.join(timeout=5)
        finally:
            s.close()
            if old_state is None:
                os.environ.pop("STICKIES_STATE", None)
            else:
                os.environ["STICKIES_STATE"] = old_state
        self.assertEqual(fired[0]["text"], SECRET)  # for serve's event
        self.assertNotIn("zebraquartz", json.dumps(server.got))
        self.assertEqual(server.got["strings"][3], f"Reminder for sticky note #{n['id']}")
        self.assertTrue(server.got["strings"][2].startswith("Sticky note reminder (missed"))
        # a click on the toast shows the note: its id, no words
        self.assertEqual(json.loads(server.got["hints"]["omarchy-exec-argv"]),
                         ["omarchy-shell", "stickies", "showNote", str(n["id"])])

    def test_notify_returns_the_id(self):
        server = FakeNotificationServer(self.address)
        server.start()
        self.assertEqual(stickies.dbus_notify("Stickies", "x", "t", "ü " * 300), 42)
        server.join(timeout=5)
        self.assertEqual(server.got["strings"][3], "ü " * 300)

    def test_no_notification_server_is_a_clear_error(self):
        # the daemon checked our messages (it drops a malformed one) and answers
        with self.assertRaisesRegex(stickies.StickiesError, "ServiceUnknown"):
            stickies.dbus_notify("Stickies", "x", "t", "b")

    def test_bus_address_forms(self):
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:abstract=/tmp/dbus-x,guid=1;unix:path=/b"
        self.assertEqual(stickies.session_bus_socket(), "\0/tmp/dbus-x")
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = "tcp:host=localhost,port=1"
        with self.assertRaisesRegex(stickies.StickiesError, "unsupported"):
            stickies.session_bus_socket()
        os.environ.pop("DBUS_SESSION_BUS_ADDRESS")
        self.assertTrue(stickies.session_bus_socket().endswith("/bus"))


if __name__ == "__main__":
    unittest.main()
