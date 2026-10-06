# CLI

`stickies` is the command: a small launcher next to `stickies.py` that
caches its byte code in `~/.cache/stickies/pycache`, which halves the cold
start. A plugin install reaches it as
`~/.config/omarchy/plugins/eastbluewizard.stickies/stickies` until the
first-start set-up (or `stickies integrate --yes`) links it into
`~/.local/bin`. It needs Python 3's standard library and SQLite, nothing
else.

    stickies add "Cardmarket fees went up" [-c pink] [--tag work ...] [--pin] [--workspace 2] [--x/--y/--w/--h N]
    echo "from stdin" | stickies add          # no text and a tty -> $EDITOR
    stickies add --clipboard                  # the clipboard's text (wl-paste), at most 10,000 characters
    stickies edit 3 "new text" | --append "more" | --stdin
    stickies show 3
    stickies list [--archived | --all] [--workspace N] [--color C] [--pinned] [--tag T ...] [--limit N]
    stickies list --open [--tag T ...]   # open the desktop's All notes view (that filter preselected)
    stickies search cardm fee [--limit 20] [--archived] [--mode fts|semantic|hybrid] [--tag T ...]
    stickies tag 3 work cardmarket       # add tags (a-z, 0-9, -, at most 32 characters; "#Work" is "work")
    stickies untag 3 work
    stickies tags [--archived]           # every tag with its note count, most used first
    stickies pin 3 | unpin 3 | color 3 blue
    stickies roll 3 [--off]    # fold a note to its first line / open it
    stickies move 3 [--x --y --w --h --z N] [--workspace N] [--monitor eDP-1] [--raise]
    stickies rm 3 4            # archive (recoverable)
    stickies restore 3
    stickies undo              # restore the most recently archived note
    stickies purge 3 | --all-archived    # permanent; only archived notes unless --force
    stickies tidy [--monitor M] [--workspace N] | --undo   # notes in a grid, gaps_out apart; undo once
    stickies clean [--older-than SECONDS] # delete live notes with no text (nothing to lose)
    stickies reminders         # pending `@ <when>` reminders, soonest first
    stickies layout [free|waterfall-right|waterfall-left|next]   # no argument: print the current one
    stickies layout [--width 320] [--reserve on|off] [--collapsed on|off|toggle]
    stickies layout [--order 5,1,6] [--dock 3] [--undock 3]   # column order; in / out of the column
    stickies shell newNote|front|paste|find|chat|tidy|cycleLayout|collapse|...   # call the plugin; a failure is logged, exit 1
    stickies hub [--dry-run]   # publish the hub dashboard card now
    stickies setup [--yes] [--model NAME] [--status]   # install search by meaning (asks first)
    stickies setup --idle-unload MINUTES   # serve frees the model after this long unused (default 10; 0: never)
    stickies backfill          # embed every note whose vector is missing or stale
    stickies ask "what did I write about the Cardmarket fees?" [-k 6] [--notes 3,4] [--exclude 5] [--dry-run]
    stickies apply '{"action": "new_note", "body": "..."}'   # apply one proposal from `ask` (or on stdin)
    stickies integrate [--yes | --no]   # the first-start offer: keys + ~/.local/bin/stickies (no flag: status)
    stickies integrate --yes --install  # what install.sh runs: the keys as a drop-in file, enable the plugin
    stickies integrate --refresh        # restart the shell if the plugin's QML/JS changed since the last install
    stickies keys [on | off | status]   # the runtime keybindings (hyprctl eval; nothing on disk)
    stickies uninstall [--purge]        # undo everything outside the plugin folder; --purge: notes, model, venv too
    stickies serve                      # JSON lines on stdin/stdout for the desktop plugin, see serve.md
    stickies --version                  # e.g. "stickies 1.3.0 (<plugin folder>)", from manifest.json

`integrate` run by hand (`--yes`/`--no`) also replaces a plugin symlink
left by an install before 1.3.0 with a git clone of its checkout (shown as
`migrated` in `--json`). `uninstall --json` prints `{"done", "kept",
"next", "left"}`: `next` holds `omarchy plugin remove
eastbluewizard.stickies` when the plugin folder is there, and `left` holds
`{"path", "why"}` for anything it chose not to delete. Today that is only
onnxruntime's shared telemetry folder, and only with `--purge`.

