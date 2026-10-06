#!/usr/bin/env python3
"""What an idle `stickies serve` costs: CPU time and wake-ups while nobody
touches a note.

    python3 bench_idle.py [--seconds 60] [--script OTHER/stickies.py] [--reminder] [--json]

Starts serve in a throwaway STICKIES_STATE (20 notes; the model loads too
when `stickies setup` has run), lets it settle for 5 s, then reads
/proc/<pid>/stat (utime + stime, all threads) and the context-switch counts
of every thread before and after `--seconds` of silence. Context switches
stand in for wake-ups: a select() with a timeout wakes the process every
time it expires. `--script` measures another copy (e.g. an older release)
the same way, for a before/after.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def ticks(pid):
    with open(f"/proc/{pid}/stat") as f:
        parts = f.read().rsplit(")", 1)[1].split()
    return int(parts[11]) + int(parts[12])  # utime + stime, in clock ticks


def switches(pid):
    total = 0
    for tid in os.listdir(f"/proc/{pid}/task"):
        with open(f"/proc/{pid}/task/{tid}/status") as f:
            for line in f:
                if line.startswith(("voluntary_ctxt_switches", "nonvoluntary_ctxt_switches")):
                    total += int(line.split()[1])
    return total


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--script", default=os.path.join(HERE, "stickies.py"))
    ap.add_argument("--reminder", action="store_true",
                    help="one reminder pending (tomorrow 09:00), notifications on (a stand-in sender)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    hz = os.sysconf("SC_CLK_TCK")
    with tempfile.TemporaryDirectory() as state:
        env = dict(os.environ, STICKIES_STATE=state, STICKIES_HUB="")
        if a.reminder:
            env["STICKIES_NOTIFY"] = "true"
            subprocess.run([sys.executable, a.script, "add", "Dentist\n@ tomorrow 09:00 leave"], env=env,
                           stdout=subprocess.DEVNULL, check=True)
        for i in range(20):
            subprocess.run([sys.executable, a.script, "add", f"idle note {i}"], env=env,
                           stdout=subprocess.DEVNULL, check=True)
        p = subprocess.Popen([sys.executable, a.script, "serve"], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            p.stdout.readline()  # ready
            time.sleep(5)        # model load, backfill, the first hub publish
            t0, s0, w0 = ticks(p.pid), switches(p.pid), time.monotonic()
            time.sleep(a.seconds)
            t1, s1, w1 = ticks(p.pid), switches(p.pid), time.monotonic()
        finally:
            p.stdin.close()
            p.wait(10)
    secs = w1 - w0
    res = {"script": os.path.relpath(a.script, HERE), "reminder_pending": a.reminder, "seconds": round(secs, 1),
           "cpu_ms": round((t1 - t0) * 1000 / hz, 1),
           "cpu_percent": round((t1 - t0) / hz / secs * 100, 3),
           "context_switches": s1 - s0, "switches_per_s": round((s1 - s0) / secs, 2)}
    print(json.dumps(res) if a.json else "\n".join(f"{k:<18} {v}" for k, v in res.items()))


if __name__ == "__main__":
    main()
