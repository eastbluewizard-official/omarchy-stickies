"""Semantic + hybrid search, embeddings bookkeeping, serve's background
indexer, `stickies setup`/`backfill`. Logic runs against a deterministic
fake embedder (numpy only); tests that need the real model run the CLI
against the user's model cache and are skipped when it isn't installed."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from helpers import SCRIPT, TempState, stickies

try:
    import numpy as np
except ImportError:  # the CLI never needs numpy outside the .venv
    np = None

needs_numpy = unittest.skipIf(np is None, "numpy not importable in this Python (it lives in the .venv)")

# words -> concept; a text's vector is its normalised bag of concepts, so
# "dentist" and "tandarts" are near-identical and share no letters.
CONCEPTS = {
    "dentist": "teeth", "tandarts": "teeth", "teeth": "teeth", "kies": "teeth",
    "money": "money", "payout": "money", "geld": "money", "owed": "money", "transfer": "money",
    "fees": "cost", "fee": "cost", "commission": "cost", "kosten": "cost", "price": "cost",
    "milk": "food", "melk": "food", "brood": "food", "bread": "food", "groceries": "food",
}
AXES = sorted(set(CONCEPTS.values()))


class FakeEmbedder:
    name = "fake"
    spec = {"min_sim": 0.5}

    def __init__(self):
        self.np = np
        self.calls = 0

    def embed(self, texts, kind="doc"):
        self.calls += 1
        out = np.zeros((len(texts), len(AXES) + 1), dtype=np.float32)
        for r, t in enumerate(texts):
            for w in stickies._TOKEN.findall(t.lower()):
                c = CONCEPTS.get(w)
                if c:
                    out[r, AXES.index(c)] += 1
            out[r, -1] = 0.01  # never all-zero
            out[r] /= np.linalg.norm(out[r])
        return out

    def query(self, text):
        return self.embed([text], "query")[0]


@needs_numpy
class SemanticStoreTest(TempState):
    def setUp(self):
        super().setUp()
        self.emb = FakeEmbedder()
        self.s = stickies.Store(embedder=self.emb)
        self.dentist = self.s.add("Tandarts afspraak vrijdag 14:30")["id"]
        self.payout = self.s.add("Consignment payout for Jeroen before the 10th")["id"]
        self.fees = self.s.add("Cardmarket fees went up: 5% commission")["id"]
        self.milk = self.s.add("Boodschappen: melk, brood, kaas")["id"]
        self.s.add("")  # empty notes are never embedded
        self.assertEqual(self.s.embed_notes(self.emb), 4)

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def ids(self, q, **kw):
        return [h["id"] for h in self.s.search(q, **kw)]

    def test_semantic_finds_what_words_miss(self):
        self.assertEqual(self.ids("dentist appointment", mode="fts"), [])
        hits = self.s.search("dentist appointment", mode="semantic")
        self.assertEqual(hits[0]["id"], self.dentist)
        self.assertEqual(hits[0]["match"], "semantic")
        self.assertGreater(hits[0]["similarity"], 0.9)
        self.assertEqual(self.ids("money owed", mode="semantic"), [self.payout])

    def test_hybrid_fuses_and_labels(self):
        hits = self.s.search("cardmarket fees")
        self.assertEqual(hits[0]["id"], self.fees)
        self.assertEqual(hits[0]["match"], "both")
        self.assertIsNotNone(hits[0]["similarity"])
        # a meaning-only hit is in the same list, after it
        hits = self.s.search("fees milk")  # FTS needs both words: no FTS hit
        self.assertEqual({h["id"] for h in hits}, {self.fees, self.milk})
        self.assertTrue(all(h["match"] == "semantic" for h in hits))
        # same keys in every mode
        keys = set(self.s.search("cardmarket", mode="fts")[0])
        self.assertEqual(keys, set(hits[0]))
        self.assertEqual(keys, set(self.s.search("payout", mode="semantic")[0]))

    def test_semantic_snippet_highlights_query_words(self):
        h = self.s.search("payout money", mode="hybrid")[0]
        self.assertEqual(h["id"], self.payout)
        self.assertEqual(h["match"], "semantic")
        for a, b in h["highlights"]:
            self.assertEqual(h["snippet"][a:b].lower(), "payout")
        long = self.s.add("word " * 40 + "payout at the end " + "word " * 40)["id"]
        self.s.embed_notes(self.emb)
        text, spans = stickies.plain_snippet(self.s.get(long)["body"], "payout")
        self.assertTrue(text.startswith("…") and text.endswith("…"))
        self.assertEqual([text[a:b] for a, b in spans], ["payout"])
        self.assertEqual(stickies.plain_snippet("Café crème", "cafe")[1], [[0, 4]])
        self.assertEqual(stickies.plain_snippet("the dentist", "the")[1], [])  # stopword

    def test_short_queries_stay_fts(self):
        self.assertEqual(self.s.search("de", mode="semantic"), [])
        self.assertTrue(all(h["match"] == "fts" for h in self.s.search("ca")))

    def test_archived_excluded_unless_asked(self):
        self.s.archive(self.dentist)
        self.assertNotIn(self.dentist, self.ids("dentist", mode="semantic"))
        self.assertIn(self.dentist, self.ids("dentist", mode="semantic", archived=True))

    def test_edit_makes_embedding_stale_and_backfill_fixes_it(self):
        self.assertEqual(self.s.stale_embeddings("fake"), [])
        self.s.edit(self.milk, "Geld overmaken naar Jeroen")
        self.s.move(self.fees, x=5)  # geometry doesn't touch the vector
        self.assertEqual([i for i, _, _ in self.s.stale_embeddings("fake")], [self.milk])
        self.assertEqual(self.s.embed_notes(self.emb), 1)
        self.assertIn(self.milk, self.ids("money", mode="semantic"))
        # a different model makes everything stale
        self.assertEqual(len(self.s.stale_embeddings("other")), 4)

    def test_purge_cascades(self):
        self.s.archive(self.payout)
        self.s.purge(self.payout)
        n = self.s.db.execute("SELECT COUNT(*) FROM embeddings WHERE note_id=?", (self.payout,)).fetchone()[0]
        self.assertEqual(n, 0)

    def test_vectors_written_by_another_connection_are_seen(self):
        new = self.s.add("Kies getrokken, tandarts rekening betalen")["id"]
        other = stickies.Store(self.s.path, embedder=self.emb)
        other.embed_notes(self.emb)
        other.close()
        self.assertIn(new, self.ids("teeth", mode="semantic"))

    def test_stats(self):
        st = self.s.embedding_stats("fake")
        self.assertEqual((st["notes"], st["embedded"], st["stale"]), (4, 4, 0))

    def test_unknown_mode(self):
        with self.assertRaises(stickies.StickiesError):
            self.s.search("x", mode="vibes")


class DegradeTest(TempState):
    """No model installed (TempState's empty STICKIES_CACHE)."""

    def test_hybrid_is_fts_without_model(self):
        s = stickies.Store()
        a = s.add("Cardmarket fees went up")["id"]
        s.add("Cardmarket fees fees fees")
        self.assertIsNone(s.embedder)
        fts = [h["id"] for h in s.search("fees", mode="fts")]
        hits = s.search("fees")
        self.assertEqual([h["id"] for h in hits], fts)
        self.assertTrue(all(h["match"] == "fts" and h["similarity"] is None for h in hits))
        self.assertIn(a, fts)
        with self.assertRaises(stickies.StickiesError) as e:
            s.search("fees", mode="semantic")
        self.assertIn("stickies setup", str(e.exception))
        s.close()

    def run_cli(self, *args, input=""):
        return subprocess.run([sys.executable, SCRIPT, *args], input=input, capture_output=True,
                              text=True, env=dict(os.environ, STICKIES_HUB=""))

    def test_cli_modes(self):
        self.run_cli("add", "Tandarts vrijdag")
        p = self.run_cli("search", "tandarts", "--json")
        self.assertEqual(json.loads(p.stdout)[0]["match"], "fts")
        p = self.run_cli("search", "tandarts", "--mode", "semantic", "--json")
        self.assertEqual(p.returncode, 1)
        self.assertIn("stickies setup", json.loads(p.stdout)["error"])
        p = self.run_cli("backfill", "--json")
        self.assertEqual(p.returncode, 1)
        self.assertIn("error", json.loads(p.stdout))
        self.assertNotEqual(self.run_cli("search", "x", "--mode", "vibes").returncode, 0)

    def test_setup_status_and_consent(self):
        p = self.run_cli("setup", "--status", "--json")
        r = json.loads(p.stdout)
        self.assertEqual(r["model"], stickies.DEFAULT_MODEL)
        self.assertTrue(r["model_missing"])
        self.assertFalse(r["ready"])
        # not a tty and no --yes: refuses before fetching anything (an
        # unroutable mirror proves no download was attempted)
        p = subprocess.run([sys.executable, SCRIPT, "setup", "--json"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True,
                           env=dict(os.environ, STICKIES_HF="http://127.0.0.1:9", STICKIES_HUB=""))
        self.assertEqual(p.returncode, 1)
        self.assertIn("--yes", json.loads(p.stdout)["error"])
        self.assertIn("huggingface.co", p.stderr)  # the plan was shown
        # ... and why: what it is for, that it stays local, its cost, how to undo
        for words in ("by what it says", "words only", "never sent", "Wi-Fi off", "RAM", "Undo"):
            self.assertIn(words, p.stderr)
        self.assertFalse(os.path.exists(stickies.model_dir(stickies.DEFAULT_MODEL)))

    def test_model_choice_is_remembered(self):
        self.assertEqual(stickies.model_name(), stickies.DEFAULT_MODEL)
        os.makedirs(stickies.cache_dir())
        with open(stickies.active_model_file(), "w") as f:
            f.write("minilm-l6-q8\n")
        self.assertEqual(stickies.model_name(), "minilm-l6-q8")
        self.assertEqual(stickies.model_name("bge-small-en"), "bge-small-en")
        with self.assertRaises(stickies.StickiesError):
            stickies.model_name("nope")


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stickies-dl-")
        self.src = os.path.join(self.tmp, "src.bin")
        with open(self.src, "wb") as f:
            f.write(b"x" * 3000)
        self.sha = hashlib.sha256(b"x" * 3000).hexdigest()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_verified_download(self):
        dest = os.path.join(self.tmp, "m", "model.onnx")
        stickies.download("file://" + self.src, dest, 3000, self.sha)
        self.assertEqual(os.path.getsize(dest), 3000)

    def test_bad_hash_or_size_leaves_nothing(self):
        dest = os.path.join(self.tmp, "m", "model.onnx")
        with self.assertRaises(stickies.StickiesError):
            stickies.download("file://" + self.src, dest, 3000, "0" * 64)
        with self.assertRaises(stickies.StickiesError):
            stickies.download("file://" + self.src, dest, 2999, None)
        self.assertEqual(os.listdir(os.path.dirname(dest)), [])

    def test_every_model_file_is_pinned(self):
        for name, spec in stickies.MODELS.items():
            self.assertRegex(spec["rev"], r"^[0-9a-f]{40}$", name)
            for local, (remote, size, sha) in spec["files"].items():
                self.assertGreater(size, 0)
                self.assertRegex(sha or "", r"^[0-9a-f]{64}$", f"{name}/{local}")
                self.assertIn(spec["rev"], stickies.model_url(name, remote))
        self.assertIn(stickies.DEFAULT_MODEL, stickies.MODELS)


class SchemaUpgradeTest(TempState):
    def test_v1_db_gains_embeddings_table(self):
        s = stickies.Store()
        s.add("old note")
        s.db.execute("DROP TABLE embeddings")
        s.db.execute("PRAGMA user_version=1")
        s.close()
        s = stickies.Store()
        self.assertEqual(s.db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 0)
        self.assertEqual(s.search("old")[0]["body"], "old note")
        s.close()


@needs_numpy
class ServeIndexerTest(TempState):
    """serve embeds in the background: existing notes at start, then each
    note shortly after it changes, from this process or another."""

    def test_embed_on_write(self):
        emb = FakeEmbedder()
        pre = stickies.Store()
        old = pre.add("Tandarts op vrijdag")["id"]
        pre.close()
        rfd, wfd = os.pipe()
        out = Lines()

        def run():
            store = stickies.Store()
            try:
                stickies.serve(store, infd=rfd, out=out, poll=0.05, hub_delay=60,
                               embedder=emb, embed_delay=0.05)
            finally:
                store.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        rid = [0]

        def ask(op, **args):
            rid[0] += 1
            os.write(wfd, (json.dumps({"id": rid[0], "op": op, "args": args}) + "\n").encode())
            end = time.monotonic() + 5
            while time.monotonic() < end:
                for line in list(out.lines):
                    msg = json.loads(line)
                    if msg.get("id") == rid[0]:
                        return msg
                time.sleep(0.01)
            self.fail(f"no answer to {op}")

        def until(pred, what):
            end = time.monotonic() + 5
            while time.monotonic() < end:
                if pred():
                    return
                time.sleep(0.05)
            self.fail(what)

        try:
            until(lambda: ask("ping")["result"]["semantic"], "indexer never loaded the model")
            until(lambda: [h["id"] for h in ask("search", query="dentist", mode="semantic")["result"]] == [old],
                  "existing note not backfilled")
            new = ask("add", body="Payout to Jeroen")["result"]["id"]
            until(lambda: new in [h["id"] for h in ask("search", query="money owed", mode="semantic")["result"]],
                  "new note not embedded")
            ask("edit", id=new, body="Melk en brood")
            until(lambda: new in [h["id"] for h in ask("search", query="groceries", mode="semantic")["result"]],
                  "edited note not re-embedded")
            # written by another process (an agent)
            subprocess.run([sys.executable, SCRIPT, "add", "Commission fees for the shop"], check=True,
                           capture_output=True, env=dict(os.environ, STICKIES_HUB=""))
            until(lambda: len(ask("search", query="price", mode="semantic")["result"]) == 1,
                  "other process's note not embedded")
            hits = ask("search", query="melk")["result"]  # hybrid is the default op mode too
            self.assertEqual(hits[0]["match"], "both")
        finally:
            os.close(wfd)
            t.join(timeout=5)
            os.close(rfd)



@needs_numpy
class ServeSocketTest(TempState):
    """A cold `stickies search` asks the running serve (model already
    loaded) over $STICKIES_STATE/serve.sock instead of loading the model."""

    def start_serve(self, emb):
        rfd, wfd = os.pipe()
        out = Lines()

        def run():
            store = stickies.Store()
            try:
                stickies.serve(store, infd=rfd, out=out, poll=0.05, hub_delay=60,
                               embedder=emb, embed_delay=0.05)
            finally:
                store.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()

        def stop():
            os.close(wfd)
            t.join(timeout=5)
            os.close(rfd)
        return stop

    def until(self, pred, what):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            r = pred()
            if r:
                return r
            time.sleep(0.05)
        self.fail(what)

    def test_cli_search_goes_through_serve(self):
        pre = stickies.Store()
        dentist = pre.add("Tandarts op vrijdag")["id"]
        pre.add("Cardmarket fees")
        pre.close()
        sock = stickies.search_socket_path()
        stop = self.start_serve(FakeEmbedder())
        try:
            self.until(lambda: os.path.exists(sock), "no socket")
            self.assertEqual(os.stat(sock).st_mode & 0o777, 0o600)
            hits = self.until(lambda: stickies.search_via_serve("dentist", mode="semantic"),
                              "serve never answered with meaning")
            self.assertEqual([h["id"] for h in hits], [dentist])
            # The CLI process has no model (empty STICKIES_CACHE), so a
            # meaning-only hit can only have come from serve.
            p = subprocess.run([sys.executable, SCRIPT, "search", "dentist", "--json"],
                               capture_output=True, text=True, env=dict(os.environ, STICKIES_HUB=""))
            self.assertEqual(p.returncode, 0, p.stderr)
            r = json.loads(p.stdout)
            self.assertEqual([(h["id"], h["match"]) for h in r], [(dentist, "semantic")])
            # words-only never needs it; the same answer as before
            p = subprocess.run([sys.executable, SCRIPT, "search", "cardm", "--mode", "fts", "--json"],
                               capture_output=True, text=True, env=dict(os.environ, STICKIES_HUB=""))
            self.assertEqual(len(json.loads(p.stdout)), 1)
            # anything but search is refused
            import socket
            c = socket.socket(socket.AF_UNIX)
            c.connect(sock)
            c.sendall(b'{"op": "add", "args": {"body": "x"}}\n')
            resp = json.loads(c.makefile().readline())
            c.close()
            self.assertFalse(resp["ok"])
            self.assertEqual(len(stickies.Store().list()), 2)
        finally:
            stop()
        self.assertFalse(os.path.exists(sock), "serve leaves no socket behind")
        self.assertIsNone(stickies.search_via_serve("dentist"))

    def test_not_answered_before_the_model_is_loaded(self):
        sock = stickies.search_socket_path()
        stop = self.start_serve(None)  # no model: serve stays words-only
        try:
            self.until(lambda: os.path.exists(sock), "no socket")
            self.assertIsNone(stickies.search_via_serve("anything"))
        finally:
            stop()

    def test_stale_socket_is_replaced_and_a_live_one_kept(self):
        import socket
        sock = stickies.search_socket_path()
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(sock)
        stale.close()  # file left behind, nobody listening
        lst = stickies.open_search_socket(sock)
        self.assertIsNotNone(lst)
        self.assertIsNone(stickies.open_search_socket(sock))  # live: not stolen
        lst.close()
        os.unlink(sock)


class Lines:
    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines.append(s)

    def flush(self):
        pass


def real_model_problem():
    """Why the real-model tests can't run, or None."""
    try:
        name = stickies.model_name()
    except stickies.StickiesError as e:
        return str(e)
    if stickies.missing_model_files(name):
        return f"embedding model {name} not downloaded to {stickies.cache_dir()} (run `stickies setup`)"
    if not (stickies.deps_importable() or stickies.venv_python()):
        return "no onnxruntime/tokenizers and no .venv (run `stickies setup`)"
    return None


_REAL = real_model_problem()


@unittest.skipIf(_REAL, _REAL)
class RealModelTest(unittest.TestCase):
    """The installed model, through the CLI (system python re-execs into
    the .venv), on a temp STICKIES_STATE."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="stickies-test-")
        self.env = dict(os.environ, STICKIES_STATE=self.state, STICKIES_HUB="")
        self.env.pop("STICKIES_REEXEC", None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.state, ignore_errors=True)

    def cli(self, *args):
        p = subprocess.run([sys.executable, SCRIPT, *args, "--json"], capture_output=True, text=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        return json.loads(p.stdout)

    def test_cross_language_and_paraphrase(self):
        notes = {
            "dentist": "Tandarts afspraak vrijdag 14:30, verwijzing meenemen.",
            "fees": "Cardmarket fees went up: 5% commission on every sale now.",
            "groceries": "Boodschappen: melk, brood, kaas, eieren.",
            "payout": "Consignment payout for Jeroen: 14 cards sold, transfer before the 10th.",
        }
        ids = {k: self.cli("add", v)["id"] for k, v in notes.items()}
        r = self.cli("backfill")
        self.assertEqual((r["embedded"], r["stale"]), (4, 0))
        for q, want in (("dentist appointment", "dentist"),
                        ("how much does the marketplace take per sale", "fees"),
                        ("wat moet ik kopen in de supermarkt", "groceries"),
                        ("money owed to people whose cards I sell", "payout")):
            hits = self.cli("search", q)
            self.assertEqual(hits[0]["id"], ids[want], (q, hits[:2]))
        hits = self.cli("search", "cardmarket")
        self.assertEqual((hits[0]["id"], hits[0]["match"]), (ids["fees"], "both"))


if __name__ == "__main__":
    unittest.main()
