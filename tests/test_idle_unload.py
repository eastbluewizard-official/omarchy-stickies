"""serve drops the embedding model after its idle time and loads it again
on demand. The lifecycle tests use a stdlib-only stand-in model that
stores no vectors (so search by meaning finds nothing and nothing needs
numpy); the ones that check by-meaning hits and note writes use
test_semantic's numpy fake and are skipped without numpy."""

import json
import os
import subprocess
import sys
import threading
import time

from helpers import SCRIPT, TempState, stickies
from test_semantic import FakeEmbedder, Lines, needs_numpy

IDLE = 0.005  # minutes: 0.3 s


class StubEmbedder:
    """A model that loads (optionally only once `gate` is set) and embeds
    nothing. Passed as the class, serve makes a new one per load."""
    name = "stub"
    spec = {"min_sim": 0.5}
    np = None
    gate = None
    made = 0

    def __init__(self):
        if StubEmbedder.gate is not None:
            StubEmbedder.gate.wait(5)
        StubEmbedder.made += 1

    def embed(self, texts, kind="doc"):
        return []

    def query(self, text):
        raise AssertionError("no vectors are stored, so no query is embedded")


class ServeRun:
    """serve on a thread with a pipe for requests; every line it writes,
    and every select() timeout it chose."""

    def __init__(self, test, embedder, preload=True):
        self.test = test
        self.rfd, self.wfd = os.pipe()
        self.out = Lines()
        self.waits = []
        self.rid = 0

        def run():
            store = stickies.Store()
            try:
                stickies.serve(store, infd=self.rfd, out=self.out, poll=0.05, hub_delay=0,
                               embedder=embedder, embed_delay=0.05, notify=None, preload=preload,
                               on_wait=self.waits.append)
            finally:
                store.close()

        self.t = threading.Thread(target=run, daemon=True)
        self.t.start()
        self.until(lambda: self.events("ready"), "serve never got ready")

    def msgs(self):
        return [json.loads(line) for line in list(self.out.lines)]

    def events(self, kind, **match):
        return [m for m in self.msgs() if m.get("event") == kind
                and all(m.get(k) == v for k, v in match.items())]

    def loads(self):
        return len(self.events("semantic", loaded=True))

    def unloads(self):
        """Loaded -> not loaded (the first event, at start, says "installed,
        not loaded yet")."""
        n, was = 0, False
        for m in self.events("semantic"):
            n += was and not m["loaded"]
            was = m["loaded"]
        return n

    def until(self, pred, what, timeout=5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r = pred()
            if r:
                return r
            time.sleep(0.01)
        self.test.fail(what)

    def ask(self, op, **args):
        self.rid += 1
        rid = self.rid
        os.write(self.wfd, (json.dumps({"id": rid, "op": op, "args": args}) + "\n").encode())
        return self.until(lambda: next((m for m in self.msgs() if m.get("id") == rid), None),
                          f"no answer to {op}")

    def stop(self):
        os.close(self.wfd)
        self.t.join(timeout=5)
        os.close(self.rfd)


class IdleTest(TempState):
    def setUp(self):
        super().setUp()
        self._env = os.environ.get("STICKIES_IDLE_UNLOAD")
        os.environ["STICKIES_IDLE_UNLOAD"] = str(IDLE)
        StubEmbedder.gate, StubEmbedder.made = None, 0
        self.runs = []

    def tearDown(self):
        for r in self.runs:
            r.stop()
        if self._env is None:
            os.environ.pop("STICKIES_IDLE_UNLOAD", None)
        else:
            os.environ["STICKIES_IDLE_UNLOAD"] = self._env
        super().tearDown()

    def serve(self, embedder=StubEmbedder, **kw):
        r = ServeRun(self, embedder, **kw)
        self.runs.append(r)
        return r


class IdleUnloadTest(IdleTest):
    def test_unloads_after_the_idle_time_then_no_timer(self):
        sv = self.serve()
        sv.until(sv.loads, "the model never loaded at start")
        self.assertTrue(sv.ask("ping")["result"]["semantic"])
        sv.until(sv.unloads, "the model was never unloaded")
        self.assertFalse(sv.ask("ping")["result"]["semantic"])
        # Loaded, serve slept at most until the unload was due...
        timed = [w for w in sv.waits if w is not None]
        self.assertTrue(timed and max(timed) <= IDLE * 60 + 0.01, sv.waits)
        # ...and once it is gone, without a timeout again.
        n = len(sv.waits)
        sv.ask("list")
        sv.until(lambda: len(sv.waits) > n, "serve never went back to select()")
        self.assertIsNone(sv.waits[-1])
        time.sleep(IDLE * 60 * 2)
        self.assertEqual(sv.unloads(), 1)
        self.assertEqual(sv.loads(), 1)

    def test_search_reloads_and_gets_words_meanwhile(self):
        pre = stickies.Store()
        dentist = pre.add("Dentist on Friday")["id"]
        pre.close()
        StubEmbedder.gate = threading.Event()
        StubEmbedder.gate.set()
        sv = self.serve()
        sv.until(sv.unloads, "the model was never unloaded")
        StubEmbedder.gate.clear()  # the next load takes until the test says so
        hits = sv.ask("search", query="dentist")["result"]
        self.assertEqual([(h["id"], h["match"]) for h in hits], [(dentist, "fts")])
        self.assertEqual(sv.loads(), 1, "a search never waits for the model")
        self.assertEqual(sv.ask("search", query="dentist")["result"][0]["id"], dentist)
        StubEmbedder.gate.set()
        sv.until(lambda: sv.loads() == 2, "the search never reloaded the model")
        self.assertEqual(StubEmbedder.made, 2, "a fresh model per load")
        self.assertTrue(sv.ask("ping")["result"]["semantic"])
        sv.until(lambda: sv.unloads() == 2, "the reloaded model never went idle")

    def test_words_only_search_never_loads(self):
        sv = self.serve()
        sv.until(sv.unloads, "the model was never unloaded")
        sv.ask("search", query="dentist", mode="fts")
        sv.ask("list")
        time.sleep(0.2)
        self.assertEqual(sv.loads(), 1)

    def test_searches_keep_it_loaded(self):
        sv = self.serve()
        sv.until(sv.loads, "the model never loaded")
        end = time.monotonic() + IDLE * 60 * 3
        while time.monotonic() < end:
            sv.ask("search", query="something")
            time.sleep(IDLE * 60 / 4)
        self.assertEqual(sv.unloads(), 0)
        sv.until(sv.unloads, "unused, it should go")

    def test_zero_means_never(self):
        os.environ["STICKIES_IDLE_UNLOAD"] = "0"
        sv = self.serve()
        sv.until(sv.loads, "the model never loaded")
        time.sleep(IDLE * 60 * 3)
        self.assertEqual(sv.unloads(), 0)
        self.assertTrue(sv.ask("ping")["result"]["semantic"])
        self.assertFalse(any(sv.waits), sv.waits)  # None, or 0 for the (disabled) hub card

    def test_setting_applies_to_a_running_serve(self):
        os.environ.pop("STICKIES_IDLE_UNLOAD")
        sv = self.serve()
        sv.until(sv.loads, "the model never loaded")
        time.sleep(IDLE * 60 * 2)
        self.assertEqual(sv.unloads(), 0, "the default is 10 minutes")
        sv.ask("set", values={"idle_unload": IDLE})
        sv.until(sv.unloads, "the new idle time was not used")

    def test_lazy_start_loads_on_first_search(self):
        sv = self.serve(preload=False)
        self.assertEqual(sv.events("semantic"), [{"event": "semantic", "on": True, "loaded": False}])
        time.sleep(0.2)
        self.assertEqual(sv.loads(), 0)
        self.assertIsNone(sv.waits[-1], "nothing loaded: no timer")
        sv.ask("search", query="anything")
        sv.until(sv.loads, "the first search never loaded the model")

    def test_no_model_no_events_no_timer(self):
        sv = self.serve(embedder=None)
        sv.ask("search", query="anything")
        time.sleep(0.1)
        self.assertEqual(sv.events("semantic"), [])
        self.assertFalse(any(sv.waits), sv.waits)

    def test_status_over_the_socket(self):
        sv = self.serve()
        sv.until(sv.loads, "the model never loaded")
        st = sv.until(lambda: stickies.ask_serve({"op": "model"}), "no status")
        self.assertTrue(st["loaded"])
        self.assertEqual(st["model"], "stub")
        self.assertEqual(st["idle_unload"], IDLE)
        self.assertTrue(st["loaded_since"] and st["unload_at"])
        sv.until(sv.unloads, "the model was never unloaded")
        st = stickies.ask_serve({"op": "model"})
        self.assertEqual((st["loaded"], st["loaded_since"], st["unload_at"]), (False, None, None))
        # A cold CLI search is refused (it searches itself) but loads it for the next one.
        self.assertIsNone(stickies.search_via_serve("anything"))
        sv.until(lambda: sv.loads() == 2, "a socket search never reloaded the model")


class SetupCliTest(TempState):
    def cli(self, *args, **env):
        p = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True,
                           env=dict(os.environ, STICKIES_HUB="", **env))
        return p

    def test_idle_unload_setting(self):
        os.environ.pop("STICKIES_IDLE_UNLOAD", None)
        r = json.loads(self.cli("setup", "--status", "--json").stdout)
        self.assertEqual((r["idle_unload"], r["idle_unload_from"], r["serve"]), (10, "setting", None))
        r = json.loads(self.cli("setup", "--idle-unload", "30", "--json").stdout)
        self.assertEqual(r["idle_unload"], 30)
        self.assertEqual(stickies.Store().settings()["idle_unload"], 30)
        p = self.cli("setup", "--status")
        self.assertIn("idle unload: after 30 min without a search", p.stdout)
        self.assertIn("serve is not running", p.stdout)
        self.assertIn("never (kept loaded)", self.cli("setup", "--idle-unload", "0").stdout)
        r = json.loads(self.cli("setup", "--status", "--json", STICKIES_IDLE_UNLOAD="5").stdout)
        self.assertEqual((r["idle_unload"], r["idle_unload_from"]), (5, "env"))
        p = self.cli("setup", "--idle-unload", "-1", "--json")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("error", json.loads(p.stdout))


