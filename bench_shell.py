#!/usr/bin/env python3
"""Desktop plugin benchmark and screenshot, against a throwaway STICKIES_STATE.

    python3 bench_shell.py [--visible 40] [--no-type] [--json]   # drag frame time + keystroke latency
    python3 bench_shell.py --shot docs/desktop.png [--dark]      # screenshot of demo notes
    python3 bench_shell.py --shot docs/first-run.png --empty     # no notes: the first-run hint
    python3 bench_shell.py --shot docs/polish.png --dark --plain # checklist, bell, rolled-up note

Runs the real plugin (Service.qml) in its own short-lived
`quickshell -p <tmp>` next to the live omarchy-shell, on the Overlay layer
so the compositor actually presents its frames, with
tests/shell/harness.qml driving it. The live notes DB and the live shell
are not touched. Needs a Wayland session (Hyprland), quickshell and, for
typing, wtype; the typing phase takes exclusive keyboard focus for ~3 s.
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
LAUNCHER = os.path.join(HERE, "stickies")  # what the plugin runs
SYSTEM_SHELL = "/usr/share/omarchy/shell"

# Screenshot notes (--shot): a plausible desktop, nobody's real data.
DEMO = [
    ("yellow", True, "This week\n\nMon  write the release notes\nWed  dentist 14:30\nFri  ship 1.0"),
    ("pink", False, "Tandarts afspraak\nvrijdag 14:30, verwijzing meenemen\n@ 2027-01-15 13:30 leave for the dentist"),
    ("blue", False, "Reading list\n- [x] A Philosophy of Software Design\n- [ ] The Design of Everyday Things\n"
                    "- [ ] Thinking in Systems"),
    ("green", False, "Groceries\n- [x] oat milk\n- [ ] bread\n- [ ] coffee beans\n- [ ] lemons"),
    ("orange", False, "Ideas\nA bar widget for the next deadline.\nKeyboard first, mouse optional."),
    ("purple", False, "Everything here stays on this laptop.\nSearch works with Wi-Fi off."),
]


def load_stickies():
    import importlib.util
    spec = importlib.util.spec_from_file_location("stickies", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seed(state, visible, shot, hidden=0, screen_w=1920, screen_h=1080, empty=False):
    """Fill a fresh DB. Returns the id of the note to drag/type into."""
    os.environ["STICKIES_STATE"] = state
    st = load_stickies().Store()
    if empty:  # first run: no notes, the hint shows
        return 0
    if shot:
        spots = [(300, 170, 300, 240), (720, 210, 300, 200), (1140, 160, 320, 270),
                 (480, 520, 290, 190), (900, 540, 300, 210), (1310, 500, 320, 220)]
        for (color, pinned, body), (x, y, w, h) in zip(DEMO, spots):
            n = st.add(body, color=color, pinned=pinned, x=x, y=y, w=w, h=h)
            if color == "orange":  # one note rolled up to its first line
                st.roll(n["id"])
        st.db.commit()
        return 1
    colors = list(load_stickies().COLORS)[:6]
    cols = 8
    for i in range(visible):
        x = 40 + (i % cols) * 230
        y = 60 + (i // cols) * 190
        st.add(f"note {i}\n" + DEMO[i % len(DEMO)][2], color=colors[i % 6],
               x=x % (screen_w - 240), y=y % (screen_h - 200))
    # Notes parked on a workspace nobody is on: in the model, not drawn.
    for i in range(hidden):
        st.add(f"elsewhere {i}", color=colors[i % 6], workspace=99, x=100 + i % 900, y=100 + i % 500)
    # The note under test, on top.
    nid = st.add("", color="yellow", x=760, y=400, w=260, h=200)["id"]
    st.db.commit()
    return nid


def wallpaper():
    p = os.path.expanduser("~/.local/state/omarchy/current/background")
    return os.path.realpath(p) if os.path.exists(p) else ""


def run(args):
    if not shutil.which("quickshell"):
        sys.exit("quickshell not found")
    tmp = tempfile.mkdtemp(prefix="stickies-bench-")
    state = os.path.join(tmp, "state")
    conf = os.path.join(tmp, "shell")
    os.makedirs(state)
    os.makedirs(conf)
    try:
        nid = seed(state, args.visible, bool(args.shot), args.hidden, empty=args.empty)
        shutil.copy(os.path.join(HERE, "tests", "shell", "harness.qml"), os.path.join(conf, "shell.qml"))
        for name in ("Commons", "Ui"):  # Ui: the overlays the service builds
            os.symlink(os.path.join(SYSTEM_SHELL, name), os.path.join(conf, name))
        os.symlink(HERE, os.path.join(conf, "stickies"))
        env = dict(os.environ, STICKIES_STATE=state, STICKIES_CMD=f"python3 {LAUNCHER}",
                   STICKIES_LAYER="overlay", BENCH_NID=str(nid),
                   BENCH_MODE="shot" if args.shot else "bench",
                   BENCH_WALL="" if args.plain else wallpaper() if args.wall is None
                   else os.path.abspath(args.wall) if args.wall else "",
                   BENCH_DARK="1" if args.dark else "0", BENCH_LIGHT="1" if args.light else "0")
        if args.no_type:
            env["BENCH_TYPE"] = "0"
        elif not args.shot:
            env["STICKIES_FOCUS"] = "exclusive"
        proc = subprocess.Popen(["quickshell", "--no-duplicate", "-p", conf], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        result, log = None, []
        deadline = time.time() + 60
        try:
            for line in proc.stdout:
                log.append(line.rstrip())
                if "BENCH_READY" in line and args.shot:
                    time.sleep(0.6)
                    mon = subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True)
                    name = json.loads(mon.stdout)[0]["name"]
                    out = os.path.abspath(args.shot)
                    subprocess.run(["grim", "-o", name, out], check=True)
                    result = {"screenshot": out}
                    break
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
        warnings = [l for l in log if " WARN" in l or " ERROR" in l]
        if warnings:
            result["qml_warnings"] = warnings
        return result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--visible", type=int, default=40, help="notes on screen besides the one under test")
    ap.add_argument("--hidden", type=int, default=0, help="extra notes on another workspace (loaded, not drawn)")
    ap.add_argument("--shot", metavar="PNG", help="write a screenshot of demo notes instead of benchmarking")
    ap.add_argument("--dark", action="store_true", help="with --shot: render with a dark theme palette")
    ap.add_argument("--light", action="store_true", help="with --shot: render with a light theme palette")
    ap.add_argument("--wall", help="with --shot: backdrop image (default: your current wallpaper; "
                                   "'' = the theme's background colour)")
    ap.add_argument("--empty", action="store_true", help="with --shot: no notes (the first-run hint)")
    ap.add_argument("--plain", action="store_true", help="with --shot: the theme background, no wallpaper")
    ap.add_argument("--no-type", action="store_true",
                    help="drag only; don't take keyboard focus (use while someone is at the keyboard)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    r = run(args)
    if args.json or args.shot:
        print(json.dumps(r, indent=2))
        return
    d, t = r.get("drag", {}), r.get("typing", {})
    fi = d.get("frame_interval_ms") or {}
    print(f"notes loaded: {r.get('notes')}, shell start -> note on screen {r.get('startup_to_card_ms')} ms")
    print(f"drag: {d.get('frames')} frames for {d.get('ticks')} ticks, interval "
          f"p50 {fi.get('p50')} p95 {fi.get('p95')} p99 {fi.get('p99')} max {fi.get('max')} ms, "
          f"missed {d.get('missed_frames')}; JS per tick p95 {(d.get('js_work_per_tick_ms') or {}).get('p95')} ms")
    e = r.get("external") or {}
    print(f"external `stickies add`: card on screen {e.get('cli_start_to_card_ms')} ms after the CLI call "
          f"(CLI itself {e.get('cli_exit_ms')} ms), appeared {e.get('appeared')}")
    if not t:
        pass
    elif "error" in t:
        print("typing:", t["error"])
    else:
        k = t.get("text_to_swap_ms") or {}
        print(f"typing: {t.get('keys_seen')}/{t.get('keys_sent')} keys, text->frame swap "
              f"p50 {k.get('p50')} p95 {k.get('p95')} max {k.get('max')} ms, saved {t.get('body_saved')}")


if __name__ == "__main__":
    main()
