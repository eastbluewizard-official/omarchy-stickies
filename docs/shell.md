# The desktop plugin (eastbluewizard.stickies)

Sticky notes drawn on the desktop: a `service` that puts one layer-shell
surface per monitor on the **Bottom** layer, above the wallpaper and below
windows, and moves them to the **Top** layer, above windows, while you
summon them ([front mode](#front-mode)), a `bar-widget` (note count; click for pinned + recent notes,
quick capture and search), a search overlay (`SUPER + ALT + J`) that
finds any note by words or meaning, and a chat overlay (`SUPER + ALT + A`)
that answers from your notes through Omarchy's default agent.

![light](../preview.png)
![dark: a checklist, a reminder bell, a rolled-up note](polish.png)

The repository root is the plugin: `manifest.json`, the QML, and the
`stickies` command it runs (by its own path, so nothing has to be on
`PATH`). `omarchy-plugin-validate` passes on it.

## Install

Every route leaves the same thing in
`~/.config/omarchy/plugins/eastbluewizard.stickies`: a git checkout, a
real folder with no symlink in it (`omarchy-plugin-validate` refuses
symlinks, and the shell's `inotifywait -r` sees nothing through one).

- **`omarchy plugin add <url> --enable`** clones the repo there. On its
  first start the desktop asks once ([First start](#first-start)) whether
  to add the keys and a `stickies` command; nothing is added without that
  click. Nothing is ever written inside the plugin folder at run time (the
  venv, model and byte-code cache live in `~/.cache/stickies`):
  omarchy-shell reloads its plugins whenever a file in that folder
  changes.
- **`git clone <url> ~/.config/omarchy/plugins/eastbluewizard.stickies`**,
  then that folder's `./install.sh`. It refuses to run from anywhere else
  (`STICKIES_PLUGINS` names another plugins folder, for tests) and runs
  `stickies integrate --yes --install`: the command link in
  `~/.local/bin/stickies`, and the keybinding drop-in `hypr/stickies.lua`
  copied to `~/.local/state/omarchy/toggles/hypr/stickies.lua` (Omarchy's
  Hyprland config loads every `*.lua` there after the defaults and your
  `~/.config/hypr/bindings.lua`; no hand-written config is edited). In the
  live session it also enables the plugin (`omarchy-plugin-enable` puts the
  widget in the bar's right section), reloads Hyprland, publishes the hub
  card and restarts the shell if the plugin's code changed (below). It
  lists every key. The live steps run only when `HOME` is your real home,
  so `HOME=$(mktemp -d)` makes a full dry run (that is what the tests do).
- **A development checkout elsewhere**: `omarchy plugin add
  ~/path/to/checkout --enable` (a local path is a valid git URL there);
  `omarchy plugin update` then pulls its commits. See CONTRIBUTING.md.

An install from before 1.3.0 symlinked a checkout into the plugins folder.
The next `stickies integrate` or `install.sh` replaces that link with a
`git clone` of the checkout (`migrate_symlink`; the notes are not
touched). Tests and benches never do this: with `STICKIES_STATE` set, the
plugins folder is out of reach unless `STICKIES_PLUGINS` names one.

**New QML needs a shell restart.** Measured 2026-10-06 (Quickshell 0.3.1)
with a throwaway plugin updated through `omarchy plugin update`:

- The shell's watcher sees the update. Its log says "Local plugin changed,
  reloading", and it unloads and reloads every plugin service, which also
  restarts `stickies serve` from the new `stickies.py`.
- The reload calls `Qt.clearComponentCache()`, guarded by a `typeof`
  check, and that function is `undefined` in Quickshell. So the reload
  gets the cached old components back, and a method the update added
  answers `Function not found`. The same happens after disable and enable,
  and after `omarchy plugin remove` and `add` again.
- `omarchy-restart-shell` loads the new code.

So an update works like this:

- `serve`, restarted by that reload, compares the plugin's `*.qml` / `*.js`
  hash with the one recorded when the shell started it first
  (`check_loaded_code`, keyed by the shell's pid and start time in
  `integration.json`). When they differ, it logs that once and sends one
  notification for that version: restart the shell to load it.
- `stickies shell <method>` (a key's fallback) turns `Function not found`
  into the same advice, as a notification and in `stickies.log`.
- `install.sh` (live session only) and `stickies integrate` (`--yes`,
  `--no`, or `--refresh` for just this) compare the hash with the one
  stored at the last install and run `omarchy-restart-shell` when it
  differs and the shell is running. A failed restart leaves the old hash,
  so the next run tries again. The desktop's own first-start offer never
  restarts.

`stickies uninstall` (which `uninstall.sh` runs) removes whatever of this
is there and ours: runtime keys, the drop-in, the `~/.local/bin/stickies`
link, a plugin symlink from an install before 1.3.0, and the byte-code
cache. The plugin folder is the checkout itself; `omarchy plugin remove
eastbluewizard.stickies` deletes it. Notes are kept unless `--purge`.

## First start

On its first start after `omarchy plugin add`, the desktop shows a card at
the bottom of the first screen:

![the first-start offer](setup.png)

*Set up* adds the keys and the `~/.local/bin/stickies` link and
remembers the answer; *Not now* adds nothing and remembers that too. It is
asked once. Later: `stickies integrate --yes`, or `stickies keys on`.

**The keys are runtime binds**, not a file: `hyprctl eval
'hl.bind("SUPER + ALT + N", ...)'` (Omarchy's Hyprland config is Lua, and
`hyprctl keyword bind` does not apply to it). Measured on Hyprland 0.56.2:
a bind added this way shows in `hyprctl binds` within ~5 ms,
`hl.unbind(...)` removes it, and every `hyprctl reload` drops it (the Lua
config is re-run). So the service adds them again on Hyprland's
`configreloaded` event, and removes them when the plugin is unloaded
(disabled or removed). A key that is already bound to something else is
left alone and reported. Firing on a real key press could not be tested
from a script (`wtype` key events trigger no Hyprland binds at all, not
even the config's); if a key does nothing, see the README's
troubleshooting. When the install.sh drop-in is present, no runtime binds
are added.

## Keys

| key                         | does                                              |
|-----------------------------|---------------------------------------------------|
| `SUPER + ALT + N`           | new note on the focused monitor's current workspace, above windows, caret in it |
| `SUPER + ALT + SHIFT + N`   | front mode on / off (notes above windows)         |
| `SUPER + ALT + V`           | a note from the clipboard (quick capture)         |
| `SUPER + ALT + J`           | find a note: the search overlay ("jump")          |
| `SUPER + ALT + O`           | all notes: the list overlay, filter by tag / text ("overview"; IPC `list`) |
| `SUPER + ALT + A`           | ask your notes: the chat overlay                  |
| `SUPER + ALT + L`           | layout: free -> waterfall right -> waterfall left (IPC `cycleLayout`) |
| `SUPER + ALT + W`           | collapse / expand the waterfall column (IPC `collapse`) |

All eight were unbound in `~/.config/hypr/bindings.lua`, every file under
`/usr/share/omarchy/default/hypr` and the live `hyprctl binds` (2026-10-05;
`SUPER + ALT + F`, `+ G`, `+ K` and `+ S` are taken, and so are
`SUPER + CTRL + ALT + L` / `+ W`, but not `SUPER + ALT + L` / `+ W`;
`SUPER + V` and `SUPER + CTRL + V` are Omarchy's paste and clipboard
manager, `SUPER + ALT + V` was free on 2026-10-06; All notes was asked for
on `SUPER + ALT + K`, which is Omarchy's "Tmux keybindings", so it is
`SUPER + ALT + O`, free in the defaults, `bindings.lua` and `hyprctl
binds` on 2026-10-06); `tests/test_shell.py`
re-checks the config files on every run. Each runs `omarchy-shell
stickies <m> >/dev/null 2>&1 || stickies shell <m>` for `newNote` / `front`
/ `paste` / `find` / `chat` / `cycleLayout` / `collapse` / `list` (the runtime binds
name the plugin's own `stickies` by its full path): not `-q`, which exits 0
on every failure (shell down,
plugin not loaded, unknown method) and left a dead key with no trace. On
failure `stickies shell` tries once more and writes
`shell <m> failed: <reason>` to `stickies.log`. The fast path costs no
Python start (36 ms key -> notes on the Top layer, see
[PERF.md](PERF.md)).

## Front mode

On a workspace with tiled windows (the normal case in Hyprland) the
desktop is covered, and so are notes on the Bottom layer. Front mode
brings them out:

- **On:** `SUPER + ALT + N` (the new note gets the caret) or
  `SUPER + ALT + SHIFT + N` (this workspace's notes; the IPC method is
  `front`). Every monitor's surface goes to the **Top** layer and a
  `HyprlandFocusGrab` over them holds the keyboard.
- **Off:** Esc in a note, the key again, a click anywhere outside the
  notes, or a workspace change. The surfaces go back to Bottom, and the
  keyboard is handed back to the windows (the surface drops keyboard
  interactivity for 150 ms, so Hyprland refocuses the window under the
  pointer).
- **Clicks still go through.** The input region is the notes, the `+`
  button and the toast in both modes; a click on a window clears the grab
  *and* reaches the window (checked live with a virtual pointer: front mode
  ended, the clicked window became active).
- Why a focus grab: with on-demand keyboard focus Hyprland gives the
  keyboard to the window under the pointer as soon as the surface stops
  asking for it exclusively (and on any pointer motion, `follow_mouse =
  1`); with exclusive focus a click on a window never takes it back. Both
  were tried live; the grab gives both halves.
- **Empty notes go.** A note that loses focus with no text (whitespace
  only) is deleted by serve's `discard` 400 ms later, unless it got focus
  back or someone else wrote to it; notes left empty before this (or by a
  crash) are cleared once per shell start (`clean`, notes untouched for
  10 s).

## Layouts

`free` (notes on the desktop, as above) or a waterfall column at the right
or left edge (`waterfall-right`, `waterfall-left`). The setting lives in
serve's `settings` (written only through the CLI / serve), and every
surface follows serve's `settings` event, so `stickies layout` from a
terminal or an agent moves the desktop too (~110-250 ms, serve's poll).
The plugin applies its own changes optimistically, so a key press starts
the glide at once.

- **The column** (`Waterfall.qml`, layout maths in `waterfall.js`) is one
  more `PanelWindow`, on the **Top** layer so it floats above tiled windows
  on every workspace, put on the focused monitor (`screen` follows
  `Hyprland.focusedMonitor`; it remaps when focus moves). It holds that
  monitor's current workspace's notes, plus pinned ones and notes without a
  workspace: pinned first, then most recently edited (frozen while you
  type, so a note doesn't jump; refreshed on the next workspace visit),
  then any order a drag stored (`waterfall_order`). Fixed width
  (`waterfall_width`, 320 px), each note its own height capped to the
  column; overflow scrolls (wheel / touchpad) with a thin position bar.
  The column starts below the bar (Hyprland's reserved area for that
  monitor).
- **Input.** The surface is full-screen (so notes can glide between their
  free spots and the column), but its mask is only the column's filled
  part and its handle, or the collapsed strip + handle: the rest of the
  screen always clicks through. `exclusionMode: Ignore`, so windows are not
  pushed aside; with `waterfall_reserve` on, a separate invisible, inputless
  1 px surface on that edge sets an exclusive zone of the column's width
  (+ margins and handle; 6 px collapsed), like a bar.
- **The desktop** stops drawing the notes the column holds (their cards
  stay built, hidden: a switch flips visibility instead of rebuilding).
  The column's cards for this workspace are built ahead of time, in the
  background (`Loader.asynchronous`) while the layout is free. Building 40
  cards on the key press cost ~90 ms; now the first frame of a switch is
  the surface mapping (~50 ms), and the 130 ms glide follows.
- **Glide.** Cards animate x/y only (130 ms, OutCubic); animating the size
  re-wrapped every note's text each frame and dropped frames, so cards
  keep the column width on the way home and the desktop's card takes over
  at the note's own size when they land.
- **Drag** a column note by its header: the others make room (the drop
  index from `waterfall.js`), release to store the new order. Released
  more than 40 px beside the column, the note goes free right there
  (`move` x/y/monitor + `dock` docked=false: it stays out of the column in
  any layout). A free note dropped on the column joins it again (its free
  spot stays where it was). No resize grip in the column.
- **Typing** in a column note: the front-mode focus grab, over the column
  surface, holds the keyboard; Esc, a click outside, or a workspace change
  hands it back.
- **Collapse** (`waterfall_collapsed`): cards glide just past the screen
  edge, a 6 px accent strip and the handle remain; click either (or
  `SUPER + ALT + W`) to expand.
- **Free positions are never written** by the column, except for the drag
  out. Switching back to free puts every note where it was.
## On a note

- **Tags.** Small chips under the header (in the header, after the title,
  when the note is rolled up; in the free layout and the waterfall column
  alike). The header's tag button opens the editor: chips get an x, and a
  field takes new ones, with the tags in use (most used first, `tags` from
  serve) suggested as you type. Enter adds the highlighted suggestion, or
  the typed text when nothing matches; Shift+Enter adds exactly what was
  typed; Up/Down move the highlight, Tab completes; Backspace in the empty
  field removes the last tag; Esc or a click elsewhere closes it (text left
  in the field is added). A tag is `a-z`, `0-9` and `-`, at most 32
  characters; the field refuses anything else. Clicking a chip (editor
  closed) opens All notes filtered by that tag. A note with tags but no
  text is not discarded as empty. The tags live in `note_tags`
  (`ON DELETE CASCADE`, indexed by tag) with a space-separated mirror in
  `notes.tags`, which the model role, `list` and the FTS5 index read.
- **Checklists.** Lines that start with `[ ]` / `[x]` (or `- [ ]`,
  `* [x]`, `+ [ ]`) get a checkbox drawn over the brackets; a click flips
  the character in the note's text (saved at once), and the header shows
  `done/total`. The text stays plain, so the CLI, search and chat see the
  same `[x]`. A note without a `[` costs one `indexOf` per keystroke.
- **Roll-up.** Double-click the header: the note folds to its header line,
  titled with its first line (checkbox stripped); its input region shrinks
  with it. Double-click again to open. Stored per note (`rolled`).
- **Reminder bell.** A note with a pending `@ <when>` shows a bell and the
  time (`09:00` today, `Tue 09:00` this week, `7 Oct 09:00` later) in the
  header. serve sends the notification and clears the bell.
- **Snapping.** A drag snaps the note's edges to other notes' edges within
  6 px (a faint accent guide line shows which), otherwise to an 8 px grid.
  Hold **Shift** to place it freely. The guides are parked off-screen when
  unused rather than hidden: toggling `visible` re-batched every card and
  showed up in the frame times (see [PERF.md](PERF.md)).
- **Toast.** One per desktop, on the screen that caused it, for 6 s:
  *Archived - Undo* (restores that note), *Tidied 6 notes - Undo*, or a
  plain sentence when something didn't work (an image on the clipboard,
  Hyprland not answering, serve still starting). Never a traceback.

## Colours and the theme

The six colour names are fixed (a note stores `pink`, not a hex), so what
a colour *means* to you survives a theme change. How it looks comes from
the theme: each name is a hue tinted toward `Color.background` from
`qs.Commons` (72% toward it on a dark theme, 42% on a light one), and the
ink is tinted toward `Color.foreground`. omarchy-shell swaps those on a
theme switch, so every card, swatch and the `+` button recolour live, in
one frame (~12 ms to the swapped frame with the waterfall column surface
also recolouring, measured by bench_polish.py).

## Tidy and quick capture

- **Tidy** (bar panel button, `omarchy-shell stickies tidy`, or `stickies
  tidy`): the unpinned notes showing on the focused monitor's workspace
  are laid out in rows in reading order, keeping their sizes (a rolled-up
  note counts as its header), Hyprland's `gaps_out` around and between
  them, below the bar (the monitor's reserved area from `hyprctl
  monitors`). The notes glide there (220 ms) and come above the windows
  for a moment so you see the result. The toast and the panel button
  (*Undo tidy*, until you drag a note) put them back, once. Free layout
  only: in a waterfall the column owns the places, so the button is
  hidden, the IPC method and `stickies tidy` answer in a sentence, and
  drags (in the column or of a note dragged out of it) don't snap.
- **Quick capture** (`SUPER + ALT + V`, `omarchy-shell stickies paste`):
  serve runs `wl-paste` (text types only; an image, a file or nothing says
  so in the toast), trims it, cuts it at 10,000 characters (the toast says
  so), and makes a note in the middle of the focused screen. The notes come
  above the windows for 1.8 s, without taking the keyboard, while the new
  one flashes.

## Bar widget

- Shows `󰎚 <count>` (live notes); dimmed `󰎛` while the stickies are hidden.
  Tooltip: count, pinned. Right-click: new note. Middle-click: show/hide.
- Click opens a small panel: a **quick-capture** field (Enter adds the text
  as a note on the current workspace, without leaving the panel), a
  **search** field (search-as-you-type through serve's FTS5 `search`; Enter
  opens the top hit), then **Pinned** and the 8 most **Recent** notes. Click
  a note to go to it: its workspace, raised, caret in it. Header buttons:
  New, All notes (the list overlay), Tidy / Undo tidy, Hide all / Show all.
  Esc closes.
- The widget starts no process: it looks up this plugin's own service with
  `bar.shell.serviceFor("eastbluewizard.stickies")` (the scoped facade the
  bar hands third-party widgets) and uses its model and `stickies serve`
  connection. Pinned + recent come from a serve `list` (sorted off the DB
  index), refreshed on open and, debounced, on changes while open.
- Hidden state is per shell session (not persisted).

## All notes (`List.qml`)

`SUPER + ALT + O` (IPC `list`, a toggle), `stickies list --open [--tag X]`
(IPC `listTag "<tags>"`, space- or comma-separated; Quickshell IPC has no
optional arguments, hence two methods), the bar panel's *All notes* button
or a click on a note's tag chip. Same surface and keyboard handling as the
search overlay (Overlay layer, dimmed, `OverlayFocus.qml`), on the focused
monitor.

- Every live note on every workspace, pinned first, then most recently
  edited: colour swatch, pin, first line (checkbox stripped), tag chips,
  reminder bell, checklist progress, workspace (`all` for pinned or
  workspace-less notes). Rows are a fixed height, delegates are reused.
- Top: a chip per tag with its count ("All" clears), most used first; a
  click toggles a tag, several mean notes that have all of them. Below it a
  field that filters as you type: every word must appear in the note's
  first 500 characters or its tags.
- Up/Down (Tab, PageUp/PageDown) move, Enter jumps to the note on its
  workspace and flashes it (as search does), Delete archives the selected
  note with the desktop's Undo toast; that toast sits under the overlay, so
  the same Undo shows below the list (click it or Ctrl+Z). Esc, a click
  outside the card or the key again closes.
- Data: serve's `list` with `brief: true`: per note only `id`, `title`,
  `hay` (lower-cased first 500 characters + tags), `color`, `pinned`,
  `workspace`, `tags`, `remind_at`, `updated_at`, `checks` ([done, total]),
  1.3 MB for the bench's 2,000 long notes (1.5 MB as full rows). Filtering is local
  JS over those rows. Opening maps the surface with the rows from last time
  and asks for fresh ones after its first frame (parsing them first held
  that frame back); an unchanged list is not reassigned, and a changed one
  keeps the scroll position. While open it refreshes (debounced) on every
  model change, e.g. an archive or an agent's `stickies add`.

## Search overlay

`SUPER + ALT + J` (or `omarchy-shell stickies find`, which is also what the
hub card's tiles run) dims the focused monitor and opens a search box with
keyboard focus:

- **Results as you type**, by words and by meaning: serve's hybrid search
  (FTS5 + the local embedding model, see [search.md](search.md)). Matched
  words are bold in the accent colour; a note found only by meaning says
  "≈ meaning". Without the model (`stickies setup` not run) it searches
  words only and says so on the status line.
- **Up/Down** (or Tab) pick, **Enter** or a click jumps: the overlay closes,
  Hyprland switches to the note's workspace (pinned notes and notes without
  one are already on every workspace), the note is raised and an accent
  ring pulses around it three times. **Esc**, a click outside the card or
  the key again closes (see *Overlay keyboard* below).
- At most one search is in flight; keys typed meanwhile collapse into the
  newest query, so fast typing never queues stale searches.
- Note text is user text: the snippet is HTML-escaped before the highlight
  markup is added (`highlight.js`, tested with node), and highlight offsets
  are code points, converted for JS's UTF-16 strings.

It is a `PanelWindow` on the **Overlay** layer that the service builds at
start and keeps hidden, so opening it maps a surface and nothing has to load
(an `overlay`-kind entry point would be loaded on summon and would need its
own route to the service's single `stickies serve`).

### Overlay keyboard (search and chat)

Owner report, 2026-10-05: `SUPER + ALT + J` / `A` "don't do anything". The
IPC call worked and the surface mapped, but the keyboard went back to the
window under the pointer, so the key looked dead. `OverlayFocus.qml`
(decisions in `overlayfocus.js`, tested with node) now holds the keyboard
for both overlays:

- The surface asks for **Exclusive** keyboard focus always, so the commit
  that maps it already asks (it used to switch None -> Exclusive when it
  opened, racing the map). Hidden, it is unmapped and asks for nothing.
- While it maps, a `HyprlandFocusGrab` on the surface as well (front mode's
  tool). It is dropped once the field has held the keyboard for 400 ms, so a
  menu or the launcher opened on top still gets the keyboard.
- **Losing the keyboard never closes an overlay.** While it maps, the field
  takes it back (re-asking for Exclusive if the surface lost it), up to 8
  times in a row. After that, a surface that takes it keeps it until it goes,
  then Exclusive brings it back. Focus lost inside the overlay (chat's field
  is disabled while an answer streams) moves to a key catcher, so Esc still
  works.
- **Closes only** on Esc, a click on the dimmed area outside the card, or
  the key again. A second `find` / `chat` within 450 ms of opening is
  taken as the same press delivered twice and ignored.
- Opens on the monitor Hyprland says has focus, rotated ones included.

`tests/test_overlay_focus.py` runs the decisions under node.
`STICKIES_LIVE_TESTS=1` also runs `tests/shell/focus_harness.qml`: both real
overlays in a throwaway `quickshell -p`, a second Exclusive layer taking
the keyboard during the map and again after it settles, the field losing
focus, typing and Esc (as Qt key events in that process). It maps on the
live session for ~3 s, so it is opt-in.

## Chat overlay

`SUPER + ALT + A` (or `omarchy-shell stickies chat`) dims the focused
monitor and opens "Ask your notes" with keyboard focus. Same build as the
search overlay: a hidden `PanelWindow` on the Overlay layer, made at start,
talking only through the service's `stickies serve` (see the top-level
[chat.md](chat.md) for what is sent and how the agent runs).

1. **Type a question, Enter.** The notes it would send are listed (serve's
   `chat_notes`: hybrid search topped up by any-word matches, top 6), each
   with a tick box, all ticked. Click a row to untick it. Editing the
   question drops the list; Esc drops it too.
2. **Enter again** (or *Send N notes*). Only the question, the ticked notes
   and this chat's earlier turns go to the agent (`claude -p`). The panel
   says "Sent N notes to claude" and shows them as chips; the answer
   streams in underneath. *Stop* cancels it (the agent process is killed);
   Esc closes the panel and the answer keeps arriving.
3. **Citations** `[#12]` in the answer are links, and the sent-note chips
   stay bright for the notes it cited (dimmed for the rest); clicking either
   closes the panel and jumps to the note (its workspace, raised, flashing),
   like the search overlay. A citation of a note that was not sent shows as
   `#9?`, not a link, and is listed under the answer.
4. **Proposals** (new note, append to a note, hub todo) are cards with
   *Apply* / *Dismiss*. Nothing happens until Apply: serve validates the
   proposal again and applies it (`add`, `edit --append`, or `hub todo
   add`); the card then says "Applied #2004" (or the error, with Retry).
   Proposals the model got wrong (unknown actions like a delete, bad
   fields) are never shown as cards, only counted with the reason.
5. **Follow-ups** carry this chat's earlier questions and answers.
   *New chat* clears it. The conversation lives in the overlay only: it
   survives closing and reopening, and is gone when the shell restarts.

Model text is never markup: `chat.js` escapes the answer, then adds only
links for sent-note citations, `**bold**` and line breaks (tested with
node). Note text and proposal summaries are plain text.

## What it does

- **Where notes show.** A note lives on its `monitor` (unset or unplugged ->
  the first screen) and its `workspace`; it is drawn while that monitor's
  active workspace matches. Pinned notes and notes without a workspace (e.g.
  `stickies add` from an agent) show on every workspace.
- **Edit in place.** Click the text to type. Edits are sent 300 ms after the
  last keystroke and flushed when the note loses focus (or Esc). If the
  CLI/an agent changes a note you are not editing, it updates live; while you
  are typing in it, your text wins and is flushed on blur.
- **Move / resize.** Drag the header strip; drag the bottom-right grip.
  Geometry follows the pointer locally and is written once on release
  (`move`), so a drag costs no IPC per frame. Clicking or focusing a note
  raises it.
- **Header buttons:** pin (all workspaces), colour (six swatches: yellow,
  pink, blue, green, orange, purple; tinted by the active theme, see
  [Colours and the theme](#colours-and-the-theme)), archive. Archive shows an *Archived - Undo* toast for 6 s (`restore`).
- **First run:** with no live notes, a hint sits next to the `+` button on
  the first screen (new note, find, ask, with their keys). It is drawn
  only; clicks go through it. It disappears with the first note.
- **New note:** the round `+` at the bottom-right of each monitor creates an
  empty note on that monitor + workspace with the caret in it. Also
  `SUPER + ALT + N` and the bar widget.
- **One surface per monitor.** `Variants` over `Quickshell.screens`
  makes a surface for every monitor Quickshell reports, rotated ones
  included (a 1920x1080 output with `transform = 1` gets a 1080x1920
  surface), and drops it when the monitor goes. `ScreenMoveRemap` (from
  Omarchy's `qs.Ui`, as the bar and background use it) remaps a surface
  whose monitor moved in the layout, which Hyprland would otherwise leave
  at the old position after a hotplug.
- **IPC** (`omarchy-shell stickies <method>`): `newNote`, `front`, `toggle`
  (show/hide), `show`, `hide`, `find` (the search overlay; the hub card's launch command),
  `chat` (the chat overlay), `cycleLayout`, `collapse`, `layout <name>`,
  `search` (opens the bar panel on the focused monitor with the search field
  focused), `reload` (re-lists).
- **Input only where notes are.** The surface's input mask is the union of
  the shown notes, the `+` button and the toasts; clicks anywhere else reach
  the desktop (and the background plugin).
- **Pointer capture.** The header's drag MouseArea sits *under* the header
  buttons and only sets `preventStealing` once the pointer has moved 4 px, so
  a click on the strip focuses the note and pin/colour/archive get their own
  clicks.

## Talking to the DB

Only through one long-running `stickies serve` (JSON lines, see
[serve.md](serve.md)). The service keeps a ListModel mirror built from
`list` and kept current by serve's `changed` events, which also cover
writes from other processes (they poke serve's socket, so the desktop shows
an agent's `stickies add` at once). If serve exits it is restarted after
1.5 s. The command is the plugin's own `stickies`, resolved from the QML
file's location; `STICKIES_CMD` overrides it (tests, benches).

Nothing polls: no repeating timer runs while the desktop is idle, and serve
sleeps in `select()` until a request, a poke or a deadline (the hub card's
debounce) arrives. The model being loaded is pushed as an event too.

## Files

- `Service.qml` -- serve client, notes model, palette, actions, one
  `PanelWindow` per screen.
- `Desktop.qml` -- one monitor: note slots (a card is only built while its
  note shows here, so notes on other workspaces cost an empty Item), `+`
  button, undo toast, input regions.
- `NoteCard.qml`, `HeaderButton.qml` -- a note and its header buttons.
- `Waterfall.qml`, `waterfall.js` -- the waterfall column (cards, scroll,
  drag to reorder / out, collapse handle) and its pure layout functions
  (unit-tested under node in `tests/test_layout.py`).
- `BarWidget.qml` -- bar button; finds the service, loads `BarPanel.qml`
  (capture, search, lists) which uses `NoteList.qml` for each section.
- `SearchOverlay.qml`, `highlight.js` -- the search overlay and its escaped,
  highlighted snippets.
- `List.qml`, `TagChip.qml` -- the All notes overlay, and the tag chip it
  shares with the notes.
- `ChatOverlay.qml`, `chat.js` -- the chat overlay and its escaped answers
  with citation links.
- `markup.js` -- the escaping both `.js` files share.
- `stickies`, `stickies.py` -- the command the service runs (`serve`).

Dev/bench-only environment knobs (never set in a session):
`STICKIES_LAYER=overlay`, `STICKIES_FOCUS=exclusive`, `STICKIES_COLUMN_SCREEN=<output>`
(pins the column to one output, e.g. a headless test output).

## Measured

All numbers (drag frame times, keystroke latency, start-up, bar panel,
search and chat overlays), how each bench drives the plugin, and what
could not be measured on this machine: [PERF.md](PERF.md).
