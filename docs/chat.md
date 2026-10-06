# Chat with your notes

Ask a question; it is answered from your notes by Omarchy's default agent
(`omarchy-default-agent`, currently `claude`), with citations, and the model
may propose changes that you confirm. From the desktop: `SUPER + ALT + A`
(the chat overlay, see [the desktop plugin](shell.md#chat-overlay)). From agents and scripts:
`stickies ask "..." --json`.

**What is sent, exactly.** The question picks its notes: the hybrid search
(words, plus meaning when the model is installed), topped up with notes
that contain *any* of the question's words (a question like "what did I
write about the Cardmarket fees?" rarely has all its words in one note;
stopwords in English and Dutch are skipped), live and non-empty only, top
`k` = 6. The overlay lists them first, all ticked; untick any and only the
ticked ones go. The CLI sends the top `k` unless you pass `--notes` (exactly
these) or `--exclude`; `--dry-run` prints the exact prompt and sends
nothing. The message holds only: this chat's earlier questions and answers
(up to 6 turns, the overlay's session only), the notes as
`<note id="12" color="pink" updated="2026-10-05">...</note>` (cut at
4,000 characters each), and the question. The result's `sent` lists every
note that went out; `prompt` is the message itself.

**How the agent runs.** `claude -p --output-format stream-json --verbose
--include-partial-messages --tools "" --no-session-persistence
--strict-mcp-config --disable-slash-commands --setting-sources ""
--system-prompt <stickies' instructions>`, prompt on stdin, in an empty
directory (`$STICKIES_STATE/agent/`): no tools (it cannot read files or run
anything), no MCP servers, no settings, no project CLAUDE.md, no session
saved. Streamed text arrives as it is written; 180 s timeout. If the default
agent is not claude, chat says so (only claude's non-interactive mode is
known here); `STICKIES_AGENT` names any command that reads a prompt on
stdin and prints the answer (the tests use a fake one).

**Citations.** The model cites notes as `[#12]`. `citations` are the cited
ids that were sent, in order of first mention; `unknown_citations` are ids
it cited but was never sent (the overlay shows them as `#9?`, not as
links).

**Proposals, never actions.** The model can only propose, in one fenced
block at the end of its answer (hidden from the answer, also while
streaming):

    ```stickies-actions
    [{"action": "new_note", "body": "...", "color": "pink"},
     {"action": "append_note", "id": 12, "text": "..."},
     {"action": "hub_todo", "title": "...", "due": "2026-10-12"}]
    ```

Exactly these three actions exist; each is validated field by field
(types, no unknown fields, a known colour, a real `YYYY-MM-DD` date, a
one-line title, `append_note` only to a note that was sent, at most 5).
Anything else (a delete, an archive, malformed JSON, ...) lands in
`rejected` with the reason and is never offered. `ask` applies nothing; it
returns `proposals` (each with a `summary`) for a person to confirm. `apply`
(the overlay's Apply button, or `stickies apply '<json>'`) validates again
and applies one: `new_note` -> `stickies add`, `append_note` -> `edit
--append` (live notes only), `hub_todo` -> `hub todo add TITLE [--due D]
--json`. Chat never deletes or archives.

`stickies ask --json` result: `question`, `sent`, `prompt`, `answer` (the
visible answer), `raw` (everything the model wrote), `citations`,
`unknown_citations`, `proposals`, `rejected`, `ms`. Without `--json` the
answer streams to the terminal, followed by what was sent and cited and a
ready-to-run `stickies apply ...` line per proposal. Agent failures are
`{"error": ...}` with exit 1.

Measured with a fake agent and the real `claude -p`, 2,003 notes:
the notes for a question are listed ~40-47 ms after Enter. With claude, the
first words appear ~2.8 s after Send, and a short answer is complete in
~4.7 s.
