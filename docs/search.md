# Search

Search runs on your machine, always: SQLite FTS5 for words, a small local
embedding model for meaning, fused. Nothing about a search leaves the
machine, and once the model is installed it works with Wi-Fi off. Measured
numbers: [PERF.md](PERF.md#search-benchpy).

## How search works

`--mode hybrid` (the default) runs both searches below and fuses them with
reciprocal-rank fusion (each list contributes `1 / (60 + rank)`), so a note
found by both comes first. While the model isn't installed it is the
words search alone (same order, same fields), so callers never need to
care. `--mode semantic` without the model is an error naming `stickies
setup`.

**Words (`--mode fts`).** Every word in the query is quoted and
prefix-matched (`cardm fee` finds "Cardmarket fees"), all words required,
bm25-ranked, archived notes excluded unless `--archived`. Diacritics fold
(`cafe` = `café`). User text never reaches FTS5 as syntax, so `[OP-05]`,
`"`, `NEAR(` etc. are safe.

**Meaning (`--mode semantic`).** The query is embedded locally and compared
(cosine) with every note's vector; notes above the model's similarity floor
come back best first. Queries shorter than 3 characters are still being
typed and skip this part. English questions find Dutch notes and the other
way round ("dentist appointment" finds "Tandarts afspraak vrijdag").

Each hit is the note plus:

- `snippet`: plain text around the match (`…` where it's cut); for a
  meaning-only hit, the words around the first query word it contains, or
  the start of the note,
- `highlights`: `[[start, end], ...]` offsets into `snippet`, in Unicode
  code points (QML/JS strings index UTF-16, so convert if a note has emoji),
- `score`: higher is better (negated bm25 for `fts`, the fused score for
  `hybrid`, the cosine for `semantic`),
- `match`: `"fts"`, `"semantic"` or `"both"`; human output marks
  meaning-only hits with `~`,
- `similarity`: cosine to the query, or `null` when not computed.

A cold `stickies search` (an agent, a script) first asks the running
`serve` over its socket. The desktop plugin keeps a serve running with the
model loaded, so the search takes ~140 ms with no model load. Without a
running serve, the process loads the model itself (~0.9 s on the reference
laptop). Agents that only need words can pass `--mode fts` (~40 ms, nearly
all of it Python start-up).

## Search by meaning

Fully local: the model runs on the CPU with onnxruntime inside the
`stickies` process; nothing about your notes leaves the machine, and after
`stickies setup` nothing touches the network (Wi-Fi off works).

    stickies setup            # shows exactly what it will download, then asks [y/N]
    stickies setup --yes      # the same, for scripts/agents (that flag is the consent)
    stickies setup --status   # what's installed; nothing is downloaded

`setup` makes a venv in `~/.cache/stickies/venv` from
`requirements-search.txt`: onnxruntime 1.30.0, numpy 2.5.3, tokenizers
0.23.2 and sentencepiece 0.2.2, one wheel each, checked by sha256 (`pip
install --require-hashes --only-binary=:all: --no-deps --no-cache-dir`;
~45 MB download, ~165 MB on disk). The wheels are for CPython 3.14 (Arch's
`python`) on Linux x86_64; on any other Python or CPU `setup` says so
before downloading anything, and search stays words-only. `--no-deps`
leaves out what search never imports (onnxruntime's flatbuffers, packaging
and protobuf; tokenizers' huggingface-hub and its HTTP stack); the real-model
tests pass on exactly these four. It then downloads the
model files into `~/.cache/stickies/models/<name>/` (each pinned to a
Hugging Face commit, size and sha256, checked before the file is moved into
place), then embeds every existing note (`backfill`). The `stickies` script
itself stays stdlib-only: a command that needs the model (`search` in
`hybrid`/`semantic` mode, `serve`, `backfill`) re-executes under
the venv's Python when the model is downloaded and this Python lacks the
packages. `STICKIES_SEMANTIC=0` switches it all off. (`STICKIES_VENV`
overrides where the venv lives; a `.venv` that an older `setup` made next
to `stickies.py` keeps being used. The default is outside the plugin folder
on purpose: omarchy-shell reloads a plugin whenever a file in its folder
changes.)

**onnxruntime's telemetry is off.** onnxruntime's PyPI builds include
Microsoft's 1DS telemetry SDK, on by default (its `docs/Privacy.md`).
Measured with 1.30.0 on 2026-10-06, in a temp HOME, with an `LD_PRELOAD`
shim logging every `connect()` and `getaddrinfo()`:

- `import onnxruntime` writes `deviceid` (a random UUID) and
  `onnxruntime.db` (an SQLite event queue) to
  `$XDG_CACHE_HOME/Microsoft/DeveloperTools/.onnxruntime`, default
  `~/.cache/...`, a folder every onnxruntime program shares.
- It queues events such as ProcessInfo, SessionCreation, ModelLoad,
  EpDeviceUsage, RuntimePerf and SystemMetrics. They carry the host name,
  the kernel version, the path of the Python that loaded it, the model's
  file name and metadata, run counts and timings, CPU times and RSS.
- It uploads them over HTTPS to `mobile.events.data.microsoft.com`. A
  `stickies serve` running for 25 s with one search made 8 connections to
  port 443. A short CLI run exits before the uploader fires.
- `onnxruntime.disable_telemetry_events()` called after the import does
  not stop either: by then the files are written, and the connections
  still happen.
- `ORT_DISABLE_TELEMETRY=1` set before onnxruntime initialises stops all
  of it: no files, no connections (also the documented opt-out in
  1.30.0's `docs/Privacy.md` and `telemetry_environment.h`).

So stickies sets `ORT_DISABLE_TELEMETRY=1` in every process that imports
onnxruntime (the model loader and `setup --status`'s import check), and
also calls `disable_telemetry_events()`. With that the same run makes no
connections, and the temp HOME holds nothing outside `$STICKIES_STATE` and
`$STICKIES_CACHE` (`tests/test_privacy.py` checks this with a stand-in and
with the real model). Versions up to 1.2.0 did not do this. The folder
they left is shared with other onnxruntime programs and nothing in it
says whose it is, so `stickies uninstall --purge` doesn't delete it. It
prints the path and why, and you can delete it yourself if nothing else
on the machine uses onnxruntime.

**Keeping vectors current.** `serve` loads the model on a background thread
after `ready` (~0.7 s; searches are words-only until then), embeds any
note whose vector is missing or stale (hash of the text it was computed
from), then embeds each note 1 s after its last change, from the desktop or
from any other process (an agent's `stickies add`). Moving or recolouring a
note doesn't re-embed it.

**Idle unload.** After 10 minutes with no search and no embedding, serve
drops the model and hands its memory back to the system (`malloc_trim`;
see [PERF.md](PERF.md#120-idle-unload-of-the-model-bench_unloadpy-2026-10-06)).
The next search that wants meaning loads it again on the same background
thread: the overlay shows the words-only hits at once and asks again when
the model is back, so by-meaning hits fill in without a key press. A note
whose text changed meanwhile loads it too, and is embedded once it is back;
moving a note doesn't. `stickies setup --idle-unload MINUTES` changes the
time (0: keep it loaded), `$STICKIES_IDLE_UNLOAD` overrides it, and
`stickies setup --status` says whether the model is loaded and since when. `stickies backfill` does the same catch-up from
the CLI. Embedding runs with 4 threads, no busy-waiting, so serve idles at
0% CPU.

## Which model: measured on the reference laptop

`eval_embed.py` (kept in the development workspace with its eval set, not in
the published repo) compared candidates on the reference i7-8550U (4 cores, AVX2, no
GPU), each in its own process: quality on a hand-made eval set
(`eval/notes.json`: 105 notes in the style of this business and life,
English and Dutch; `eval/queries.json`: 42 queries with their relevant
notes, by kind: 6 keyword, 22 paraphrase, 6 Dutch, 8 cross-language),
index time over the 2,000 notes from `bench.py`, query latency and RSS.
R@5 = share of queries with a relevant note in the top 5; MRR over the top
10. 2026-10-05, onnxruntime 1.30, 4 threads:

| model | download | load | RSS | index / note | query embed p50 / p95 | hybrid search, 2,000 notes, p50 / p95 | R@5 semantic | R@5 hybrid | MRR hybrid | R@5 hybrid: keyword / paraphrase / Dutch / cross-language |
|---|---|---|---|---|---|---|---|---|---|---|
| all-MiniLM-L6-v2 int8 (`minilm-l6-q8`) | 23.5 MB | 252 ms | 150 MB | 31.8 ms | 3.0 / 3.8 ms | 10.7 / 24.7 ms | 0.64 | 0.64 | 0.54 | 1.0 / 0.73 / 0.83 / 0.0 |
| all-MiniLM-L6-v2 fp32 (`minilm-l6`) | 90.9 MB | 366 ms | 312 MB | 36.1 ms | 3.3 / 3.9 ms | 11.2 / 17.9 ms | 0.69 | 0.69 | 0.57 | 1.0 / 0.82 / 0.83 / 0.0 |
| bge-small-en-v1.5 fp32 (`bge-small-en`) | 133.8 MB | 530 ms | 476 MB | 67.6 ms | 9.4 / 11.6 ms | 24.1 / 30.3 ms | 0.79 | 0.79 | 0.69 | 1.0 / 0.86 / 1.0 / 0.25 |
| paraphrase-multilingual-MiniLM-L12-v2 int8 (`multi-minilm-l12-q8`) | 123.5 MB | 886 ms | 261 MB | 32.5 ms | 5.7 / 7.4 ms | 19.0 / 35.5 ms | 0.81 | 0.86 | 0.75 | 1.0 / 0.77 / 1.0 / 0.88 |
| **multilingual-e5-small int8 (`e5-small-multi-q8`, default)** | 123.4 MB | 846 ms | 297 MB | 38.1 ms | 6.2 / 7.3 ms | 17.2 / 36.8 ms | **0.95** | **0.95** | **0.86** | 1.0 / 0.91 / 1.0 / 1.0 |
| words only (FTS5) | | | | | | | | 0.17 | 0.16 | 1.0 / 0.0 / 0.17 / 0.0 |

**Picked: multilingual-e5-small (int8).** It is the only candidate that
finds nearly everything: 40 of 42 queries have a relevant note in the top 5
(the two misses: "what to buy at the supermarket" and "renting a table at a
card convention"), including every English question about a Dutch note and
vice versa, which the English models cannot do (0.0-0.25) and which matters
when notes are written in both languages. The cost is a 123 MB download,
~0.85 s to load (in the background), ~250 MB of RAM in `serve` while it is
loaded (freed after 10 minutes unused) and ~40 ms per note to embed; queries stay at ~6 ms. The fastest and smallest option,
all-MiniLM-L6-v2 int8, gets 0.64. Other models stay selectable
(`stickies setup --model minilm-l6-q8`, or `STICKIES_MODEL`); switching
re-embeds every note.

Two things the measurements changed:

- The 17 MB `tokenizer.json` of the XLM-R models took ~280 MB of RAM in HF
  `tokenizers` (twice that when each thread had its own). The 5 MB
  sentencepiece model gives identical ids (fairseq offset) at ~60 MB, so
  those two models use sentencepiece and one tokenizer is shared:
  multilingual-e5 went from 610 MB to ~300 MB RSS.
- onnxruntime's memory arena is off: 55 MB less RSS for ~25% slower
  backfill, the right trade for a process that is resident all day.

Similarity floor: notes below a per-model cosine (`min_sim`, 0.81 for e5)
never come back. 0.81 keeps the full R@5 on the eval set while nonsense
queries ("zzzz", "qwerty", "lorem ipsum") return 0-4 notes; 0.82 starts
losing relevant ones. e5's similarities are compressed (relevant notes
around 0.82-0.86, best irrelevant ~0.83), so on short queries a related
note can appear after the word matches; the overlay labels those
"≈ meaning".

