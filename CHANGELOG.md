# Changelog

All notable changes. Versions follow `manifest.json`; dates are ISO.

## 1.3.1 - 2026-10-07

### Security

- Your notes are private to your user. Under the usual umask 022 the state
  folder (`~/.local/state/stickies`) was created 0755 and `stickies.db`,
  its `-wal` and `-shm`, `stickies.log` and `integration.json` 0644, so
  any other account on the machine could read every note. The folder is
  now 0700 and every file in it 0600, whatever the umask. An existing
  install is repaired the next time stickies opens its database (any
  `stickies` command, or the shell starting `serve`). Only files and
  folders you own are changed, and never through a symlink, so a
  `STICKIES_STATE` that points at a shared or someone else's folder is
  left as it is. `~/.cache/stickies` holds no note text (the model, the
  venv, byte code) and keeps its mode.

## 1.3.0 - 2026-10-06

### Fixed

- onnxruntime's telemetry is off. Its PyPI builds wrote a device id and an
  event queue to `~/.cache/Microsoft/DeveloperTools/.onnxruntime` and
  uploaded session, model and system events (host name, the venv's path,
  timings) to `mobile.events.data.microsoft.com`. That was measured: 8
  connections in 25 s of `serve`. Stickies now sets
  `ORT_DISABLE_TELEMETRY=1` in every process that loads onnxruntime, which
  means no files and no connections. `stickies uninstall --purge` leaves the
  shared folder that older versions caused, prints its path and says why
  (docs/search.md).
- `stickies setup` no longer leaves pip's cache in `~/.cache/pip`.
- The install test no longer depends on whether a model is installed: it
  runs against a stand-in onnxruntime that writes like the real one.

### Changed

- The search venv is pinned. `requirements-search.txt` lists onnxruntime
  1.30.0, numpy 2.5.3, tokenizers 0.23.2 and sentencepiece 0.2.2, one
  sha256-checked wheel each (CPython 3.14, Linux x86_64), installed with
  `--require-hashes --only-binary=:all: --no-deps`. `stickies setup` shows
  the versions in its plan and refuses on another Python or CPU before
  downloading anything. An existing venv keeps working; to switch to the
  pinned one, delete `~/.cache/stickies/venv` (or an old `.venv` next to
  the checkout) and run `stickies setup`.
- No symlink in the plugins folder, for any route. The git route is now
  `git clone <url> ~/.config/omarchy/plugins/eastbluewizard.stickies`,
  then that folder's `install.sh`. It refuses to run anywhere else and
  only adds the extras (`stickies integrate --yes --install`). A
  development checkout is installed with `omarchy plugin add <path>`
  (CONTRIBUTING.md). `omarchy-plugin-validate` passes on the installed
  folder, which is tested.
- An install from before 1.3.0 that symlinked a checkout into the plugins
  folder is migrated by the next `install.sh` or `stickies integrate`: the
  link becomes a `git clone` of that checkout, the notes are untouched,
  and `~/.local/bin/stickies` points into the clone.
- manifest: author `eastbluewizard-official`, a shorter description.
  README, LICENSE and the export use the same handle.

### Added

