#!/usr/bin/env python3
"""Search benchmark: N generated notes in a throwaway STICKIES_STATE.

    python3 bench.py [--notes 2000] [--json] [--no-semantic]

Measures FTS search in-process, as a `stickies serve` round-trip (what the
shell plugin pays), and as a cold `stickies search` process (what an agent
pays). Typing is simulated by searching every prefix of a few words.

When the embedding model is installed (`stickies setup`; STICKIES_CACHE is
honoured) it also measures the semantic side on the same notes: backfill,
hybrid search through serve (every query embedded fresh, as when typing),
a note becoming findable by meaning after a write, serve's RSS, and a cold
hybrid `stickies search` process, with serve running (it asks serve over its
socket) and without (it loads the model itself).
"""

import argparse
import json
import os
import random
import select
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "stickies.py")
# A cold CLI process is timed as a person or agent runs it: the system
# Python on the byte-code-caching launcher (it re-executes under the venv
# itself when it needs the model).
CLI = ["/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3", os.path.join(HERE, "stickies")]

WORDS = """cardmarket fee fees verzending pakket booster display sealed singles
consignment payout klant factuur btw aangifte kwartaal deadline boodschappen
melk brood kaas afspraak tandarts vrijdag maandag weekend luffy zoro nami
sanji leader manga anime trading card grading psa bgs centering prijs korting
voorraad scan webcam inventory stock reprint preorder release japanse engelse
idee project notitie herinnering bellen mailen betalen ontvangen retour
schade verzekering postnl dhl track trace ophalen winkel beurs toernooi
deck meta lijst bestelling marge winst omzet kosten huur energie""".split()

QUERIES = ["cardmarket", "btw kwartaal", "luffy leader", "postnl track",
           "fees", "deadline vrijdag", "zzzz-no-hit", "prijs korting voorraad"]
TYPED = ["cardmarket", "consignment", "boodschappen"]  # every prefix of these


def make_notes(n, seed=8550):
    rnd = random.Random(seed)
    notes = []
    for _ in range(n):
        k = rnd.randint(8, 120)
        words = [rnd.choice(WORDS) for _ in range(k)]
        if rnd.random() < 0.2:
            words.insert(rnd.randrange(k), f"[OP-{rnd.randint(1, 12):02d}]")
        notes.append(" ".join(words))
    return notes


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def summary(ms):
    return {"n": len(ms), "median_ms": round(statistics.median(ms), 3),
            "p95_ms": round(pct(ms, 95), 3), "max_ms": round(max(ms), 3)}


