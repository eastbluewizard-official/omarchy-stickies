# Contributing

Thanks for taking the time. Bug reports, small fixes and measured
improvements are all welcome.

## Ground rules

- **The CLI is the only writer.** QML never touches the database; it talks
  to `stickies serve` (see [docs/serve.md](docs/serve.md)).
- **Standard library only** in `stickies.py`. The embedding packages live
  in an optional venv that only the commands that embed use.
- **Nothing leaves the machine** except what chat sends, after the person
  sees it (and `stickies setup`'s one download, after a yes). No new
  network calls, accounts or API keys, and no telemetry from a dependency
  either: onnxruntime's is switched off before it loads.
- **Nothing outside our own folders.** Stickies writes to
  `$STICKIES_STATE`, `$STICKIES_CACHE` and the files install and set-up
  list (the drop-in, `~/.local/bin/stickies`), nowhere else; tests check it
  file by file.
- **Never edit a user's own config files.** Keys are runtime binds or the
  drop-in that `install.sh` adds and `stickies uninstall` removes.
- **Downloads are pinned.** The search venv comes from
  `requirements-search.txt` (exact versions, sha256, `--require-hashes`),
  the model from a fixed Hugging Face commit, checked by size and sha256.
  Moving a pin updates that file, LICENSE and the CHANGELOG.
- **Nothing writes inside the plugin folder at run time.** omarchy-shell
  reloads a plugin when a file in its folder changes. Caches go to
  `~/.cache/stickies`.
- **Smooth is measured, not assumed.** The budgets: drag and resize at the
  screen's frame rate, keystroke to glyph < 16 ms, search < 50 ms by words
  and < 300 ms with meaning on 2,000 notes, panels open < 100 ms. A change
  that moves a number updates [docs/PERF.md](docs/PERF.md) with real
  measurements, never estimates.

## Running your checkout

Keep your development checkout anywhere (say `~/src/omarchy-stickies`) and
install it the way users do, with Omarchy's plugin command pointed at the
local path:

    git clone https://github.com/eastbluewizard-official/omarchy-stickies ~/src/omarchy-stickies
    omarchy plugin add ~/src/omarchy-stickies --enable
    stickies integrate --yes     # or answer the first-start card

`omarchy plugin add` accepts a local path (`omarchy-git-url-check` only
refuses `ext::`-style helpers and unknown `scheme://` transports) and clones
it into `~/.config/omarchy/plugins/eastbluewizard.stickies`. So the live
plugin is a second checkout, a real folder with no symlinks in it, which is
what `omarchy-plugin-validate` and the marketplace expect. Commit on your
branch, then bring the commits you want into the live copy:

    omarchy plugin update eastbluewizard.stickies    # fetches your checkout's HEAD, shows the diff, fast-forwards

A symlink from the plugins folder to your checkout does not work: the
validator refuses it and the shell's watcher (`inotifywait -r`) doesn't
see changes through it. An install from before 1.3.0 that did this is
converted by the next `stickies integrate` or `install.sh` run from that
checkout: the link is replaced by a clone of it, and the notes stay where
they are.

The running shell keeps the QML it loaded. After an update it reloads its
plugins but gets the old components back (it can't clear Quickshell's
component cache), so QML changes need `omarchy-restart-shell`. The
plugin's `serve` restarts with the new Python and reports this once. To
try QML without touching your desktop, run the benches: each one runs the
plugin in its own `quickshell -p` with a temp state.

## Tests

    python3 -m unittest discover -s tests

Standard library only; nothing needs the network. Every test uses a temp
`STICKIES_STATE`, so your own notes are never touched. Install and uninstall
run against a temp `HOME` with every live-session command (`hyprctl`,
`omarchy-plugin-enable`, `omarchy-shell`, ...) replaced by a stub that
proves it was not called. Hyprland keybindings are tested against a fake
`hyprctl` (`STICKIES_HYPRCTL`).

- `tests/test_e2e.py`: the git route in a throwaway `HOME`: a checkout in
  the plugins folder, its `install.sh`, use, ask (fake agent), uninstall,
  then `--purge` leaves the `HOME` identical to a snapshot taken after the
  clone. `omarchy-plugin-validate` must pass on the installed folder.
- `tests/test_privacy.py`: nothing lands outside `$STICKIES_STATE` and
  `$STICKIES_CACHE` after a search, with a stand-in onnxruntime that writes
  its telemetry files the way the real one does, and with the real model
  when it is installed.
- `tests/test_integrate.py`: the `omarchy plugin add` route: first-start
  set-up, runtime keys, use, `stickies uninstall --purge`, and that nothing
  was written inside the plugin folder.
- Semantic search runs against a deterministic fake embedder.
  `RealModelTest` uses the installed model and is skipped, saying why,
  until `stickies setup` has run.
- Chat runs against a fake agent that records its stdin, so the tests check
  the exact prompt that would be sent.
- Tests that need `node` (the QML's JavaScript helpers) or
  `omarchy-plugin-validate` skip without them.

## Benches

The QML benches run the real plugin in their own short-lived `quickshell
-p` next to your live shell, on the Overlay layer, with a temp state. They
briefly cover your screen.

    python3 bench_cold.py                         # cold CLI start, import costs
    python3 bench_idle.py                         # idle serve: CPU and wake-ups
    python3 bench.py [--notes 2000] [--json]      # search: in-process, serve, cold CLI (+ meaning once set up)
    python3 bench_shell.py [--visible 40] [--hidden 2000] [--no-type] [--json]   # drag, typing, start-up
    python3 bench_bar.py                          # bar widget
    python3 bench_search.py                       # search overlay (needs the model)
    python3 bench_chat.py [--real]                # chat overlay (fake agent; --real uses claude -p)
    python3 bench_waterfall.py [--notes 40]       # layout switch glide + column scroll frame times
    python3 bench_polish.py                       # checklists, roll-up, tidy, snapping, theme switch

`bench_shell.py` without `--no-type` sends real key events with `wtype` and
holds the keyboard for about 3 s, so don't type while it runs.

## Screenshots

Made from demo notes in a temp state, never from your own:

    python3 bench_shell.py --shot docs/polish.png --dark --plain       # checklists, a reminder bell, a rolled-up note
    python3 bench_shell.py --shot docs/first-run.png --empty --light --wall plain.png   # plain.png: any solid colour image
    python3 bench_waterfall.py --shots docs/layout --output NAME --light --wall plain.png \
        --layouts free,waterfall-right,waterfall-left,waterfall-right+reserve   # crop the bar off, see below
    python3 bench_search.py --notes 200 --shot docs/search.png
    python3 bench_chat.py --notes 0 --real --shot docs/chat.png

The overlays are cropped to their panel, so nothing else on your screen ends
up in the picture. The layout shots are whole outputs: use a headless one
(`hyprctl output create headless`) with neutral test windows on it, and crop
the top bar off afterwards, since the bar shows your own widgets.
`preview.png` (the marketplace and README picture) is the waterfall-right
shot.

## Pull requests

Keep them small and say what you measured. Run the tests first. For UI
changes, add a screenshot. For anything on a hot path (drag, typing, search,
panel open, start-up), add before and after numbers from the bench that
covers it.
