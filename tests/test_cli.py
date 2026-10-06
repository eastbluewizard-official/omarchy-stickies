import json
import os
import select
import subprocess
import sys
import time
import unittest

from helpers import SCRIPT, TempState


class CliTest(TempState):
    def run_cli(self, *args, input=None, ok=True):
        p = subprocess.run([sys.executable, SCRIPT, *args], input=input or "",
                           capture_output=True, text=True, env=os.environ.copy())
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return p

    def j(self, *args, **kw):
        p = self.run_cli(*args, "--json", **kw)
        return json.loads(p.stdout)

    def test_roundtrip_json(self):
        n = self.j("add", "Cardmarket", "fees", "[OP-05]", "--color", "pink", "--pin")
        self.assertEqual(n["body"], "Cardmarket fees [OP-05]")
        self.assertEqual(n["color"], "pink")
        self.assertIs(n["pinned"], True)
        self.assertEqual(self.j("show", str(n["id"]))["body"], n["body"])
        self.assertEqual([x["id"] for x in self.j("list")], [n["id"]])
        hits = self.j("search", "cardm")
        self.assertEqual(hits[0]["id"], n["id"])
        self.assertIn("snippet", hits[0])
        self.assertIn("highlights", hits[0])

    def test_first_run_hint(self):
        self.assertEqual(self.j("list"), [])  # JSON stays a plain empty list
        hint = self.run_cli("list").stdout
        self.assertIn("no notes yet", hint)
        self.assertIn('stickies add', hint)
        self.assertIn("stickies setup", hint)
        self.assertEqual(self.run_cli("list", "--pinned").stdout.strip(), "no notes")
        self.assertIn("start here", self.run_cli("--help").stdout)
        n = self.j("add", "x")
        self.j("rm", str(n["id"]))
        # archived notes exist: not a first run any more, just an empty list
        self.assertEqual(self.run_cli("list").stdout.strip(), "no notes")

    def test_global_json_flag_position(self):
        self.j("add", "x")
        p = self.run_cli("--json", "list")
        self.assertEqual(len(json.loads(p.stdout)), 1)

    def test_stdin_body(self):
        n = self.j("add", input="from\nstdin\n")
        self.assertEqual(n["body"], "from\nstdin")
        e = self.j("edit", str(n["id"]), "--append", input="tail")
        self.assertEqual(e["body"], "from\nstdin\ntail")

    def test_write_commands(self):
        n = self.j("add", "note")
        i = str(n["id"])
        self.assertEqual(self.j("edit", i, "changed")["body"], "changed")
        self.assertEqual(self.j("color", i, "blue")["color"], "blue")
        self.assertIs(self.j("pin", i)["pinned"], True)
        self.assertIs(self.j("unpin", i)["pinned"], False)
        m = self.j("move", i, "--x", "5", "--y", "6", "--w", "300", "--workspace", "2")
        self.assertEqual((m["x"], m["y"], m["w"], m["workspace"]), (5, 6, 300, 2))
        self.assertIsNotNone(self.j("rm", i)[0]["archived_at"])
        self.assertEqual(self.j("list"), [])
        self.assertEqual(len(self.j("list", "--archived")), 1)
        self.assertIsNone(self.j("restore", i)[0]["archived_at"])
        self.j("rm", i)
        self.assertEqual(self.j("purge", "--all-archived"), [{"id": n["id"], "purged": True}])
        self.assertEqual(self.j("list", "--all"), [])

    def test_error_contract(self):
        p = self.run_cli("show", "42", "--json", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("error", json.loads(p.stdout))
        p = self.run_cli("color", "1", "nope", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("stickies:", p.stderr)
        n = self.j("add", "live")
        p = self.run_cli("purge", str(n["id"]), "--json", ok=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not archived", json.loads(p.stdout)["error"])

    def test_human_output(self):
        self.run_cli("add", "Boodschappen: melk [2x]")
        out = self.run_cli("search", "melk").stdout
        self.assertIn("[melk]", out)
        self.assertIn("Boodschappen", self.run_cli("list").stdout)


class ServeTest(TempState):
    def setUp(self):
        super().setUp()
        self.p = subprocess.Popen([sys.executable, SCRIPT, "serve"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True, bufsize=1,
                                  env=os.environ.copy())
        self.buf = b""
        self.assertEqual(self.read()["event"], "ready")
        self.next_id = 0

    def tearDown(self):
        self.p.stdin.close()
        self.p.wait(timeout=5)
        self.p.stdout.close()
        super().tearDown()

    def read(self, timeout=5):
        # Own line buffer over the raw fd: select() on a buffered reader
        # misses lines readline() already pulled in (a response and its
        # event often arrive in one chunk).
        fd = self.p.stdout.fileno()
        while b"\n" not in self.buf:
            r, _, _ = select.select([fd], [], [], timeout)
            self.assertTrue(r, "serve produced no output")
            chunk = os.read(fd, 65536)
            self.assertTrue(chunk, "serve exited")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def send(self, op, **args):
        self.next_id += 1
        self.p.stdin.write(json.dumps({"id": self.next_id, "op": op, "args": args}) + "\n")
        self.p.stdin.flush()
        msg = self.read()
        while "event" in msg:
            msg = self.read()
        self.assertEqual(msg["id"], self.next_id)
        return msg

    def test_request_response_and_events(self):
        r = self.send("add", body="from serve", color="green")
        self.assertTrue(r["ok"])
        nid = r["result"]["id"]
        ev = self.read()
        self.assertEqual((ev["event"], ev["kind"], ev["id"]), ("changed", "add", nid))
        hits = self.send("search", query="serv")["result"]
        self.assertEqual(hits[0]["id"], nid)
        self.send("move", id=nid, x=7, y=8, **{"raise": True})
        ev = self.read()
        self.assertEqual((ev["kind"], ev["note"]["x"]), ("update", 7))
        self.assertEqual(self.send("list")["result"][0]["y"], 8)
        self.send("rm", id=nid)
        self.assertEqual(self.read()["kind"], "archive")

    def test_errors_keep_serving(self):
        self.p.stdin.write("not json\n")
        self.p.stdin.flush()
        self.assertFalse(self.read()["ok"])
        r = self.send("show", id=99)
        self.assertFalse(r["ok"])
        self.assertIn("no note", r["error"])
        self.assertFalse(self.send("frobnicate")["ok"])
        self.assertTrue(self.send("ping")["ok"])

    def test_events_from_other_processes(self):
        subprocess.run([sys.executable, SCRIPT, "add", "agent wrote this"], check=True,
                       capture_output=True, env=os.environ.copy())
        deadline = time.time() + 5
        ev = self.read()
        while ev.get("kind") != "add" and time.time() < deadline:
            ev = self.read()
        self.assertEqual(ev["note"]["body"], "agent wrote this")


if __name__ == "__main__":
    unittest.main()