def load_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("stickies", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def serve_proc():
    """A `stickies serve` and a request function returning (result, ms)."""
    p = subprocess.Popen([sys.executable, SCRIPT, "serve"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True, bufsize=1)
    fd, buf, rid = p.stdout.fileno(), [b""], [0]

    def readline():
        while b"\n" not in buf[0]:
            r, _, _ = select.select([fd], [], [], 30)
            if not r:
                raise RuntimeError("serve timed out")
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                raise RuntimeError("serve exited")
            buf[0] += chunk
        line, buf[0] = buf[0].split(b"\n", 1)
        return json.loads(line)

    def ask(op, **args):
        rid[0] += 1
        t = time.perf_counter()
        p.stdin.write(json.dumps({"id": rid[0], "op": op, "args": args}) + "\n")
        p.stdin.flush()
        msg = readline()
        while "event" in msg:
            msg = readline()
        return msg.get("result"), (time.perf_counter() - t) * 1000

    readline()  # ready
    return p, ask


def rss_mb(pid):
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)


def semantic(st, fts_queries):
    """Semantic numbers on the DB run() just filled; None without a model
    or without the eval queries (they stay in the development workspace)."""
    name = st.model_name()
    if st.missing_model_files(name) or not (st.deps_importable() or st.venv_python()):
        return None
    queries = os.path.join(HERE, "eval", "queries.json")
    if not os.path.exists(queries):
        return None
    with open(queries) as f:
        sentences = [q["q"] for q in json.load(f)]
    out = {"model": name}
    t = time.perf_counter()
    b = json.loads(subprocess.run([sys.executable, SCRIPT, "backfill", "--json"], capture_output=True,
                                  text=True, check=True).stdout)
    out["backfill"] = {"embedded": b["newly_embedded"], "s": round(time.perf_counter() - t, 1),
                       "ms_per_note": round(b["ms"] / max(1, b["newly_embedded"]), 1)}
    p, ask = serve_proc()
    try:
        t = time.perf_counter()
        while not ask("ping")[0]["semantic"]:
            time.sleep(0.02)
        out["serve_model_load_ms"] = round((time.perf_counter() - t) * 1000)
        # every query is new to serve here, so each pays for its embedding
        typed = [ask("search", query=q, mode="hybrid")[1] for q in fts_queries + sentences]
        out["hybrid_serve_roundtrip"] = summary(typed)
        out["hybrid_serve_roundtrip_sentences"] = summary(typed[len(fts_queries):])
        out["hybrid_serve_roundtrip_repeat"] = summary([ask("search", query=q)[1] for q in sentences])
        out["semantic_serve_roundtrip"] = summary([ask("search", query=q + "?", mode="semantic")[1]
                                                   for q in sentences])
        # write -> findable by meaning (serve waits 1 s after the last change)
        nid = ask("add", body="Reminder: the quokka paperwork for the zebra sanctuary")[0]["id"]
        t = time.perf_counter()
        while nid not in [h["id"] for h in ask("search", query="quokka paperwork, zebra sanctuary", mode="semantic")[0]]:
            if time.perf_counter() - t > 10:
                break
            time.sleep(0.01)
        out["write_to_findable_ms"] = round((time.perf_counter() - t) * 1000) if time.perf_counter() - t <= 10 else None
        out["serve_rss_mb"] = rss_mb(p.pid)
        # a cold `stickies search` process while serve runs: asked over
        # serve's socket, so it neither loads the model nor re-executes
        cold = []
        for q in sentences[4:24]:
            t = time.perf_counter()
            r = subprocess.run([*CLI, "search", q, "--json"], capture_output=True,
                               text=True, check=True)
            cold.append((time.perf_counter() - t) * 1000)
            json.loads(r.stdout)
        out["hybrid_cli_cold_process_serve_running"] = summary(cold)
    finally:
        p.stdin.close()
        p.wait()
        p.stdout.close()
    cold = []
    for q in sentences[:4]:
        t = time.perf_counter()
        subprocess.run([*CLI, "search", q, "--json"], capture_output=True, check=True)
        cold.append((time.perf_counter() - t) * 1000)
    out["hybrid_cli_cold_process"] = summary(cold)  # no serve: loads the model itself
    return out


def run(n_notes=2000, reps=5, with_semantic=True):
    state = tempfile.mkdtemp(prefix="stickies-bench-")
    old = os.environ.get("STICKIES_STATE")
    os.environ["STICKIES_STATE"] = state
    try:
        st = load_module()
        store = st.Store()
        bodies = make_notes(n_notes)
        t = time.perf_counter()
        store.db.execute("BEGIN")
        for b in bodies:
            store.add(b)
        store.db.execute("COMMIT")
        insert_ms = (time.perf_counter() - t) * 1000

        queries = QUERIES + [w[:i] for w in TYPED for i in range(1, len(w) + 1)]
        store.search("warm", mode="fts")
        inproc = []
        for _ in range(reps):
            for q in queries:
                t = time.perf_counter()
                store.search(q, limit=20, mode="fts")
                inproc.append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        store.add("one more note about cardmarket")
        add_ms = (time.perf_counter() - t) * 1000
        t = time.perf_counter()
        store.move(1, x=10, y=10)
        move_ms = (time.perf_counter() - t) * 1000
        store.close()

        # serve round-trip
        # FTS only: no model, so no background indexer competing for the CPU
        p = subprocess.Popen([sys.executable, SCRIPT, "serve"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, bufsize=1,
                             env=dict(os.environ, STICKIES_SEMANTIC="0"))

        def readline():
            r, _, _ = select.select([p.stdout], [], [], 10)
            if not r:
                raise RuntimeError("serve timed out")
            return json.loads(p.stdout.readline())

        readline()  # ready
        served = []
        rid = 0
        for _ in range(reps):
            for q in queries:
                rid += 1
                t = time.perf_counter()
                p.stdin.write(json.dumps({"id": rid, "op": "search", "args": {"query": q, "mode": "fts"}}) + "\n")
                p.stdin.flush()
                while "event" in readline():
                    pass
                served.append((time.perf_counter() - t) * 1000)
        p.stdin.close()
        p.wait()
        p.stdout.close()

        # cold CLI process
        cold = []
        for q in QUERIES[:4]:
            t = time.perf_counter()
            subprocess.run([*CLI, "search", q, "--json", "--mode", "fts"],
                           capture_output=True, check=True)
            cold.append((time.perf_counter() - t) * 1000)

        sem = semantic(st, queries) if with_semantic else None
        return {
            "semantic": sem,
            "notes": n_notes + 1,
            "db_bytes": os.path.getsize(os.path.join(state, "stickies.db")),
            "bulk_insert_ms": round(insert_ms, 1),
            "add_one_ms": round(add_ms, 3),
            "move_one_ms": round(move_ms, 3),
            "search_inprocess": summary(inproc),
            "search_serve_roundtrip": summary(served),
            "search_cli_cold_process": summary(cold),
        }
    finally:
        if old is None:
            os.environ.pop("STICKIES_STATE", None)
        else:
            os.environ["STICKIES_STATE"] = old
        shutil.rmtree(state, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes", type=int, default=2000)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-semantic", action="store_true", help="FTS numbers only")
    a = ap.parse_args()
    r = run(a.notes, with_semantic=not a.no_semantic)
    if a.json:
        print(json.dumps(r))
        return
    for k, v in r.items():
        print(f"{k:<26} {v}")


if __name__ == "__main__":
    main()
