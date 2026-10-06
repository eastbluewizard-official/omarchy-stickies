#!/usr/bin/env python3
"""Bar widget benchmark, against a throwaway STICKIES_STATE.

Runs the real Service.qml + BarWidget.qml in a short-lived `quickshell -p`
(tests/shell/bar_harness.qml, with a stub standing in for omarchy.bar), so
the live shell, bar and notes are untouched. It does briefly map a small bar
strip and the popup (Overlay layer) on the first screen, and the popup takes
keyboard focus while open (5 x well under a second).

Measures: panel open (open() -> popup's first swapped frame), quick capture
(newNoteHere -> note in the model, a `stickies serve` round-trip), search
round-trip from QML, and that the widget sees count/pinned. Prints JSON.

    python3 bench_bar.py [--notes 2000]
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


def seed(state, n):
    os.environ["STICKIES_STATE"] = state
    os.environ["STICKIES_HUB"] = ""
    spec = importlib.util.spec_from_file_location("stickies", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    st = mod.Store()
    st.add("Cardmarket fees went up to 5%", pinned=True, color="pink")
    st.add("Pinned: ship the OP-09 preorders", pinned=True, color="blue")
    for i in range(n):
        st.add(f"note {i} about fees and stock", workspace=99)
    st.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--notes", type=int, default=2000, help="extra notes, parked on workspace 99")
    args = ap.parse_args()
    if not shutil.which("quickshell"):
        sys.exit("quickshell not found")
    tmp = tempfile.mkdtemp(prefix="stickies-bar-")
    state, conf = os.path.join(tmp, "state"), os.path.join(tmp, "shell")
    os.makedirs(state)
    os.makedirs(conf)
    try:
        seed(state, args.notes)
        shutil.copy(os.path.join(HERE, "tests", "shell", "bar_harness.qml"), os.path.join(conf, "shell.qml"))
        for name in ("Commons", "Ui"):
            os.symlink(os.path.join(SYSTEM_SHELL, name), os.path.join(conf, name))
        os.symlink(HERE, os.path.join(conf, "stickies"))
        env = dict(os.environ, STICKIES_STATE=state, STICKIES_HUB="", STICKIES_CMD=f"python3 {SCRIPT}")
        proc = subprocess.Popen(["quickshell", "--no-duplicate", "-p", conf], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        result, log = None, []
        try:
            for line in proc.stdout:
                log.append(line.rstrip())
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
        result["notes"] = args.notes + 2
        print(json.dumps(result, indent=2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
