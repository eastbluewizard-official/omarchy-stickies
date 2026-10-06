# Performance: measured against the budget

Machine: i7-8550U (4 cores / 8 threads, AVX2, no GPU), Intel UHD 620, 15 GB
RAM, eDP-1 1920x1080 at 60.02 Hz (16.66 ms a frame), Omarchy on Hyprland,
Python 3.14, onnxruntime 1.30, Hyprland 0.56.2, Quickshell 0.3.1. Measured
under normal load (other apps open, nobody typing). Each number comes from
the bench named next to it. Where a value is a range, it covers all the
runs listed; no run was dropped. Rows measured for 1.0.0 (2026-10-06) say
so; the rest are from 2026-10-05 and cover code 1.0.0 did not change (the
note cards, drag and typing paths, the overlays' QML).

The embedding model (multilingual-e5-small int8, the default) came from
`stickies setup --yes` (on 2026-10-05 into a throwaway `STICKIES_VENV` /
`STICKIES_CACHE`; on 2026-10-06 the owner's installed copy, read only).
Every bench uses a throwaway `STICKIES_STATE`.

## 1.2.0: idle unload of the model (`bench_unload.py`, 2026-10-06)

2,003 notes (bench.py's generated ones plus three real ones), all embedded
with the default model (multilingual-e5-small int8), a fresh copy of that
state for each serve. Requests go straight to serve's stdin; times are
request -> answer line on its stdout. Two full runs on the final code; the
table shows the second, the first was within a few percent on every row
(e.g. first search after unload: by meaning at 530 vs 557 ms). Before = 1.1.0
(`main` at 25385bc) run the same way.

| what | 1.1.0 | 1.2.0 |
|---|---|---|
| serve RSS, model loaded | 271.4 MB (222.5 anon + 48.9 file) | 245.3-271.8 MB (199.0-222.8 anon) |
| serve RSS, model unloaded after 2 min idle | (never unloads) | **87.4 MB** (39.7 anon + 47.6 file) |
| serve RSS, model never loaded (lazy start, nothing searched yet) | | 17.5 MB |
| wake-ups (context switches, all threads) per minute, loaded | 9 in 60 s (1.0.0's row above) | 6 |
| wake-ups per minute after the unload | | **0** |
| first search after unload: request -> words-only answer (20 hits) | | **4.4 ms** |
| ... -> model loaded (`semantic` event) | | 524 ms |
| ... -> by-meaning answer (asked again on that event, as the overlay does) | | **557 ms** |
| first search right after start, model preloaded (as in 1.1.0) | | 20.8 ms |
| first search right after start, lazy load: words / by meaning | | 1.3 / 713 ms |

**Freeing the memory.** Dropping the Embedder alone did not give it back:
in a loop of load -> embed -> drop (`gc.collect()`), RSS went 237 -> 146 MB,
glibc kept the freed arenas. `malloc_trim(0)` through ctypes then took it
to 72 MB, where the private part (RssAnon) is 27 MB against 193 MB loaded;
the other ~43 MB are the onnxruntime/numpy shared libraries' file pages,
which stay mapped once imported (Python can't unload an extension module)
and which the kernel can drop under pressure. 20 more load/unload cycles
added 4 MB. That was good enough not to move the model into a child
process: a child would save those ~45-70 MB more, but costs a second
Python start (~+0.1 s on every reload), a pipe protocol for every query
embedding, and a second place for the vectors. In serve the unloaded RSS is
87 MB (40 MB anon: serve's own state, the SQLite cache, the 2,003 notes'
rows); never loaded it is 17.5 MB.

**Preload or lazy at start.** Measured both: lazy keeps serve at 17.5 MB
until the first search, but that search then gets its by-meaning hits after
713 ms instead of 21 ms. That is the cost every search after an idle unload
pays anyway, but the first search after logging in is a common one, so serve
still loads the model right after `ready` (as 1.1.0 did) and the idle clock
starts then: 10 minutes without a search and it goes.

**No timer once it is gone.** While the model is loaded, serve's `select()`
sleeps for the time left until the unload (one wake-up at the end, plus
re-sleeps if the indexer embedded meanwhile); the 6 switches a minute are
onnxruntime's threads. After the unload: 0 in 60 s, as with the model off.
The indexer's load wakes the main loop through a pipe, so the timer starts
even when nothing else happens (found in testing: without it, an idle serve
would have slept with no timeout and kept the model for good).

## 1.1.0: All notes and tags (`bench_list.py`, 2026-10-06)

2,003 notes (bench.py's generated ones, ~450 characters on average, plus
three real ones), each with 0-3 of 12 tags, 10% checklists, 1% pinned; the
real `Service.qml` and `List.qml` in their own `quickshell -p`, 60.02 Hz
screen. Opening, filtering and chip clicks are calls into the overlay
(no key injection); scrolling moves the list 24 px per frame, top to
bottom, 299 frames. Three runs; each cell is the range over them.

| what | p50 | p95 | max | budget |
|---|---|---|---|---|
| open (open() -> first swapped frame, with last time's rows), 6x a run | 45.6-50.1 ms | 49.0-54.7 ms | 54.7 ms | < 100 ms: holds |
| first open (no rows yet) -> first frame / -> 2,003 rows on screen | | | 27.7-38.4 ms / 111.7-115.2 ms | |
| open -> serve's fresh rows landed in QML (asked after the first frame) | 92.5-94.3 ms | 98.9-101.4 ms | 101.4 ms | |
| scrolling, gap between frames (16.66 ms = every frame) | 16.6-16.7 ms | 17.3-17.4 ms | 17.6-21.2 ms | frame rate: holds |
| filter keystroke -> frame, 24 keys | 15.6-16.1 ms | 18.3-18.6 ms | 19.6 ms | |
| tag chip click -> frame, 10x | 17.7-18.0 ms | 18.2-18.8 ms | 18.8 ms | |
| Enter -> overlay closed, note flashing | | | 43.2-44.5 ms | |

What changed on the way there (same bench):

- A first version sent every note's first 2,000 characters for the text
  filter (2.1 MB for these notes) and asked for them before mapping: the
  warm open's first frame waited for that JSON, p50 77.8 ms. Rows now carry
  a lower-cased `hay` of the first 500 characters + tags and a title cut at
  120 (1.3 MB here, against 1.5 MB for full rows; real notes are much
  shorter), and the request goes out after the first frame.
- Reassigning the filter to a new, equal `[]` on every open rebuilt the
  2,000-row view: warm open p50 55.3-57.3 ms -> 50.5-52.1 ms (two runs each)
  once it is only assigned when it changes; the shorter rows then gave
  45.6-50.1 ms.
- An unchanged refresh is not reassigned (it also reset the scroll).

Tags in the store, 2,000 notes with 0-3 tags each, in-process, 30 runs:
FTS search 2.2 ms p50 / 2.5 ms max, with `--tag` 1.8 / 1.9 ms, a word that
is a tag 2.1 / 2.3 ms; `list --tag a --tag b` 0.9 / 1.1 ms; `tags` (counts)
1.0 / 1.1 ms. The FTS index now has a second column (`tags`); the
`test_bench` budget run (2,000 notes, FTS p95 < 50 ms) still passes.

## 1.0.0: start-up, idle, cold CLI (2026-10-06)

Measured after rebasing onto 0.8.0 (layouts, checklists, reminders), same
day, same machine, against 0.8.0 itself (`main` at cab3aed, run from a
copy) so both columns saw the same load.

| what | 0.8.0 | 1.0.0 | bench |
|---|---|---|---|
| cold `stickies list --json` (20 notes) | p50 88.3 ms | **40.6 ms** (launcher), 69.1 ms (`python3 stickies.py`) | `bench_cold.py`, 20 runs each |
| cold `stickies search --mode fts` (20 notes) | p50 87.2 ms | **42.8 ms** (launcher) | `bench_cold.py` |
| cold `stickies add` | p50 93.8 ms | **44.4 ms** (launcher) | `bench_cold.py` |
| shell start -> first note on screen, 41 notes, 3 runs | 306-319 ms | 258-307 ms | `bench_shell.py --no-type` |
| another process's `stickies add` -> card on screen, 3 runs | 261-262 ms (the CLI 104-108 ms, then the 250 ms poll) | **65-71 ms** (the CLI 56-63 ms, then a poke) | `bench_shell.py --no-type` |
| drag, 41 notes, 300 frames, 3 runs | p99 18.8-19.4 ms, 0 missed | p99 18.8-19.5 ms, 0 missed | `bench_shell.py --no-type` |
| idle `stickies serve`, 60 s, model off | 20 ms CPU, 242 context switches (4/s) | **0 ms CPU, 0** | `bench_idle.py` |
| the same with one reminder pending | 20 ms CPU, 241 | **0 ms CPU, 2** (the 30 s check) | `bench_idle.py --reminder` |
| layout switch to waterfall, 40 notes, 10 switches x 3 runs: first frame p50 / settled p50 | 53.3-55.2 / 179.3-182.1 ms | 51.8-54.1 / 176.7-177.9 ms | `bench_waterfall.py` |
| `python3 -c pass` (the floor) | 11.8 ms | 11.8 ms | `bench_cold.py` |

Start-up hardly moves since 0.8.0: most of it is now QML (the waterfall
column's cards are built ahead of time), not the Python start of serve.
Drag p99 is ~19 ms on both builds today against 17.2-17.6 ms on
2026-10-05; the same code paths, so it is the machine's load, not this
release. No frame was missed in any run.

A first layout-switch run after the rebase was ~10 ms slower (first frame
p50 62-70 ms). Bisecting found the input, not the code: the new demo
notes have checklists, and `bench_waterfall.py` seeded its 40 bench notes
from them. 0.8.0's own code with those notes measured 65 ms too, and
neither the 1.0.0 Python, nor Service/Desktop, nor the plugin being the
repo root moved it. The bench notes are plain text again, as when the
Layouts numbers below were taken. Checkbox rows do make a card dearer to
build. Seen here as ~10 ms on the switch's first frame for 40 notes, a
third of them checklists: still well inside the 200 ms the glide was
designed for.

Before the rebase (2026-10-06, against 0.6.0, the code without layouts):
cold `list` 75.2 -> 36.2 ms, `search --mode fts` on 2,001 notes 76-93 ->
40.1 ms (`bench.py`), cold hybrid search with serve running 149 -> 137 ms
and without serve 1,071-1,186 -> 911 ms, start-up 297-328 -> 152-173 ms,
external add 209-260 -> 55-57 ms, idle serve with the model loaded: 80 ms
CPU and 9 context switches in 60 s (onnxruntime's threads).

Where the cold start went (`python3 -X importtime`, `bench_cold.py`): of
~75 ms (0.6.0), ~12 ms is the interpreter, ~20 ms compiling the 2,400-line
`stickies.py` (32 ms for 0.8.0's 3,100 lines) (a script is compiled on every run; only imported modules
get cached byte code) and ~16 ms argparse pulling in `_colorize`
(+ `dataclasses`, `inspect`) and `shutil` to build help formatters nobody
sees. What changed:

- **argparse:** a formatter that only works out colour and terminal width
  when help is actually printed, and `add_subparsers(prog=...)` so the
  usage line isn't formatted at start. `hashlib` is imported only
  when a hash is needed. Nothing heavy is imported at start: numpy and
  onnxruntime load only in the venv, only for a command that embeds.
  Together, on 0.6.0's code: 75 -> 54 ms through `python3 stickies.py`.
- **The `stickies` launcher:** compiles `stickies.py` once and keeps the
  byte code in `~/.cache/stickies/pycache`, keyed on its mtime and size.
  54 -> 36 ms (0.6.0's code; 69 -> 41 ms on today's). Not `__pycache__` next to the code: in a plugin installed with
  `omarchy plugin add`, any write inside the plugin folder makes
  omarchy-shell reload every plugin (it runs `inotifywait -r` on
  `~/.config/omarchy/plugins`).
- **No split into a package.** Considered and measured against the
  launcher: a package's gain is the same cached byte code (above), it would
  need the same out-of-folder cache to avoid the reloads, and the tests
  patch module-level names (`stickies.run_agent`, ...) that a split would
  scatter. So `stickies.py` stays one file.
- **No polling.** serve used to wake every 250 ms to check `PRAGMA
  data_version` for other processes' writes. Now every write pokes
  `serve.sock` after it commits, and serve sleeps in `select()` with no
  timeout (it keeps a 1 s DB check only if the socket can't be opened).
  The QML's 1.5 s "is the model loaded yet" poll became a `semantic` event
  from serve. 0.8.0's reminders ran on a 1 s tick; now serve plans its
  next wake-up from the earliest pending reminder (re-planned after every
  change), checking at least every 30 s while one is pending because the
  monotonic clock stops during suspend. With nothing to do, no timer
  repeats in the plugin either (the overlays' 100 ms focus check runs only
  while an overlay is open).

Hybrid search through serve was re-run too (1 run, 2,001 notes): p50
18.5 ms, p95 41.4, max 55.9 (hybrid); semantic only p50 31.8, p95 61.2,
max 66.1, slower than the 2026-10-05 runs (14.3-22.0 ms p50). The search
path did not change, and the hybrid numbers sit inside the old range. The
semantic-only numbers are under the budget but were not re-run to see
whether that was load.

## The budget (from CLAUDE.md)

| budget | path | measured | verdict |
|---|---|---|---|
| drag / resize at the compositor's frame rate | 41 notes on screen, 300 frames, 2 runs | frame interval p50 16.66 ms, p99 17.2-17.3 ms, max 17.4-17.8 ms; **0 frames missed** | holds |
| | 41 on screen + 2,000 on another workspace, 1 run | p99 17.6 ms, max 17.8 ms, 0 missed | holds |
| | 151 on screen, 1 run | p99 17.3 ms, max 18.1 ms, 0 missed | holds |
| | snapping on (2026-10-06), 41 and 151 on screen, 4 runs each (2 before, 2 after rebasing onto the layouts) | p99 17.2-17.7 ms, max 18.9 ms, 0 missed | holds |
| keystroke to glyph < 16 ms | 41 notes, real key events (`wtype`), 59 keys x 6 runs | p50 1.3-1.4 ms, p95 1.6-1.9 ms, p99 1.7-3.2 ms; max 2.3-5.3 ms in 4 runs, **18.7 / 19.6 ms in 2 runs (one key each)** | holds at p99; 2 of 354 keys over (see below) |
| | 151 notes, 1 run | p50 3.4 ms, p95 4.4 ms, max 10.3 ms | holds |
| search < 50 ms, FTS, 2,000 notes | `serve` round-trip (the plugin's path), 205 queries x 4 runs | p50 2.5-3.2 ms, p95 3.7-4.9 ms, max 4.8-8.4 ms | holds, ~10x headroom |
| | cold `stickies search --mode fts` process (an agent) | p50 40.1 ms, max 63.9 ms (1.0.0; was 76-93 ms) | **holds since 1.0.0**, see above |
| search < 300 ms with embeddings, 2,000 notes | `serve` round-trip, hybrid, 83 queries each embedded fresh, 4 runs | p50 17.8-39.5 ms, p95 22.8-54.5 ms, max 31.5-64.9 ms | holds, ~5x headroom |
| | search overlay: keystroke -> changed list on screen, 72 keys | p50 41.8 ms, p95 83.1 ms, max 99.4 ms | holds, ~3x |
| | cold `stickies search` process (hybrid), serve running, 20 queries | p50 137 ms, p95 170 ms, max 180 ms (1.0.0) | holds |
| | cold `stickies search` (hybrid), no serve running | p50 911 ms (1.0.0; was 1,071-1,186) | over; model load, see below |
| panel open < 100 ms | search overlay, open() -> first swapped frame, 5x (with OverlayFocus, #135) | p50 22.6 ms, max 33.3 ms | holds, ~3x |
| | chat overlay, 5x x 2 runs (with OverlayFocus, #135) | p50 22.3-23.4 ms, max 27.0-28.1 ms | holds, ~3.5x |
| | bar panel, 5x x 2 runs | p50 39.7-41.4 ms, max 47.6-50.0 ms | holds, 2x |
| | front mode: SUPER+ALT+SHIFT+N's command -> stickies surface on the Top layer (`hyprctl layers`), 10x | 35-41 ms, median 36 ms; back to Bottom: median 40 ms | holds, ~2.5x |
| layout switch: glide < 200 ms at frame rate (ticket) | free -> waterfall, 40 notes, eDP-1, 10x x 2 runs: switch -> every card landed | p50 169-176 ms, max 176-185 ms (first frame p50 47-52 ms: mapping the column surface); frame interval p50 16.3 ms, max 23.3-26.2 ms; **1 frame over 25 ms in 20 switches** | holds |
| | waterfall -> free, same runs | p50 138-139 ms, max 144-146 ms; frame interval p50 16.6 ms, max 23.2-23.6 ms | holds |
| drag/scroll at the compositor's frame rate | scrolling a 40-note column (8,210 px) top -> bottom -> top, 12 px a frame, eDP-1, 2 runs | p50 16.66 ms, p99 17.3 ms, max 20.0-20.4 ms; **0 frames missed** | holds |

### What was over and what was done

- **Cold hybrid `stickies search` took ~1.1 s (fixed).** A fresh process
  has to load the model before it can embed the query. Measured on this
  laptop: importing numpy and onnxruntime takes ~150 ms, the sentencepiece
  tokenizer ~355 ms, the ONNX session ~230 ms, and the `.venv` re-exec
  starts Python a second time. A cold process can't get that under 300 ms.
  So `stickies serve`, which the desktop plugin keeps running with the
  model loaded, now also answers read-only searches on
  `$STICKIES_STATE/serve.sock`, and the CLI asks it first: **149 ms p50,
  200 ms max**. Without serve (no desktop session), the CLI loads the model
  itself, as before. Agents that only need words can pass `--mode fts`.
  (Pre-optimising the ONNX graph would save ~40-60 ms of the 230 ms
  session load; not worth a hardware-specific cached model.)
- **Cold `--mode fts` process: 76-93 ms (fixed in 1.0.0: 40 ms).** Nothing
  in it was search (the query takes ~2 ms); it was Python start-up,
  compiling the script and argparse. See "1.0.0" above.
- **Two keystrokes at 18.7 / 19.6 ms.** Both were in the first two
  41-note runs, one key out of 59 each time. The timing runs from the
  editor's text change to the next swapped frame, so a key that lands just
  after a frame has started waits for the following vsync. Four more runs
  (which also record the three slowest keys) stayed at a 2.3-5.3 ms max.
  No code path stands out, so nothing was changed.

## Overlay keyboard (#135, live)

Measured 2026-10-05 in the owner's session (eDP-1 only; HDMI-A-1 was
unplugged), Hyprland 0.56.2, `follow_mouse = 1`.

`tests/shell/focus_harness.qml` (`STICKIES_LIVE_TESTS=1`), 3 runs, both
overlays, each run identical within a few ms:

| step | search | chat |
|---|---|---|
| toggle -> field has the keyboard, while another Exclusive layer holds it for the first 250 ms | 285-286 ms | 286-295 ms |
| that layer takes the keyboard again after settling, then goes -> keyboard back | 8-36 ms | 35-36 ms |
| the field loses focus inside the overlay -> taken back | 64-70 ms | 63-64 ms |
| a second toggle at once (same press twice) | ignored | ignored |
| "abc" as Qt key events, then Esc | typed, closed | typed, closed |

Without anything competing (the first harness version), toggle -> keyboard
took 18 ms (search) and 22 ms (chat).

By hand, with three tiled `foot` windows on a scratch workspace and the
pointer warped over one of them: each bind's exact command was run
through `hyprctl dispatch hl.dsp.exec_cmd(...)`, the pointer was moved,
then text was typed with `wtype`. Overlay mapped on Overlay (level 3) 150 ms
after the dispatch (`hyprctl layers` polled every ~30 ms). The text landed
in the overlay's field, nothing reached the window under the pointer, and
Esc and the key again closed it. `wtype` chords cannot fire Hyprland binds
on this machine (even SUPER+ALT+SPACE does nothing), so this checks the
bind's command, not the physical key press.

The search bench in the same session (2,003 notes) measured keystroke ->
changed list on screen at p50 66.9 ms, p95 87.2 ms, max 95.1 ms, against
41.8 / 83.1 / 99.4 ms before. The key path didn't change in #135; the
p50 is noted here, not explained.

## Front mode (live, by hand)

Measured 2026-10-05 in the owner's session (eDP-1, plus a headless
1920x1080 output rotated with `transform = 1` at x=-1080 standing in for
HDMI-A-1, which was unplugged), with a Python loop that starts the bind's
command and polls `hyprctl layers -j` (one poll: median 4.4 ms, so each
number is up to ~4 ms late). 10 rounds each way, 300 ms apart.

| path | median | range |
|---|---|---|
| bind command (`omarchy-shell stickies front ... \|\| stickies shell front`) -> surface on level 2 (top) | 36 ms | 35-41 ms |
| same again -> back on level 1 (bottom) | 40 ms | 35-41 ms |
| `omarchy-shell -q stickies reload` (the IPC round trip alone) | 33 ms | |
| `stickies shell reload` (the logging wrapper, only run when the plain call fails) | 113 ms | |

The wrapper costs ~80 ms of Python start-up, so the binds call
`omarchy-shell` directly and only fall through to it on failure; routing
every key press through it measured 112 ms to the Top layer, over the
100 ms budget.

The notes draw the same on the Top layer as on Bottom (the drag and
keystroke benches above run on the Overlay layer, `STICKIES_LAYER=overlay`),
so those numbers carry over; they were not re-run for this change.

## Search (`bench.py`)

2,001 generated notes (8-120 words each, 6.3 MB DB with embeddings), 41
FTS queries (8 phrases plus every prefix of three words, as typed) x 5
reps. Semantic: the 42 sentences of the eval query set (development workspace only;
without it `bench.py` skips the semantic part). Four runs; runs 3
and 4 also time the cold CLI with serve running.

| what (2,001 notes) | p50 | p95 | max |
|---|---|---|---|
| FTS search, in-process | 1.9-2.2 ms | 3.1-3.7 ms | 4.2-6.9 ms |
| FTS search, `serve` round-trip | 2.5-3.2 ms | 3.7-4.9 ms | 4.8-8.4 ms |
| FTS search, cold `stickies search --mode fts` | 76-93 ms | | 77-98 ms |
| hybrid, `serve` round-trip, 41 as-typed + 42 sentences | 17.8-39.5 ms | 22.8-54.5 ms | 31.5-64.9 ms |
| hybrid, `serve`, the 42 sentences only | 16.0-34.3 ms | 20.4-50.2 ms | 26.3-60.7 ms |
| hybrid, repeated query (embedding memoised) | 6.2-10.0 ms | 11.6-26.3 ms | 12.3-43.8 ms |
| semantic only, `serve` round-trip | 14.3-22.0 ms | 17.8-41.3 ms | 20.2-69.8 ms |
| hybrid, cold `stickies search`, serve running (socket) | 149 ms | 178-196 ms | 196-200 ms |
| hybrid, cold `stickies search`, no serve (loads the model) | 1,071-1,186 ms | | 1,098-1,273 ms |
| add one note / move one note | 0.46-0.61 ms / 0.10-0.13 ms | | |
| `stickies backfill`, all 2,001 notes | 83.5-88.5 s (41.2-43.6 ms a note) | | |
| serve: model loaded after `ready` (background) | 766-998 ms | | |
| serve RSS with the model loaded | 273-281 MB | | |
| `add` through serve -> findable by meaning | 1,049-1,101 ms (1 s of it is the debounce) | | |

Run 3 was noisier than the others on the semantic rows; the higher end of
each semantic range comes from it.

## Desktop plugin (`bench_shell.py`)

Runs the real plugin in its own short-lived `quickshell -p`, next
to the live shell, on the Overlay layer so the compositor really presents
its frames. There is no pointer injection on this machine (no uinput or
ydotool), so the drag goes through the same `beginMove/moveTo/endMove`
path as the header MouseArea, one step per animation tick. Frame times are
the intervals between real `frameSwapped`s of the layer surface.

Typing uses real key events from `wtype` (a virtual keyboard, 45 ms apart,
59 keys) into a focused note. Latency runs from the editor's text change to
the next swapped frame. The harness takes exclusive keyboard focus for ~3 s
and stops `wtype` as soon as the note loses focus. Every run here saw
59/59 keys.

| drag, 300 frames | p50 | p95 | p99 | max | missed |
|---|---|---|---|---|---|
| 41 notes on screen (2 runs) | 16.66 ms | 17.06-17.10 ms | 17.23-17.31 ms | 17.41-17.80 ms | 0 |
| 41 on screen + 2,000 on another workspace (1 run) | 16.66 ms | 17.12 ms | 17.55 ms | 17.75 ms | 0 |
| 151 on screen (1 run) | 16.66 ms | 17.16 ms | 17.33 ms | 18.13 ms | 0 |

JS work per drag step: p95 0.05-0.08 ms.

| keystroke -> frame | p50 | p95 | p99 | max |
|---|---|---|---|---|
| 41 notes (6 runs) | 1.33-1.41 ms | 1.58-1.90 ms | 1.67-3.21 ms | 2.31-19.58 ms |
| 151 notes (1 run) | 3.37 ms | 4.44 ms | 4.61 ms | 10.30 ms |

Start-up (shell start -> first note on screen, including the Python start
of `stickies serve`): 152-173 ms with 41 notes (1.0.0; was 297-328 ms);
on 2026-10-05, 506 ms with 151 and 470 ms with 2,041 (2,000 of them on
another workspace; not re-run). A `stickies add` from another process
appears on the desktop 55-57 ms after the CLI is called (1.0.0; the CLI
itself takes 46-48 ms and pokes serve; was 209-260 ms with a 250 ms poll).
Drag in the same 1.0.0 runs: p99 17.24-17.35 ms, 0 missed frames.

## Bar widget (`bench_bar.py`)

Real `Service.qml` + `BarWidget.qml` with a stub bar, 2,002 notes, 2 runs.

| what | p50 | p95 / max |
|---|---|---|
| panel open (open() -> popup's first swapped frame), 5x | 39.7-41.4 ms | 47.6-50.0 ms |
| open -> pinned + recent lists filled (serve `list`) | 25-39 ms (one sample a run) | |
| quick capture -> note in the model, panel open, 5x | 16.4-16.5 ms | 23.3-26.8 ms |
| search round-trip from QML, 24 queries | 1.0-2.5 ms | 23.9-27.4 ms |

## Search overlay (`bench_search.py`)

2,000 generated notes plus three real ones, all embedded (84.7 s backfill),
then the real `Service.qml` with serve's model loaded (1,141 ms after
start). Keystrokes are text changes on the overlay's field (no key
injection): every prefix of six queries, each one embedded fresh by serve.
One run.

| what | p50 | p95 | max |
|---|---|---|---|
| open (open() -> first swapped frame), 5x | 28.3 ms | 32.7 ms | 32.7 ms |
| keystroke -> hybrid results in QML, 78 keys | 32.0 ms | 64.9 ms | 88.3 ms |
| keystroke -> changed list on screen, 72 keys | 41.8 ms | 83.1 ms | 99.4 ms |
| fast typing (60 ms a key): last key -> final results, 3 phrases | 42.1 ms | | 83.8 ms |
| Enter -> overlay closed, note raised and flashing | 159.8 ms (one sample) | | |

"dentist appointment" puts the Dutch "Tandarts afspraak" note first, as a
meaning-only hit. Enter -> flash was 82-84 ms in the run that built the
overlay. It has no budget, but it is noted here as slower in this one sample.

## Chat overlay (`bench_chat.py`)

2,000 generated notes plus three real ones, words-only retrieval, a fake
hub. The question is "When is the consignment payout due?" (on 2026-10-05 the same question named a person). The bench
unticks the last listed note, sends, then clicks Apply on a proposed new
note. The fake agent waits 400 ms, then streams a canned answer. `--real`
uses `claude -p` and sends only the seeded test notes.

| what | fake agent (2 runs) | claude -p (1 run) |
|---|---|---|
| open, 5x, p50 / max | 29.2-31.4 / 32.9-33.2 ms | 29.9 / 34.7 ms |
| Enter -> notes listed (serve `chat_notes`) | 39.1-41.5 ms | 45.5 ms |
| Enter -> list on screen | 42.0-43.4 ms | 46.7 ms |
| Send -> first streamed text | 429.7-429.9 ms (400 of it the fake's wait) | 2,797 ms |
| Send -> answer done, citations + proposal cards | 1,047-1,049 ms | 4,652 ms |
| Apply -> note created and in the model | 7.1 ms | (not clicked: it proposed only a hub todo) |

In every run, the unticked note never reached the agent, and the note
count did not change until Apply. With claude, the answer cited the right
note (`[#2]`) and proposed one valid hub todo.

## Layouts (`bench_waterfall.py`)

Measured 2026-10-05 with the code in this commit. Runs the real plugin in
its own `quickshell -p` against a throwaway state, on the real layers (the
column on Top above the windows of that output, free notes on Bottom),
`tests/shell/waterfall_harness.qml` driving it. Outputs: eDP-1 (1920x1080,
60.02 Hz) and a headless 1920x1080 output (`hyprctl output create headless`,
60 Hz) with two tiled test terminals on it, also rotated (`transform = 1`,
a 1080x1920 surface). The owner's HDMI monitor was unplugged; the headless
output stood in for it. The column was pinned to the output under test
(`STICKIES_COLUMN_SCREEN`), except in the focus-following check.

**Switch glide** (`python3 bench_waterfall.py --output NAME`): 40 notes,
free -> waterfall-right -> free, 10 rounds. "Landed" is the first swapped
frame on which every card sits where it is going.

Each row is 10 switches; times are p50 / max.

| output | direction | first frame | landed | frame interval p50 / p95 / max | over 1.5 frames |
|---|---|---|---|---|---|
| eDP-1, run 1 | to waterfall | 47.1 / 48.2 ms | 168.7 / 176.0 ms | 16.3 / 20.0 / 23.3 ms | 0 |
| | to free | 4.5 / 6.6 ms | 138.9 / 143.9 ms | 16.6 / 19.7 / 23.6 ms | 0 |
| eDP-1, run 2 | to waterfall | 51.9 / 56.0 ms | 176.0 / 184.7 ms | 16.3 / 19.0 / 26.2 ms | 1 |
| | to free | 4.9 / 5.6 ms | 137.9 / 145.5 ms | 16.6 / 20.4 / 23.2 ms | 0 |
| headless | to waterfall | 55.3 / 64.1 ms | 173.8 / 191.4 ms | 16.0 / 20.5 / 24.0 ms | 0 |
| | to free | 4.6 / 5.6 ms | 139.6 / 143.3 ms | 16.7 / 18.3 / 19.8 ms | 0 |
| headless, rotated | to waterfall | 51.7 / 53.5 ms | 173.4 / 174.9 ms | 16.3 / 17.7 / 23.1 ms | 0 |
| | to free | 5.2 / 5.9 ms | 139.4 / 142.8 ms | 16.7 / 18.0 / 20.0 ms | 0 |

How it got under 200 ms: the first version built the column's 40 cards
on the key press and glided for 180 ms. Its first frame came 91 ms after the
switch (p50), and it landed at p50 253 ms, max 287 ms (both directions
together). Now the column's cards for the
current workspace are built ahead of time in the background, and the
desktop keeps its cards, hidden. A switch then only flips visibility. What
is left in the first frame is mapping the column surface (~50 ms; about
the same with 6 notes). Animating the card size as well re-wrapped every
note's text each frame: 8 frames over 25 ms in 20 glides. So the glide
moves position only, for 130 ms.

**Scrolling a 40-note column** (same runs; content 8,210 px in a 1,030 px
column, 12 px a frame down and back up, `FrameAnimation` driving the same
`scrollBy` the wheel uses):

| output | frames | frame interval p50 / p95 / p99 / max | missed | JS per frame p95 |
|---|---|---|---|---|
| eDP-1, run 1 | 1,151 | 16.66 / 17.11 / 17.30 / 20.35 ms | 0 | 0.14 ms |
| eDP-1, run 2 | 1,151 | 16.66 / 17.09 / 17.32 / 20.04 ms | 0 | 0.13 ms |
| headless | 1,150 | 16.68 / 17.09 / 17.27 / 19.61 ms | 0 | 0.13 ms |
| headless, rotated (1,870 px column) | 1,017 | 16.67 / 17.11 / 17.25 / 21.46 ms | 0 | 0.13 ms |

**Live checks** (`--interact`, 6 notes, all passed on the headless output
and on eDP-1). Drags go through NoteCard's `beginMove/moveTo/endMove`, the
path the header MouseArea uses; there is no pointer injection here.

- Dragging the third card to the top reorders the column, and serve stores
  the order.
- Dragging a card out to (700, 300) takes it out of the column. The desktop
  draws it at exactly 700, 300, the DB has x=700 y=300, and the note is in
  `waterfall_free`.
- Dropping it back on the column re-docks it and leaves its free x/y as
  they were.
- Collapse parks the cards off screen, leaves 2 input regions (strip and
  handle), and is stored.
- Typing (headless run only, with real `wtype` keys): the caret in a column
  note gives the column surface the keyboard through the focus grab
  (editor focus and window active both true). The text lands in the note
  and is saved.
- `stickies layout waterfall-left` from another process reaches the
  surface in 249 ms (headless) / 112 ms (eDP-1); serve polled every 250 ms then (0.7.0; since 1.0.0 the CLI pokes serve).
- After switching back to free, the only note whose x/y/w/h differ from
  the seed is the one dragged out. All 6 desktop cards show.

**Focus following** (`--follow STK-1`, column not pinned): on eDP-1
(workspace 7) the column is on eDP-1. After `hl.dsp.focus({ monitor =
"STK-1" })` it is on STK-1 with that monitor's workspace 1, and back on
eDP-1 after focus returns.

**Screenshots** (`--shots`, over the two test terminals on the headless
output; in `docs/layout-*.png` the bar strip is cropped off and the images
are halved): free (notes under the windows), right, left, reserve on (the
terminals shrink by the 356 px exclusive zone), collapsed, and rotated.
## Polish pass: checklists, snapping, roll-up, tidy, clipboard, theme (`bench_polish.py`)

Measured 2026-10-06, same machine, two runs each. The drag numbers come
from `bench_shell.py --no-type` with this code, where every drag step now
snaps (8 px grid or another note's edge, with a guide line); the polish
numbers from `bench_polish.py`, which runs the real plugin in its own
`quickshell` with 22 notes and drives the same functions the pointer and
keys call (no pointer injection here), with a fake `wl-paste`. All 18 of
its checks passed in both runs.

**Re-measured after rebasing onto the layouts (#136) and overlay focus
(#135) work** (2026-10-06, same machine): the harness now also switches to
`waterfall-right` and checks, in the column, that the checklist note keeps
its 3 boxes, roll-up shrinks its slot to the 26 px header (the column's
110 px minimum height had left a gap; `waterfall.js` now knows rolled
notes), a column drag doesn't snap, and tidy is refused. 24 of 24 checks
passed in both runs.

| after the rebase | run 1 | run 2 |
|---|---|---|
| drag, 41 notes, snapping on: p95 / p99 / max, missed | 17.08 / 17.23 / 17.43 ms, 0 | 17.13 / 17.31 / 17.40 ms, 0 |
| drag, 151 notes: p95 / p99 / max, missed | 17.14 / 17.29 / 17.58 ms, 0 | 17.17 / 17.41 / 17.71 ms, 0 |
| typing in a checklist note, text change -> frame, p50 / p95 / max | 2.53 / 2.85 / 3.19 ms | 2.60 / 2.81 / 3.86 ms |
| the same in a plain note | 1.75 / 1.88 / 3.40 ms | 1.82 / 1.91 / 3.74 ms |
| checkbox click (JS) | 1.73 ms | 1.98 ms |
| tidy round trip | 15.7 ms | 13.2 ms |
| quick capture round trip | 15.5 ms | 14.9 ms |
| theme switch -> recoloured frame | 11.87 ms | 12.34 ms |

The theme switch moved from 7-8 ms to ~12 ms. Since #136 the plugin also
keeps the column surface and its prebuilt cards, which recolour with the
desktop's; that is the likely cause (not separately measured). It is
still under one frame. `bench_waterfall.py` (eDP-1) with
this branch: switch glide 0 frames over 1.5 intervals in 20 glides, column
scroll p99 17.26 ms, 0 missed; `--interact --no-type`: every check passed
(reorder, drag out, dock back, collapse, column focus, CLI follow, free
positions kept).

| drag, 300 frames, snapping on | p50 | p95 | p99 | max | missed |
|---|---|---|---|---|---|
| 41 notes on screen (2 runs) | 16.65-16.67 ms | 17.12-17.18 ms | 17.29-17.40 ms | 17.37-17.71 ms | 0 |
| 151 on screen (2 runs) | 16.67 ms | 17.27-17.29 ms | 17.61-17.72 ms | 18.82-18.89 ms | 0 |

JS work per drag step: p95 0.11-0.12 ms with 41 notes, 0.20-0.22 ms with
151 (was 0.05-0.08 ms without snapping); the snap itself is 0.01 ms p95
(300 steps over 22 notes' edges). **One thing moved and was fixed:** the
first version hid the guide lines with `visible` when no edge was near.
At 151 notes that put drag p95 at 18.4-18.6 ms (p99 19.1-19.2, still 0
missed); hiding the guides altogether gave 17.4 ms, so the toggling was
the cost (the scene graph re-batches every card when a node comes and
goes). The guides now stay mapped and are parked off-screen: 17.3 ms.

| what | run 1 | run 2 |
|---|---|---|
| typing into a checklist note, text change -> swapped frame, 60 keys (every key shifts all three boxes) p50 / p95 / max | 2.68 / 2.87 / 5.89 ms | 2.50 / 2.82 / 3.54 ms |
| the same in a plain note | 1.80 / 1.91 / 2.26 ms | 1.80 / 1.94 / 2.05 ms |
| click a checkbox -> text edited, saved, progress updated (JS) | 1.33 ms | 1.39 ms |
| tidy: request -> notes moved in the model (serve + hyprctl for gaps and the bar) | 17.1 ms | 16.3 ms |
| quick capture: request -> note in the model (serve runs wl-paste) | 10.9 ms | 11.4 ms |
| theme switch (`Color.background` changed) -> recoloured frame swapped | 7.72 ms | 7.18 ms |

Keystrokes here are text changes made by the harness, not real key events
from `wtype` (that run takes the keyboard for ~3 s; it was skipped because
this pass ran unattended next to a live session); the wtype numbers above
were not re-run. A note
without a `[` adds one `indexOf` per key. Reminders cost serve one indexed
query a second (`reminders(fired_at, due)`); not measurable next to its
250 ms poll. The bar panel gained one button; its open time was not
re-measured.

## Not measured here

- A real-pointer drag, real clicks on the header buttons, and click-through
  outside the notes: there is no pointer injection on this machine. Try
  them by hand after install.
- Keystroke-to-photon. That needs a camera; the numbers above stop at the
  swapped frame.
- Real pointer drags in the waterfall column, real wheel events and real
  clicks on the collapse handle or strip (see the live checks above for what
  was driven instead). Same reason: no pointer injection.
- Search-overlay Enter when the note is on another workspace (the bench
  doesn't switch your workspaces), and real key events in the overlays.

## Idle

`bench_idle.py` starts serve in a throwaway state (20 notes), lets it settle
5 s, then counts CPU time and context switches (all threads) over 60 s of
silence. 0.6.0's serve (`--script` pointed at the old file): 20 ms CPU and
239 context switches, the 250 ms `select()` timeout. 1.0.0: 0 and 0 with
the model off; with the model loaded, 80 ms CPU and 9 switches in 60 s
(onnxruntime's thread pool; nothing in stickies wakes up). The QML side has
no repeating timer while idle (the bar widget's 500 ms lookup runs only
until the service exists).

## Rerun

    python3 bench_cold.py                       # cold CLI start: launcher vs script, import costs
    python3 bench_idle.py [--script OLD/stickies.py]   # idle serve: CPU and wake-ups
    python3 bench_unload.py [--idle 2] [--old OLD/stickies.py]   # model RSS loaded / unloaded, reload, wake-ups (~6 min)
    python3 bench.py --json                     # + the semantic side once `stickies setup` has run
    python3 bench_shell.py --json               # add --no-type when someone is at the keyboard
    python3 bench_shell.py --json --hidden 2000 --no-type
    python3 bench_shell.py --json --visible 150
    python3 bench_bar.py
    python3 bench_search.py                     # needs the model; ~85 s of it is embedding
    python3 bench_chat.py [--real]
    python3 bench_waterfall.py [--output NAME]  # switch glide + column scroll; covers that output with notes ~15 s
    python3 bench_waterfall.py --interact [--no-type] [--output NAME]
    python3 bench_waterfall.py --follow OTHER   # moves monitor focus to OTHER for ~0.6 s, then back
    python3 bench_polish.py                     # checks + times the polish features; no keyboard focus

To bench with the model without installing it for real, point
`STICKIES_VENV` and `STICKIES_CACHE` at a scratch directory and run
`stickies setup --yes` there first.