@needs_numpy
class ReloadWithMeaningTest(IdleTest):
    """With a model that stores vectors: by-meaning hits come back after a
    reload; a note written while it is away is embedded once it is back,
    and a move (no new text) doesn't load it."""

    def test_meaning_after_reload_and_writes_backfilled(self):
        pre = stickies.Store()
        dentist = pre.add("Tandarts op vrijdag")["id"]
        pre.close()
        sv = self.serve(embedder=FakeEmbedder)
        sv.until(sv.unloads, "the model was never unloaded")
        self.assertEqual(sv.ask("search", query="dentist")["result"], [])  # words only meanwhile
        sv.until(lambda: sv.loads() == 2, "never reloaded")
        hits = sv.ask("search", query="dentist")["result"]
        self.assertEqual([(h["id"], h["match"]) for h in hits], [(dentist, "semantic")])
        sv.until(lambda: sv.unloads() == 2, "never unloaded again")

        sv.ask("move", id=dentist, x=40, y=40)
        time.sleep(0.3)
        self.assertEqual(sv.loads(), 2, "a move has no new text: no reload")

        milk = sv.ask("add", body="Melk en brood")["result"]["id"]
        sv.until(lambda: sv.loads() == 3, "a new note never loaded the model to embed it")
        hits = sv.until(lambda: sv.ask("search", query="groceries", mode="semantic")["result"],
                        "the note written while unloaded was not embedded")
        self.assertEqual([h["id"] for h in hits], [milk])