- README "Updating": `omarchy plugin update eastbluewizard.stickies` or
  `git pull`, then `omarchy-restart-shell`. Measured: the shell sees an
  update and reloads its plugins, but gets its cached QML back
  (`Qt.clearComponentCache` doesn't exist in Quickshell 0.3.1), so new
  QML only loads with a restart. `serve`, which that reload restarts on
  the new code, notices this and sends one notification. A key that hits
  "Function not found" says the same.
- `stickies --version` (from manifest.json; `--json` before it for
  `{"version", "plugin_dir"}`).

## 1.2.0 - 2026-10-06

### Added

- `stickies setup --idle-unload MINUTES`: how long serve keeps the
  embedding model loaded without a search (default 10; 0 keeps it loaded).
  Stored as the `idle_unload` setting; `STICKIES_IDLE_UNLOAD` overrides it.
- `stickies setup --status` also shows the idle time and whether the
  running serve has the model loaded right now, since when and until when
  (`serve` field in `--json`; `{"op": "model"}` on serve's socket).

### Changed

- serve drops the embedding model after 10 minutes with no search and no
  embedding and hands the memory back (`malloc_trim`): RSS 245-272 MB ->
  87 MB on 2,003 notes, 0 wake-ups once it is gone. The
  next search loads it again in the background (~0.5 s): words-only hits
  come at once, by-meaning hits fill in when it is back (the search
  overlay and the chat's note preview ask again by themselves). A note
  whose text changed meanwhile also loads it and is embedded; a move
  doesn't.
- The `semantic` event carries `loaded`: `{"on": true, "loaded": false}` at
  start when search by meaning is installed and after an unload,
  `{"on": true, "loaded": true}` when the model is in. `ping`'s `semantic`
  still means loaded right now.
- A cold `stickies search` refused by an unloaded serve starts the reload
  for the next one.
- README: the version badge said 1.0.0.

## 1.1.0 - 2026-10-06

### Added

- Tags (buckets) on notes: lower-case `a-z`, `0-9` and `-`, up to 32
  characters. `stickies add --tag work --tag cardmarket "..."`,
  `stickies tag <id> work [more]`, `stickies untag <id> work`,
  `stickies tags` (every tag with its count, most used first). Note rows in
  `--json` carry `"tags": [...]` (`[]` when none). Tags are in the search
  index, so searching `work` finds notes tagged work, and `search --tag X`
  / `list --tag X` narrow to notes that have every given tag.
- On the desktop, tags show as small chips under a note's header (in the
  header when it is rolled up), in the free layout and the waterfall
  column. The header's tag button opens an editor that suggests the tags in
  use; Enter adds, Backspace in the empty field removes the last one. A
  click on a chip opens All notes filtered by it.
- All notes (`SUPER + ALT + O`): an overlay listing every live note on
  every workspace, pinned first then most recently edited, with colour,
  first line, tags, workspace, reminder and checklist progress. Tag chips
  with counts filter (several tags: notes that have all of them), a field
  filters by text as you type. Enter jumps to the note and flashes it,
  Delete archives it with Undo, Esc closes. Also in the bar panel (*All
  notes*), from `stickies list --open [--tag X]`, and over IPC
  (`omarchy-shell stickies list`, `listTag "work op-09"`).
- Chat may propose tags with a new note.
- `stickies integrate --refresh`.

### Changed

- `SUPER + ALT + K` was asked for, but Omarchy uses it for its tmux
  keybindings; All notes is on `SUPER + ALT + O`.
- A note that has tags but no text is no longer thrown away as empty.
- Database schema 5: a `note_tags` table and a `tags` column; the search
  index is rebuilt once, on first open, with tags in it.

### Fixed

- After an update, new keys failed with "Function not found" until the
  shell was restarted: the running shell kept the old plugin QML (disabling
  and enabling the plugin did not help). `install.sh` (in the live session)
  and `stickies integrate` now restart the shell when the plugin's QML/JS
  changed since the last install.
- `install.sh`'s final message lists every key, read from
  `hypr/stickies.lua`, instead of a hand-written subset.

## 1.0.0 - 2026-10-06

First public release.

### Added

- Installs with `omarchy plugin add <url> --enable`: the repository root is
  the plugin, and it runs its own `stickies` command by path, so nothing
  needs to be on `PATH`.
- A first-start card that offers, once, to add the keys and a `stickies`
  command in `~/.local/bin`. Nothing is added without a click.
- Keys added as runtime binds (`hyprctl eval`), with nothing written to
  disk. They are added again after every Hyprland config reload and removed
  when the plugin is unloaded. A key that is already bound is left alone.
- `stickies integrate`, `stickies keys on|off|status` and
  `stickies uninstall [--purge]`.
- The runtime keys cover all seven: N, SHIFT + N, V, J, A, L and W (with
  SUPER + ALT), the same as the drop-in.
- README for new users, CONTRIBUTING, issue templates, a GitHub Actions
  workflow that runs the tests.

### Changed

- Cold start of the `stickies` command: 88 ms -> 41 ms. A launcher caches
  the compiled `stickies.py` in `~/.cache/stickies/pycache`, and argparse
  no longer builds colour themes nobody sees.
- `stickies serve` no longer polls. Writers poke its socket, so an idle
  serve has 0 wake-ups a minute instead of ~240, and another process's
  `stickies add` reaches the desktop in ~68 ms instead of ~260 ms.
- Reminders no longer tick every second: serve wakes when the next one is
  due (and at least every 30 s while one is pending, for suspend).
- "Model loaded" is pushed by serve instead of being polled from QML.
- `stickies setup` puts its venv in `~/.cache/stickies/venv`, outside the
  plugin folder (a `.venv` that an older setup made next to `stickies.py`
  is still used).
- The plugin files moved from `shell/` to the repository root. Clone
  installs: run `./install.sh` once more and it re-points the link.
- `uninstall.sh` now runs `stickies uninstall`.
- Search and chat details, the serve protocol and the CLI reference moved
  from the README to `docs/`.

### Removed

- Duplicate helpers in the overlays (escaping, focused screen), now shared.

## 0.8.0 - 2026-10-06

- Checklists (`- [ ]` lines with a done count), undo for archive, colours
  that follow the Omarchy theme, roll-up to one line, tidy into a grid with
  undo and snapping while dragging, a note from the clipboard
  (`SUPER + ALT + V`), reminders (`@ tomorrow 9:00 ...`) sent by serve.
- Empty-note cleanup is now `stickies clean`; `stickies tidy` arranges.

## 0.7.0 - 2026-10-05

- Layouts: free, or a waterfall column on the left or right that floats
  above tiled windows (`SUPER + ALT + L`), foldable (`SUPER + ALT + W`),
  with reorder, drag out and an optional reserved strip.
- Find and Ask open and stay open: they keep the keyboard through the map.

## 0.6.0 - 2026-10-05

- Front mode: summoned notes come above tiled windows on every monitor and
  keep the keyboard until Esc, a click outside or a workspace change.
- Notes left empty are deleted. Key failures are logged to `stickies.log`.

## 0.5.0 - 2026-10-05

- First-run hint, performance report, README with screenshots, an
  install -> use -> uninstall test in a throwaway `HOME`, `tools/export.sh`.

## 0.4.0 - 2026-10-05

- Chat with your notes through Omarchy's default agent: you pick which
  notes are sent, answers cite them, proposals apply only on confirm.

## 0.3.0 - 2026-10-05

- Local search by meaning (multilingual embeddings on the CPU), fused with
  word search, and the search overlay.

## 0.2.0 - 2026-10-05

- Bar widget, quick capture, keys, dashboard card.

## 0.1.0 - 2026-10-05

- Notes on the desktop: drag, resize, type, recolour, pin, archive.
