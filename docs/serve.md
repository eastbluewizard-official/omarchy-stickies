# serve: the protocol

`stickies serve` is a long-running JSON-lines process on stdin/stdout for
the shell plugin; it keeps one DB connection open, so a request costs the
query, not a Python start.

    -> {"id": 1, "op": "search", "args": {"query": "cardm"}}
    <- {"id": 1, "ok": true, "result": [...]}
    <- {"id": 2, "ok": false, "error": "no note #9"}

Ops and their `args` mirror the CLI: `ping` (its result also says whether
the embedding model is loaded right now: `semantic`), `colors`, `list` (archived,
all, workspace, color, pinned, limit, tags; `brief: true` gives the All notes
view's rows: id, title, hay, color, pinned, workspace, tags, remind_at,
updated_at, checks), `show` (id), `search` (query, limit, archived, mode,
tags; default hybrid), `add` (body, color, pinned, workspace, monitor, x, y,
w, h, tags), `tags` (archived), `tag` / `untag` (id, tags), `set_tags` (id,
tags: exactly these), `edit` (id, body | append), `pin` (id, pinned),
`unpin`, `color` (id, color), `move` (id, x, y, w, h, z, workspace,
monitor, raise), `rm`/`archive` (id), `restore` (id), `purge` (id, force),
`discard` (id: deletes the note only if it has no text and no tags; the desktop sends
it when a note loses focus empty), `clean` (older_than: every empty note;
the plugin runs it once at start), `undo` (the newest archive back),
`roll` (id, rolled), `tidy` (workspace, monitor, width, height, primary;
gaps and reserved from hyprctl unless given) and `tidy_undo`, `paste`
(monitor, workspace, x, y: a note from the clipboard; result `{note,
truncated}`), `reminders`. An unexpected failure answers
`{"ok": false, "error": "something went wrong (...); details in
stickies.log"}` and serve carries on. serve also sends due reminders
over D-Bus to the notification daemon (no `notify-send`, so the text is in
no process's arguments; without a bus, `notify-send` says only "Reminder
for sticky note #N"). `STICKIES_NOTIFY` names a stand-in that gets the
title and text on stdin; empty turns reminders off, and they are off while
`$STICKIES_STATE` is set unless it names one, like the hub card. They go
out once at start for any that came due while serve wasn't running, then at each reminder's due time. It plans the next
wake-up from the earliest pending reminder (re-planned after every change),
looking at least every 30 s while one is pending, because the monotonic
clock stops during suspend. With no reminder pending it doesn't wake for
them at all.
Layout ops: `settings`, `set` (values: `{key: value, ...}`, validated like the
CLI), `dock` (id, docked). `tidy` refuses while the layout is a waterfall.

Chat ops: `chat_info` (`{"agent": "claude", "error": null}`: who chat
would talk to, or why it can't), `chat_notes` (question, k: the notes a
question would send), `ask` (question, ids, history, chat; streamed, see
[Chat](chat.md)), `chat_cancel` (chat), `apply` (proposal).

Set-up ops (the first-run offer, see [the desktop plugin](shell.md#first-start)):
`integration` (what is set up: `asked`, `keys` = `runtime` | `dropin` | `off`,
`link` = the `stickies` command and its state), `integrate` (yes: the
answer to the offer, remembered), `keys_on` (adds the runtime keybindings
again when the person said yes; the plugin sends it at start and after
every Hyprland config reload, which drops runtime binds).

serve also embeds notes in the background when the model is installed; see
[Search](search.md#search-by-meaning). It loads the model at start, drops it
after `idle_unload` minutes (default 10) without a search that wants meaning
(`search` not in `fts` mode, `chat_notes`, `ask`) or an embedding, and
loads it again on the next one. Meanwhile that search is answered with words
only, at once; the `semantic` event says when meaning is back.

**Socket.** serve also listens on `$STICKIES_STATE/serve.sock` (mode
0600) for two requests from other processes, one per connection:

- `{"op": "changed"}`: another process wrote (every `Store` write sends it
  after the commit). serve reads its change log and pushes the events. No
  answer.
- `{"op": "search", "args": {...}}`: read-only, answered as `{"ok": true,
  "result": [...]}`, and only while its model is loaded. Refused while it
  isn't, but it starts loading it for the next search.
- `{"op": "model"}`: whether the model is loaded, `{"available",
  "model", "loaded", "loaded_since", "last_use", "idle_unload",
  "unload_at"}` (`stickies setup --status` shows it).

A cold `stickies search` (hybrid or semantic) asks there first, and falls
back to searching in-process if serve isn't running, isn't ready or
doesn't answer within 2 s. It leaves a live socket that another serve owns
alone, replaces a stale one, and removes its own on exit.

Unsolicited lines carry `"event"`:

    {"event": "ready", "seq": 12, "pid": 4242}
    {"event": "semantic", "on": true, "loaded": false}   # search by meaning is installed (at start; and after an idle unload)
    {"event": "semantic", "on": true, "loaded": true}    # the embedding model finished loading
    {"event": "settings", "seq": 14, "settings": {"layout": "waterfall-right", ...}}
    {"event": "changed", "seq": 13, "kind": "add|update|archive|restore|purge", "id": 3, "note": {...} | null}
    {"event": "reminder", "id": 3, "spec": "tomorrow 9:00", "due": "2026-10-08T07:00:00.000Z", "missed": false, "text": "call the plumber"}

    {"event": "chat", "chat": "chat-1", "kind": "sent", "sent": [{"id", "color", "title", "body"}, ...]}
    {"event": "chat", "chat": "chat-1", "kind": "delta", "answer": "the visible answer so far"}

A `reminder` event comes when a reminder fires (`missed`: it came due
more than two minutes before serve got to it, e.g. while serve wasn't
running). It is the only place the reminder's words go: the plugin shows
them in a toast on the notes, above the windows, with a Show button that
raises the note. The system notification serve sends at the same moment
says only "Reminder for sticky note #3", because the notification server
keeps what it shows where other accounts can read it (Omarchy's writes
the summary and body to 0644 files under `~/.local/state/omarchy/` and
passes them to `bash -c`, so they are in `/proc/<pid>/cmdline` too). Its
`omarchy-exec-argv` hint is `omarchy-shell stickies showNote 3`: a click
on it raises the note, with its id and nothing else.

Change events come for writes from this serve process (sent right after the
response) *and* from any other process, e.g. an agent running `stickies
add`: the writer pokes `serve.sock`, so the event follows the write within
a millisecond or two, with no polling. Idle, serve sleeps in `select()`
with no timeout (measured: 0 wake-ups a minute); only if the socket can't be opened does it fall
back to checking `PRAGMA data_version` once a second. Several changes to
one note between checks are coalesced into one event with the final
state.
