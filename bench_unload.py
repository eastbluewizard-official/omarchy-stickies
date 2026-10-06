#!/usr/bin/env python3
"""What the idle unload of the embedding model costs and saves, against a
throwaway STICKIES_STATE with the installed model.

    python3 bench_unload.py [--notes 2000] [--idle 2] [--old OLD/stickies.py] [--json]

Seeds N generated notes (bench.py's) plus three real ones and embeds them
(`stickies backfill`, ~40 ms a note), then runs `stickies serve` (in the
venv's Python) and measures, from the JSON lines it writes:

- lazy start (the default): serve's RSS before anything searched, then the
  first search: request -> FTS answer, -> the `semantic` loaded event, and
  -> by-meaning answer (the overlay asks again when that event arrives);
- preload (the model loads right after `ready`, as 1.1.0 did): ready ->
  loaded, and the first search once it is loaded;
- idle unload with `--idle` minutes: RSS loaded, context switches (all
  threads; they stand in for wake-ups) over 60 s while loaded, the unload,
  RSS after it, 60 s of switches after it, then the first search again;
- with `--old`, another copy's serve with its model loaded: RSS (the before).
"""

import argparse
import importlib.util
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "stickies.py")


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rss_mb(pid):
    out = {}
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith(("VmRSS:", "RssAnon:", "RssFile:")):
                out[line.split(":")[0]] = round(int(line.split()[1]) / 1024, 1)
    return out


def switches(pid):
    total = 0
    for tid in os.listdir(f"/proc/{pid}/task"):
        try:
            with open(f"/proc/{pid}/task/{tid}/status") as f:
                for line in f:
                    if line.startswith(("voluntary_ctxt_switches", "nonvoluntary_ctxt_switches")):
                        total += int(line.split()[1])
        except OSError:  # a thread that just ended
            pass
    return total


