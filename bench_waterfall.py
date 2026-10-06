#!/usr/bin/env python3
"""Waterfall layout: switch-animation and column-scroll frame times, and a
screenshot per layout, against a throwaway STICKIES_STATE.

    python3 bench_waterfall.py [--notes 40] [--switches 10] [--json]     # frame times
    python3 bench_waterfall.py --shots docs/layout [--output NAME]       # docs/layout-<layout>.png
    python3 bench_waterfall.py --interact [--no-type]                    # drag/collapse/typing checks
    python3 bench_waterfall.py --follow OTHER                            # column follows monitor focus

Runs the real plugin in its own short-lived `quickshell -p <tmp>`
next to the live omarchy-shell (tests/shell/waterfall_harness.qml drives
it), on the real layers: the column on Top above whatever windows are on
that monitor, free notes on Bottom. --output pins the column to one output
(STICKIES_COLUMN_SCREEN) and puts the notes there, e.g. a headless output
with test windows on it; by default the focused monitor. The live notes
DB and the live shell are not touched. Needs Hyprland, quickshell, grim.
"""

import argparse
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
SYSTEM_SHELL = "/usr/share/omarchy/shell"

# Screenshot notes (--shots): a plausible desktop, nobody's real data.
DEMO = [
    ("yellow", True, "This week\n\nMon  write the release notes\nWed  dentist 14:30\nFri  ship 1.0"),
    ("pink", False, "Tandarts afspraak\nvrijdag 14:30, verwijzing meenemen\n@ 2027-01-15 13:30 leave for the dentist"),
    ("blue", False, "Reading list\n- [x] A Philosophy of Software Design\n- [ ] The Design of Everyday Things\n"
                    "- [ ] Thinking in Systems"),
    ("green", False, "Groceries\n- [x] oat milk\n- [ ] bread\n- [ ] coffee beans\n- [ ] lemons"),
    ("orange", False, "Ideas\nA bar widget for the next deadline.\nKeyboard first, mouse optional."),
    ("purple", False, "Everything here stays on this laptop.\nSearch works with Wi-Fi off."),
]

# The bench notes are plain text, as they were when the numbers in
# docs/PERF.md were first taken: checkbox rows make cards heavier to build
# (measured: +10 ms on the switch's first frame), which would make runs
# incomparable.
PLAIN = [re.sub(r"\n@ [^\n]*", "", body).replace("- [x] ", "- ").replace("- [ ] ", "- ")
         for _, _, body in DEMO]


def load_stickies():
    import importlib.util
    spec = importlib.util.spec_from_file_location("stickies", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def monitors():
    return json.loads(subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True).stdout)


