#!/usr/bin/env python3
"""All notes overlay benchmark, against a throwaway STICKIES_STATE.

Seeds N generated notes (bench.py's) with tags, checklists, reminders and
a few pinned, then runs the real Service.qml in a short-lived `quickshell -p`
(tests/shell/list_harness.qml), so the live shell and notes are untouched.
It does briefly map the overlay (a dimmed full-screen layer with keyboard
focus, ~5 s in all) on the focused monitor.

Measures: open (open() -> first swapped frame, and -> serve's fresh rows on
screen; the first open is cold, with no rows yet), scrolling the whole list
one 24 px step per frame (the gaps between frames), each filter keystroke
-> next frame, a tag chip click -> next frame, and Enter -> the note's
flash. Prints JSON, with the focused monitor's refresh rate to compare.

    python3 bench_list.py [--notes 2000] [--shot out.png]
"""

import argparse
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "stickies.py")
SYSTEM_SHELL = os.path.join(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy", "shell")
TAGS = ["work", "cardmarket", "home", "op-09", "consignment", "btw", "ideas", "stock", "payout",
        "groceries", "later", "waiting-on"]


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seed(state, n):
    os.environ["STICKIES_STATE"] = state
    os.environ["STICKIES_HUB"] = ""
    st, bench = load("stickies"), load("bench")
    rnd = random.Random(139)
    s = st.Store()
    target = s.add("Cardmarket fees went up: 5% seller fee from November.", color="pink",
                   tags=["cardmarket", "work"])["id"]
    s.add("OP-09 preorders\n[x] 2 displays\n[ ] 1 case\n[ ] sleeves", color="blue", pinned=True,
          tags=["op-09", "stock"])
    s.add("BTW aangifte Q4 @2026-12-20 10:00", color="orange", tags=["btw"])
    s.db.execute("BEGIN")
    for i, body in enumerate(bench.make_notes(n)):
        if rnd.random() < 0.1:
            body = "todo\n[ ] " + "\n[x] ".join(body.split(" ")[:4])
        tags = rnd.sample(TAGS, rnd.choice((0, 1, 1, 2, 3)))
        s.add(body, workspace=99, tags=tags, pinned=rnd.random() < 0.01,
              color=rnd.choice(list(st.COLORS)))
    s.db.execute("COMMIT")
    s.close()
    return target


def shoot_panel(line, out):
    """`SHOT x y w h` (card rectangle in the focused monitor's window) -> a
    grim capture of just that rectangle."""
    x, y, w, h = (int(v) for v in line.split("SHOT", 1)[1].split()[:4])
    mons = json.loads(subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True).stdout)
    m = next(m for m in mons if m.get("focused"))
    subprocess.run(["grim", "-g", f"{m['x'] + x},{m['y'] + y} {w}x{h}", out], check=True)


def refresh_hz():
    try:
        mons = json.loads(subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True).stdout)
        return round(next(m for m in mons if m.get("focused"))["refreshRate"], 1)
    except (OSError, ValueError, StopIteration, KeyError):
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--notes", type=int, default=2000, help="generated notes, parked on workspace 99")
    ap.add_argument("--shot", help="save a screenshot of the overlay card here (grim, cropped)")
    args = ap.parse_args()
    if not shutil.which("quickshell"):
        sys.exit("quickshell not found")
    tmp = tempfile.mkdtemp(prefix="stickies-list-")
    state, conf = os.path.join(tmp, "state"), os.path.join(tmp, "shell")
    os.makedirs(state)
    os.makedirs(conf)
    try:
        target = seed(state, args.notes)
        shutil.copy(os.path.join(HERE, "tests", "shell", "list_harness.qml"), os.path.join(conf, "shell.qml"))
        for name in ("Commons", "Ui"):
            os.symlink(os.path.join(SYSTEM_SHELL, name), os.path.join(conf, name))
        os.symlink(HERE, os.path.join(conf, "stickies"))
        env = dict(os.environ, STICKIES_STATE=state, STICKIES_HUB="", STICKIES_CMD=f"python3 {SCRIPT}",
                   STICKIES_SEMANTIC="0", BENCH_TARGET_NID=str(target), BENCH_TARGET_TAG="cardmarket",
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
        result["refresh_hz"] = refresh_hz()
        result["target_nid"] = target
        print(json.dumps(result, indent=2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
