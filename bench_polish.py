#!/usr/bin/env python3
"""Polish features, live: checklists, drag snapping, roll-up, tidy + undo,
archive + undo, clipboard note, theme switch. Checks and times them.

    python3 bench_polish.py [--extra 20] [--json]

Runs the real plugin in its own short-lived `quickshell -p <tmp>`
(like bench_shell.py) on the Overlay layer, with tests/shell/polish_harness.qml
calling the same functions the pointer and keys call, a throwaway
STICKIES_STATE and a fake wl-paste. Takes no keyboard focus; tidy asks the
live Hyprland for gaps_out and the bar's space (read-only).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "stickies.py")
SYSTEM_SHELL = "/usr/share/omarchy/shell"

FAKE_WL_PASTE = """#!/bin/sh
mode=$(cat "{mode}" 2>/dev/null)
if [ "$1" = "--list-types" ]; then
  if [ "$mode" = image ]; then echo image/png; else printf 'text/plain;charset=utf-8\\nTEXT\\n'; fi
  exit 0
fi
printf '  copied from somewhere  \\n'
"""


def seed(state, extra):
    import importlib.util
    os.environ["STICKIES_STATE"] = state
    spec = importlib.util.spec_from_file_location("stickies", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    st = mod.Store()
    a = st.add("OP-09 preorder\n- [x] 2 displays (JP)\n- [ ] 1 case (EN) for consignment\n"
               "- [ ] ask klant re: payout", color="blue", x=200, y=150, w=260, h=200)
    b = st.add("Cardmarket fees\nSeller fee went to 5% + €0.15 per order.", color="yellow",
               x=700, y=150, w=260, h=200)
    colors = list(mod.COLORS)[:6]
    for i in range(extra):
        st.add(f"note {i}\nsome text to wrap over a couple of lines here", color=colors[i % 6],
               x=60 + (i * 211) % 1500, y=420 + (i * 97) % 500)
    st.close()
    return a["id"], b["id"]


def run(args):
    if not shutil.which("quickshell"):
        sys.exit("quickshell not found")
    tmp = tempfile.mkdtemp(prefix="stickies-polish-")
    state, conf = os.path.join(tmp, "state"), os.path.join(tmp, "shell")
    os.makedirs(state)
    os.makedirs(conf)
    try:
        nid, plain = seed(state, args.extra)
        mode = os.path.join(tmp, "clip-mode")
        with open(mode, "w") as f:
            f.write("text")
        paste = os.path.join(tmp, "wl-paste")
        with open(paste, "w") as f:
            f.write(FAKE_WL_PASTE.format(mode=mode))
        os.chmod(paste, 0o755)
        shutil.copy(os.path.join(HERE, "tests", "shell", "polish_harness.qml"), os.path.join(conf, "shell.qml"))
        for name in ("Commons", "Ui"):
            os.symlink(os.path.join(SYSTEM_SHELL, name), os.path.join(conf, name))
        os.symlink(HERE, os.path.join(conf, "stickies"))
        env = dict(os.environ, STICKIES_STATE=state, STICKIES_CMD=f"python3 {SCRIPT}",
                   STICKIES_LAYER="overlay", STICKIES_HUB="", STICKIES_NOTIFY="",
                   STICKIES_WL_PASTE=paste, BENCH_CLIP_MODE_FILE=mode,
                   BENCH_NID=str(nid), BENCH_PLAIN=str(plain))
        proc = subprocess.Popen(["quickshell", "--no-duplicate", "-p", conf], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        result, log, deadline = None, [], time.time() + 90
        try:
            for line in proc.stdout:
                log.append(line.rstrip())
                if "BENCH {" in line:
                    result = json.loads(line[line.index("BENCH {") + 6:])
                    break
                if time.time() > deadline:
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
        warnings = [l for l in log if (" WARN" in l or " ERROR" in l) and "portal" not in l]
        if warnings:
            result["qml_warnings"] = warnings
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--extra", type=int, default=20, help="other notes on screen")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    r = run(args)
    print(json.dumps(r, indent=2))
    sys.exit(0 if r.get("all_ok") else 1)


if __name__ == "__main__":
    main()