class Serve:
    """A serve process; every line it writes, with the time it arrived."""

    def __init__(self, python, script, env, preload=False):
        code = ("import sys; sys.path.insert(0, %r); import stickies; stickies.PRELOAD_MODEL = %r; "
                "sys.exit(stickies.main(['serve']))" % (os.path.dirname(script), preload))
        self.p = subprocess.Popen([python, "-c", code], env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.lines, self.cv, self.rid = [], threading.Condition(), 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf = b""
        fd = self.p.stdout.fileno()
        while True:
            r, _, _ = select.select([fd], [], [])
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return
            t = time.perf_counter()
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                with self.cv:
                    self.lines.append((t, json.loads(line)))
                    self.cv.notify_all()

    def wait(self, pred, after=0.0, timeout=60):
        """(time, message) of the first line after `after` matching pred."""
        end = time.monotonic() + timeout
        with self.cv:
            while True:
                for t, m in self.lines:
                    if t >= after and pred(m):
                        return t, m
                left = end - time.monotonic()
                if left <= 0:
                    raise RuntimeError("serve: timed out waiting")
                self.cv.wait(left)

    def send(self, op, **args):
        self.rid += 1
        t = time.perf_counter()
        self.p.stdin.write((json.dumps({"id": self.rid, "op": op, "args": args}) + "\n").encode())
        self.p.stdin.flush()
        return self.rid, t

    def ask(self, op, **args):
        rid, t = self.send(op, **args)
        t1, m = self.wait(lambda m: m.get("id") == rid, after=t)
        return m.get("result"), (t1 - t) * 1000, t

    def close(self):
        self.p.stdin.close()
        self.p.wait(10)


def is_loaded(m):
    return m.get("event") == "semantic" and m.get("loaded", m.get("on"))


def first_search(sv, query="payout transfer"):
    """Request -> FTS answer; -> loaded event; -> by-meaning answer (asked
    again when the event arrives, as the overlay does)."""
    res, fts_ms, t0 = sv.ask("search", query=query, limit=20)
    t_loaded, _ = sv.wait(is_loaded, after=t0)
    res2, ms2, _ = sv.ask("search", query=query, limit=20)
    meaning = sum(h["match"] != "fts" for h in res2)
    return {"to_fts_ms": round(fts_ms, 1), "fts_hits": len(res),
            "to_loaded_ms": round((t_loaded - t0) * 1000, 1),
            "to_meaning_ms": round((t_loaded - t0) * 1000 + ms2, 1),
            "meaning_hits": meaning, "loaded_search_ms": round(ms2, 1)}


def window(pid, secs):
    s0, w0 = switches(pid), time.monotonic()
    time.sleep(secs)
    n, w = switches(pid) - s0, time.monotonic() - w0
    return {"seconds": round(w, 1), "switches": n, "per_min": round(n / w * 60, 1)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--notes", type=int, default=2000)
    ap.add_argument("--idle", type=float, default=2, help="idle unload minutes for the unload run")
    ap.add_argument("--old", help="another stickies.py whose serve (model loaded) to measure for RSS")
    ap.add_argument("--skip-idle", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    st = load("stickies")
    python = st.venv_python() or sys.executable
    out = {"notes": a.notes + 3}
    with tempfile.TemporaryDirectory() as tmp:
        seeded = os.path.join(tmp, "seed")
        os.makedirs(seeded)
        env = dict(os.environ, STICKIES_STATE=seeded, STICKIES_HUB="", STICKIES_NOTIFY="")
        os.environ.update(STICKIES_STATE=seeded, STICKIES_HUB="")
        bench = load("bench")
        s = st.Store()
        s.add("Tandarts afspraak vrijdag 14:30, verwijzing meenemen.")
        s.add("Marketplace fees went up: 5% commission on every sale now.")
        s.add("Payout for the March consignment: 14 items sold, transfer before the 10th.")
        s.db.execute("BEGIN")
        for body in bench.make_notes(a.notes):
            s.add(body)
        s.db.execute("COMMIT")
        s.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        s.close()
        p = subprocess.run([python, SCRIPT, "backfill", "--json"], env=env, capture_output=True, text=True)
        if p.returncode != 0:
            sys.exit("backfill failed (is the model installed? `stickies setup`): " + p.stderr)

        def fresh(name, script=SCRIPT, preload=False, **extra):
            d = os.path.join(tmp, name)
            shutil.copytree(seeded, d)
            return Serve(python, script, dict(env, STICKIES_STATE=d, **extra), preload=preload)

        # lazy (the default)
        sv = fresh("lazy")
        sv.wait(lambda m: m.get("event") == "ready")
        time.sleep(3)
        out["lazy_rss_before_use"] = rss_mb(sv.p.pid)
        out["lazy_first_search"] = first_search(sv)
        time.sleep(2)
        out["lazy_rss_loaded"] = rss_mb(sv.p.pid)
        sv.close()

        # preload (1.1.0's behaviour)
        sv = fresh("preload", preload=True)
        t_ready, _ = sv.wait(lambda m: m.get("event") == "ready")
        t_loaded, _ = sv.wait(is_loaded)
        out["preload_ready_to_loaded_ms"] = round((t_loaded - t_ready) * 1000, 1)
        time.sleep(2)
        res, ms, _ = sv.ask("search", query="dentist appointment", limit=20)
        out["preload_first_search"] = {"to_meaning_ms": round(ms, 1),
                                       "meaning_hits": sum(h["match"] != "fts" for h in res)}
        out["preload_rss_loaded"] = rss_mb(sv.p.pid)
        sv.close()

        if not a.skip_idle:
            sv = fresh("idle", STICKIES_IDLE_UNLOAD=str(a.idle))
            sv.wait(lambda m: m.get("event") == "ready")
            _, _, t0 = sv.ask("search", query="warm up", limit=20)
            t_loaded, _ = sv.wait(is_loaded, after=t0)
            time.sleep(2)
            out["idle_rss_loaded"] = rss_mb(sv.p.pid)
            out["idle_loaded_window"] = window(sv.p.pid, 60)
            t_un, _ = sv.wait(lambda m: m.get("event") == "semantic" and m.get("loaded") is False,
                              after=t_loaded, timeout=a.idle * 60 + 30)
            out["idle_unloaded_after_s"] = round(t_un - t_loaded, 1)
            time.sleep(2)
            out["idle_rss_unloaded"] = rss_mb(sv.p.pid)
            out["idle_unloaded_window"] = window(sv.p.pid, 60)
            out["after_unload_first_search"] = first_search(sv, "consignment payout")
            sv.close()

        if a.old:
            sv = fresh("old", script=os.path.abspath(a.old))
            sv.wait(lambda m: m.get("event") == "semantic" and m.get("on"))
            time.sleep(2)
            sv.ask("search", query="dentist appointment", limit=20)
            time.sleep(1)
            out["old_rss_loaded"] = rss_mb(sv.p.pid)
            sv.close()
    print(json.dumps(out) if a.json else json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
