#!/usr/bin/env python3
"""CLI cold start: how long a fresh `stickies` process takes, and where it goes.

    python3 bench_cold.py [--runs 30] [--json]

Runs each command `--runs` times, through stickies.py and through the
`stickies` launcher (which loads stickies.py's cached byte code instead of
compiling it), in a throwaway STICKIES_STATE (20 notes) and cache, model off (STICKIES_SEMANTIC=0) so only the stdlib path is timed, and reports
p50 / min / max wall time. Also: bare `python3 -c pass` (the floor), the time
to compile stickies.py (paid on every run of a script that isn't imported, so
never cached in __pycache__), and the slowest imports from `-X importtime`.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "stickies.py")
LAUNCHER = os.path.join(HERE, "stickies")  # caches stickies.py's byte code


def timed(cmd, env, runs):
    out = []
    for _ in range(runs):
        t = time.perf_counter()
        subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        out.append((time.perf_counter() - t) * 1000)
    out.sort()
    return {"p50": round(statistics.median(out), 1), "min": round(out[0], 1), "max": round(out[-1], 1)}


def import_costs(env, top=8):
    p = subprocess.run([sys.executable, "-X", "importtime", CLI, "list", "--json"],
                       env=env, capture_output=True, text=True)
    rows = []
    for line in p.stderr.splitlines():
        if not line.startswith("import time:") or "self [us]" in line:
            continue
        self_part, cum, name = line.split("|", 2)
        rows.append((int(cum), int(self_part.split(":")[1]), name.rstrip()))
    # Top-level imports are the ones without leading indentation (after the
    # one space importtime always puts after the bar).
    total_top = sum(c for c, _, n in rows if not n[1:].startswith(" "))
    rows.sort(reverse=True)
    return {"total_ms": round(total_top / 1000, 1),
            "slowest": [{"module": n.strip(), "cumulative_ms": round(c / 1000, 1), "self_ms": round(s / 1000, 1)}
                        for c, s, n in rows[:top]]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runs", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as state:
        env = dict(os.environ, STICKIES_STATE=state, STICKIES_HUB="", STICKIES_SEMANTIC="0",
                   STICKIES_CACHE=os.path.join(state, "cache"))
        for i in range(20):
            subprocess.run([sys.executable, CLI, "add", f"note {i} about cardmarket fees and payouts"],
                           env=env, stdout=subprocess.DEVNULL, check=True)
        src = open(CLI, encoding="utf-8").read()
        t = time.perf_counter()
        for _ in range(20):
            compile(src, CLI, "exec")
        compile_ms = round((time.perf_counter() - t) * 1000 / 20, 1)
        res = {
            "python": sys.version.split()[0],
            "runs": a.runs,
            "python -c pass": timed([sys.executable, "-c", "pass"], env, a.runs),
            "stickies --help": timed([sys.executable, CLI, "--help"], env, a.runs),
            "stickies list --json": timed([sys.executable, CLI, "list", "--json"], env, a.runs),
            "stickies search --mode fts": timed([sys.executable, CLI, "search", "cardm", "fee", "--mode", "fts", "--json"], env, a.runs),
            "stickies add": timed([sys.executable, CLI, "add", "bench"], env, a.runs),
            "launcher: stickies list --json": timed([sys.executable, LAUNCHER, "list", "--json"], env, a.runs),
            "launcher: stickies search --mode fts": timed([sys.executable, LAUNCHER, "search", "cardm", "fee", "--mode", "fts", "--json"], env, a.runs),
            "launcher: stickies add": timed([sys.executable, LAUNCHER, "add", "bench"], env, a.runs),
            "compile stickies.py (ms)": compile_ms,
            "imports": import_costs(env),
        }
    if a.json:
        print(json.dumps(res, indent=1))
        return
    for k, v in res.items():
        if isinstance(v, dict) and "p50" in v:
            print(f"{k:<38} p50 {v['p50']:6.1f} ms   min {v['min']:6.1f}   max {v['max']:6.1f}")
        elif k == "imports":
            print(f"imports (top level, cumulative): {v['total_ms']} ms")
            for r in v["slowest"]:
                print(f"  {r['module']:<24} {r['cumulative_ms']:6.1f} ms (self {r['self_ms']})")
        else:
            print(f"{k:<38} {v}")


if __name__ == "__main__":
    main()