def seed(state, notes, shots, monitor, screen_w, screen_h):
    os.environ["STICKIES_STATE"] = state
    st = load_stickies().Store()
    if shots:
        spots = [(90, 120, 280, 220), (420, 150, 260, 190), (740, 110, 260, 240),
                 (240, 430, 280, 200), (600, 470, 230, 180), (1000, 420, 270, 210)]
        for (color, pinned, body), (x, y, w, h) in zip(DEMO, spots):
            st.add(body, color=color, pinned=pinned, monitor=monitor,
                   x=min(x, screen_w - w - 20), y=min(y, screen_h - h - 20), w=w, h=h)
    else:
        colors = list(load_stickies().COLORS)[:6]
        for i in range(notes):
            st.add(f"note {i}\n" + PLAIN[i % len(PLAIN)], color=colors[i % 6], monitor=monitor,
                   x=40 + (i % 8) * 200 % max(1, screen_w - 260), y=60 + (i // 8) * 150 % max(1, screen_h - 220),
                   h=140 + (i * 37) % 120)
    st.close()


def run(args):
    if not shutil.which("quickshell"):
        sys.exit("quickshell not found")
    mons = monitors()
    mon = next((m for m in mons if m["name"] == args.output), None) if args.output \
        else next((m for m in mons if m["focused"]), mons[0])
    if mon is None:
        sys.exit(f"no output {args.output}")
    rotated = mon["transform"] % 2 == 1
    sw, sh = (mon["height"], mon["width"]) if rotated else (mon["width"], mon["height"])
    sw, sh = int(sw / mon["scale"]), int(sh / mon["scale"])
    tmp = tempfile.mkdtemp(prefix="stickies-wf-")
    state = os.path.join(tmp, "state")
    conf = os.path.join(tmp, "shell")
    os.makedirs(state)
    os.makedirs(conf)
    try:
        seed(state, args.notes, bool(args.shots or args.interact or args.follow), mon["name"], sw, sh)
        shutil.copy(os.path.join(HERE, "tests", "shell", "waterfall_harness.qml"), os.path.join(conf, "shell.qml"))
        for name in ("Commons", "Ui"):
            os.symlink(os.path.join(SYSTEM_SHELL, name), os.path.join(conf, name))
        os.symlink(HERE, os.path.join(conf, "stickies"))
        env = dict(os.environ, STICKIES_STATE=state, STICKIES_CMD=f"python3 {SCRIPT}", STICKIES_HUB="",
                   STICKIES_COLUMN_SCREEN="" if args.follow else mon["name"], BENCH_FOLLOW_TO=args.follow or "",
                   BENCH_MODE="shot" if args.shots else "interact" if args.interact
                   else "follow" if args.follow else "bench",
                   BENCH_TYPE="0" if args.no_type else "1",
                   BENCH_SWITCHES=str(args.switches), BENCH_HZ=str(mon["refreshRate"]))
        if args.layouts:
            env["BENCH_LAYOUTS"] = args.layouts
        if args.wall:
            env["BENCH_WALL"] = os.path.abspath(args.wall)
        if args.light:
            env["BENCH_LIGHT"] = "1"
        proc = subprocess.Popen(["quickshell", "--no-duplicate", "-p", conf], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        result, log, shots = None, [], {}
        deadline = time.time() + 120
        try:
            for line in proc.stdout:
                log.append(line.rstrip())
                if "BENCH_SHOT " in line and args.shots:
                    layout = line.split("BENCH_SHOT ", 1)[1].strip()
                    out = os.path.abspath(f"{args.shots}-{layout}.png")
                    subprocess.run(["grim", "-o", mon["name"], out], check=True)
                    shots[layout] = out
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
        result["output"] = {"name": mon["name"], "logical": [sw, sh], "transform": mon["transform"],
                            "refresh": mon["refreshRate"]}
        if shots:
            result["screenshots"] = shots
        warnings = [l for l in log if " WARN" in l or " ERROR" in l]
        if warnings:
            result["qml_warnings"] = warnings
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--notes", type=int, default=40, help="notes in the column (bench)")
    ap.add_argument("--switches", type=int, default=10, help="free -> waterfall -> free rounds")
    ap.add_argument("--shots", metavar="PREFIX", help="screenshot each layout to PREFIX-<layout>.png")
    ap.add_argument("--layouts", help="with --shots: comma-separated (default all three)")
    ap.add_argument("--wall", help="with --shots: a plain backdrop image under the windows (default: none, "
                                   "your live wallpaper shows)")
    ap.add_argument("--light", action="store_true", help="with --shots: a light theme palette")
    ap.add_argument("--output", help="run on this output (default: the focused one)")
    ap.add_argument("--interact", action="store_true", help="drag/collapse/typing/CLI checks")
    ap.add_argument("--no-type", action="store_true", help="with --interact: don't send real key events")
    ap.add_argument("--follow", metavar="OTHER", help="focus output OTHER and back; the column must follow")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    r = run(args)
    if args.json or args.shots or args.interact or args.follow:
        print(json.dumps(r, indent=2))
        return
    s, c = r.get("switch") or {}, r.get("scroll") or {}
    print(f"output {r['output']}")
    for k in ("to_waterfall_interval_ms", "to_free_interval_ms"):
        v = s.get(k) or {}
        print(f"switch {k}: p50 {v.get('p50')} p95 {v.get('p95')} p99 {v.get('p99')} max {v.get('max')} (n {v.get('n')})")
    print(f"frames per glide: {s.get('frames_per_glide')}, missed {s.get('missed_frames')}")
    v = c.get("frame_interval_ms") or {}
    print(f"scroll {c.get('column_notes')} notes, content {c.get('content_h')} px in {c.get('view_h')}: "
          f"{c.get('frames')} frames / {c.get('ticks')} ticks, p50 {v.get('p50')} p95 {v.get('p95')} "
          f"p99 {v.get('p99')} max {v.get('max')}, missed {c.get('missed_frames')}; "
          f"JS per tick p95 {(c.get('js_per_tick_ms') or {}).get('p95')} ms")
    if r.get("qml_warnings"):
        print("QML warnings:\n  " + "\n  ".join(r["qml_warnings"]))


if __name__ == "__main__":
    main()
