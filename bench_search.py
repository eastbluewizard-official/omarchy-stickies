#!/usr/bin/env python3
"""Search overlay benchmark, against a throwaway STICKIES_STATE.

Seeds N generated notes (bench.py's) plus a few real ones, embeds them all
with `stickies backfill` (the installed model; takes ~40 ms a note), then
runs the real Service.qml in a short-lived `quickshell -p`
(tests/shell/search_harness.qml), so the live shell and notes are untouched.
It does briefly map the overlay (a dimmed full-screen layer with keyboard
focus, ~5 s in all) on the focused monitor and three notes on the first
screen's desktop.

Measures: overlay open (open() -> first swapped frame), each keystroke ->
results landed -> next frame (serve's hybrid search, every query embedded
fresh), the last key of a fast burst (60 ms/key) -> final results, and
Enter -> the note's flash starting. Prints JSON.

    python3 bench_search.py [--notes 2000] [--shot out.png]
"""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "stickies.py")
SYSTEM_SHELL = os.path.join(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy", "shell")


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seed(state, n):
    os.environ["STICKIES_STATE"] = state
    os.environ["STICKIES_HUB"] = ""
    st, bench = load("stickies"), load("bench")
    s = st.Store()
    first = s.add("Tandarts afspraak vrijdag 14:30, verwijzing meenemen.", color="pink")["id"]
    s.add("Marketplace fees went up: 5% commission on every sale now.", color="blue")
    s.add("Payout for the March consignment: 14 items sold, transfer before the 10th.", color="green")
    s.db.execute("BEGIN")
    for body in bench.make_notes(n):
        s.add(body, workspace=99)  # off screen: only the three above are drawn
    s.db.execute("COMMIT")
    s.close()
    t = time.perf_counter()
    p = subprocess.run([sys.executable, SCRIPT, "backfill", "--json"], capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit("backfill failed (is the model installed? `stickies setup`): " + (p.stdout or p.stderr))
    return first, json.loads(p.stdout), time.perf_counter() - t


def shoot_panel(line, out):
    """`SHOT x y w h` (panel rectangle in the focused monitor's window) ->
    a grim capture of just that rectangle, so nothing else on the live
    screen ends up in the picture."""
    x, y, w, h = (int(v) for v in line.split("SHOT", 1)[1].split()[:4])
    mons = json.loads(subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True).stdout)
    m = next(m for m in mons if m.get("focused"))
    subprocess.run(["grim", "-g", f"{m['x'] + x},{m['y'] + y} {w}x{h}", out], check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--notes", type=int, default=2000, help="generated notes, parked on workspace 99")
    ap.add_argument("--shot", help="save a screenshot of the results panel here (grim, cropped to the panel)")
    args = ap.parse_args()
    if not shutil.which("quickshell"):
        sys.exit("quickshell not found")
    tmp = tempfile.mkdtemp(prefix="stickies-search-")
    state, conf = os.path.join(tmp, "state"), os.path.join(tmp, "shell")
    os.makedirs(state)
    os.makedirs(conf)
    try:
        target, backfill, backfill_s = seed(state, args.notes)
        shutil.copy(os.path.join(HERE, "tests", "shell", "search_harness.qml"), os.path.join(conf, "shell.qml"))
        for name in ("Commons", "Ui"):
            os.symlink(os.path.join(SYSTEM_SHELL, name), os.path.join(conf, name))
        os.symlink(HERE, os.path.join(conf, "stickies"))
        env = dict(os.environ, STICKIES_STATE=state, STICKIES_HUB="", STICKIES_CMD=f"python3 {SCRIPT}",
                   BENCH_TARGET="dentist appointment", BENCH_TARGET_NID=str(target),
                   BENCH_SHOT="1" if args.shot else "0")
        proc = subprocess.Popen(["quickshell", "--no-duplicate", "-p", conf], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        result, log = None, []
        try:
            for line in proc.stdout:
                log.append(line.rstrip())
                if " SHOT " in line and args.shot:
                    time.sleep(0.5)
                    shoot_panel(line, args.shot)
                if "BENCH {" in line:
                    result = json.loads(line[line.index("BENCH {") + 6:])
                    break
        finally:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if result is None:
            sys.stderr.write("\n".join(log[-40:]) + "\n")
            sys.exit("no result from harness")
        warnings = [l for l in log if " WARN" in l or " ERROR" in l]
        if warnings:
            result["qml_warnings"] = warnings
        result["target_nid"] = target
        result["backfill"] = {"notes": backfill["embedded"], "s": round(backfill_s, 1)}
        print(json.dumps(result, indent=2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
