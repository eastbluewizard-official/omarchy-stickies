#!/usr/bin/env python3
"""Chat overlay benchmark, against a throwaway STICKIES_STATE.

Seeds three real-looking notes plus N generated ones (bench.py's, parked on
workspace 99), then runs the real Service.qml in a short-lived `quickshell
-p` (tests/shell/chat_harness.qml), so the live shell and notes are
untouched. It briefly maps the chat overlay (a dimmed full-screen layer with
keyboard focus, a few seconds) on the focused monitor.

By default the agent is a fake (it "thinks" 400 ms, then streams a canned
answer that cites the notes it was sent and proposes a new note and a hub
todo); --real uses Omarchy's default agent (`claude -p`) and sends it the
question and the seeded test notes, nothing else. Hub is a fake either way.

Measures: overlay open (open() -> first swapped frame), Enter -> notes
listed (serve's chat_notes) and on screen, Send -> first streamed text and
-> answer done, Apply -> note created. Checks the unticked note never
reached the agent and nothing was applied before Apply. Prints JSON.

    python3 bench_chat.py [--notes 2000] [--real] [--shot out.png]
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "stickies.py")
SYSTEM_SHELL = os.path.join(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy", "shell")

FAKE_AGENT = r'''#!{py}
import json, os, re, sys, time
prompt = sys.stdin.read()
open(os.environ["BENCH_AGENT_LOG"], "a").write(json.dumps({{"stdin": prompt}}) + "\n")
ids = re.findall(r'<note id="(\d+)"', prompt)
time.sleep(0.4)
cite = " ".join("[#%s]" % i for i in ids[:2])
answer = ("The March consignment payout (14 items) is due before the 10th " + cite + "; the "
          "transfer still has to go out.\n\nNothing else in these notes mentions "
          "it.\n\n```stickies-actions\n"
          + json.dumps([{{"action": "new_note", "body": "Transfer the March consignment payout", "color": "orange"}},
                        {{"action": "hub_todo", "title": "Pay the March consignment payout", "due": "2026-10-09"}}])
          + "\n```\n")
for i in range(0, len(answer), 12):
    sys.stdout.write(answer[i:i + 12]); sys.stdout.flush(); time.sleep(0.02)
'''


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seed(state, n):
    os.environ["STICKIES_STATE"] = state
    os.environ["STICKIES_HUB"] = ""
    st, bench = load("stickies"), load("bench")
    s = st.Store(embedder=None)
    s.add("Marketplace fees went up: 5% commission on every sale now.", color="blue")
    s.add("Payout for the March consignment: 14 items sold, transfer before the 10th.", color="green")
    s.add("Tandarts afspraak vrijdag 14:30, verwijzing meenemen.", color="pink")
    s.db.execute("BEGIN")
    for body in bench.make_notes(n):
        s.add(body, workspace=99)
    s.db.execute("COMMIT")
    s.close()


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
    ap.add_argument("--real", action="store_true", help="use Omarchy's default agent (claude -p)")
    ap.add_argument("--shot", help="save a screenshot of the answered chat panel here (grim, cropped to the panel)")
    args = ap.parse_args()
    if not shutil.which("quickshell"):
        sys.exit("quickshell not found")
    tmp = tempfile.mkdtemp(prefix="stickies-chat-")
    state, conf = os.path.join(tmp, "state"), os.path.join(tmp, "shell")
    os.makedirs(state)
    os.makedirs(conf)
    try:
        seed(state, args.notes)
        agent_log = os.path.join(tmp, "agent.jsonl")
        fake = os.path.join(tmp, "fake-agent")
        with open(fake, "w") as f:
            f.write(FAKE_AGENT.format(py=sys.executable))
        os.chmod(fake, 0o755)
        hub = os.path.join(tmp, "fake-hub")
        with open(hub, "w") as f:
            f.write("#!/bin/sh\necho '{\"id\": 1}'\n")
        os.chmod(hub, 0o755)
        shutil.copy(os.path.join(HERE, "tests", "shell", "chat_harness.qml"), os.path.join(conf, "shell.qml"))
        for name in ("Commons", "Ui"):
            os.symlink(os.path.join(SYSTEM_SHELL, name), os.path.join(conf, name))
        os.symlink(HERE, os.path.join(conf, "stickies"))
        env = dict(os.environ, STICKIES_STATE=state, STICKIES_HUB=hub, STICKIES_SEMANTIC="0",
                   STICKIES_CMD=f"python3 {SCRIPT}", BENCH_AGENT_LOG=agent_log,
                   BENCH_SHOT="1" if args.shot else "0")
        if args.real:
            env.pop("STICKIES_AGENT", None)
        else:
            env["STICKIES_AGENT"] = fake
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
        if not args.real and os.path.exists(agent_log):
            with open(agent_log) as f:
                prompts = [json.loads(l)["stdin"] for l in f if l.strip()]
            sent = [int(i) for i in re.findall(r'<note id="(\d+)"', prompts[-1])] if prompts else []
            result["agent_saw"] = sent
            result["unticked_not_sent"] = result.get("unticked") not in sent
        print(json.dumps(result, indent=2, ensure_ascii=False))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