Every command takes `--json` (before or after the subcommand). Output
follows one contract: one JSON document on stdout, DB column names
as keys, `null` for missing, `{"error": "..."}` and exit 1 on failure.
Writes print the note as it is afterwards (`rm`/`restore`/`purge` print a
list). A note's `tags` is a list of strings, `[]` when it has none; `tags`
prints `[{"tag": "work", "count": 3}, ...]`. Several `--tag` on `list` or
`search` mean notes that have all of them; searching a word also finds
notes tagged with it (tags are in the FTS5 index). `list --open` prints
`{"method", "ok", "output", "tags"}`; without `--open`, `list` always
prints the notes, so scripts and agents are unaffected. `layout` prints the settings (`layout`, `waterfall_width`,
`waterfall_reserve`, `waterfall_collapsed`, `waterfall_order`,
`waterfall_free`), the keys they are stored under.

## State

`$STICKIES_STATE` (default `~/.local/state/stickies/`):

- `stickies.db`: table `notes` (id, body, color, pinned, workspace, monitor,
  x, y, w, h, z, created_at, updated_at, archived_at, rolled, remind_at:
  the next reminder not yet sent), `reminders` (note_id, spec as written,
  due, fired_at), `meta` (the one-step tidy undo), the external-content
  FTS5 table `notes_fts` kept in sync by triggers, `embeddings` (one
  float32 vector per note: note_id, model, body_hash, vec; purge cascades)
  and `changes` (a change log that `serve` turns into events), `settings`
  (key, value as JSON: the layout and the waterfall column's width,
  reserve, collapsed, order and the notes dragged out of it; `idle_unload`,
  the minutes serve keeps an unused model loaded).
- `stickies.log`: one line per write.

- `integration.json`: the answer to the first-start offer, the plugin
  code hash at the last install, and the one the running shell loaded
  (`loaded`).
- `serve.sock`: the running serve's socket (see [serve.md](serve.md)).

`$STICKIES_CACHE` (default `~/.cache/stickies/`): `models/<name>/` (the
downloaded model), `model` (which one `setup` installed), `venv/` (the
Python packages `setup` installed) and `pycache/` (the launcher's byte
code).

Other variables: `STICKIES_VENV` (where the venv is; empty for none),
`STICKIES_MODEL`, `STICKIES_SEMANTIC=0` (words only), `STICKIES_THREADS`
(embedding threads, default 4), `STICKIES_IDLE_UNLOAD` (minutes; overrides
`setup --idle-unload`), `STICKIES_AGENT` (the chat agent command,
see [chat.md](chat.md)), `STICKIES_BIN`, `STICKIES_PLUGINS` and
`STICKIES_HYPR_DIR` (install targets; while `STICKIES_STATE` is set, the
plugins folder is only touched if `STICKIES_PLUGINS` names one),
`STICKIES_HUB` (see below). `ORT_DISABLE_TELEMETRY` is always set to 1
for onnxruntime ([search.md](search.md)).

Timestamps are ISO 8601 UTC with milliseconds (`2026-10-05T17:30:47.988Z`).
Colours are `yellow pink blue green orange purple gray` (`stickies colors`)
or any `#rrggbb`.

## Hub card (optional)

For people who run a `hub` dashboard command; without one on `PATH`,
nothing happens, and a chat `hub_todo` proposal fails on Apply with "hub
todo add failed: ... No such file or directory". `hub module set --name
stickies`: stat
tiles for live notes, pinned, and last edited (local `HH:MM` today, else the
ISO date; from `updated_at`, so moving a note counts), plus a text line with
the last-edited note's first line. Clicking a tile runs
`omarchy-shell -q stickies find`, which opens the search overlay.

It is published by `stickies hub`, by `serve` 2 s after the last change
(and once at start; that covers writes from agents too), and in the
background after each CLI write. Automatic publishing is off while
`$STICKIES_STATE` is set, so tests and benches never overwrite the real
card; `STICKIES_HUB` names the hub executable (empty disables, tests point
it at a fake). `hub` has no `module remove`, so uninstall leaves the last
card in place.
