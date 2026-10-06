#!/usr/bin/env python3
"""stickies -- Omarchy-native sticky notes: store, CLI, FTS5 search, serve mode.

The CLI is the only writer of the database. The shell plugin talks to a
long-running `stickies serve` (JSON lines over stdin/stdout); agents and
humans use the subcommands, every read takes --json.

State lives in $STICKIES_STATE (default ~/.local/state/stickies/):
stickies.db (notes, FTS5 index, embeddings, change log) and stickies.log;
the dir is 0700 and its files 0600 (make_private).

Semantic search is optional and fully local: `stickies setup` (once, with
consent) makes a venv under ~/.cache/stickies with onnxruntime + tokenizers and
downloads a small embedding model into ~/.cache/stickies. Without them every
search quietly falls back to FTS5; this file itself stays stdlib-only and
re-executes under the venv's Python only for the commands that embed.
"""

import argparse
import json
import os
import re
import shlex
import sqlite3
import stat
import sys
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = 5

# Palette names the UI knows how to draw; any #rrggbb is accepted too.
COLORS = {
    "yellow": "#fff3a3",
    "pink": "#ffc4d6",
    "blue": "#bde0fe",
    "green": "#c7f0bd",
    "orange": "#ffd6a5",
    "purple": "#dcc6ff",
    "gray": "#e0e0e0",
}
DEFAULT_COLOR = "yellow"
DEFAULT_W, DEFAULT_H = 240, 200

# Public note shape: exactly the DB columns, in table order.
COLUMNS = ("id", "body", "color", "pinned", "workspace", "monitor",
           "x", "y", "w", "h", "z", "created_at", "updated_at", "archived_at",
           "rolled", "remind_at", "tags")

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY,
    body        TEXT    NOT NULL DEFAULT '',
    color       TEXT    NOT NULL DEFAULT 'yellow',
    pinned      INTEGER NOT NULL DEFAULT 0,
    workspace   INTEGER,
    monitor     TEXT,
    x           INTEGER NOT NULL DEFAULT 0,
    y           INTEGER NOT NULL DEFAULT 0,
    w           INTEGER NOT NULL DEFAULT 240,
    h           INTEGER NOT NULL DEFAULT 200,
    z           INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    archived_at TEXT,
    rolled      INTEGER NOT NULL DEFAULT 0,  -- collapsed to its header line
    remind_at   TEXT,                        -- next reminder not yet fired (UTC), or NULL
    tags        TEXT    NOT NULL DEFAULT ''  -- note_tags, space-separated, in the order added
);
CREATE INDEX IF NOT EXISTS notes_live ON notes(archived_at, pinned, updated_at);

-- Tags (buckets): the source of truth. notes.tags mirrors them (written in
-- the same transaction) so a note row, the FTS index and `list` need no join.
CREATE TABLE IF NOT EXISTS note_tags (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    tag     TEXT    NOT NULL,
    PRIMARY KEY (note_id, tag)
);
CREATE INDEX IF NOT EXISTS note_tags_tag ON note_tags(tag);

-- External-content FTS5 index over notes.body and notes.tags, kept in sync
-- by triggers. remove_diacritics so "cafe" finds "café"; prefix indexes
-- make as-you-type prefix queries cheap.
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    body, tags,
    content='notes', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2',
    prefix='2 3'
);

-- Change log read by `stickies serve` to push events, including for
-- writes made by other processes (agents using the CLI).
CREATE TABLE IF NOT EXISTS changes (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    kind    TEXT    NOT NULL
);

-- One vector per note for the active model (float32, L2-normalised).
-- body_hash says which text it was computed from, so stale rows are found
-- by comparing hashes; purge cascades (foreign_keys is on).
CREATE TABLE IF NOT EXISTS embeddings (
    note_id   INTEGER PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
    model     TEXT    NOT NULL,
    body_hash TEXT    NOT NULL,
    vec       BLOB    NOT NULL
);

-- Desktop settings (layout and the waterfall column), one JSON value per
-- key. Writes log a 'settings' change (note_id 0, never a real note) so
-- serve pushes them to every surface, whoever wrote them.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS settings_ai AFTER INSERT ON settings BEGIN
    INSERT INTO changes(note_id, kind) VALUES (0, 'settings');
END;
CREATE TRIGGER IF NOT EXISTS settings_au AFTER UPDATE ON settings BEGIN
    INSERT INTO changes(note_id, kind) VALUES (0, 'settings');
END;
-- Reminders: one row per `@ <when>` in a note. `spec` is the text as
-- written ("tomorrow 9:00"), resolved to `due` (UTC) when first seen, so a
-- relative day counts from when it was written. fired_at: notified (or
-- already past when written). notes.remind_at mirrors the earliest unfired.
CREATE TABLE IF NOT EXISTS reminders (
    note_id  INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    spec     TEXT    NOT NULL,
    due      TEXT    NOT NULL,
    fired_at TEXT,
    PRIMARY KEY (note_id, spec)
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(fired_at, due);

-- Small key -> JSON state (the one-step undo of `tidy`).
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, body, tags) VALUES (new.id, new.body, new.tags);
    INSERT INTO changes(note_id, kind) VALUES (new.id, 'add');
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, body, tags) VALUES ('delete', old.id, old.body, old.tags);
    INSERT INTO changes(note_id, kind) VALUES (old.id, 'purge');
END;
CREATE TRIGGER IF NOT EXISTS notes_au_fts AFTER UPDATE OF body, tags ON notes
WHEN old.body IS NOT new.body OR old.tags IS NOT new.tags BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, body, tags) VALUES ('delete', old.id, old.body, old.tags);
    INSERT INTO notes_fts(rowid, body, tags) VALUES (new.id, new.body, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
    INSERT INTO changes(note_id, kind) VALUES (new.id,
        CASE
            WHEN old.archived_at IS NULL AND new.archived_at IS NOT NULL THEN 'archive'
            WHEN old.archived_at IS NOT NULL AND new.archived_at IS NULL THEN 'restore'
            ELSE 'update'
        END);
END;
"""


class StickiesError(Exception):
    pass


# -- settings ------------------------------------------------------------------
# Layouts: free (notes where you put them) or a waterfall column docked to
# the left/right edge of the focused monitor. SUPER + ALT + L cycles them
# in this order.
LAYOUTS = ("free", "waterfall-right", "waterfall-left")
WATERFALL_MIN_W, WATERFALL_MAX_W = 160, 900


def _setting_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.lower() in ("on", "true", "1", "yes"):
        return True
    if isinstance(v, str) and v.lower() in ("off", "false", "0", "no"):
        return False
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    raise StickiesError(f"expected on/off, got {v!r}")


def _setting_layout(v):
    if v not in LAYOUTS:
        raise StickiesError(f"layout must be one of {', '.join(LAYOUTS)}, got {v!r}")
    return v


def _setting_width(v):
    if isinstance(v, bool):
        raise StickiesError(f"width must be a number of pixels, got {v!r}")
    try:
        w = int(v)
    except (TypeError, ValueError):
        raise StickiesError(f"width must be a number of pixels, got {v!r}")
    if not WATERFALL_MIN_W <= w <= WATERFALL_MAX_W:
        raise StickiesError(f"width must be {WATERFALL_MIN_W}-{WATERFALL_MAX_W} px, got {w}")
    return w


def _setting_minutes(v):
    """Minutes, 0 or more (a fraction is fine: the tests use seconds)."""
    if isinstance(v, bool):
        raise StickiesError(f"expected a number of minutes, got {v!r}")
    try:
        m = float(v)
    except (TypeError, ValueError):
        raise StickiesError(f"expected a number of minutes, got {v!r}")
    if not 0 <= m <= 10080:
        raise StickiesError(f"minutes must be 0-10080 (0: never), got {v!r}")
    return int(m) if m == int(m) else m


def _setting_ids(v):
    if not isinstance(v, (list, tuple)):
        raise StickiesError(f"expected a list of note ids, got {v!r}")
    out = []
    for x in v:
        if isinstance(x, bool):
            raise StickiesError(f"bad note id {x!r}")
        try:
            i = int(x)
        except (TypeError, ValueError):
            raise StickiesError(f"bad note id {x!r}")
        if i not in out:
            out.append(i)
    return out


# key -> (default, validator). waterfall_order: the column order a drag
# stored (notes not in it go first, pinned then most recently edited);
# waterfall_free: notes dragged out of the column, which stay free.
# idle_unload: minutes without a search or an embedding before serve drops
# the embedding model (0: keep it loaded); $STICKIES_IDLE_UNLOAD overrides.
SETTINGS = {
    "layout": ("free", _setting_layout),
    "waterfall_width": (320, _setting_width),
    "waterfall_reserve": (False, _setting_bool),
    "waterfall_collapsed": (False, _setting_bool),
    "waterfall_order": ([], _setting_ids),
    "waterfall_free": ([], _setting_ids),
    "idle_unload": (10, _setting_minutes),
}


def state_dir():
    d = os.environ.get("STICKIES_STATE") or os.path.expanduser("~/.local/state/stickies")
    private_dir(d)
    return d


# The state holds every note body: the dir is 0700, its files 0600, under
# any umask. make_private also repairs an install from before 1.3.1 (0755 /
# 0644), but only what we own and never through a symlink, so a
# STICKIES_STATE pointing somewhere shared is left as it is.
PRIVATE_DIR, PRIVATE_FILE = 0o700, 0o600


def make_private(path, mode):
    try:
        st = os.lstat(path)
        if (not stat.S_ISLNK(st.st_mode) and st.st_uid == os.getuid()
                and stat.S_IMODE(st.st_mode) != mode):
            os.chmod(path, mode)
    except OSError:
        pass


def private_dir(d):
    os.makedirs(d, mode=PRIVATE_DIR, exist_ok=True)  # mode: new dirs only, minus umask
    make_private(d, PRIVATE_DIR)


def open_private(path, mode="a"):
    """open() for a file in the state dir: created 0600, and an existing one
    of ours is made 0600."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | (os.O_APPEND if mode == "a" else os.O_TRUNC)
    fd = os.open(path, flags, PRIVATE_FILE)
    try:
        st = os.fstat(fd)
        if st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) != PRIVATE_FILE:
            os.fchmod(fd, PRIVATE_FILE)
        return os.fdopen(fd, mode)
    except BaseException:
        os.close(fd)
        raise


def now_iso():
    # UTC with milliseconds: sorts lexically, and rapid edits stay ordered.
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_color(color):
    c = (color or "").strip().lower()
    if c in COLORS:
        return c
    if re.fullmatch(r"#[0-9a-f]{6}", c):
        return c
    raise StickiesError(f"unknown color {color!r} (use {', '.join(COLORS)} or #rrggbb)")


# Tags (buckets): lower-case a-z, 0-9 and "-", at most 32 characters. A
# leading "#" is dropped, so "#Work" is the tag "work".
TAG_MAX = 32
TAGS_PER_NOTE = 16
_TAG = re.compile(r"^[a-z0-9-]{1,%d}$" % TAG_MAX)


def normalize_tag(tag):
    t = str(tag or "").strip().lstrip("#").lower()
    if not _TAG.match(t):
        raise StickiesError(f"bad tag {tag!r}: lower-case a-z, 0-9 and -, at most {TAG_MAX} characters")
    return t


def normalize_tags(tags):
    """A clean list (order kept, duplicates dropped) from a list of tags or
    one string of them (spaces or commas between)."""
    if not tags:
        return []
    if isinstance(tags, str):
        tags = tags.replace(",", " ").split()
    return list(dict.fromkeys(normalize_tag(t) for t in tags))


def tagged_sql(tags, col="id"):
    """WHERE part: `col` is a note with every one of `tags` (params: the
    tags, then their count)."""
    return (f"{col} IN (SELECT note_id FROM note_tags WHERE tag IN ({','.join('?' * len(tags))})"
            " GROUP BY note_id HAVING COUNT(*) = ?)")


# FTS5 tokenizer (unicode61) splits on anything that isn't a letter/number;
# mirror that so user text never reaches FTS5 as query syntax.
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def fts_query(text):
    """Turn free text into a safe FTS5 query: every word quoted and
    prefix-matched, all words required. None if there's nothing to search."""
    tokens = _TOKEN.findall(text or "")
    if not tokens:
        return None
    return " ".join('"%s"*' % t for t in tokens)


def fts_query_any(text):
    """Like fts_query, but any word may match (OR), skipping stopwords and
    words under 3 letters: for questions in natural language."""
    words = [t for t in _TOKEN.findall(text or "") if len(t) >= 3 and _fold(t) not in _STOP]
    return " OR ".join('"%s"*' % t for t in dict.fromkeys(words)) or None


_HL_START, _HL_END = "\x01", "\x02"


def split_highlights(marked):
    """Strip snippet markers; return (plain text, [[start, end], ...])."""
    out, spans, start, pos = [], [], None, 0
    for ch in marked:
        if ch == _HL_START:
            start = pos
        elif ch == _HL_END:
            if start is not None:
                spans.append([start, pos])
            start = None
        else:
            out.append(ch)
            pos += 1
    return "".join(out), spans


def _fold(word):
    """Lower-case and strip diacritics, like the FTS5 tokenizer does."""
    w = word.lower()
    if w.isascii():
        return w
    w = unicodedata.normalize("NFKD", w)
    return "".join(c for c in w if not unicodedata.combining(c))


# Query words not worth highlighting in a meaning-only hit (EN + NL).
_STOP = set("""the and for with what how when which that this from into about are was
have has but not you your all any can will het een van voor met wat hoe wanneer
die dat deze naar ook nog bij aan zijn heb niet maar
did does write wrote written note notes say said tell know who where why
over schreef geschreven notitie notities waar waarom wie welke weet""".split())


def plain_snippet(body, query, tokens=16):
    """A snippet for a hit FTS5 didn't find (semantic only): the words
    around the first word that starts with a query word (3+ letters, not a
    stopword), or the start of the note; same (text, highlights) shape as
    FTS5's snippet()."""
    words = list(_TOKEN.finditer(body))
    if not words:
        return body.strip(), []
    qs = [q for q in (_fold(t) for t in _TOKEN.findall(query or "")) if len(q) >= 3 and q not in _STOP]

    def hit(i):
        return bool(qs) and any(_fold(words[i].group()).startswith(q) for q in qs)

    first = next((i for i in range(len(words)) if hit(i)), 0)
    start = max(0, min(first - 3, len(words) - tokens))
    end = min(len(words), start + tokens)
    lead = "…" if start > 0 else ""
    base = words[start].start()
    text = lead + body[base:words[end - 1].end()] + ("…" if end < len(words) else "")
    spans = [[len(lead) + words[i].start() - base, len(lead) + words[i].end() - base]
             for i in range(start, end) if hit(i)]
    return text, spans


# -- local embeddings --------------------------------------------------------
# Small sentence-embedding models, run on the CPU with onnxruntime. Every file
# is pinned to a revision, size and sha256; `stickies setup` downloads the
# active model once (with consent) into ~/.cache/stickies/models/<name>/ and
# nothing touches the network after that. docs/search.md has the numbers
# behind DEFAULT_MODEL.

HF = "https://huggingface.co"
MODELS = {
    "minilm-l6-q8": {
        "repo": "sentence-transformers/all-MiniLM-L6-v2",
        "rev": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
        "files": {"model.onnx": ("onnx/model_quint8_avx2.onnx", 23046789,
                                 "b941bf19f1f1283680f449fa6a7336bb5600bdcd5f84d10ddc5cd72218a0fd21"),
                  "tokenizer.json": ("tokenizer.json", 466247,
                                     "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037")},
        "pooling": "mean", "max_len": 256, "dim": 384, "min_sim": 0.25,
        "about": "all-MiniLM-L6-v2, int8 (AVX2), English",
    },
    "minilm-l6": {
        "repo": "sentence-transformers/all-MiniLM-L6-v2",
        "rev": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
        "files": {"model.onnx": ("onnx/model.onnx", 90405214,
                                 "6fd5d72fe4589f189f8ebc006442dbb529bb7ce38f8082112682524616046452"),
                  "tokenizer.json": ("tokenizer.json", 466247,
                                     "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037")},
        "pooling": "mean", "max_len": 256, "dim": 384, "min_sim": 0.25,
        "about": "all-MiniLM-L6-v2, fp32, English",
    },
    "bge-small-en": {
        "repo": "BAAI/bge-small-en-v1.5",
        "rev": "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a",
        "files": {"model.onnx": ("onnx/model.onnx", 133093490,
                                 "828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35"),
                  "tokenizer.json": ("tokenizer.json", 711396,
                                     "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66")},
        "pooling": "cls", "max_len": 256, "dim": 384, "min_sim": 0.55,
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "about": "bge-small-en-v1.5, fp32, English",
    },
    "multi-minilm-l12-q8": {
        "repo": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "rev": "e8f8c211226b894fcb81acc59f3b34ba3efd5f42",
        "files": {"model.onnx": ("onnx/model_quint8_avx2.onnx", 118453870,
                                 "98a01d88b7de996cdea58c32ca71208c09968d143798814b2ea09d3439dc334f"),
                  "sentencepiece.bpe.model": ("sentencepiece.bpe.model", 5069051,
                                              "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865")},
        "tokenizer": "xlmr-spm", "pooling": "mean", "max_len": 128, "dim": 384, "min_sim": 0.36,
        "about": "paraphrase-multilingual-MiniLM-L12-v2, int8 (AVX2), 50+ languages incl. Dutch",
    },
    "e5-small-multi-q8": {
        "repo": "intfloat/multilingual-e5-small",
        "rev": "614241f622f53c4eeff9890bdc4f31cfecc418b3",
        "files": {"model.onnx": ("onnx/model_qint8_avx512_vnni.onnx", 118346824,
                                 "dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88"),
                  "sentencepiece.bpe.model": ("onnx/sentencepiece.bpe.model", 5069051,
                                              "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865")},
        "tokenizer": "xlmr-spm", "pooling": "mean", "max_len": 256, "dim": 384, "min_sim": 0.81,
        "query_prefix": "query: ", "doc_prefix": "passage: ",
        "about": "multilingual-e5-small, int8, 100 languages incl. Dutch",
    },
}
DEFAULT_MODEL = "e5-small-multi-q8"
VENV_PACKAGES = ("onnxruntime", "numpy", "tokenizers", "sentencepiece")
# Exact versions + sha256 of their wheels: requirements-search.txt, installed
# with --require-hashes. Its wheels are for this Python and CPU only.
VENV_LOCK = "requirements-search.txt"
VENV_PYTHON = (3, 14)
VENV_MACHINE = "x86_64"
# Below this many characters a query is still being typed; embedding "ca"
# only adds noise, so hybrid is FTS-only until then.
MIN_SEMANTIC_CHARS = 3
RRF_K = 60


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.environ.get("STICKIES_CACHE") or os.path.join(base, "stickies")


def active_model_file():
    return os.path.join(cache_dir(), "model")


def model_name(name=None):
    """The model to use: explicit, $STICKIES_MODEL, the one `stickies setup
    --model` last installed (cache_dir()/model), else DEFAULT_MODEL."""
    if not name and not os.environ.get("STICKIES_MODEL"):
        try:
            with open(active_model_file()) as f:
                name = f.read().strip()
        except OSError:
            pass
    name = name or os.environ.get("STICKIES_MODEL") or DEFAULT_MODEL
    if name not in MODELS:
        raise StickiesError(f"unknown model {name!r} (one of: {', '.join(MODELS)})")
    return name


def model_dir(name):
    return os.path.join(cache_dir(), "models", name)


def missing_model_files(name):
    """[(local name, remote path, size, sha256)] not (fully) downloaded yet."""
    out = []
    for local, (remote, size, sha) in MODELS[name]["files"].items():
        try:
            ok = os.path.getsize(os.path.join(model_dir(name), local)) == size
        except OSError:
            ok = False
        if not ok:
            out.append((local, remote, size, sha))
    return out


def venv_dir():
    """The venv holding onnxruntime + tokenizers: $STICKIES_VENV ("" = none),
    else a .venv next to this script if an older `stickies setup` made one
    there, else cache_dir()/venv. Never inside the plugin folder by default:
    omarchy-shell reloads a plugin when a file in its folder changes."""
    v = os.environ.get("STICKIES_VENV")
    if v is not None:
        return v or None
    legacy = os.path.join(os.path.dirname(os.path.realpath(__file__)), ".venv")
    if os.path.isdir(legacy):
        return legacy
    return os.path.join(cache_dir(), "venv")


def venv_python():
    v = venv_dir()
    py = os.path.join(v, "bin", "python") if v else None
    return py if py and os.path.exists(py) else None


def deps_importable():
    import importlib.util
    return all(importlib.util.find_spec(m) for m in VENV_PACKAGES)


# onnxruntime's PyPI builds ship Microsoft's 1DS telemetry, on by default: on
# import it writes a device id and an event queue to
# $XDG_CACHE_HOME/Microsoft/DeveloperTools/.onnxruntime and uploads session,
# model and system-metric events (with the host name and the venv's path) to
# mobile.events.data.microsoft.com (measured with 1.30.0, docs/search.md).
# ORT_DISABLE_TELEMETRY=1 before onnxruntime initialises stops all of it:
# no files, no connections. disable_telemetry_events() alone does not (the
# import has already written and connected), so this env goes on every
# process that imports onnxruntime, including `setup --status`'s check.
ORT_ENV = {"ORT_DISABLE_TELEMETRY": "1"}


def onnxruntime_shared_dir():
    """Where onnxruntime keeps its telemetry state when it is not switched
    off. Shared by every program on the machine that uses onnxruntime."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "Microsoft", "DeveloperTools", ".onnxruntime")


def import_onnxruntime():
    os.environ.update(ORT_ENV)
    import onnxruntime as ort
    try:
        ort.disable_telemetry_events()  # the API switch too, should the env ever stop counting
    except AttributeError:
        pass
    return ort


def embed_threads():
    try:
        return max(1, int(os.environ.get("STICKIES_THREADS", "")))
    except ValueError:
        return max(1, min(4, os.cpu_count() or 1))


class Embedder:
    """One ONNX sentence-embedding model. embed() returns L2-normalised
    float32 rows, so a dot product is the cosine similarity. Thread-safe:
    serve's indexer thread and its request loop share one instance."""

    def __init__(self, name=None, threads=None):
        ort = import_onnxruntime()
        import numpy as np
        self.np = np
        self.name = model_name(name)
        self.spec = MODELS[self.name]
        if missing_model_files(self.name):
            raise StickiesError(f"model {self.name} is not downloaded (run `stickies setup`)")
        d = model_dir(self.name)
        max_len = self.spec["max_len"]
        if self.spec.get("tokenizer") == "xlmr-spm":
            # XLM-R vocabularies: the 5 MB sentencepiece model gives the same
            # ids as the 17 MB tokenizer.json at ~60 MB of RAM instead of
            # ~280 MB. fairseq ids are sentencepiece's + 1, with <s>=0,
            # <pad>=1, </s>=2, <unk>=3.
            import sentencepiece
            sp = sentencepiece.SentencePieceProcessor(model_file=os.path.join(d, "sentencepiece.bpe.model"))
            self.pad_id = 1
            self.encode = lambda texts: [[0] + [i + 1 if i else 3 for i in ids[:max_len - 2]] + [2]
                                         for ids in sp.encode(texts)]
        else:
            from tokenizers import Tokenizer
            tok = Tokenizer.from_file(os.path.join(d, "tokenizer.json"))
            tok.no_padding()
            tok.enable_truncation(max_len)
            self.pad_id = next((tok.token_to_id(t) for t in ("[PAD]", "<pad>")
                                if tok.token_to_id(t) is not None), 0)
            self.encode = lambda texts: [e.ids for e in tok.encode_batch(texts)]
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads or embed_threads()
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # No busy-waiting worker threads: serve idles at 0% CPU between edits.
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        # serve keeps the model all day: ~55 MB less RSS for ~25% slower backfill.
        so.enable_cpu_mem_arena = False
        so.log_severity_level = 3
        self.session = ort.InferenceSession(os.path.join(d, "model.onnx"), so,
                                            providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}
        self.output = self.session.get_outputs()[0].name
        self.dim = self.spec["dim"]
        self._queries = {}

    def embed(self, texts, kind="doc", batch=16):
        np = self.np
        prefix = self.spec.get("query_prefix" if kind == "query" else "doc_prefix", "")
        enc = self.encode([prefix + t for t in texts])
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        # Similar lengths share a batch, so little time goes into padding.
        order = sorted(range(len(enc)), key=lambda i: len(enc[i]))
        for s in range(0, len(order), batch):
            idx = order[s:s + batch]
            width = max(len(enc[i]) for i in idx)
            ids = np.full((len(idx), width), self.pad_id, dtype=np.int64)
            mask = np.zeros((len(idx), width), dtype=np.int64)
            for r, i in enumerate(idx):
                ids[r, :len(enc[i])] = enc[i]
                mask[r, :len(enc[i])] = 1
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            h = self.session.run([self.output], feed)[0]
            if self.spec["pooling"] == "cls":
                v = h[:, 0]
            else:
                m = mask[..., None].astype(np.float32)
                v = (h * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1.0)
            v = v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)
            out[idx] = v
        return out

    def query(self, text):
        """Embedding of a search query, memoised (typing repeats queries)."""
        v = self._queries.get(text)
        if v is None:
            v = self.embed([text], kind="query")[0]
            if len(self._queries) > 256:
                self._queries.clear()
            self._queries[text] = v
        return v


_EMBEDDERS = {}


def embedder_problem(name=None):
    """Why semantic search is unavailable in this process, or None."""
    if os.environ.get("STICKIES_SEMANTIC") == "0":
        return "semantic search is switched off ($STICKIES_SEMANTIC=0)"
    name = model_name(name)
    if missing_model_files(name):
        return f"the embedding model ({name}) is not downloaded; run `stickies setup`"
    if not deps_importable():
        return "onnxruntime/tokenizers are not installed for this Python; run `stickies setup`"
    return None


def load_embedder(name=None):
    """A new Embedder for `name`, or None when the model or its packages
    are missing (search then degrades to FTS5)."""
    if embedder_problem(name) is None:
        try:
            return Embedder(name)
        except Exception as err:  # a broken download must not break FTS
            sys.stderr.write(f"stickies: embeddings unavailable: {err}\n")
    return None


def get_embedder(name=None):
    """The process-wide Embedder for `name` (kept for the process's life;
    serve's Indexer loads and drops its own instead), or None."""
    name = model_name(name)
    if name not in _EMBEDDERS:
        _EMBEDDERS[name] = load_embedder(name)
    return _EMBEDDERS[name]


def release_memory():
    """Give freed heap back to the OS. Dropping the model frees ~190 MB
    inside glibc's arenas, which keep it (RSS barely moves) until asked:
    malloc_trim(0) returns it. A no-op without glibc."""
    import gc
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def idle_unload(store=None):
    """(minutes, source): how long serve keeps an unused model loaded (0:
    for good), from $STICKIES_IDLE_UNLOAD, else the `idle_unload` setting."""
    env = os.environ.get("STICKIES_IDLE_UNLOAD", "").strip()
    if env:
        try:
            return _setting_minutes(env), "env"
        except StickiesError as e:
            Store.log_line(f"ignoring $STICKIES_IDLE_UNLOAD: {e}")
    if store is None:
        return SETTINGS["idle_unload"][0], "setting"  # its default
    return store.settings()["idle_unload"], "setting"


def model_url(name, remote):
    spec = MODELS[name]
    return f"{os.environ.get('STICKIES_HF') or HF}/{spec['repo']}/resolve/{spec['rev']}/{remote}"


def download(url, dest, size, sha256=None, progress=None):
    """Fetch url to dest via dest.part, checking size and sha256 before the
    rename, so a cut-off or tampered download never looks installed."""
    import urllib.request
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    import hashlib
    part, h, got, ok = dest + ".part", hashlib.sha256(), 0, False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "stickies-setup"})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if progress:
                    progress(got, size)
        if got != size:
            raise StickiesError(f"{url}: got {got} bytes, expected {size}")
        if sha256 and h.hexdigest() != sha256:
            raise StickiesError(f"{url}: sha256 mismatch ({h.hexdigest()})")
        os.replace(part, dest)
        ok = True
    except OSError as e:
        raise StickiesError(f"download failed: {url}: {e}")
    finally:
        if not ok:
            try:
                os.unlink(part)
            except OSError:
                pass
    return h.hexdigest()


def venv_lock_path():
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), VENV_LOCK)


def venv_pins():
    """[(name, version)] from the lock file, in its order."""
    pins = []
    with open(venv_lock_path()) as f:
        for line in f:
            spec = line.split("#", 1)[0].split()
            if spec and "==" in spec[0]:
                pins.append(tuple(spec[0].split("==", 1)))
    return pins


def venv_platform_problem():
    """Why the pinned wheels can't install for this Python, or None."""
    import platform
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    if (sys.implementation.name == "cpython" and sys.version_info[:2] == VENV_PYTHON and gil
            and platform.system() == "Linux" and platform.machine() == VENV_MACHINE):
        return None
    here = (f"{sys.implementation.name} {sys.version_info.major}.{sys.version_info.minor}"
            f"{'' if gil else ' (free-threaded)'} on {platform.system()} {platform.machine()}")
    return (f"the pinned packages ({VENV_LOCK}) are wheels for CPython "
            f"{'.'.join(map(str, VENV_PYTHON))} on Linux {VENV_MACHINE}; this is {here}")


def venv_has_deps(py):
    import subprocess
    return subprocess.run([py, "-c", "import " + ", ".join(VENV_PACKAGES)],
                          capture_output=True, env=dict(os.environ, **ORT_ENV)).returncode == 0


def setup_status(name=None):
    name = model_name(name)
    py = venv_python()
    return {"model": name, "about": MODELS[name]["about"], "model_dir": model_dir(name),
            "model_missing": [m[0] for m in missing_model_files(name)],
            "venv": venv_dir(), "venv_ready": bool(py and venv_has_deps(py)),
            "deps_in_this_python": deps_importable()}


def run_setup(name=None, yes=False, backfill=True, say=None):
    """Install semantic search: the .venv (onnxruntime, tokenizers, numpy
    from PyPI) and the model files (pinned, from huggingface.co). Shows
    exactly what it will fetch and asks first; --yes is the consent for
    scripts. Afterwards everything runs offline. Returns a summary."""
    import subprocess
    say = say or (lambda msg: sys.stderr.write(msg + "\n"))
    name = model_name(name)
    status = setup_status(name)
    venv = venv_dir()
    need_venv = not status["venv_ready"] and not status["deps_in_this_python"]
    if need_venv and venv is None:
        raise StickiesError("onnxruntime/tokenizers are missing and $STICKIES_VENV is empty")
    if need_venv and venv_platform_problem():
        raise StickiesError(venv_platform_problem() + "; search by meaning is not available "
                            "here, search by words works as before")
    missing = missing_model_files(name)
    plan = []
    if need_venv:
        plan.append(f"create {venv} and pip install from PyPI, exact versions checked by sha256 "
                    f"({VENV_LOCK}): " + ", ".join(f"{n} {v}" for n, v in venv_pins())
                    + " (~45 MB download, ~165 MB on disk)")
    for local, remote, size, _ in missing:
        plan.append(f"download {MODELS[name]['repo']}/{remote} ({size / 1e6:.1f} MB) "
                    f"from huggingface.co -> {model_dir(name)}/{local}")
    if plan and not yes:
        say("Search by meaning finds a note by what it says, not only by its words:\n"
            "\"dentist\" finds \"Tandarts afspraak vrijdag\", English questions find Dutch\n"
            "notes. It runs on this laptop's CPU. Without it, search is by words only and\n"
            "everything else works the same.\n")
        say(f"stickies setup will, once (model: {name}, {MODELS[name]['about']}):")
        for line in plan:
            say("  - " + line)
        if backfill:
            say("  - then embed your existing notes, locally (~40 ms a note)")
        say("This is the only time search touches the network; your notes are never sent,\n"
            "and afterwards search works with Wi-Fi off. It costs ~250 MB of RAM while in\n"
            "use, freed after 10 minutes without a search. Undo: delete " + " and ".join(
                d for d in (venv if need_venv else None, model_dir(name)) if d)
            + "\n(`stickies uninstall --purge` does that too).")
        if not sys.stdin.isatty():
            raise StickiesError("setup needs your OK to download; run `stickies setup --yes`")
        if input("Download now? [y/N] ").strip().lower() not in ("y", "yes"):
            raise StickiesError("setup cancelled; nothing was downloaded")
    if need_venv:
        say(f"creating {venv} ...")
        subprocess.run([sys.executable, "-m", "venv", venv], check=True)
        p = subprocess.run([os.path.join(venv, "bin", "pip"), "install", "--disable-pip-version-check",
                            "-q", "--no-cache-dir",  # not ~/.cache/pip: nothing outside our dirs
                            "--require-hashes", "--only-binary=:all:", "--no-deps",
                            "-r", venv_lock_path()])
        if p.returncode != 0:
            raise StickiesError("pip install failed (see above)")
    tty = sys.stderr.isatty()
    for local, remote, size, sha in missing:
        def progress(got, total, label=local):
            if tty:
                sys.stderr.write(f"\r  {label}: {got / 1e6:6.1f} / {total / 1e6:.1f} MB")
        say(f"downloading {remote} ...")
        download(model_url(name, remote), os.path.join(model_dir(name), local), size, sha, progress)
        if tty:
            sys.stderr.write("\n")
    os.makedirs(cache_dir(), exist_ok=True)
    with open(active_model_file(), "w") as f:
        f.write(name + "\n")
    out = dict(setup_status(name), installed=plan, backfill=None)
    if backfill:
        py = venv_python() if not deps_importable() else sys.executable
        p = subprocess.run([py, os.path.realpath(__file__), "backfill", "--json"],
                           capture_output=True, text=True, env=dict(os.environ, STICKIES_REEXEC="1"))
        try:
            out["backfill"] = json.loads(p.stdout)
        except ValueError:
            out["backfill"] = {"error": (p.stderr or p.stdout).strip()}
    return out


def doc_text(body):
    return (body or "").strip()


def body_hash(model, text):
    import hashlib
    return hashlib.sha1(f"{model}\0{text}".encode()).hexdigest()


# -- reminders ---------------------------------------------------------------
# "@ 2026-10-07 09:00", "@tomorrow 9:00" or "@ 17:30 call the plumber" in a note
# schedules a desktop notification. The `@` starts a line or follows a
# space (so e-mail addresses don't count). Days: YYYY-MM-DD, today/tomorrow
# (Dutch vandaag/morgen too), time optional (09:00); a time alone means its
# next occurrence. `stickies serve` (the desktop plugin) sends them.

_REMIND = re.compile(
    r"(?:^|(?<=\s))@[ \t]?(?:(?P<day>\d{4}-\d{2}-\d{2}|today|tomorrow|vandaag|morgen)"
    r"(?:[ \t]+(?P<time>\d{1,2}(?:[:.]\d{2})?))?|(?P<clock>\d{1,2}[:.]\d{2}))(?=[\s,;!?)]|$)",
    re.I | re.M)
DEFAULT_REMIND_TIME = (9, 0)
# Height of a rolled-up note: its header line (NoteCard.headerHeight).
TITLE_H = 26


def _clock(text):
    if not text:
        return DEFAULT_REMIND_TIME
    h, _, m = text.replace(".", ":").partition(":")
    h, m = int(h), int(m or 0)
    return (h, m) if h < 24 and m < 60 else None


def resolve_reminder(day, clock, now):
    """UTC ISO time for one `@` spec, relative to `now` (aware, local), or
    None if it isn't a real date/time."""
    t = _clock(clock)
    if t is None:
        return None
    day = (day or "").lower()
    if day in ("today", "vandaag", ""):
        d = now.date()
    elif day in ("tomorrow", "morgen"):
        d = now.date() + timedelta(days=1)
    else:
        try:
            d = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            return None
    when = datetime(d.year, d.month, d.day, *t).astimezone()  # naive = local time
    if not day and when <= now:  # a time alone: its next occurrence
        when = (datetime(d.year, d.month, d.day, *t) + timedelta(days=1)).astimezone()
    return when.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def find_reminders(body, now=None):
    """[(spec, due UTC ISO, match)] for every valid `@ <when>` in body."""
    now = now or datetime.now().astimezone()
    out = []
    for m in _REMIND.finditer(body or ""):
        day = m.group("day")
        clock = m.group("time") if day else m.group("clock")
        due = resolve_reminder(day, clock, now)
        if due:
            spec = " ".join(x for x in ((day or "").lower(), (clock or "").replace(".", ":")) if x)
            out.append((spec, due, m))
    return out


def reminder_text(body, spec):
    """What a reminder says: the rest of its line, else the note's first
    line (without the `@` part either way)."""
    for s, _, m in find_reminders(body):
        if s == spec:
            end = body.find("\n", m.end())
            rest = body[m.end():None if end < 0 else end].strip(" \t-:,;")
            start = body.rfind("\n", 0, m.start()) + 1
            before = body[start:m.start()].strip(" \t-:,;")
            if rest or before:
                return (rest or before)[:200]
            break
    return _first_line(_REMIND.sub("", body or ""), 120) or "(empty note)"


def notify_bin():
    """notify-send, or $STICKIES_NOTIFY (empty: off). Off while
    $STICKIES_STATE points somewhere (tests, benches) unless
    $STICKIES_NOTIFY says otherwise, like hub_bin."""
    if "STICKIES_NOTIFY" in os.environ:
        return os.environ["STICKIES_NOTIFY"] or None
    return None if os.environ.get("STICKIES_STATE") else "notify-send"


def send_notification(title, text):
    import subprocess
    exe = notify_bin()
    if exe is None:
        return False
    p = subprocess.run([exe, "--app-name=Stickies", "--icon=accessories-text-editor", title, text],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
    if p.returncode != 0:
        raise StickiesError((p.stderr or p.stdout).strip() or f"{exe} exited {p.returncode}")
    return True


# -- clipboard ---------------------------------------------------------------

CLIP_MAX = 10000
_CLIP_TYPES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT")


def clipboard_text():
    """(text, truncated): the clipboard's text via wl-paste, trimmed, at
    most CLIP_MAX characters. Images and files are refused in plain words."""
    import subprocess
    exe = os.environ.get("STICKIES_WL_PASTE") or "wl-paste"
    try:
        types = subprocess.run([exe, "--list-types"], capture_output=True, text=True, timeout=3)
    except FileNotFoundError:
        raise StickiesError("can't read the clipboard: wl-paste is missing (install wl-clipboard)")
    except subprocess.TimeoutExpired:
        raise StickiesError("the clipboard didn't answer; try copying again")
    offered = [t.strip() for t in types.stdout.splitlines() if t.strip()]
    if types.returncode != 0 or not offered:
        raise StickiesError("the clipboard is empty")
    kind = next((t for t in _CLIP_TYPES if t in offered), None)
    kind = kind or next((t for t in offered if t.startswith("text/") and t != "text/uri-list"), None)
    if kind is None:
        what = ("a file" if "text/uri-list" in offered else
                "an image" if any(t.startswith("image/") for t in offered) else offered[0])
        raise StickiesError(f"the clipboard holds {what}, not text")
    try:
        p = subprocess.run([exe, "--no-newline", "--type", kind], capture_output=True, timeout=3)
    except subprocess.TimeoutExpired:
        raise StickiesError("the clipboard didn't answer; try copying again")
    text = p.stdout.decode("utf-8", "replace").strip()
    if p.returncode != 0 or not text:
        raise StickiesError("the clipboard is empty")
    return (text[:CLIP_MAX].rstrip(), True) if len(text) > CLIP_MAX else (text, False)


# -- Hyprland (tidy) ---------------------------------------------------------

def hyprctl_json(*args):
    import subprocess
    exe = os.environ.get("STICKIES_HYPRCTL") or "hyprctl"
    try:
        p = subprocess.run([exe, "-j", *args], capture_output=True, text=True, timeout=3)
        return json.loads(p.stdout) if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def hypr_gaps_out():
    """(top, right, bottom, left) of general:gaps_out; 10 all round if unknown."""
    d = hyprctl_json("getoption", "general:gaps_out") or {}
    raw = str(d.get("css") or d.get("custom") or d.get("int") or "10")
    nums = [int(float(n)) for n in re.findall(r"-?\d+(?:\.\d+)?", raw)][:4] or [10]
    while len(nums) < 4:  # css shorthand: 1, 2 or 3 values
        nums.append(nums[{1: 0, 2: 0, 3: 1}[len(nums)]])
    return tuple(nums)


def hypr_screen(monitor=None):
    """{monitor, workspace, width, height, reserved, primary} of a monitor
    (default: the focused one) in layout pixels, or StickiesError."""
    mons = hyprctl_json("monitors")
    if not mons:
        raise StickiesError("can't see the screens (is Hyprland running?); "
                            "run it from the desktop session")
    if monitor:
        m = next((m for m in mons if m.get("name") == monitor), None)
        if m is None:
            raise StickiesError(f"no monitor named {monitor!r} (there is {', '.join(x['name'] for x in mons)})")
    else:
        m = next((m for m in mons if m.get("focused")), mons[0])
    scale = m.get("scale") or 1
    w, h = m["width"] / scale, m["height"] / scale
    if m.get("transform", 0) % 2:
        w, h = h, w
    return {"monitor": m["name"], "workspace": (m.get("activeWorkspace") or {}).get("id"),
            "width": round(w), "height": round(h), "reserved": tuple(m.get("reserved") or (0, 0, 0, 0)),
            "primary": m["id"] == min(x["id"] for x in mons)}


def _writes(fn):
    """A Store method that changes notes: afterwards, tell a running serve
    (so the desktop shows it at once, with no polling)."""
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        finally:
            self._poke()
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


def notify_serve(d):
    """One `{"op": "changed"}` line to serve's socket in state dir `d`, if a
    serve listens there. Fire and forget: serve reads the change log."""
    path = os.path.join(d, SEARCH_SOCKET)
    if not os.path.exists(path):
        return
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(path)
        s.sendall(b'{"op": "changed"}\n')
    except OSError:
        pass
    finally:
        s.close()


class Store:
    def __init__(self, path=None, embedder="auto"):
        self.dir = state_dir() if path is None else os.path.dirname(path)
        self.path = path or os.path.join(self.dir, "stickies.db")
        # Created 0600 before SQLite sees it: SQLite gives its -wal and -shm
        # the main file's mode (whatever the umask), so they are 0600 too.
        try:
            os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, PRIVATE_FILE))
        except OSError:  # there already (or not ours to create: SQLite says why)
            pass
        self.db = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        # Repair files an older version left 0644 (an existing -wal / -shm
        # keeps its mode, so they need it as well as the db).
        for name in ("", "-wal", "-shm", "-journal"):
            make_private(self.path + name, PRIVATE_FILE)
        for name in ("stickies.log", "integration.json"):
            make_private(os.path.join(self.dir, name), PRIVATE_FILE)
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            self._migrate()
        # "auto": the process-wide model if it is installed (looked up on the
        # first search that wants it); None: FTS only; or an Embedder.
        self._embedder = embedder
        self._vec_cache = None
        self._vec_writes = 0
        # Poke serve after writes; serve's own Store says no (it drains itself).
        self.notify = True
        self._poked = self.db.total_changes

    def _poke(self):
        if self.notify and self.db.total_changes != self._poked:
            self._poked = self.db.total_changes
            notify_serve(self.dir)

    def _migrate(self):
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(notes)")}
        # An older DB: v4 added rolled + remind_at, v5 tags. Another process
        # opening it at the same moment may have added one first.
        for col, ddl in (("rolled", "rolled INTEGER NOT NULL DEFAULT 0"), ("remind_at", "remind_at TEXT"),
                         ("tags", "tags TEXT NOT NULL DEFAULT ''")):
            if cols and col not in cols:
                try:
                    self.db.execute(f"ALTER TABLE notes ADD COLUMN {ddl}")
                except sqlite3.OperationalError as e:
                    if "duplicate column" not in str(e):
                        raise
        fts = [r[1] for r in self.db.execute("PRAGMA table_info(notes_fts)")]
        if fts and "tags" not in fts:
            # v5 indexes the tags too: a new FTS table and triggers, rebuilt
            # from the notes, in one transaction (a writer waits, never misses).
            self.db.executescript("""BEGIN IMMEDIATE;
                DROP TRIGGER IF EXISTS notes_ai; DROP TRIGGER IF EXISTS notes_ad;
                DROP TRIGGER IF EXISTS notes_au_body; DROP TABLE IF EXISTS notes_fts;
                """ + SCHEMA + """
                INSERT INTO notes_fts(notes_fts) VALUES ('rebuild');
                COMMIT;""")
        self.db.executescript(SCHEMA)
        for r in self.db.execute("SELECT id, body FROM notes WHERE instr(body, '@') > 0").fetchall():
            self._sync_reminders(r["id"], r["body"])
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @property
    def embedder(self):
        if self._embedder == "auto":
            self._embedder = get_embedder()
        return self._embedder

    @embedder.setter
    def embedder(self, value):
        self._embedder = value

    def close(self):
        self.db.close()

    # -- helpers -----------------------------------------------------------

    def log(self, msg):
        self.log_line(msg, self.dir)

    @staticmethod
    def log_line(msg, d=None):
        try:
            with open_private(os.path.join(d or state_dir(), "stickies.log")) as f:
                f.write(f"{now_iso()} {msg}\n")
        except OSError:
            pass

    @staticmethod
    def to_dict(row):
        d = {k: row[k] for k in COLUMNS}
        d["pinned"] = bool(d["pinned"])
        d["rolled"] = bool(d["rolled"])
        d["tags"] = d["tags"].split() if d["tags"] else []
        return d

    def _row(self, note_id):
        row = self.db.execute("SELECT * FROM notes WHERE id=?", (int(note_id),)).fetchone()
        if row is None:
            raise StickiesError(f"no note #{note_id}")
        return row

    def _update(self, note_id, **fields):
        self._row(note_id)
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE notes SET {cols} WHERE id=?", (*fields.values(), int(note_id)))
        return self.get(note_id)

    def _tx(self):
        """A transaction, or a part of the caller's (bench seeding wraps
        thousands of adds in one)."""
        import contextlib
        if self.db.in_transaction:
            return contextlib.nullcontext()

        @contextlib.contextmanager
        def tx():
            self.db.execute("BEGIN")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
        return tx()

    def _top_z(self):
        return self.db.execute("SELECT COALESCE(MAX(z), 0) FROM notes WHERE archived_at IS NULL").fetchone()[0]

    # -- reads -------------------------------------------------------------

    def get(self, note_id):
        return self.to_dict(self._row(note_id))

    def list(self, archived=False, all=False, workspace=None, color=None,
             pinned=None, limit=None, tags=None):
        """Notes, pinned first then most recently updated. tags: only notes
        that have every one of them."""
        where, params = [], []
        tags = normalize_tags(tags)
        if tags:
            where.append(tagged_sql(tags))
            params += [*tags, len(tags)]
        if not all:
            where.append("archived_at IS NOT NULL" if archived else "archived_at IS NULL")
        if workspace is not None:
            where.append("workspace=?")
            params.append(int(workspace))
        if color is not None:
            where.append("color=?")
            params.append(normalize_color(color))
        if pinned is not None:
            where.append("pinned=?")
            params.append(1 if pinned else 0)
        sql = "SELECT * FROM notes"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY pinned DESC, updated_at DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self.to_dict(r) for r in self.db.execute(sql, params)]

    def search(self, query, limit=20, archived=False, tokens=16, mode="hybrid", tags=None):
        """Search notes. mode: "fts" (FTS5 only), "semantic" (embeddings
        only; error without the model) or "hybrid" (both, reciprocal-rank
        fused; FTS5 alone while the model is missing).
        Each hit is the note plus `snippet`, `highlights` (char offsets into
        snippet), `score` (higher is better; -bm25 for fts, the fused or
        cosine score otherwise), `match` ("fts", "semantic" or "both") and
        `similarity` (cosine to the query, null when not computed).
        tags: only notes that have every one of them."""
        tags = normalize_tags(tags)
        if mode == "fts":
            return self.search_fts(query, limit, archived, tokens, tags=tags)
        if mode not in ("semantic", "hybrid"):
            raise StickiesError(f"unknown search mode {mode!r} (fts, semantic or hybrid)")
        emb = self.embedder
        if mode == "semantic":
            if emb is None:
                raise StickiesError("semantic search unavailable: " + (embedder_problem() or "model not loaded yet"))
            hits = []
            for n, sim in self.search_semantic(query, emb, limit, archived, tags=tags):
                n["snippet"], n["highlights"] = plain_snippet(n["body"], query, tokens)
                n.update(score=round(sim, 6), match="semantic", similarity=round(sim, 4))
                hits.append(n)
            return hits

        pool = max(int(limit), 50)
        fused = {}
        for rank, h in enumerate(self.search_fts(query, pool, archived, tokens, tags=tags)):
            h.update(score=1.0 / (RRF_K + rank + 1), match="fts", similarity=None)
            fused[h["id"]] = h
        if emb is not None:
            for rank, (n, sim) in enumerate(self.search_semantic(query, emb, pool, archived, tags=tags)):
                h = fused.get(n["id"])
                if h is None:
                    h = fused[n["id"]] = n
                    h.update(score=0.0, match="semantic")
                else:
                    h["match"] = "both"
                h["score"] += 1.0 / (RRF_K + rank + 1)
                h["similarity"] = round(sim, 4)
        hits = sorted(fused.values(), key=lambda h: (-h["score"], h["match"] != "both"))[:int(limit)]
        for h in hits:
            h["score"] = round(h["score"], 6)
            if h["match"] == "semantic":
                h["snippet"], h["highlights"] = plain_snippet(h["body"], query, tokens)
        return hits

    def search_semantic(self, query, embedder, limit=20, archived=False, tags=None):
        """[(note, cosine)] best first, above the model's min_sim. Empty for
        queries shorter than MIN_SEMANTIC_CHARS (still being typed)."""
        if len((query or "").strip()) < MIN_SEMANTIC_CHARS:
            return []
        ids, mat = self._vectors(embedder)
        if ids is None:
            return []
        np = embedder.np
        sims = mat @ embedder.query(query.strip())
        k = min(len(sims), max(int(limit) * 2, 50))
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        floor = embedder.spec["min_sim"]
        best = [(int(ids[i]), float(sims[i])) for i in top if sims[i] >= floor]
        if not best:
            return []
        live = "" if archived else "AND archived_at IS NULL"
        params = [i for i, _ in best]
        if tags:
            live += " AND " + tagged_sql(tags)
            params += [*tags, len(tags)]
        rows = {r["id"]: r for r in self.db.execute(
            f"SELECT * FROM notes WHERE id IN ({','.join('?' * len(best))}) {live}", params)}
        return [(self.to_dict(rows[i]), s) for i, s in best if i in rows][:int(limit)]

    def _vectors(self, embedder):
        """(ids, matrix) of every stored vector for the embedder's model,
        cached until this or another connection writes."""
        key = (embedder.name, self.data_version(), self._vec_writes)
        if self._vec_cache is None or self._vec_cache[0] != key:
            np = embedder.np
            rows = self.db.execute("SELECT note_id, vec FROM embeddings WHERE model=?",
                                   (embedder.name,)).fetchall()
            if rows:
                ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
                mat = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
            else:
                ids = mat = None
            self._vec_cache = (key, ids, mat)
        return self._vec_cache[1], self._vec_cache[2]

    # -- embeddings --------------------------------------------------------

    def stale_embeddings(self, model, ids=None):
        """[(id, text, hash)] of notes whose vector is missing, from another
        model, or computed from different text. Empty notes are skipped."""
        sql = ("SELECT n.id, n.body, e.body_hash, e.model FROM notes n "
               "LEFT JOIN embeddings e ON e.note_id = n.id")
        params = []
        if ids is not None:
            ids = [int(i) for i in ids]
            if not ids:
                return []
            sql += f" WHERE n.id IN ({','.join('?' * len(ids))})"
            params = ids
        out = []
        for r in self.db.execute(sql, params):
            text = doc_text(r["body"])
            h = body_hash(model, text)
            if text and (r["model"] != model or r["body_hash"] != h):
                out.append((r["id"], text, h))
        return out

    def embed_notes(self, embedder, ids=None, batch=16, should_stop=None):
        """Embed every stale note (or just `ids`); returns how many."""
        todo = self.stale_embeddings(embedder.name, ids)
        done = 0
        for s in range(0, len(todo), batch):
            if should_stop and should_stop():
                break
            chunk = todo[s:s + batch]
            vecs = embedder.embed([t for _, t, _ in chunk])
            self.db.execute("BEGIN")
            try:
                for (nid, _, h), v in zip(chunk, vecs):
                    # The note may have been purged meanwhile (FK would fail).
                    self.db.execute(
                        "INSERT OR REPLACE INTO embeddings(note_id, model, body_hash, vec) "
                        "SELECT id, ?, ?, ? FROM notes WHERE id=?", (embedder.name, h, v.tobytes(), nid))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            done += len(chunk)
            self._vec_writes += 1
        return done

    def embedding_stats(self, model):
        notes = self.db.execute("SELECT COUNT(*) FROM notes WHERE TRIM(body, ' '||char(9,10,13)) != ''").fetchone()[0]
        return {"model": model, "notes": notes,
                "embedded": self.db.execute("SELECT COUNT(*) FROM embeddings WHERE model=?",
                                            (model,)).fetchone()[0],
                "stale": len(self.stale_embeddings(model))}

    def search_fts(self, query, limit=20, archived=False, tokens=16, any_word=False, tags=None):
        """bm25-ranked FTS5 search with every word prefix-matched.
        Each hit is the note plus `snippet`, `highlights` (char offsets
        into snippet) and `score` (higher is better). any_word: a note
        needs only one of the (non-stopword) words, more rank higher."""
        q = fts_query_any(query) if any_word else fts_query(query)
        if q is None:
            return []
        live = "" if archived else "AND n.archived_at IS NULL"
        extra = []
        if tags:
            live += " AND " + tagged_sql(tags, "n.id")
            extra = [*tags, len(tags)]
        sql = f"""
            SELECT n.*, bm25(notes_fts) AS rank,
                   snippet(notes_fts, 0, ?, ?, '…', ?) AS snip
            FROM notes_fts JOIN notes n ON n.id = notes_fts.rowid
            WHERE notes_fts MATCH ? {live}
            ORDER BY rank, n.updated_at DESC
            LIMIT ?"""
        try:
            rows = self.db.execute(sql, (_HL_START, _HL_END, int(tokens), q, *extra, int(limit))).fetchall()
        except sqlite3.OperationalError as e:
            raise StickiesError(f"search failed: {e}")
        hits = []
        for r in rows:
            d = self.to_dict(r)
            d["snippet"], d["highlights"] = split_highlights(r["snip"])
            d["score"] = -r["rank"]
            d["match"], d["similarity"] = "fts", None
            hits.append(d)
        return hits

    # -- writes ------------------------------------------------------------

    @_writes
    def add(self, body, color=None, pinned=False, workspace=None, monitor=None,
            x=None, y=None, w=None, h=None, tags=None):
        color = normalize_color(color or DEFAULT_COLOR)
        tags = self._check_tags(tags)
        ts = now_iso()
        n = self.db.execute("SELECT COUNT(*) FROM notes WHERE archived_at IS NULL").fetchone()[0]
        # Cascade new notes so they don't stack exactly on top of each other.
        step = (n % 8) * 32
        x = 80 + step if x is None else int(x)
        y = 80 + step if y is None else int(y)
        with self._tx():
            cur = self.db.execute(
                "INSERT INTO notes(body, color, pinned, workspace, monitor, x, y, w, h, z,"
                " created_at, updated_at, tags) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (body or "", color, 1 if pinned else 0,
                 None if workspace is None else int(workspace), monitor,
                 x, y, int(w or DEFAULT_W), int(h or DEFAULT_H), self._top_z() + 1, ts, ts, " ".join(tags)))
            self.db.executemany("INSERT INTO note_tags(note_id, tag) VALUES (?, ?)",
                                [(cur.lastrowid, t) for t in tags])
        self.log(f"add #{cur.lastrowid}")
        self._sync_reminders(cur.lastrowid, body or "")
        return self.get(cur.lastrowid)

    @_writes
    def edit(self, note_id, body=None, append=None):
        if body is None and append is None:
            raise StickiesError("edit needs body or append")
        if append is not None:
            old = self._row(note_id)["body"]
            body = (old + ("\n" if old and not old.endswith("\n") else "") + append) if old else append
        self.log(f"edit #{note_id}")
        self._update(note_id, body=body)
        self._sync_reminders(note_id, body)
        return self.get(note_id)

    # -- tags ----------------------------------------------------------------

    @staticmethod
    def _check_tags(tags):
        tags = normalize_tags(tags)
        if len(tags) > TAGS_PER_NOTE:
            raise StickiesError(f"at most {TAGS_PER_NOTE} tags on a note")
        return tags

    def _write_tags(self, note_id, tags):
        """Replace the note's tags (note_tags + the notes.tags mirror, one
        transaction). An unchanged list writes nothing."""
        row = self._row(note_id)
        tags = self._check_tags(tags)
        if row["tags"] == " ".join(tags):
            return self.to_dict(row)
        nid = int(note_id)
        with self._tx():
            self.db.execute("DELETE FROM note_tags WHERE note_id=?", (nid,))
            self.db.executemany("INSERT INTO note_tags(note_id, tag) VALUES (?, ?)", [(nid, t) for t in tags])
            self._update(nid, tags=" ".join(tags))
        self.log(f"tags #{nid}: {' '.join(tags) or '(none)'}")
        return self.get(nid)

    @_writes
    def tag(self, note_id, tags):
        """Add tags (the ones it already has stay where they are)."""
        return self._write_tags(note_id, self.get(note_id)["tags"] + normalize_tags(tags))

    @_writes
    def untag(self, note_id, tags):
        drop = set(normalize_tags(tags))
        return self._write_tags(note_id, [t for t in self.get(note_id)["tags"] if t not in drop])

    @_writes
    def set_tags(self, note_id, tags):
        """Exactly these tags (the desktop's tag editor)."""
        return self._write_tags(note_id, tags)

    def all_tags(self, archived=False):
        """[{tag, count}] over the live notes (archived=True: every note),
        most used first."""
        live = "" if archived else "WHERE n.archived_at IS NULL"
        return [{"tag": r[0], "count": r[1]} for r in self.db.execute(
            f"SELECT t.tag, COUNT(*) AS c FROM note_tags t JOIN notes n ON n.id = t.note_id {live}"
            " GROUP BY t.tag ORDER BY c DESC, t.tag")]

    @_writes
    def pin(self, note_id, pinned=True):
        self.log(f"{'pin' if pinned else 'unpin'} #{note_id}")
        return self._update(note_id, pinned=1 if pinned else 0)

    @_writes
    def color(self, note_id, color):
        return self._update(note_id, color=normalize_color(color))

    @_writes
    def move(self, note_id, x=None, y=None, w=None, h=None, z=None,
             workspace=None, monitor=None, raise_=False):
        fields = {}
        for k, v in (("x", x), ("y", y), ("w", w), ("h", h), ("z", z), ("workspace", workspace)):
            if v is not None:
                fields[k] = int(v)
        if monitor is not None:
            fields["monitor"] = monitor
        if raise_:
            top = self._top_z()
            if self._row(note_id)["z"] < top or self.db.execute(
                    "SELECT COUNT(*) FROM notes WHERE z=? AND archived_at IS NULL", (top,)).fetchone()[0] > 1:
                fields["z"] = top + 1
        for k in ("w", "h"):
            if k in fields and fields[k] < 1:
                raise StickiesError(f"{k} must be positive")
        if not fields:
            return self.get(note_id)
        return self._update(note_id, **fields)

    @_writes
    def archive(self, note_id):
        row = self._row(note_id)
        if row["archived_at"] is not None:
            return self.to_dict(row)
        self.log(f"archive #{note_id}")
        return self._update(note_id, archived_at=now_iso())

    @_writes
    def restore(self, note_id):
        self.log(f"restore #{note_id}")
        return self._update(note_id, archived_at=None, z=self._top_z() + 1)

    @_writes
    def purge(self, note_id, force=False):
        row = self._row(note_id)
        if row["archived_at"] is None and not force:
            raise StickiesError(f"note #{note_id} is not archived; rm it first (or --force)")
        self.db.execute("DELETE FROM notes WHERE id=?", (int(note_id),))
        self.log(f"purge #{note_id}")
        return {"id": int(note_id), "purged": True}

    @_writes
    def discard_empty(self, note_id=None, older_than=None):
        """Delete live notes with no text (whitespace only): a note opened
        and left empty is not worth keeping, not even in the archive (one
        with tags is: those were put there on purpose). One
        note (`note_id`; a note with text is left alone) or all of them,
        optionally only those untouched for `older_than` seconds. Returns
        the ids deleted."""
        sql = ("SELECT id, updated_at FROM notes WHERE archived_at IS NULL AND trim(body, ' \t\r\n') = ''"
               " AND tags = ''")
        params = []
        if note_id is not None:
            sql += " AND id=?"
            params.append(int(note_id))
        cutoff = None
        if older_than is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=float(older_than))) \
                .isoformat(timespec="milliseconds").replace("+00:00", "Z")
        ids = [r["id"] for r in self.db.execute(sql, params)
               if cutoff is None or r["updated_at"] <= cutoff]
        for i in ids:
            self.db.execute("DELETE FROM notes WHERE id=? AND archived_at IS NULL"
                            " AND trim(body, ' \t\r\n') = '' AND tags = ''", (i,))
            self.log(f"discard empty #{i}")
        return ids

    # -- settings ----------------------------------------------------------

    def settings(self):
        """Every setting, defaults filled in (a stored value that no longer
        validates falls back to the default)."""
        stored = {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM settings")}
        out = {}
        for key, (default, check) in SETTINGS.items():
            v = default
            if key in stored:
                try:
                    v = check(json.loads(stored[key]))
                except (ValueError, StickiesError):
                    pass
            out[key] = list(v) if isinstance(v, list) else v
        return out

    @_writes
    def set_settings(self, values):
        """Validate and store several settings at once (one change event).
        Id lists are trimmed to live notes."""
        if not isinstance(values, dict) or not values:
            raise StickiesError("set needs {key: value, ...}")
        clean = {}
        for key, v in values.items():
            if key not in SETTINGS:
                raise StickiesError(f"unknown setting {key!r} (known: {', '.join(SETTINGS)})")
            clean[key] = SETTINGS[key][1](v)
            if key in ("waterfall_order", "waterfall_free") and clean[key]:
                live = {r[0] for r in self.db.execute("SELECT id FROM notes WHERE archived_at IS NULL")}
                clean[key] = [i for i in clean[key] if i in live]
        cur = self.settings()
        changed = {k: v for k, v in clean.items() if cur[k] != v}
        if changed:
            self.db.execute("BEGIN")
            try:
                for k, v in changed.items():
                    self.db.execute("INSERT INTO settings(key, value) VALUES (?, ?)"
                                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                    (k, json.dumps(v)))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            self.log("set " + " ".join(f"{k}={json.dumps(v)}" for k, v in changed.items()))
        return self.settings()

    @_writes
    def dock(self, note_id, docked=True):
        """Put a note back in the waterfall column, or take it out (it then
        stays free on the desktop, where it is)."""
        nid = self.get(note_id)["id"]
        free = [i for i in self.settings()["waterfall_free"] if i != nid]
        return self.set_settings({"waterfall_free": free + ([] if docked else [nid])})

    @_writes
    def roll(self, note_id, rolled=True):
        """Collapse a note to its header line (the first line as its title)
        or open it again."""
        return self._update(note_id, rolled=1 if rolled else 0)

    @_writes
    def undo_archive(self):
        """Restore the most recently archived note (the desktop's Undo, and
        `stickies undo`). Each call goes one archive further back."""
        row = self.db.execute("SELECT id FROM notes WHERE archived_at IS NOT NULL"
                              " ORDER BY archived_at DESC, id DESC LIMIT 1").fetchone()
        if row is None:
            raise StickiesError("nothing to undo: no archived notes")
        return self.restore(row["id"])

    # -- tidy: arrange a workspace's notes in a grid -------------------------

    @_writes
    def tidy(self, workspace, monitor, width, height, gaps=(10, 10, 10, 10),
             reserved=(0, 0, 0, 0), primary=True):
        """Arrange the free (unpinned) notes showing on `workspace` of
        `monitor` in rows, reading order kept, `gaps` (top, right, bottom,
        left; Hyprland's gaps_out) around and between them, inside the
        screen minus `reserved` (left, top, right, bottom; the bar). Notes
        without a workspace show everywhere, so they count too; notes
        without a monitor belong to the primary one. The old places are
        kept for one tidy_undo. Returns {"moved": [notes], "undo": bool}."""
        if workspace is None or not monitor or not width or not height:
            raise StickiesError("tidy needs the workspace, monitor and screen size")
        if self.settings()["layout"] != "free":
            raise StickiesError("tidy is for the free layout; the waterfall column already lines "
                                "the notes up (`stickies layout free` to place them yourself)")
        gt, gr, gb, gl = (int(g) for g in gaps)
        rl, rt, rr, rb = (int(r) for r in reserved)
        mon = "monitor=?" + (" OR monitor IS NULL OR monitor=''" if primary else "")
        rows = self.db.execute(
            f"SELECT * FROM notes WHERE archived_at IS NULL AND pinned=0"
            f" AND (workspace=? OR workspace IS NULL) AND ({mon})"
            " ORDER BY y, x, id", (int(workspace), monitor)).fetchall()
        if not rows:
            return {"moved": [], "undo": False}
        left, top, right = rl + gl, rt + gt, int(width) - rr - gr
        x, y, row_h, places = left, top, 0, []
        for r in rows:
            h = TITLE_H if r["rolled"] else r["h"]
            if x > left and x + r["w"] > right:
                x, y, row_h = left, y + row_h + gt, 0
            places.append((r["id"], x, y))
            x += r["w"] + gl
            row_h = max(row_h, h)
        undo = [[r["id"], r["x"], r["y"], r["monitor"]] for r in rows]
        ts = now_iso()
        self.db.execute("BEGIN")
        try:
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('tidy_undo', ?)",
                            (json.dumps({"at": ts, "notes": undo}),))
            for nid, nx, ny in places:
                self.db.execute("UPDATE notes SET x=?, y=?, monitor=?, updated_at=? WHERE id=?",
                                (nx, ny, monitor, ts, nid))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.log(f"tidy {len(places)} notes on workspace {workspace} ({monitor})")
        return {"moved": [self.get(nid) for nid, _, _ in places], "undo": True}

    def can_undo_tidy(self):
        return self.db.execute("SELECT 1 FROM meta WHERE key='tidy_undo'").fetchone() is not None

    @_writes
    def tidy_undo(self):
        """Put the notes the last tidy moved back where they were (once)."""
        row = self.db.execute("SELECT value FROM meta WHERE key='tidy_undo'").fetchone()
        if row is None:
            raise StickiesError("nothing to undo: no tidy since the last undo")
        out, ts = [], now_iso()
        self.db.execute("BEGIN")
        try:
            for nid, x, y, monitor in json.loads(row["value"])["notes"]:
                cur = self.db.execute("UPDATE notes SET x=?, y=?, monitor=?, updated_at=?"
                                      " WHERE id=? AND archived_at IS NULL", (x, y, monitor, ts, nid))
                if cur.rowcount:
                    out.append(nid)
            self.db.execute("DELETE FROM meta WHERE key='tidy_undo'")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.log(f"tidy undone ({len(out)} notes)")
        return {"restored": [self.get(i) for i in out]}

    # -- reminders -----------------------------------------------------------

    def _sync_reminders(self, note_id, body):
        """Bring the note's reminder rows in line with its `@ <when>` parts:
        new ones are resolved now (already past: marked fired, so writing a
        past date never notifies), removed ones dropped; then remind_at."""
        nid = int(note_id)
        found = {}
        if "@" in body:
            for spec, due, _ in find_reminders(body):
                found.setdefault(spec, due)
        have = {r["spec"] for r in self.db.execute("SELECT spec FROM reminders WHERE note_id=?", (nid,))}
        for spec in have - set(found):
            self.db.execute("DELETE FROM reminders WHERE note_id=? AND spec=?", (nid, spec))
        now = now_iso()
        for spec in set(found) - have:
            due = found[spec]
            self.db.execute("INSERT INTO reminders(note_id, spec, due, fired_at) VALUES (?,?,?,?)",
                            (nid, spec, due, now if due <= now else None))
        self._refresh_remind_at(nid)

    def _refresh_remind_at(self, nid):
        nxt = self.db.execute("SELECT MIN(due) FROM reminders WHERE note_id=? AND fired_at IS NULL",
                              (nid,)).fetchone()[0]
        # Not through _update: a reminder is not an edit (updated_at stays).
        self.db.execute("UPDATE notes SET remind_at=? WHERE id=? AND remind_at IS NOT ?", (nxt, nid, nxt))

    def reminders(self, pending=True):
        """[{note_id, spec, due, fired_at, text}] soonest first: the pending
        ones of live notes, or (pending=False) every one."""
        where = "WHERE r.fired_at IS NULL AND n.archived_at IS NULL" if pending else ""
        rows = self.db.execute(
            f"SELECT r.note_id, r.spec, r.due, r.fired_at, n.body FROM reminders r"
            f" JOIN notes n ON n.id = r.note_id {where} ORDER BY r.due, r.note_id").fetchall()
        return [{"note_id": r["note_id"], "spec": r["spec"], "due": r["due"], "fired_at": r["fired_at"],
                 "text": reminder_text(r["body"], r["spec"])} for r in rows]

    def next_reminder_due(self):
        """The due time (ISO) of the earliest pending reminder on a live note, or None."""
        return self.db.execute(
            "SELECT MIN(r.due) FROM reminders r JOIN notes n ON n.id = r.note_id"
            " WHERE r.fired_at IS NULL AND n.archived_at IS NULL").fetchone()[0]

    @_writes
    def fire_reminders(self, notify, now=None):
        """Every pending reminder due by `now` (live notes only): marked
        fired first (a reminder fires once, even if notifying fails), then
        notify(title, text). Missed ones (serve wasn't running) fire on the
        first call after start. Returns what fired."""
        now = now or now_iso()
        rows = self.db.execute(
            "SELECT r.note_id, r.spec, r.due, n.body FROM reminders r JOIN notes n ON n.id = r.note_id"
            " WHERE r.fired_at IS NULL AND r.due <= ? AND n.archived_at IS NULL"
            " ORDER BY r.due", (now,)).fetchall()
        out = []
        late_before = (datetime.now(timezone.utc) - timedelta(minutes=2)) \
            .isoformat(timespec="milliseconds").replace("+00:00", "Z")
        for r in rows:
            self.db.execute("UPDATE reminders SET fired_at=? WHERE note_id=? AND spec=?",
                            (now_iso(), r["note_id"], r["spec"]))
            self._refresh_remind_at(r["note_id"])
            text = reminder_text(r["body"], r["spec"])
            title = "Sticky note reminder"
            if r["due"] < late_before:
                title += f" (missed, was due {_local_when(r['due'])})"
            try:
                notify(title, text)
                self.log(f"reminder #{r['note_id']} {r['spec']!r} sent")
            except Exception as e:  # logged, never raised: serve keeps going
                self.log(f"reminder #{r['note_id']} {r['spec']!r}: notification failed: {e}")
            out.append({"note_id": r["note_id"], "spec": r["spec"], "due": r["due"], "text": text})
        return out

    # -- change feed (serve) -----------------------------------------------

    def last_seq(self):
        return self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()[0]

    def changes_since(self, seq):
        """Coalesced events since `seq`: one per note, last kind wins; any
        number of settings writes become one {"event": "settings"}, first."""
        rows = self.db.execute(
            "SELECT seq, note_id, kind FROM changes WHERE seq > ? ORDER BY seq", (seq,)).fetchall()
        latest = {}
        events = []
        settings_seq = None
        for r in rows:
            if r["kind"] == "settings":
                settings_seq = r["seq"]
            else:
                latest[r["note_id"]] = (r["seq"], r["kind"])
        if settings_seq is not None:
            events.append({"event": "settings", "seq": settings_seq, "settings": self.settings()})
        for note_id, (s, kind) in sorted(latest.items(), key=lambda kv: kv[1][0]):
            ev = {"event": "changed", "seq": s, "kind": kind, "id": note_id, "note": None}
            if kind != "purge":
                row = self.db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
                if row is None:
                    ev["kind"] = "purge"
                else:
                    ev["note"] = self.to_dict(row)
            events.append(ev)
        new_seq = rows[-1]["seq"] if rows else seq
        return new_seq, events

    def prune_changes(self, keep=10000):
        self.db.execute("DELETE FROM changes WHERE seq <= (SELECT MAX(seq) FROM changes) - ?", (keep,))

    def data_version(self):
        return self.db.execute("PRAGMA data_version").fetchone()[0]


# -- hub card ----------------------------------------------------------------
# A glanceable card on the hub dashboard via `hub module set` (zero coupling:
# hub is only ever reached through its CLI). `stickies hub` publishes it on
# demand; serve republishes a moment after changes and CLI writes publish in
# the background, but only for the live notes (see hub_bin).

HUB_LAUNCH = "omarchy-shell -q stickies find"


# -- keybindings -> plugin ----------------------------------------------------
# A key runs `omarchy-shell stickies <method>` without -q (which exits 0 on
# every failure: shell down, plugin not enabled, method missing) and, only
# when that fails, `stickies shell <method>`: it tries once more and writes
# the reason to stickies.log, so a dead SUPER + ALT + N leaves a trace.

SHELL_METHODS = ("newNote", "paste", "toggle", "front", "show", "hide", "find", "chat", "search",
                 "tidy", "reload", "cycleLayout", "collapse", "list")


def call_shell(method, timeout=6, args=()):
    import subprocess
    cmd = [os.environ.get("STICKIES_OMARCHY_SHELL") or "omarchy-shell", "stickies", method, *args]
    # omarchy-shell refuses to run without OMARCHY_PATH, which a caller
    # outside the session's environment lacks (seen in stickies.log).
    env = dict(os.environ)
    if not env.get("OMARCHY_PATH") and os.path.isdir("/usr/share/omarchy"):
        env["OMARCHY_PATH"] = "/usr/share/omarchy"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        out, err, code = p.stdout.strip(), p.stderr.strip(), p.returncode
    except FileNotFoundError:
        out, err, code = "", f"{cmd[0]} not found", 127
    except subprocess.TimeoutExpired:
        out, err, code = "", f"no answer in {timeout} s", 124
    if code == 0:
        return {"method": method, "ok": True, "output": out or None}, out
    problem = err or out or f"exit {code}"
    if "Function not found" in problem:
        # The shell runs plugin code from before an update (see check_loaded_code).
        problem += f" (the shell still runs older Stickies code; restart it to load {plugin_version()}: " \
                   "omarchy-restart-shell)"
    Store.log_line(f"shell {method} failed: {problem}")
    raise StickiesError(f"omarchy-shell stickies {method}: {problem} (logged to stickies.log)")


# -- integration: keys, the `stickies` command, uninstall --------------------
# Installed with `omarchy plugin add`, the plugin is only a folder. On first
# start the desktop offers (a toast with a button; never silently) to add the
# keys and a `stickies` symlink in ~/.local/bin. The keys are runtime binds
# (`hyprctl eval 'hl.bind(...)'`): nothing is written to disk, a config
# reload drops them and the plugin adds them again. The clone route
# (install.sh) uses a drop-in file instead; when it is there, runtime binds
# are never added. `stickies uninstall` undoes all of it.

PLUGIN_ID = "eastbluewizard.stickies"
DROPIN_MARKER = "eastbluewizard.stickies keybindings"
# (keys, description, plugin method). The descriptions identify our binds in
# `hyprctl binds`; hypr/stickies.lua uses the same ones.
KEYS = (("SUPER + ALT + N", "New sticky note", "newNote"),
        ("SUPER + ALT + SHIFT + N", "Stickies above windows (toggle)", "front"),
        ("SUPER + ALT + V", "Sticky note from the clipboard", "paste"),
        ("SUPER + ALT + J", "Find a sticky note", "find"),
        ("SUPER + ALT + A", "Ask your sticky notes", "chat"),
        ("SUPER + ALT + L", "Stickies layout: free / waterfall right / left", "cycleLayout"),
        ("SUPER + ALT + W", "Collapse the stickies column (toggle)", "collapse"),
        ("SUPER + ALT + O", "All sticky notes", "list"))
_MODS = {"SHIFT": 1, "CAPS": 2, "CTRL": 4, "CONTROL": 4, "ALT": 8, "MOD2": 16, "MOD3": 32,
         "SUPER": 64, "WIN": 64, "LOGO": 64, "MOD4": 64, "MOD5": 128}


def here_dir():
    return os.path.dirname(os.path.realpath(__file__))


def launcher_path():
    """The command to run: the byte-code-caching launcher next to this file
    (stickies.py itself if the launcher is missing)."""
    p = os.path.join(here_dir(), "stickies")
    return p if os.path.isfile(p) else os.path.join(here_dir(), "stickies.py")


def bin_link_path():
    return os.path.join(os.environ.get("STICKIES_BIN") or os.path.expanduser("~/.local/bin"), "stickies")


def dropin_path():
    d = os.environ.get("STICKIES_HYPR_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "omarchy", "toggles", "hypr")
    return os.path.join(d, "stickies.lua")


def plugin_dir_path():
    return os.path.join(os.environ.get("STICKIES_PLUGINS") or os.path.expanduser("~/.config/omarchy/plugins"),
                        PLUGIN_ID)


def integration_file():
    return os.path.join(state_dir(), "integration.json")


def read_integration():
    try:
        with open(integration_file()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_integration(d):
    tmp = integration_file() + ".tmp"
    with open_private(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, integration_file())


def dropin_installed():
    try:
        with open(dropin_path()) as f:
            return DROPIN_MARKER in f.read()
    except OSError:
        return False


def _ours(link, root=None, extra=()):
    """A `stickies` symlink this install may manage: it points at a
    stickies / stickies.py (here, in root or an extra dir, or a checkout
    that has since moved)."""
    if not os.path.islink(link):
        return False
    target = os.path.join(os.path.dirname(link), os.readlink(link))
    if os.path.basename(target) not in ("stickies", "stickies.py"):
        return False
    real = os.path.realpath(target)
    return os.path.dirname(real) in (here_dir(), root or here_dir(), *extra) or not os.path.exists(real)


def link_status(root=None, extra=()):
    """~/.local/bin/stickies: missing, ours (-> root's launcher, default
    this copy's), stale (a stickies link to another copy of ours) or
    taken."""
    root = root or here_dir()
    link = bin_link_path()
    if not os.path.lexists(link):
        return {"path": link, "state": "missing"}
    if _ours(link, root, extra) and os.path.dirname(os.path.realpath(link)) == root:
        return {"path": link, "state": "ours"}
    return {"path": link, "state": "stale" if _ours(link, root, extra) else "taken"}


def make_link(root=None, extra=()):
    """Point ~/.local/bin/stickies at root's launcher, unless it is someone
    else's. extra: other dirs whose launcher counts as ours (the checkout a
    symlink install was migrated from)."""
    root = root or here_dir()
    st = link_status(root, extra)
    if st["state"] in ("missing", "stale"):
        os.makedirs(os.path.dirname(st["path"]), exist_ok=True)
        if st["state"] == "stale":
            os.unlink(st["path"])
        launcher = os.path.join(root, "stickies")
        os.symlink(launcher if os.path.isfile(launcher) else os.path.join(root, "stickies.py"), st["path"])
        st["state"] = "ours"
    return st


def hyprctl(*args, timeout=5):
    """(returncode, stdout) of hyprctl, or None when there is no Hyprland to
    talk to. $STICKIES_HYPRCTL names a stand-in (the tests' fake); without
    it, a temporary HOME never reaches the live compositor."""
    import subprocess
    exe = os.environ.get("STICKIES_HYPRCTL")
    if not exe:
        if not live_session():
            return None
        exe = "hyprctl"
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.returncode, p.stdout.strip()


def _combo(keys):
    *mods, key = [k.strip().upper() for k in keys.split("+")]
    return sum(_MODS.get(m, 0) for m in mods), key


def _lua_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def bind_command(method):
    """What a key runs: the plugin over IPC; only if that fails, this
    launcher's `shell` (retries once, logs the reason). By path, not PATH."""
    return (f"omarchy-shell stickies {method} >/dev/null 2>&1 || "
            f"{shlex.quote(launcher_path())} shell {method}")


def unbind_lua(keys=None):
    return "; ".join(f"hl.unbind({_lua_str(k)})" for k in (keys or [k for k, _, _ in KEYS]))


def current_binds():
    r = hyprctl("binds", "-j")
    if r is None or r[0] != 0:
        return None
    try:
        return json.loads(r[1])
    except ValueError:
        return None


def keys_on():
    """Add the runtime binds that aren't there yet. A key someone else
    already uses is left to them and reported as taken."""
    if dropin_installed():
        return {"keys": "dropin", "bound": [], "already": [], "taken": []}
    binds = current_binds()
    if binds is None:
        return {"keys": "unavailable", "bound": [], "already": [], "taken": []}
    by_combo = {}
    for b in binds:
        by_combo.setdefault((b.get("modmask", 0), str(b.get("key", "")).upper()), []).append(
            b.get("description") or b.get("arg") or "")
    out = {"keys": "runtime", "bound": [], "already": [], "taken": []}
    lua = []
    for keys, desc, method in KEYS:
        users = by_combo.get(_combo(keys), [])
        if desc in users:
            out["already"].append(keys)
        elif users:
            out["taken"].append({"keys": keys, "by": users[0]})
        else:
            lua.append(f"hl.bind({_lua_str(keys)}, hl.dsp.exec_cmd({_lua_str(bind_command(method))}), "
                       f"{{ description = {_lua_str(desc)} }})")
            out["bound"].append(keys)
    if lua:
        r = hyprctl("eval", "; ".join(lua))
        if r is None or r[0] != 0 or r[1].startswith("Error"):
            raise StickiesError(f"hyprctl eval failed: {r[1] if r else 'no hyprctl'}")
    out["unbind_lua"] = unbind_lua(out["bound"] + out["already"])
    return out


def keys_off():
    """Remove our runtime binds (matched by description, so a key the user
    bound to something else is never touched). The drop-in is uninstall's."""
    binds = current_binds()
    if binds is None:
        return {"removed": []}
    ours = {d for _, d, _ in KEYS}
    combos = {(b.get("modmask", 0), str(b.get("key", "")).upper())
              for b in binds if b.get("description") in ours}
    keys = [k for k, _, _ in KEYS if _combo(k) in combos]
    if keys and not dropin_installed():
        hyprctl("eval", unbind_lua(keys))
    else:
        keys = []
    return {"removed": keys}


def integration_status():
    d = read_integration()
    dropin = dropin_installed()
    return {"asked": bool(d.get("asked")) or dropin, "keys": "dropin" if dropin else
            ("runtime" if d.get("keys") else "off"), "link": link_status(),
            "plugin_dir": here_dir(), "command": launcher_path(),
            "code_changed": d.get("code_hash") != plugin_code_hash()}


def integrate(yes, restart=False, install=False):
    """The answer to the desktop's offer (or `stickies integrate --yes/--no`).
    Asked once: either answer is remembered. restart (the CLI, not the
    desktop that is asking): first replace a plugin symlink from an older
    install with a checkout (migrate_symlink), and restart the shell if the
    plugin code changed. install (install.sh, the git route): the keys as
    the drop-in file instead of runtime binds, and in the live session
    enable the plugin and publish the hub card too."""
    reach = restart and plugins_in_reach()
    migrated = migrate_symlink() if reach else None
    root = installed_root() if reach else here_dir()
    d = read_integration()
    d.update(asked=True, keys=bool(yes) and not install, link=bool(yes))
    write_integration(d)
    out = integration_status()
    if migrated:
        out["migrated"] = migrated
    if yes:
        # A link into the checkout the plugin was cloned from (a migrated or
        # `omarchy plugin add <path>` install) is ours to re-point.
        out["link"] = make_link(root, extra=(migrated["from"],) if migrated else local_origin(root))
        if install:
            out["result"] = install_dropin(root)
        else:
            out["result"] = keys_on()
    if install and live_session() and not os.environ.get("STICKIES_PLUGINS"):
        out["enabled"] = enable_plugin()
        out["hub"] = publish_hub_cli(root)
    out["shell"] = refresh_shell(restart, root)
    return out


def plugins_in_reach():
    """May this process change the plugins folder? Not while $STICKIES_STATE
    points somewhere (tests, benches) unless $STICKIES_PLUGINS names the
    folder too, like hub_bin and notify_bin: a test once replaced the real
    plugin symlink because only the state was redirected."""
    return bool(os.environ.get("STICKIES_PLUGINS")) or not os.environ.get("STICKIES_STATE")


def installed_root():
    """The installed plugin folder (a real one with the CLI in it), else
    this copy: what the CLI's integrate sets up the command and keys for."""
    p = plugin_dir_path()
    if not os.path.islink(p) and os.path.isfile(os.path.join(p, "stickies.py")):
        return os.path.realpath(p)
    return here_dir()


def migrate_symlink():
    """Installs before 1.3.0 symlinked a checkout into the plugins folder.
    The shell's watcher (inotifywait -r) sees nothing through a symlink and
    omarchy-plugin-validate refuses one, so replace it with a git clone of
    that checkout (what `omarchy plugin add <path>` makes): updates are then
    `omarchy plugin update`. The notes are not touched. None if there is no
    symlink to replace."""
    import subprocess
    p = plugin_dir_path()
    if not os.path.islink(p):
        return None
    src = os.path.realpath(p)
    if not os.path.isdir(os.path.join(src, ".git")):
        raise StickiesError(f"{p} is a symlink to {src}, which is not a git checkout; remove the "
                            f"link and install with: omarchy plugin add <url or path> --enable")
    tmp = os.path.join(os.path.dirname(p), f".{PLUGIN_ID}.migrate.{os.getpid()}")  # hidden: not a plugin
    try:
        subprocess.run(["git", "clone", "--quiet", "--", src, tmp], check=True, capture_output=True, text=True)
        validate = shutil_which("omarchy-plugin-validate")
        if validate:
            v = subprocess.run([validate, tmp], capture_output=True, text=True)
            if v.returncode != 0:
                raise StickiesError(f"the clone of {src} is not a valid plugin: {v.stderr.strip()}")
        dirty = subprocess.run(["git", "-C", src, "status", "--porcelain"], capture_output=True,
                               text=True).stdout.strip()
        head = subprocess.run(["git", "-C", tmp, "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True).stdout.strip()
        os.unlink(p)
        os.rename(tmp, p)
    except subprocess.CalledProcessError as e:
        raise StickiesError(f"git clone {src} failed: {e.stderr.strip()}")
    finally:
        if os.path.isdir(tmp):
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    Store.log_line(f"replaced the plugin symlink {p} -> {src} with a clone of it ({head})")
    out = {"path": p, "from": src, "commit": head, "uncommitted": bool(dirty)}
    if os.path.isdir(os.path.join(src, ".venv")) and not venv_python_in(os.path.join(p, ".venv")):
        out["venv"] = os.path.join(src, ".venv")  # the old checkout's venv; the clone doesn't see it
    return out


def local_origin(root):
    """(the checkout root was cloned from,) when its git origin is a local
    folder (route c: `omarchy plugin add ~/path/to/checkout`), else ()."""
    import subprocess
    if not os.path.isdir(os.path.join(root, ".git")):
        return ()
    url = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"], capture_output=True,
                         text=True).stdout.strip()
    return (os.path.realpath(url),) if url.startswith("/") and os.path.isdir(url) else ()


def shutil_which(name):
    import shutil
    return shutil.which(name)


def venv_python_in(v):
    return os.path.exists(os.path.join(v, "bin", "python"))


def install_dropin(root):
    """The keys as Omarchy's drop-in file (the git route): our runtime binds,
    if any, make way for it; Hyprland reloads it in the live session."""
    removed = keys_off()["removed"]
    drop = dropin_path()
    if os.path.exists(drop) and not dropin_installed():
        raise StickiesError(f"{drop} exists and is not ours; leaving it alone")
    os.makedirs(os.path.dirname(drop), exist_ok=True)
    # A copy, not a symlink: Omarchy's loader only picks up regular files.
    with open(os.path.join(root, "hypr", "stickies.lua")) as f:
        text = f.read()
    with open(drop + ".tmp", "w") as f:
        f.write(text)
    os.replace(drop + ".tmp", drop)
    reloaded = hyprctl("reload") is not None
    return {"keys": "dropin", "path": drop, "unbound": removed, "reloaded": reloaded,
            "bound": [k for k, _, _ in KEYS], "already": [], "taken": []}


def enable_plugin():
    """omarchy-plugin-enable, when the shell is running (live session only)."""
    import subprocess
    if not shutil_which("omarchy-plugin-enable") or subprocess.run(
            ["pgrep", "-f", "quickshell.*omarchy/shell"], capture_output=True).returncode != 0:
        return False
    subprocess.run(["omarchy-shell", "shell", "rescanPlugins"], capture_output=True)
    return subprocess.run(["omarchy-plugin-enable", PLUGIN_ID], capture_output=True).returncode == 0


def publish_hub_cli(root):
    """The hub card, through the installed copy's own CLI (live session only)."""
    import subprocess
    if not hub_bin():
        return False
    return subprocess.run([os.path.join(root, "stickies"), "hub"], capture_output=True).returncode == 0


# -- the running shell's copy of the plugin ------------------------------------
# omarchy-shell keeps the QML it loaded. Its watcher (inotifywait -r on the
# plugins folder) does see an update and reloads the plugins, but the reload
# calls Qt.clearComponentCache(), which Quickshell (0.3.1) doesn't have, so
# the old components come back: new IPC methods fail with "Function not
# found" until the shell restarts (measured 2026-10-06; disable/enable is
# the same reload). Through a symlinked folder the watcher sees nothing.
# The reload does restart `serve`, from the new stickies.py, so serve can
# tell: check_loaded_code() notices new QML/JS on disk under the same shell
# and says once that a restart loads it. install.sh and `stickies integrate`
# (explicit runs) compare the hash with the last install's and restart the
# shell themselves.

def shell_identity():
    """'pid:start' of the omarchy-shell that started this process (serve's
    parent), else None. $STICKIES_SHELL_ID stands in for it in tests."""
    if os.environ.get("STICKIES_SHELL_ID"):
        return os.environ["STICKIES_SHELL_ID"]
    ppid = os.getppid()
    try:
        with open(f"/proc/{ppid}/cmdline", "rb") as f:
            cmd = f.read().split(b"\0")
        with open(f"/proc/{ppid}/stat") as f:
            start = f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None
    if not cmd or os.path.basename(cmd[0]) != b"quickshell" or b"/omarchy/shell" not in b" ".join(cmd):
        return None
    return f"{ppid}:{start}"


def check_loaded_code():
    """serve, at start: the first serve under a shell records the plugin's
    QML/JS hash (what that shell loaded). A later serve under the same shell
    with other code on disk means an update the shell did not load: it
    sends one notification per new version. Returns {"stale", "shell"} or
    None outside a shell."""
    sid = shell_identity()
    if not sid:
        return None
    d = read_integration()
    h = plugin_code_hash()
    loaded = d.get("loaded") if isinstance(d.get("loaded"), dict) else {}
    if loaded.get("shell") != sid:
        d["loaded"] = {"shell": sid, "code_hash": h}
        write_integration(d)
        return {"stale": False, "shell": sid}
    if loaded.get("code_hash") == h:
        return {"stale": False, "shell": sid}
    if loaded.get("notified") != h:
        loaded["notified"] = h
        write_integration(d)
        Store.log_line(f"plugin updated to {plugin_version()}; the shell still runs the code it loaded "
                       f"({loaded.get('code_hash')}, now {h}): omarchy-restart-shell loads it")
        try:
            send_notification("Stickies updated",
                              f"Restart the shell to load {plugin_version()}: omarchy-restart-shell")
        except Exception as e:  # a notification must never take serve down
            Store.log_line(f"update notification failed: {e}")
    return {"stale": True, "shell": sid}


def plugin_version(d=None):
    """manifest.json's version (the one place it is written down)."""
    try:
        with open(os.path.join(d or here_dir(), "manifest.json")) as f:
            return str(json.load(f).get("version") or "unknown")
    except (OSError, ValueError):
        return "unknown"

def plugin_code_hash(d=None):
    import hashlib
    d = d or here_dir()
    h = hashlib.sha256()
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if name.endswith((".qml", ".js")) and os.path.isfile(p):
            with open(p, "rb") as f:
                h.update(name.encode() + b"\0" + f.read() + b"\0")
    return h.hexdigest()[:16]


def live_session():
    """This user's own Hyprland session (not a temp HOME, not a test:
    $STICKIES_NO_LIVE or a $STICKIES_PLUGINS override)."""
    import pwd
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and not os.environ.get("STICKIES_NO_LIVE")
                and not os.environ.get("STICKIES_PLUGINS")
                and os.path.expanduser("~") == pwd.getpwuid(os.getuid()).pw_dir)


def restart_shell_command():
    """What restarts the shell: $STICKIES_RESTART_SHELL (tests), else
    omarchy-restart-shell, only in the live session with the shell running."""
    import shutil
    import subprocess
    if os.environ.get("STICKIES_RESTART_SHELL"):
        return shlex.split(os.environ["STICKIES_RESTART_SHELL"])
    if not live_session() or not shutil.which("omarchy-restart-shell"):
        return None
    if subprocess.run(["pgrep", "-f", "quickshell.*omarchy/shell"], capture_output=True).returncode != 0:
        return None
    return ["omarchy-restart-shell"]


def refresh_shell(restart=True, root=None):
    """{code_hash, changed, restarted}. changed: the plugin's QML/JS differ
    from the last install's (or none was recorded). With restart, a running
    shell is restarted so it loads them. The hash is stored unless a needed
    restart failed (so the next run tries again)."""
    import subprocess
    d = read_integration()
    h = plugin_code_hash(root)
    out = {"code_hash": h, "changed": d.get("code_hash") != h, "restarted": False}
    if out["changed"] and restart:
        cmd = restart_shell_command()
        if cmd:
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                out["restarted"] = p.returncode == 0
                if not out["restarted"]:
                    out["error"] = (p.stderr or p.stdout).strip()[-300:] or f"exit {p.returncode}"
            except (OSError, subprocess.TimeoutExpired) as e:
                out["error"] = str(e)
            Store.log_line("shell restarted: the plugin code changed" if out["restarted"]
                           else f"shell restart failed: {out['error']}")
    if "error" not in out and d.get("code_hash") != h:
        d["code_hash"] = h
        write_integration(d)
    return out


def uninstall(purge=False):
    """Undo everything stickies added outside its own folder, each only if
    it is ours: runtime binds, the keybinding drop-in, the ~/.local/bin
    link, a plugin symlink from an install before 1.3.0, the byte-code
    cache and the first-run answer. The plugin folder itself is a checkout
    that `omarchy plugin remove` deletes (returned in "next"). --purge also
    deletes the notes, the model and the venv. Returns what was done, the
    notes kept, what is left to run, and what was left alone and why."""
    import shutil
    home = os.path.realpath(os.path.expanduser("~"))

    def safe(p):
        return p and os.path.realpath(p) not in ("/", home)

    done, nxt = [], []
    if read_integration().get("keys"):
        done += [f"unbound {k}" for k in keys_off()["removed"]]
    drop = dropin_path()
    if dropin_installed():
        os.unlink(drop)
        done.append(f"removed {drop}")
        if hyprctl("reload") is not None:
            done.append("reloaded Hyprland")
    link = bin_link_path()
    if _ours(link, installed_root()):
        os.unlink(link)
        done.append(f"removed {link}")
    plugin = plugin_dir_path() if plugins_in_reach() else ""
    if not plugin:
        pass
    elif os.path.islink(plugin) and os.path.realpath(plugin) in (here_dir(), os.path.join(here_dir(), "shell")):
        if hyprctl("version") is not None:  # the live session: unload it first
            import subprocess
            subprocess.run(["omarchy-plugin-disable", PLUGIN_ID], capture_output=True)
        os.unlink(plugin)
        done.append(f"removed {plugin}")
    elif os.path.isdir(plugin) and not os.path.islink(plugin):
        # Every route installs a git checkout there; omarchy's command disables
        # and deletes it (the clone is the plugin, not where notes live).
        nxt.append(f"omarchy plugin remove {PLUGIN_ID}")
    if os.path.exists(integration_file()):
        os.unlink(integration_file())
    pyc = os.path.join(cache_dir(), "pycache")
    if os.path.isdir(pyc) and safe(pyc):
        shutil.rmtree(pyc, ignore_errors=True)
    kept = None
    if purge:
        for p in (state_dir(), cache_dir(), venv_dir()):
            if safe(p) and os.path.exists(p):
                shutil.rmtree(p, ignore_errors=True)
                done.append(f"deleted {p}")
    else:
        for p in (cache_dir(), state_dir()):  # nothing of ours left in them
            if safe(p) and os.path.isdir(p) and not os.listdir(p):
                os.rmdir(p)
        if os.path.isdir(state_dir()):
            kept = state_dir()
    # Stickies stopped onnxruntime writing this (ORT_ENV), but versions up to
    # 1.2.0 did not, and any other program using onnxruntime writes the same
    # folder: nothing in it says whose it is, so it is shown, never deleted.
    left = []
    shared = onnxruntime_shared_dir()
    if purge and os.path.isdir(shared):
        left.append({"path": shared, "why": (
            "onnxruntime's telemetry id and event queue. Stickies 1.2.0 and older let onnxruntime "
            "write it; other programs that use onnxruntime write the same folder, so it is left "
            "alone. Delete it yourself if nothing else here uses onnxruntime.")})
    return {"done": done, "kept": kept, "next": nxt, "left": left}


def hub_bin(explicit=False):
    """The hub executable to call, or None. $STICKIES_HUB overrides it (empty
    disables). Automatic publishing is off while $STICKIES_STATE points
    somewhere (tests, benches) so a throwaway DB never overwrites the real
    card; an explicit `stickies hub` always publishes."""
    if "STICKIES_HUB" in os.environ:
        return os.environ["STICKIES_HUB"] or None
    if explicit or not os.environ.get("STICKIES_STATE"):
        return "hub"
    return None


def _local_when(iso):
    """'17:42' for today, else the ISO date, in local time."""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except (AttributeError, ValueError):
        return None
    return t.strftime("%H:%M") if t.date() == datetime.now().astimezone().date() else t.date().isoformat()


def _local_stamp(iso):
    """'2026-10-07 09:00' in local time."""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return iso


def hub_summary(store):
    count, pinned = store.db.execute(
        "SELECT COUNT(*), COALESCE(SUM(pinned), 0) FROM notes WHERE archived_at IS NULL").fetchone()
    last = store.db.execute(
        "SELECT * FROM notes WHERE archived_at IS NULL ORDER BY updated_at DESC, id DESC LIMIT 1").fetchone()
    return {"count": count, "pinned": pinned,
            "last_edited": last["updated_at"] if last else None,
            "last_edited_id": last["id"] if last else None,
            "last_edited_title": _first_line(last["body"], 48) if last else None}


def hub_args(s):
    when = _local_when(s["last_edited"]) if s["last_edited"] else None
    args = ["module", "set", "--name", "stickies", "--title", "Stickies", "--icon", "🗒",
            "--launch", HUB_LAUNCH,
            "--line", f"{s['count']} note{'' if s['count'] == 1 else 's'}, {s['pinned']} pinned"]
    if when:
        args += ["--line", f"last edited {when}: {s['last_edited_title'] or '(empty)'}"]
    args += ["--stat", str(s["count"]), "notes", "--stat", str(s["pinned"]), "pinned",
             "--stat", when or "-", "last edited"]
    return args


def publish_hub(store, explicit=False, wait=True):
    """Publish the card. wait=False fires and forgets (a Popen, or None when
    publishing is off or hub is missing); wait=True returns the summary plus
    `published`."""
    import subprocess
    s = hub_summary(store)
    exe = hub_bin(explicit)
    if exe is None:
        return dict(s, published=False) if wait else None
    cmd = [exe, *hub_args(s)]
    try:
        if not wait:
            return subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        if wait:
            raise StickiesError(f"hub module set failed: {e}")
        return None
    if p.returncode != 0:
        raise StickiesError(f"hub module set failed: {(p.stderr or p.stdout).strip()}")
    return dict(s, published=True)


# -- chat --------------------------------------------------------------------
# Ask your notes. The question picks the top-k notes by hybrid search; the
# person sees (and can untick) exactly which go out; only the question, those
# notes and this session's earlier turns are sent to Omarchy's default agent
# (`claude -p`, no tools, no MCP, no settings, no session saved). The answer
# cites notes as [#id]. The model can only *propose* actions, in one fenced
# JSON block at the end; they are validated here and applied only by
# `apply` (the person's Apply click, or an agent's explicit `stickies apply`).
# Nothing is ever deleted from chat.

CHAT_K = 6
CHAT_TIMEOUT = 180
CHAT_MAX_NOTE_CHARS = 4000
CHAT_MAX_HISTORY = 6
CHAT_MAX_PROPOSALS = 5
ACTIONS_FENCE = "```stickies-actions"
_CITE = re.compile(r"\[#(\d+)\]")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# action -> {field: (type, required)}; anything else is rejected.
ACTIONS = {
    "new_note": {"body": (str, True), "color": (str, False), "tags": (list, False)},
    "append_note": {"id": (int, True), "text": (str, True)},
    "hub_todo": {"title": (str, True), "due": (str, False)},
}

CHAT_SYSTEM = f"""You answer questions about the user's own sticky notes.
The notes you may use are given between <note> tags; nothing else about
the user is available to you. Answer briefly, in the language of the
question. Cite every note you rely on as [#id] (e.g. [#12]), right after
the claim. If the notes don't contain the answer, say so plainly; don't
invent facts.

You cannot change anything yourself. If (and only if) it clearly helps,
end your answer with ONE fenced block proposing at most {CHAT_MAX_PROPOSALS}
actions, which the user will confirm or dismiss:

{ACTIONS_FENCE}
[{{"action": "new_note", "body": "text", "color": "yellow", "tags": ["work"]}},
 {{"action": "append_note", "id": 12, "text": "text to add"}},
 {{"action": "hub_todo", "title": "short todo", "due": "YYYY-MM-DD"}}]
```

Only these three actions exist ("color", "tags" and "due" are optional;
colours: {", ".join(COLORS)}; tags: lower-case a-z, 0-9 and -, best the
ones the notes already use). append_note only targets a note you were given.
There is no delete, archive or replace. Write nothing after the block."""


def agent_command():
    """(argv, kind). $STICKIES_AGENT (a command line; prompt on stdin, plain
    text answer on stdout) overrides; otherwise `omarchy-default-agent`,
    which must name claude. kind is "claude" (stream-json) or "text"."""
    import subprocess
    custom = os.environ.get("STICKIES_AGENT")
    if custom:
        return shlex.split(custom), "text"
    try:
        agent = subprocess.run(["omarchy-default-agent"], capture_output=True, text=True,
                               timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        agent = ""
    agent = agent or "claude"
    if agent != "claude":
        raise StickiesError(f"chat runs Omarchy's default agent non-interactively and only knows "
                            f"how for claude (default agent is {agent!r}); set STICKIES_AGENT to a "
                            f"command that reads a prompt on stdin and prints the answer")
    return (["claude", "-p", "--output-format", "stream-json", "--verbose",
             "--include-partial-messages", "--tools", "", "--no-session-persistence",
             "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", "",
             "--system-prompt", CHAT_SYSTEM], "claude")


def chat_info():
    """Who chat would talk to, for the UI: {"agent": name, "error": null or why not}."""
    try:
        argv, kind = agent_command()
        return {"agent": os.path.basename(argv[0]) if kind == "text" else "claude", "error": None}
    except StickiesError as e:
        return {"agent": None, "error": str(e)}


def chat_notes(store, question, k=CHAT_K, mode="hybrid"):
    """The top-k live, non-empty notes for a question: the hybrid search
    (all words, plus meaning when the model is there), topped up by notes
    that match any of the question's words, since a question rarely has
    all its words in one note."""
    k = max(int(k), 1)
    hits = store.search(question, limit=k * 2, mode=mode)
    seen = {h["id"] for h in hits}
    for h in store.search_fts(question, limit=k * 2, any_word=True):
        if h["id"] not in seen:
            h.update(score=0.0, match="fts")
            hits.append(h)
            seen.add(h["id"])
    return [h for h in hits if h["body"].strip()][:k]


def notes_by_id(store, ids):
    """Live notes for these ids, in the given order; unknown/archived ids
    are an error (the person picked them, so they must all go out)."""
    out, seen = [], set()
    for i in ids:
        i = int(i)
        if i in seen:
            continue
        seen.add(i)
        n = store.get(i)
        if n["archived_at"]:
            raise StickiesError(f"note #{i} is archived")
        out.append(n)
    return out


def _note_block(n):
    body = n["body"]
    if len(body) > CHAT_MAX_NOTE_CHARS:
        body = body[:CHAT_MAX_NOTE_CHARS] + " …(cut)"
    body = body.replace("</note>", "<\\/note>")
    tags = f' tags="{" ".join(n["tags"])}"' if n.get("tags") else ""
    return f'<note id="{n["id"]}" color="{n["color"]}"{tags} updated="{n["updated_at"][:10]}">\n{body}\n</note>'


def build_prompt(question, notes, history=None):
    """The user message: earlier turns of this chat, the notes, the question.
    Nothing else about the person goes in."""
    parts = []
    for t in (history or [])[-CHAT_MAX_HISTORY:]:
        q, a = str(t.get("question") or "").strip(), visible_answer(str(t.get("answer") or "")).strip()
        if q:
            parts.append(f"Earlier question: {q}\nYour earlier answer: {a[:2000]}")
    if notes:
        parts.append("Notes:\n" + "\n\n".join(_note_block(n) for n in notes))
    else:
        parts.append("Notes: (none matched this question)")
    parts.append(f"Question: {question.strip()}")
    return "\n\n".join(parts)


def visible_answer(text, partial=False):
    """The answer without the proposals block. partial (mid-stream): a
    fence that has only partly arrived is held back too, and nothing
    already shown is taken back (each result extends the previous one)."""
    i = text.find(ACTIONS_FENCE)
    if i >= 0:
        return text[:i] if partial else text[:i].rstrip()
    if partial:
        for n in range(len(ACTIONS_FENCE) - 1, 0, -1):
            if text.endswith(ACTIONS_FENCE[:n]):
                return text[:-n]
    return text


def parse_citations(text, sent_ids):
    """(cited ids in order of first mention, ids cited but never sent)."""
    sent = {int(i) for i in sent_ids}
    cited, unknown = [], []
    for m in _CITE.finditer(visible_answer(text)):
        i = int(m.group(1))
        bucket = cited if i in sent else unknown
        if i not in bucket:
            bucket.append(i)
    return cited, unknown


def validate_proposal(p, allowed_ids=None):
    """A clean copy of one proposed action plus a `summary`, or raises
    StickiesError. allowed_ids: notes append_note may target (the ones that
    were sent); None means any (checked against the DB when applied)."""
    if not isinstance(p, dict):
        raise StickiesError("proposal must be an object")
    action = p.get("action")
    if action not in ACTIONS:
        raise StickiesError(f"unknown action {action!r} (only {', '.join(ACTIONS)})")
    spec = ACTIONS[action]
    extra = set(p) - set(spec) - {"action", "summary"}
    if extra:
        raise StickiesError(f"{action}: unexpected field(s) {', '.join(sorted(extra))}")
    out = {"action": action}
    for field, (typ, required) in spec.items():
        v = p.get(field)
        if v is None:
            if required:
                raise StickiesError(f"{action}: missing {field}")
            continue
        if typ is int and isinstance(v, str) and v.lstrip("#").isdigit():
            v = int(v.lstrip("#"))
        if not isinstance(v, typ) or isinstance(v, bool):
            raise StickiesError(f"{action}: {field} must be {typ.__name__}")
        if typ is str:
            v = v.strip()
            if not v and required:
                raise StickiesError(f"{action}: {field} is empty")
        out[field] = v
    if action == "new_note":
        if len(out["body"]) > 20000:
            raise StickiesError("new_note: body too long")
        if "color" in out:
            out["color"] = normalize_color(out["color"])
        if "tags" in out:
            if not all(isinstance(t, str) for t in out["tags"]):
                raise StickiesError("new_note: tags must be strings")
            out["tags"] = Store._check_tags(out["tags"])
            if not out["tags"]:
                del out["tags"]
        out["summary"] = (f"New {out.get('color', DEFAULT_COLOR)} note: {_first_line(out['body'], 60)}"
                          + "".join(f" #{t}" for t in out.get("tags", [])))
    elif action == "append_note":
        if allowed_ids is not None and out["id"] not in {int(i) for i in allowed_ids}:
            raise StickiesError(f"append_note: note #{out['id']} was not one of the notes sent")
        if len(out["text"]) > 20000:
            raise StickiesError("append_note: text too long")
        out["summary"] = f"Append to #{out['id']}: {_first_line(out['text'], 60)}"
    else:
        if len(out["title"]) > 200 or "\n" in out["title"]:
            raise StickiesError("hub_todo: title must be one line, at most 200 characters")
        if "due" in out:
            if not _ISO_DATE.match(out["due"]):
                raise StickiesError("hub_todo: due must be YYYY-MM-DD")
            try:
                datetime.strptime(out["due"], "%Y-%m-%d")
            except ValueError:
                raise StickiesError(f"hub_todo: {out['due']} is not a date")
        out["summary"] = f"Hub todo: {out['title']}" + (f" (due {out['due']})" if "due" in out else "")
    return out


def parse_proposals(text, sent_ids):
    """(valid proposals, rejected [{proposal, error}]) from the answer's
    fenced block. A block that isn't a JSON list is one rejection."""
    i = text.find(ACTIONS_FENCE)
    if i < 0:
        return [], []
    raw = text[i + len(ACTIONS_FENCE):]
    j = raw.find("```")
    raw = (raw[:j] if j >= 0 else raw).strip()
    try:
        items = json.loads(raw)
    except ValueError as e:
        return [], [{"proposal": raw[:2000], "error": f"not valid JSON: {e}"}]
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return [], [{"proposal": items, "error": "expected a list of actions"}]
    ok, bad = [], []
    for n, p in enumerate(items):
        if n >= CHAT_MAX_PROPOSALS:
            bad.append({"proposal": p, "error": f"more than {CHAT_MAX_PROPOSALS} proposals"})
            continue
        try:
            ok.append(validate_proposal(p, sent_ids))
        except StickiesError as e:
            bad.append({"proposal": p, "error": str(e)})
    return ok, bad


def _claude_events(line):
    """One stream-json line from `claude -p` -> ("delta", text) /
    ("result", text) / ("error", message) / None."""
    try:
        d = json.loads(line)
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    if d.get("type") == "stream_event":
        ev = d.get("event") or {}
        delta = ev.get("delta") or {}
        if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            return ("delta", delta.get("text") or "")
    elif d.get("type") == "result":
        if d.get("is_error") or d.get("subtype") not in (None, "success"):
            return ("error", str(d.get("result") or d.get("subtype") or "agent failed"))
        return ("result", d.get("result") or "")
    return None


def run_agent(prompt, on_delta=None, timeout=CHAT_TIMEOUT, on_start=None):
    """Run the agent on one prompt; on_delta(text) per streamed chunk.
    on_start(proc) lets a caller keep the process to cancel it. Returns the
    full answer text."""
    import codecs
    import subprocess
    argv, kind = agent_command()
    stdin = prompt if kind == "claude" else CHAT_SYSTEM + "\n\n" + prompt
    try:
        # Its own empty cwd: no project files or CLAUDE.md for it to pick up.
        cwd = os.path.join(state_dir(), "agent")
        private_dir(cwd)
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=cwd, start_new_session=True)
    except OSError as e:
        raise StickiesError(f"can't run the agent ({argv[0]}): {e}")
    if on_start:
        on_start(proc)
    timed_out = threading.Event()

    def expire():
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    err = []
    errt = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    errt.start()
    text, final, failure = [], None, None
    try:
        try:
            proc.stdin.write(stdin.encode())
            proc.stdin.close()
        except OSError:
            pass
        if kind == "claude":
            for line in proc.stdout:
                ev = _claude_events(line.decode("utf-8", "replace"))
                if ev is None:
                    continue
                if ev[0] == "delta":
                    text.append(ev[1])
                    if on_delta and ev[1]:
                        on_delta(ev[1])
                elif ev[0] == "result":
                    final = ev[1]
                else:
                    failure = ev[1]
        else:
            dec = codecs.getincrementaldecoder("utf-8")("replace")
            fd = proc.stdout.fileno()
            while True:
                chunk = os.read(fd, 4096)
                s = dec.decode(chunk, final=not chunk)
                if s:
                    text.append(s)
                    if on_delta:
                        on_delta(s)
                if not chunk:
                    break
        rc = proc.wait()
    finally:
        timer.cancel()
        errt.join(timeout=1)
        proc.stdout.close()
        proc.stderr.close()
    if timed_out.is_set():
        raise StickiesError(f"agent timed out after {timeout} s")
    if rc < 0:
        raise StickiesError("agent stopped")
    if failure:
        raise StickiesError(f"agent error: {failure}")
    if rc != 0:
        msg = (b"".join(err).decode("utf-8", "replace").strip() or "".join(text).strip())[-500:]
        raise StickiesError(f"agent exited {rc}: {msg}")
    return final if final is not None and final.strip() else "".join(text)


def prepare_ask(store, question, ids=None, k=CHAT_K, history=None):
    """Everything a turn sends, before anything is sent. ids: exactly the
    notes to send (what the person left ticked); None: the top-k by hybrid
    search."""
    question = (question or "").strip()
    if not question:
        raise StickiesError("empty question")
    notes = notes_by_id(store, ids) if ids is not None else chat_notes(store, question, k)
    sent = [{"id": n["id"], "color": n["color"], "title": _first_line(n["body"], 60),
             "body": n["body"]} for n in notes]
    return {"question": question, "sent": sent, "prompt": build_prompt(question, notes, history)}


def finish_ask(res, raw, ms=None):
    """The turn's result from the agent's raw answer: visible answer,
    citations and validated proposals. Applies nothing."""
    sent_ids = [n["id"] for n in res["sent"]]
    cited, unknown = parse_citations(raw, sent_ids)
    proposals, rejected = parse_proposals(raw, sent_ids)
    return dict(res, answer=visible_answer(raw).strip(), raw=raw, citations=cited,
                unknown_citations=unknown, proposals=proposals, rejected=rejected, ms=ms)


def ask(store, question, ids=None, k=CHAT_K, history=None, on_delta=None, dry_run=False):
    """One chat turn, start to end. Never applies anything."""
    res = prepare_ask(store, question, ids, k, history)
    if dry_run:
        return dict(res, answer=None, raw=None, citations=[], unknown_citations=[],
                    proposals=[], rejected=[], ms=None)
    t = time.perf_counter()
    raw = run_agent(res["prompt"], on_delta=on_delta)
    out = finish_ask(res, raw, round((time.perf_counter() - t) * 1000))
    store.log(f"ask: {len(out['sent'])} notes sent, {len(out['proposals'])} proposals")
    return out


def apply_proposal(store, p):
    """Apply one confirmed proposal. Revalidated here: whatever the caller
    hands in, only the three actions exist and nothing is deleted."""
    import subprocess
    p = validate_proposal(p)
    if p["action"] == "new_note":
        n = store.add(p["body"], color=p.get("color"), tags=p.get("tags"))
        return {"action": p["action"], "applied": True, "note": n}
    if p["action"] == "append_note":
        if store.get(p["id"])["archived_at"]:
            raise StickiesError(f"note #{p['id']} is archived")
        n = store.edit(p["id"], append=p["text"])
        return {"action": p["action"], "applied": True, "note": n}
    exe = hub_bin(explicit=True)
    if exe is None:
        raise StickiesError("hub is disabled ($STICKIES_HUB is empty)")
    cmd = [exe, "todo", "add", p["title"], "--json"]
    if p.get("due"):
        cmd += ["--due", p["due"]]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise StickiesError(f"hub todo add failed: {e}")
    if r.returncode != 0:
        raise StickiesError(f"hub todo add failed: {(r.stderr or r.stdout).strip()[-300:]}")
    try:
        todo = json.loads(r.stdout)
    except ValueError:
        todo = None
    store.log(f"hub todo added from chat: {p['title']}")
    return {"action": p["action"], "applied": True, "todo": todo}


# -- serve -------------------------------------------------------------------

def _req_id(arg):
    if arg is None:
        raise StickiesError("missing id")
    try:
        return int(arg)
    except (TypeError, ValueError):
        raise StickiesError(f"note id must be a number, not {arg!r}")


def tidy_args(store, a):
    """serve's tidy: the desktop knows its monitor, workspace and size;
    Hyprland's gaps_out and the bar's reserved space come from hyprctl
    unless given."""
    if a.get("workspace") is None or not a.get("monitor") or not a.get("width") or not a.get("height"):
        raise StickiesError("tidy needs the workspace, monitor and screen size")
    reserved = a.get("reserved")
    if reserved is None:
        try:
            reserved = hypr_screen(a["monitor"])["reserved"]
        except StickiesError:
            reserved = (0, 0, 0, 0)
    return store.tidy(a["workspace"], a["monitor"], a["width"], a["height"],
                      gaps=a.get("gaps") or hypr_gaps_out(), reserved=reserved,
                      primary=a.get("primary", True))


_CHECK = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?\[([ xX])\]", re.M)
_CHECK_PREFIX = re.compile(r"^[ \t]*(?:[-*+][ \t]+)?\[[ xX]\][ \t]*")
BRIEF_TEXT = 500
BRIEF_TITLE = 120  # one elided line in the list


def note_title(body):
    """The first line with text, without a checkbox (a rolled note's title)."""
    for line in (body or "").split("\n"):
        t = _CHECK_PREFIX.sub("", line).strip()
        if t:
            return t
    return ""


def brief_row(n):
    """A note as the All notes view needs it: title, checklist progress
    ([done, total] or null) and `hay`, what its text filter looks in (the
    first BRIEF_TEXT characters and the tags, lower-cased), instead of the
    whole body and geometry: 2,000 notes go over the pipe on every open."""
    body = n["body"]
    marks = _CHECK.findall(body) if "[" in body else []
    hay = (body[:BRIEF_TEXT] + "\n" + " ".join(n["tags"])).lower()
    return {"id": n["id"], "title": note_title(body)[:BRIEF_TITLE], "hay": hay, "color": n["color"],
            "pinned": n["pinned"], "workspace": n["workspace"], "tags": n["tags"],
            "remind_at": n["remind_at"], "updated_at": n["updated_at"],
            "checks": [sum(m != " " for m in marks), len(marks)] if marks else None}


def dispatch(store, op, a):
    """One request -> result. Same operations as the CLI."""
    a = a or {}
    if op == "ping":
        return {"pong": True, "seq": store.last_seq(),
                "semantic": store._embedder not in (None, "auto")}
    if op == "colors":
        return COLORS
    if op == "list":
        ns = store.list(archived=bool(a.get("archived")), all=bool(a.get("all")),
                        workspace=a.get("workspace"), color=a.get("color"),
                        pinned=a.get("pinned"), limit=a.get("limit"), tags=a.get("tags"))
        return [brief_row(n) for n in ns] if a.get("brief") else ns
    if op == "tags":
        return store.all_tags(archived=bool(a.get("archived")))
    if op == "tag":
        return store.tag(_req_id(a.get("id")), a.get("tags"))
    if op == "untag":
        return store.untag(_req_id(a.get("id")), a.get("tags"))
    if op == "set_tags":
        return store.set_tags(_req_id(a.get("id")), a.get("tags") or [])
    if op == "show":
        return store.get(_req_id(a.get("id")))
    if op == "search":
        return store.search(a.get("query", ""), limit=a.get("limit", 20),
                            archived=bool(a.get("archived")), mode=a.get("mode") or "hybrid",
                            tags=a.get("tags"))
    if op == "add":
        return store.add(a.get("body", ""), color=a.get("color"), pinned=bool(a.get("pinned")),
                         workspace=a.get("workspace"), monitor=a.get("monitor"),
                         x=a.get("x"), y=a.get("y"), w=a.get("w"), h=a.get("h"), tags=a.get("tags"))
    if op == "edit":
        return store.edit(_req_id(a.get("id")), body=a.get("body"), append=a.get("append"))
    if op == "pin":
        return store.pin(_req_id(a.get("id")), a.get("pinned", True))
    if op == "unpin":
        return store.pin(_req_id(a.get("id")), False)
    if op == "color":
        return store.color(_req_id(a.get("id")), a.get("color"))
    if op == "move":
        return store.move(_req_id(a.get("id")), x=a.get("x"), y=a.get("y"), w=a.get("w"),
                          h=a.get("h"), z=a.get("z"), workspace=a.get("workspace"),
                          monitor=a.get("monitor"), raise_=bool(a.get("raise")))
    if op in ("rm", "archive"):
        return store.archive(_req_id(a.get("id")))
    if op == "restore":
        return store.restore(_req_id(a.get("id")))
    if op == "purge":
        return store.purge(_req_id(a.get("id")), force=bool(a.get("force")))
    if op == "discard":  # the desktop: a note that lost focus with no text
        return {"id": _req_id(a.get("id")), "discarded": bool(store.discard_empty(_req_id(a.get("id"))))}
    if op == "clean":  # shell start: notes a past session left empty
        return {"discarded": store.discard_empty(older_than=a.get("older_than"))}
    if op == "settings":
        return store.settings()
    if op == "set":  # {"values": {key: value, ...}}
        return store.set_settings(a.get("values"))
    if op == "dock":
        return store.dock(_req_id(a.get("id")), bool(a.get("docked", True)))
    if op == "undo":  # the newest archive back (the toast's Undo)
        return store.undo_archive()
    if op == "roll":
        return store.roll(_req_id(a.get("id")), a.get("rolled", True))
    if op == "tidy":
        return tidy_args(store, a)
    if op == "tidy_undo":
        return store.tidy_undo()
    if op == "paste":  # SUPER + ALT + V: a note from the clipboard
        text, cut = clipboard_text()
        n = store.add(text, monitor=a.get("monitor"), workspace=a.get("workspace"),
                      x=a.get("x"), y=a.get("y"))
        return {"note": n, "truncated": cut}
    if op == "reminders":
        return store.reminders()
    if op == "chat_notes":
        return chat_notes(store, a.get("question", ""), a.get("k") or CHAT_K)
    if op == "chat_info":
        return chat_info()
    if op == "ask":  # serve streams this one from a thread; this is the blocking form
        return ask(store, a.get("question", ""), ids=a.get("ids"), k=a.get("k") or CHAT_K,
                   history=a.get("history"))
    if op == "chat_cancel":
        return {"cancelled": False}  # serve handles live chats itself
    if op == "apply":
        return apply_proposal(store, a.get("proposal"))
    if op == "integration":  # the desktop: offer keys + command on first start?
        return integration_status()
    if op == "integrate":  # the desktop asks: it runs this code already, no restart
        return integrate(bool(a.get("yes")))
    if op == "keys_on":  # the desktop, at start and after a Hyprland config reload
        return keys_on() if read_integration().get("keys") else {"keys": "off"}
    raise StickiesError(f"unknown op {op!r}")


class Indexer(threading.Thread):
    """serve's embedding model and background embedder. The model loads on
    this thread (serve never waits for it) when something needs it: a
    search that wants meaning (`use()`), a note whose text has no current
    vector, or at once with `preload`. Loaded, it goes to the serving
    Store, every stale note is embedded, then each note `delay` s after its
    last change, on this thread's own DB connection. serve calls `unload()`
    once nothing has searched or embedded for its idle time: the model is
    dropped and its memory handed back. Failures are logged; they never
    take serve down."""

    def __init__(self, store, embedder="auto", delay=1.0, batch=8, on_loaded=None, on_unloaded=None,
                 preload=False):
        super().__init__(name="stickies-indexer", daemon=True)
        self.store, self.delay, self.batch = store, delay, batch
        # "auto", None (FTS only), or (tests) an Embedder-like object, or a
        # class: a new one is made for each load.
        self.source = embedder
        self.on_loaded, self.on_unloaded = on_loaded, on_unloaded
        auto = isinstance(embedder, str)
        self.available = embedder is not None and (not auto or embedder_problem() is None)
        self.name = (model_name() if auto else embedder.name) if self.available else None
        self.cv = threading.Condition()
        self.due = {}          # note id -> monotonic time it may be embedded
        self.stopped = False
        self.emb = None        # the loaded model, or None
        self.loaded_at = None  # wall clock (ISO) it was loaded
        self.last_use = None   # monotonic time of the last search or embedding
        self.loads = 0
        self.want_load = preload and self.available
        self.want_unload = False
        self.unloading = False  # popped, not gone yet: a use() now must reload

    def touch(self, ids):
        with self.cv:
            t = time.monotonic() + self.delay
            for i in ids:
                self.due[i] = t
            self.cv.notify()

    def use(self):
        """A search wants meaning: it counts as use, and loads the model if
        it isn't (this search gets words only; the next gets both)."""
        with self.cv:
            self.last_use = time.monotonic()
            self.want_unload = False
            if (self.emb is None or self.unloading) and self.available and not self.want_load:
                self.want_load = True
                self.cv.notify()

    def idle_since(self):
        """Monotonic time of the last use while the model is loaded (and not
        already on its way out), else None: then nothing is timed."""
        with self.cv:
            return None if self.emb is None or self.want_unload else self.last_use

    def unload(self):
        with self.cv:
            if self.emb is not None:
                self.want_unload = True
                self.cv.notify()

    def status(self, idle_minutes=None):
        with self.cv:
            loaded, since, last = self.emb is not None, self.loaded_at, self.last_use
        ago = None if last is None else time.monotonic() - last
        last_iso = None if ago is None else (datetime.now(timezone.utc) - timedelta(seconds=ago)).isoformat(
            timespec="seconds").replace("+00:00", "Z")
        unload_at = None
        if loaded and idle_minutes and ago is not None:
            unload_at = (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, idle_minutes * 60 - ago))
                         ).isoformat(timespec="seconds").replace("+00:00", "Z")
        return {"available": self.available, "model": self.name, "loaded": loaded, "loaded_since": since,
                "last_use": last_iso, "idle_unload": idle_minutes, "unload_at": unload_at}

    def stop(self):
        with self.cv:
            self.stopped = True
            self.cv.notify()

    def _load(self, st):
        t = time.perf_counter()
        src = self.source
        emb = load_embedder() if isinstance(src, str) else src() if isinstance(src, type) else src
        if emb is None:  # a broken download: words only from now on
            self.available = False
            return
        with self.cv:
            self.emb, self.loaded_at = emb, now_iso()
            self.last_use = time.monotonic()
            self.loads += 1
        self.store.embedder = emb
        st.log(f"model loaded ({emb.name}, {(time.perf_counter() - t) * 1000:.0f} ms)")
        if self.on_loaded:
            self.on_loaded()

    def _unload(self, st):
        self.store.embedder = None
        self.store._vec_cache = st._vec_cache = None  # the vector matrix goes too
        with self.cv:
            self.emb = self.loaded_at = None
            self.unloading = False
        release_memory()
        st.log("model unloaded (idle)")
        if self.on_unloaded:
            self.on_unloaded()

    def _embed(self, st, ids=None):
        n = st.embed_notes(self.emb, ids=ids, batch=self.batch,
                           should_stop=lambda: self.stopped or self.want_unload)
        if n:
            with self.cv:
                self.last_use = time.monotonic()
        return n

    def run(self):
        if not self.available:
            return
        st = Store(self.store.path, embedder=None)
        try:
            while True:
                with self.cv:
                    while not self.stopped:
                        now = time.monotonic()
                        ready = [i for i, t in self.due.items() if t <= now]
                        if ready or self.want_unload or (self.want_load and self.emb is None):
                            break
                        self.cv.wait(timeout=min(self.due.values()) - now if self.due else None)
                    if self.stopped:
                        return
                    for i in ready:
                        del self.due[i]
                    unload, load = self.want_unload, self.want_load
                    self.want_unload = self.want_load = False
                    self.unloading = unload and self.emb is not None
                try:
                    if unload and self.emb is not None:
                        self._unload(st)  # a search meanwhile set want_load: back next round
                    if ready and self.emb is None and not load:
                        # A move or a colour change: load only for text without a vector.
                        load = bool(st.stale_embeddings(self.name, ready))
                    if load and self.emb is None:
                        self._load(st)
                        if self.emb is not None:
                            n = self._embed(st)  # everything written while it was away
                            if n:
                                st.log(f"embedded {n} notes ({self.emb.name})")
                            continue
                    if ready and self.emb is not None:
                        self._embed(st, ready)
                except (sqlite3.Error, StickiesError, ValueError, RuntimeError) as e:
                    st.log(f"embedding failed: {e}")
        except Exception as e:
            st.log(f"indexer stopped: {e!r}")
        finally:
            st.close()


# A cold `stickies search` (an agent, a script) would spend ~1 s loading the
# model before it can embed the query. While `serve` runs (the desktop plugin
# keeps one up) it already has the model loaded, so it also answers read-only
# searches on a Unix socket in the state dir, and the CLI asks it first.
# Only `search`, one request per connection, owner-only permissions.

SEARCH_SOCKET = "serve.sock"

# serve: requests that want the embedding model (each counts as use, and
# loads it if it isn't). PRELOAD_MODEL: load it at start, not on first use.
MODEL_OPS = ("search", "chat_notes", "ask")
PRELOAD_MODEL = True


def search_socket_path():
    return os.path.join(state_dir(), SEARCH_SOCKET)


def open_search_socket(path):
    """A listening socket at `path`, or None (another serve answers there,
    the path is too long for AF_UNIX, ...). A stale socket is replaced."""
    import socket
    if len(path.encode()) > 100:
        return None
    if os.path.exists(path):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(path)
            return None  # live: someone else's
        except OSError:
            try:
                os.unlink(path)
            except OSError:
                return None
        finally:
            probe.close()
    lst = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        lst.bind(path)
        lst.listen(8)
    except OSError:
        lst.close()
        return None
    finally:
        os.umask(old)
    lst.setblocking(False)
    return lst


def answer_search_socket(store, lst, indexer=None, idle=None):
    """One request from the socket. {"op": "changed"}: another process wrote
    (returns "changed"; nothing is answered). {"op": "search", "args":
    {...}} -> {"ok": true, "result": [...]} or {"ok": false, "error"},
    refused until the model is loaded, so the caller falls back to
    searching itself (and gets meaning, not a words-only answer); a refused
    search starts loading it for the next one. {"op": "model"} -> {"ok":
    true, "result": Indexer.status()} (`stickies setup --status`)."""
    try:
        conn, _ = lst.accept()
    except OSError:
        return None
    with conn:
        conn.settimeout(1.0)
        try:
            data = b""
            while b"\n" not in data and len(data) < 65536:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            req = json.loads(data.split(b"\n", 1)[0] or b"null")
            op = req.get("op") if isinstance(req, dict) else None
            if op == "changed":
                return "changed"
            if op == "model" and indexer is not None:
                res = {"ok": True, "result": indexer.status(idle)}
            elif op != "search":
                raise StickiesError("only search is answered here")
            else:
                if indexer is not None and (req.get("args") or {}).get("mode") != "fts":
                    indexer.use()
                if store._embedder in (None, "auto"):
                    raise StickiesError("the model is not loaded yet")
                res = {"ok": True, "result": dispatch(store, "search", req.get("args") or {})}
        except (StickiesError, ValueError, TypeError, sqlite3.Error) as e:
            res = {"ok": False, "error": str(e)}
        except OSError:
            return None
        try:
            conn.sendall((json.dumps(res, ensure_ascii=False) + "\n").encode())
        except OSError:
            pass


def ask_serve(req, timeout=2.0):
    """One request to a running serve's socket -> its result, or None (no
    serve, refused, any error)."""
    import socket
    path = search_socket_path()
    if not os.path.exists(path):
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        s.sendall((json.dumps(req) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(1 << 16)
            if not chunk:
                break
            data += chunk
        resp = json.loads(data)
    except (OSError, ValueError):
        return None
    finally:
        s.close()
    return resp.get("result") if isinstance(resp, dict) and resp.get("ok") else None


def search_via_serve(query, limit=20, archived=False, mode="hybrid", timeout=2.0, tags=None):
    """The hits from a running serve that has the model loaded, or None
    (no serve, not loaded yet, any error): then search in-process."""
    return ask_serve({"op": "search", "args": {"query": query, "limit": limit, "archived": archived,
                                               "mode": mode, "tags": tags or []}}, timeout)


def serve(store, infd=0, out=None, poll=1.0, hub_delay=2.0, embedder="auto", embed_delay=1.0,
          notify="auto", remind_every=30.0, preload=None, on_wait=None):
    """JSON-lines loop. Request:  {"id": any, "op": "...", "args": {...}}
    Response: {"id": same, "ok": true, "result": ...} or {"id", "ok": false, "error"}.
    Events (unsolicited): {"event": "changed", "seq", "kind", "id", "note"} for
    every add/update/archive/restore/purge, and {"event": "settings", "seq",
    "settings"} after settings writes, from this process or any other;
    {"event": "semantic", "on": true, "loaded": bool} when search by meaning
    is installed (at start), and each time the model loads or unloads.
    The hub card is republished `hub_delay` s after the last change (and
    once at start).

    The model loads on first use (`preload`: at start) and is dropped after
    `idle_unload` minutes without a search or an embedding.

    Idle, it sleeps in select() with no timeout: other processes' writes
    arrive as a `changed` poke on serve.sock (Store does that after every
    write). Only when that socket can't be opened does it fall back to
    checking the DB every `poll` s. While the model is loaded, the time
    left until it unloads is the timeout; once it is gone, none again.
    `on_wait(timeout)` (tests) sees each select() timeout."""
    import selectors
    import time
    out = out or sys.stdout

    lock = threading.Lock()  # chat threads emit too

    def emit(obj):
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        with lock:
            out.write(line)
            out.flush()

    chats = {}  # chat id -> running agent process, None before it starts, "cancel"

    def start_ask(rid, a):
        """Notes are read here, on serve's thread; the agent runs on its own
        thread, streaming {"event": "chat", "chat", "kind": "delta", "answer"}
        (the visible answer so far) and finally answering the request."""
        cid = str(a.get("chat") if a.get("chat") is not None else rid)
        if cid in chats:
            raise StickiesError(f"chat {cid} is still answering")
        res = prepare_ask(store, a.get("question", ""), ids=a.get("ids"),
                          k=a.get("k") or CHAT_K, history=a.get("history"))
        emit({"event": "chat", "chat": cid, "kind": "sent", "sent": res["sent"]})
        chats[cid] = None

        def started(proc):
            if chats.get(cid) == "cancel":
                proc.kill()
            chats[cid] = proc

        def work():
            t = time.perf_counter()
            buf, last = [], [""]

            def delta(chunk):
                buf.append(chunk)
                vis = visible_answer("".join(buf), partial=True)
                if vis != last[0]:
                    last[0] = vis
                    emit({"event": "chat", "chat": cid, "kind": "delta", "answer": vis})
            try:
                raw = run_agent(res["prompt"], on_delta=delta, on_start=started)
                r = finish_ask(res, raw, round((time.perf_counter() - t) * 1000))
                r["chat"] = cid
                store.log(f"ask: {len(r['sent'])} notes sent, {len(r['proposals'])} proposals")
                emit({"id": rid, "ok": True, "result": r})
            except Exception as e:  # never take serve down
                emit({"id": rid, "ok": False, "error": str(e), "chat": cid})
            finally:
                chats.pop(cid, None)

        threading.Thread(target=work, name=f"stickies-chat-{cid}", daemon=True).start()

    def cancel_ask(a):
        cid = str(a.get("chat"))
        if cid not in chats:
            return {"cancelled": False}
        proc = chats[cid]
        if proc is None:
            chats[cid] = "cancel"
        elif proc != "cancel":
            proc.kill()
        return {"cancelled": True}

    seq = store.last_seq()
    dv = store.data_version()
    store.notify = False  # serve drains its own writes after each request
    # Searches stay FTS-only until the indexer has loaded the model.
    store.embedder = None
    # The indexer loads the model while serve may sleep with no timeout:
    # a byte on this pipe wakes it to start timing the idle unload.
    wake_r, wake_w = os.pipe()

    def loaded():
        emit({"event": "semantic", "on": True, "loaded": True})
        os.write(wake_w, b"l")

    indexer = Indexer(store, embedder, embed_delay, preload=PRELOAD_MODEL if preload is None else preload,
                      on_loaded=loaded,
                      on_unloaded=lambda: emit({"event": "semantic", "on": True, "loaded": False}))
    idle = idle_unload(store)[0]
    emit({"event": "ready", "seq": seq, "pid": os.getpid()})
    if indexer.available:
        emit({"event": "semantic", "on": True, "loaded": False})
    indexer.start()

    hub_due = time.monotonic() + hub_delay
    hub_proc = None
    # Reminders: checked now (so ones missed while nothing ran fire once,
    # at start), then when the next one is due, re-planned after every
    # change. While one is pending serve looks at least every `remind_every`
    # s (the monotonic clock stops during suspend; the wall clock doesn't);
    # with none pending it never wakes for them. No extra daemon.
    remind_due = 0.0

    if notify == "auto":  # off (reminders wait, unfired) when there is nowhere to send them
        notify = send_notification if notify_bin() else None

    def remind():
        if notify is None:
            return
        try:
            store.fire_reminders(notify)
        except (sqlite3.Error, StickiesError) as e:
            store.log(f"reminders: {e}")

    def plan_reminders():
        nonlocal remind_due
        nxt = store.next_reminder_due() if notify is not None else None
        if nxt is None:
            remind_due = None
            return
        left = (datetime.fromisoformat(nxt.replace("Z", "+00:00"))
                - datetime.now(timezone.utc)).total_seconds()
        remind_due = time.monotonic() + max(0.0, min(left, remind_every))

    def drain():
        nonlocal seq, hub_due, idle
        seq, events = store.changes_since(seq)
        for ev in events:
            emit(ev)
            if ev["event"] == "settings":
                idle = idle_unload(store)[0]
        events = [ev for ev in events if ev["event"] == "changed"]
        if events:
            plan_reminders()
            hub_due = time.monotonic() + hub_delay
            indexer.touch([ev["id"] for ev in events if ev["kind"] in ("add", "update", "restore")])

    sel = selectors.DefaultSelector()
    sel.register(infd, selectors.EVENT_READ)
    sel.register(wake_r, selectors.EVENT_READ)
    sock_path = os.path.join(store.dir, SEARCH_SOCKET)
    listener, listen_retry, sock_ino = None, 0.0, None

    def listen():
        nonlocal listener, listen_retry, sock_ino
        listen_retry = time.monotonic() + 5  # e.g. an older serve still holds it
        listener = open_search_socket(sock_path)
        if listener is not None:
            sock_ino = os.stat(sock_path).st_ino  # ours to remove at exit
            sel.register(listener, selectors.EVENT_READ)
    listen()
    try:
        check_loaded_code()
    except (OSError, ValueError, StickiesError) as e:
        store.log(f"loaded-code check: {e}")

    def unload_due():
        """Monotonic time the idle model goes, or None (none loaded, or 0 =
        keep it)."""
        since = indexer.idle_since() if idle else None
        return None if since is None else since + idle * 60

    buf = b""
    handled = 0
    while True:
        now = time.monotonic()
        waits = [t - now for t in (hub_due, remind_due, unload_due()) if t is not None]
        if listener is None:  # no pokes: retry the socket, look at the DB now and then
            waits += [poll, listen_retry - now]
        timeout = max(0.0, min(waits)) if waits else None
        if on_wait:
            on_wait(timeout)
        ready = sel.select(timeout=timeout)
        if any(k.fileobj == wake_r for k, _ in ready):
            os.read(wake_r, 64)
            ready = [(k, e) for k, e in ready if k.fileobj != wake_r]
            if not ready:
                continue
        due = unload_due()
        if due is not None and time.monotonic() >= due:
            indexer.unload()
        if listener is not None and any(k.fileobj is listener for k, _ in ready):
            if answer_search_socket(store, listener, indexer, idle) == "changed":
                dv = store.data_version()
                drain()
            ready = [(k, e) for k, e in ready if k.fileobj is not listener]
            if not ready:
                continue
        if listener is None and time.monotonic() >= listen_retry:
            listen()
        if ready:
            chunk = os.read(infd, 65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                rid = None
                try:
                    req = json.loads(line)
                    if not isinstance(req, dict):
                        raise StickiesError("request must be a JSON object")
                    rid = req.get("id")
                    op, args = req.get("op"), req.get("args") or {}
                    if op in MODEL_OPS and args.get("mode") != "fts":
                        indexer.use()
                    if op == "ask":
                        start_ask(rid, args)
                    else:
                        result = cancel_ask(args) if op == "chat_cancel" else dispatch(store, op, args)
                        emit({"id": rid, "ok": True, "result": result})
                except (StickiesError, ValueError, TypeError, sqlite3.Error) as e:
                    emit({"id": rid, "ok": False, "error": str(e)})
                except Exception as e:  # a bug: logged in full, plain words to the UI
                    import traceback
                    store.log(f"serve {line[:200]!r} failed:\n{traceback.format_exc()}")
                    emit({"id": rid, "ok": False,
                          "error": f"something went wrong ({type(e).__name__}); details in stickies.log"})
                handled += 1
                drain()
                if handled % 1000 == 0:
                    store.prune_changes()
        elif listener is None:
            # Fallback without the socket: pick up other processes' writes.
            v = store.data_version()
            if v != dv:
                dv = v
                drain()
        if remind_due is not None and time.monotonic() >= remind_due:
            remind()
            drain()
            plan_reminders()
        if hub_due is not None and time.monotonic() >= hub_due:
            if hub_proc is None or hub_proc.poll() is not None:
                hub_proc = publish_hub(store, wait=False)
                hub_due = None
    sel.close()
    os.close(wake_r)
    if listener is not None:
        try:
            if os.stat(sock_path).st_ino == sock_ino:
                os.unlink(sock_path)
        except OSError:
            pass
        listener.close()
    for proc in list(chats.values()):
        if proc not in (None, "cancel"):
            proc.kill()
    indexer.stop()
    indexer.join(timeout=5)  # let a running batch finish before the interpreter exits
    if not indexer.is_alive():  # else it may still write to it
        os.close(wake_w)


# -- CLI ---------------------------------------------------------------------

def fmt_serve_model(m):
    if m is None:
        return "model in serve: serve is not running"
    if not m["available"]:
        return "model in serve: none (words only)"
    if not m["loaded"]:
        return "model in serve: not loaded (loads on the next search, ~0.7 s)"
    return (f"model in serve: loaded since {m['loaded_since']}"
            + (f", unloads at {m['unload_at']} if unused" if m.get("unload_at") else ""))


def fmt_keys(r):
    if r.get("keys") == "dropin":
        return "keys: from the drop-in " + dropin_path()
    if r.get("keys") == "unavailable":
        return "keys: no Hyprland session to bind them in (they are added when the desktop starts)"
    lines = [f"bound {k}" for k in r.get("bound", [])] + [f"already bound {k}" for k in r.get("already", [])]
    lines += [f"not bound {t['keys']}: already used for {t['by']!r}" for t in r.get("taken", [])]
    return "\n".join(lines) or "keys: off"


def fmt_integration(r):
    link = r["link"]
    lines = []
    m = r.get("migrated")
    if m:
        lines.append(f"plugin: replaced the symlink {m['path']} -> {m['from']} with a git clone of it "
                     f"({m['commit']}); update it with: omarchy plugin update {PLUGIN_ID}")
        if m["uncommitted"]:
            lines.append(f"  {m['from']} has uncommitted changes: the clone has its last commit only")
        if m.get("venv"):
            lines.append(f"  search by meaning used {m['venv']}; run `stickies setup` once for its own venv")
    keys = r["result"]["keys"] if r.get("result") else r["keys"]
    lines += [f"keys: {keys}" + (" (" + ", ".join(k.replace(" ", "") for k, _, _ in KEYS) + ")"
                                 if keys != "off" else ""),
              f"command: {link['path']} ({link['state']})"]
    if r.get("result", {}).get("keys") == "dropin":
        lines.append(f"keybindings -> {r['result']['path']}:")
        lines += [f"  {k}: {desc}" for k, desc, _ in KEYS]  # the drop-in's own descriptions
    elif "result" in r:
        lines.append(fmt_keys(r["result"]))
    if r.get("enabled"):
        lines.append(f"enabled {PLUGIN_ID}")
    if not r["asked"]:
        lines.append("not set up yet: stickies integrate --yes")
    if "shell" in r:
        lines.append(fmt_refresh(r["shell"]))
    elif r.get("code_changed"):
        lines.append("plugin code changed since the last install: stickies integrate --refresh "
                     "restarts the shell so it loads it")
    return "\n".join(lines)


def fmt_refresh(r):
    if r.get("error"):
        return f"shell restart failed: {r['error']} (run omarchy-restart-shell)"
    if r["restarted"]:
        return "restarted the shell: it loaded the new plugin code"
    return "plugin code: changed, the shell will load it when it starts" if r["changed"] \
        else "plugin code: unchanged since the last install"


def _tty():
    return sys.stdout.isatty()


def _first_line(body, width=60):
    line = next((l.strip() for l in body.splitlines() if l.strip()), "")
    return line if len(line) <= width else line[: width - 1] + "…"


def fmt_note_line(n):
    pin = "📌" if n["pinned"] else "  "
    arch = " (archived)" if n["archived_at"] else ""
    tags = "".join(f"  #{t}" for t in n["tags"])
    return f"#{n['id']:<4} {pin} {n['color']:<7} {_first_line(n['body'])}{tags}{arch}"


def fmt_note_full(n):
    lines = [f"#{n['id']}  {n['color']}{'  pinned' if n['pinned'] else ''}"
             f"{'  archived ' + n['archived_at'] if n['archived_at'] else ''}",
             f"at {n['x']},{n['y']} {n['w']}x{n['h']} z={n['z']}"
             f"  workspace={n['workspace']} monitor={n['monitor']}",
             f"created {n['created_at']}  updated {n['updated_at']}"]
    if n["tags"]:
        lines.append("tags " + " ".join(n["tags"]))
    lines += ["", n["body"]]
    return "\n".join(lines)


def fmt_hit(h):
    """One hit per line; `~` marks a note found by meaning only."""
    s = h["snippet"].replace("\n", " ")
    if _tty():
        parts, last = [], 0
        for a, b in h["highlights"]:
            parts += [s[last:a], "\x1b[1;7m", s[a:b], "\x1b[0m"]
            last = b
        parts.append(s[last:])
        s = "".join(parts)
    else:
        for a, b in reversed(h["highlights"]):
            s = s[:a] + "[" + s[a:b] + "]" + s[b:]
    mark = "~" if h.get("match") == "semantic" else " "
    return f"#{h['id']:<4}{mark}{h['color']:<7} {s}" + "".join(f"  #{t}" for t in h.get("tags") or [])


def read_body(words, use_stdin, initial=""):
    if words:
        return " ".join(words)
    if use_stdin or not sys.stdin.isatty():
        return sys.stdin.read().rstrip("\n")
    import subprocess
    import tempfile
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nvim"
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        f.write(initial)
        path = f.name
    try:
        subprocess.call([editor, path])
        with open(path) as f:
            return f.read().rstrip("\n")
    finally:
        os.unlink(path)


QUICK_START = """start here:
  stickies add "Cardmarket fees went up"   a note (on the desktop: SUPER + ALT + N)
  stickies search cardm fee                find it (SUPER + ALT + J)
  stickies ask "what about the fees?"      ask your notes via Omarchy's default agent (SUPER + ALT + A)
  stickies setup                           optional, once: search by meaning (asks before downloading)
every command takes --json."""

FIRST_RUN = """no notes yet
  stickies add "your first note"   or SUPER + ALT + N on the desktop
  stickies setup                   optional, once: search by meaning, fully offline (asks before downloading)
  stickies --help                  everything else"""


class _Formatter(argparse.RawDescriptionHelpFormatter):
    """argparse builds a formatter for every parser just to validate its
    arguments, and on 3.14 each one imports _colorize (+ dataclasses,
    inspect) and shutil: ~16 ms of a ~75 ms cold start for output nobody
    sees. Width and colour are only worked out when help is printed."""

    def __init__(self, prog, indent_increment=2, max_help_position=24, width=None, **kw):
        if width is None:
            try:
                width = int(os.environ["COLUMNS"])
            except (KeyError, ValueError):
                try:
                    width = os.get_terminal_size(sys.__stdout__.fileno()).columns
                except (AttributeError, ValueError, OSError):
                    width = 80
            width -= 2
        super().__init__(prog, indent_increment, max_help_position, width, **kw)  # color: 3.14+

    def _set_color(self, color):
        self._want_color = color

    def __getattr__(self, name):
        if name in ("_theme", "_decolor"):
            argparse.HelpFormatter._set_color(self, self.__dict__.get("_want_color", False))
            return self.__dict__[name]
        raise AttributeError(name)


class _Version(argparse.Action):
    """--version: read from manifest.json when asked (not on every start);
    with --json before it, {"version", "plugin_dir"}."""

    def __call__(self, parser, namespace, values, option_string=None):
        v = {"version": plugin_version(), "plugin_dir": here_dir()}
        print(json.dumps(v) if getattr(namespace, "json", False) else f"stickies {v['version']} ({v['plugin_dir']})")
        parser.exit()


def build_parser():
    common = argparse.ArgumentParser(add_help=False, formatter_class=_Formatter)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="one JSON document on stdout")

    p = argparse.ArgumentParser(prog="stickies", parents=[common],
                                description="Omarchy sticky notes (state: $STICKIES_STATE).",
                                epilog=QUICK_START, formatter_class=_Formatter)
    p.add_argument("--version", action=_Version, nargs=0,
                   help="the installed version (from manifest.json) and the plugin folder")
    sub = p.add_subparsers(dest="cmd", required=True, prog="stickies")  # prog: see _Formatter

    def sp(name, help, **kw):
        return sub.add_parser(name, help=help, parents=[common], formatter_class=_Formatter, **kw)

    s = sp("add", "new note (text args, stdin, or $EDITOR)")
    s.add_argument("text", nargs="*")
    s.add_argument("--stdin", action="store_true")
    s.add_argument("--clipboard", action="store_true",
                   help=f"the clipboard's text (wl-paste; trimmed, at most {CLIP_MAX:,} characters)")
    s.add_argument("--color", "-c")
    s.add_argument("--tag", "-t", action="append", metavar="TAG",
                   help="a tag (repeat for more): lower-case a-z, 0-9, -, at most 32 characters")
    s.add_argument("--pin", action="store_true")
    s.add_argument("--workspace", type=int)
    s.add_argument("--monitor")
    for k in ("x", "y", "w", "h"):
        s.add_argument(f"--{k}", type=int)

    s = sp("edit", "replace (or --append to) a note's text")
    s.add_argument("id", type=int)
    s.add_argument("text", nargs="*")
    s.add_argument("--stdin", action="store_true")
    s.add_argument("--append", action="store_true", help="append instead of replace")

    s = sp("show", "one note")
    s.add_argument("id", type=int)

    s = sp("list", "notes, pinned first then most recently updated")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--archived", action="store_true", help="only archived notes")
    g.add_argument("--all", action="store_true", help="live and archived")
    s.add_argument("--workspace", type=int)
    s.add_argument("--color")
    s.add_argument("--pinned", action="store_true")
    s.add_argument("--tag", "-t", action="append", metavar="TAG",
                   help="only notes with this tag (repeat: notes with all of them)")
    s.add_argument("--limit", type=int)
    s.add_argument("--open", action="store_true",
                   help="open the All notes view on the desktop instead (with --tag preselected)")

    s = sp("search", "search by words (FTS5) and meaning (local embeddings), fused")
    s.add_argument("query", nargs="+")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--archived", action="store_true", help="include archived notes")
    s.add_argument("--mode", choices=("fts", "semantic", "hybrid"), default="hybrid",
                   help="fts: words only; semantic: meaning only; hybrid (default): both, "
                        "FTS-only while the model isn't installed")
    s.add_argument("--tag", "-t", action="append", metavar="TAG",
                   help="only notes with this tag (repeat: notes with all of them)")

    s = sp("tag", "add tags to a note")
    s.add_argument("id", type=int)
    s.add_argument("tags", nargs="+", metavar="TAG")

    s = sp("untag", "remove tags from a note")
    s.add_argument("id", type=int)
    s.add_argument("tags", nargs="+", metavar="TAG")

    s = sp("tags", "every tag with its number of notes, most used first")
    s.add_argument("--archived", action="store_true", help="count archived notes too")

    s = sp("ask", "ask your notes (top-k notes + question -> Omarchy's default agent)")
    s.add_argument("question", nargs="+")
    s.add_argument("-k", type=int, default=CHAT_K, help=f"notes to send (default {CHAT_K})")
    s.add_argument("--notes", help="send exactly these note ids instead (comma-separated)")
    s.add_argument("--exclude", help="don't send these note ids (comma-separated)")
    s.add_argument("--dry-run", action="store_true", help="show what would be sent; call nothing")

    s = sp("apply", "apply one action that `ask` proposed (JSON object, arg or stdin)")
    s.add_argument("proposal", nargs="?")

    s = sp("setup", "one-time download of the local embedding model (asks first)")
    s.add_argument("--model", choices=tuple(MODELS), help=f"default {DEFAULT_MODEL}")
    s.add_argument("--yes", "-y", action="store_true", help="agree to the downloads without asking")
    s.add_argument("--status", action="store_true",
                   help="only report what is installed, and whether serve has the model loaded")
    s.add_argument("--idle-unload", type=float, metavar="MINUTES",
                   help="serve drops the model after this long without a search (default 10; 0: never)")
    s.add_argument("--no-backfill", action="store_true", help="don't embed existing notes afterwards")

    sp("backfill", "embed every note whose embedding is missing or stale")

    s = sp("rm", "archive notes (recoverable; see restore, purge)")
    s.add_argument("ids", type=int, nargs="+")

    s = sp("restore", "un-archive notes")
    s.add_argument("ids", type=int, nargs="+")

    sp("undo", "restore the most recently archived note (again: the one before)")

    s = sp("purge", "permanently delete archived notes")
    s.add_argument("ids", type=int, nargs="*")
    s.add_argument("--all-archived", action="store_true", help="purge every archived note")
    s.add_argument("--force", action="store_true", help="also purge notes that aren't archived")

    s = sp("pin", "pin a note")
    s.add_argument("id", type=int)
    s.add_argument("--off", action="store_true", help="unpin")

    s = sp("unpin", "unpin a note")
    s.add_argument("id", type=int)

    s = sp("roll", "roll a note up to its header line (its first line), or --off to open it")
    s.add_argument("id", type=int)
    s.add_argument("--off", action="store_true", help="unroll")

    s = sp("color", "recolour a note")
    s.add_argument("id", type=int)
    s.add_argument("color")

    s = sp("move", "set position / size / stacking / placement")
    s.add_argument("id", type=int)
    for k in ("x", "y", "w", "h", "z"):
        s.add_argument(f"--{k}", type=int)
    s.add_argument("--workspace", type=int)
    s.add_argument("--monitor")
    s.add_argument("--raise", dest="raise_", action="store_true", help="bring to front")

    s = sp("layout", "desktop layout: free, or a waterfall column on the right/left edge "
                     "(no argument: print the current one)")
    s.add_argument("layout", nargs="?", choices=LAYOUTS + ("next",),
                   help="next: the one SUPER + ALT + L would switch to")
    s.add_argument("--width", type=int, help="column width in px (default 320)")
    s.add_argument("--reserve", choices=("on", "off"),
                   help="on: the column pushes windows aside like a bar (default off)")
    s.add_argument("--collapsed", choices=("on", "off", "toggle"),
                   help="collapse the column to a thin strip (SUPER + ALT + W)")
    s.add_argument("--order", metavar="IDS", help="column order, comma-separated note ids")
    s.add_argument("--dock", type=int, metavar="ID", help="put a free note back in the column")
    s.add_argument("--undock", type=int, metavar="ID", help="take a note out of the column (it stays free)")

    s = sp("hub", "publish the hub dashboard card (count, pinned, last edited)")
    s.add_argument("--dry-run", action="store_true", help="print the card, don't call hub")

    s = sp("tidy", "arrange this workspace's unpinned notes in a grid (Hyprland's gaps_out "
                   "apart); --undo puts them back, once")
    s.add_argument("--undo", action="store_true", help="undo the last tidy")
    s.add_argument("--monitor", help="default: the focused monitor")
    s.add_argument("--workspace", type=int, help="default: the monitor's active workspace")

    s = sp("clean", "delete live notes that have no text (no undo; there is nothing to lose)")
    s.add_argument("--older-than", type=float, metavar="SECONDS",
                   help="only notes untouched this long")

    sp("reminders", "pending reminders (`@ 2026-10-07 09:00`, `@tomorrow 9:00` in a note), soonest first")

    s = sp("shell", "call the desktop plugin (omarchy-shell stickies METHOD); "
                    "failures are logged to stickies.log, not swallowed")
    s.add_argument("method", choices=SHELL_METHODS)

    s = sp("integrate", "add the keys (runtime binds) and ~/.local/bin/stickies; "
                        "what the desktop offers on first start")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--yes", action="store_true", help="add them (and remember the answer)")
    g.add_argument("--no", action="store_true", help="don't, and don't offer again")
    g.add_argument("--refresh", action="store_true",
                   help="only restart the shell if the plugin's code changed since the last install")
    s.add_argument("--install", action="store_true",
                   help="what install.sh runs (with --yes): the keys as a drop-in file instead of "
                        "runtime binds, and enable the plugin")

    s = sp("keys", "the keybindings: on (runtime binds, nothing on disk), off, or status")
    s.add_argument("action", nargs="?", choices=("on", "off", "status"), default="status")

    s = sp("uninstall", "remove the keys, the drop-in, ~/.local/bin/stickies and caches "
                        "(notes are kept without --purge)")
    s.add_argument("--purge", action="store_true",
                   help="also delete the notes, the downloaded model and the venv (no undo)")

    sp("colors", "the named palette")
    sp("serve", "JSON-lines request/response + change events on stdin/stdout")
    return p


def run(args):
    """Execute a parsed command; returns (json_payload, human_text)."""
    if args.cmd == "setup":
        # Reading the setting never creates a DB (install.sh runs --status).
        store = (Store() if args.idle_unload is not None
                 or os.path.exists(os.path.join(state_dir(), "stickies.db")) else None)
        try:
            if args.idle_unload is not None:
                store.set_settings({"idle_unload": args.idle_unload})
            idle, source = idle_unload(store)
        finally:
            if store is not None:
                store.close()
        if args.status or args.idle_unload is not None:
            r = setup_status(args.model)
        else:
            r = run_setup(args.model, yes=args.yes, backfill=not args.no_backfill)
        ready = not r["model_missing"] and (r["venv_ready"] or r["deps_in_this_python"])
        # serve's live state: is the model in memory right now?
        r = dict(r, idle_unload=idle, idle_unload_from=source, serve=ask_serve({"op": "model"}))
        text = (f"model {r['model']} ({r['about']}): "
                + ("installed" if not r["model_missing"] else "missing " + ", ".join(r["model_missing"]))
                + f"\nvenv {r['venv']}: {'ready' if r['venv_ready'] else 'not set up'}"
                + f"\nsemantic search: {'on' if ready else 'off (run `stickies setup`)'}"
                + "\nidle unload: " + (f"after {idle:g} min without a search" if idle else "never (kept loaded)")
                + (" ($STICKIES_IDLE_UNLOAD)" if source == "env" else "")
                + "\n" + fmt_serve_model(r["serve"]))
        b = r.get("backfill")
        if b:
            text += "\n" + (f"backfill failed: {b['error']}" if "error" in b
                            else f"embedded {b.get('embedded')} of {b.get('notes')} notes")
        return dict(r, ready=ready), text
    if args.cmd == "shell":
        try:
            return call_shell(args.method)
        except StickiesError as e:
            if "Function not found" in str(e):  # a key, after an update: say so where it is seen
                try:
                    send_notification("Stickies updated",
                                      f"Restart the shell to load {plugin_version()}: omarchy-restart-shell")
                except Exception:
                    pass
            raise
    if args.cmd == "integrate":
        if args.refresh:
            r = refresh_shell()
            return r, fmt_refresh(r)
        if args.install and not args.yes:
            raise StickiesError("--install goes with --yes (it is what install.sh runs)")
        r = (integrate(True, restart=True, install=args.install) if args.yes
             else integrate(False, restart=True) if args.no else integration_status())
        return r, fmt_integration(r)
    if args.cmd == "list" and args.open:
        if args.archived or args.all:
            raise StickiesError("the All notes view shows live notes; drop --archived / --all")
        tags = normalize_tags(args.tag)
        r, _ = call_shell("listTag", args=[" ".join(tags)]) if tags else call_shell("list")
        return dict(r, tags=tags), "opened All notes" + (" (" + ", ".join(tags) + ")" if tags else "")
    if args.cmd == "keys":
        if args.action == "status":
            r = integration_status()
            return r, fmt_integration(r)
        if args.action == "on":
            d = read_integration()
            d.update(asked=True, keys=True)
            write_integration(d)
            r = keys_on()
            return r, fmt_keys(r)
        d = read_integration()
        r = keys_off()
        if d:
            d["keys"] = False
            write_integration(d)
        return r, ("removed " + ", ".join(r["removed"])) if r["removed"] else "no runtime keys to remove"
    if args.cmd == "uninstall":
        r = uninstall(purge=args.purge)
        lines = r["done"] or ["nothing to remove"]
        if r["kept"]:
            lines.append(f"kept your notes in {r['kept']} (stickies uninstall --purge deletes them)")
        lines += [f"to remove the plugin folder too: {c}" for c in r["next"]]
        lines += [f"left {x['path']}: {x['why']}" for x in r["left"]]
        return r, "\n".join(lines)
    if args.cmd == "serve":
        store = Store()
        try:
            serve(store)
        finally:
            store.close()
        return None, None

    store = Store()
    try:
        c = args.cmd
        if c == "add":
            cut = False
            if args.clipboard:
                body, cut = clipboard_text()
            else:
                body = read_body(args.text, args.stdin)
            n = store.add(body, color=args.color, pinned=args.pin,
                          workspace=args.workspace, monitor=args.monitor,
                          x=args.x, y=args.y, w=args.w, h=args.h, tags=args.tag)
            return n, f"added #{n['id']}" + (f" (cut to the first {CLIP_MAX:,} characters)" if cut else "")
        if c == "edit":
            initial = "" if args.text or args.stdin else store.get(args.id)["body"]
            text = read_body(args.text, args.stdin, initial=initial)
            n = store.edit(args.id, append=text) if args.append else store.edit(args.id, body=text)
            return n, f"edited #{n['id']}"
        if c == "show":
            n = store.get(args.id)
            return n, fmt_note_full(n)
        if c == "list":
            ns = store.list(archived=args.archived, all=args.all, workspace=args.workspace,
                            color=args.color, pinned=True if args.pinned else None, limit=args.limit,
                            tags=args.tag)
            filtered = (args.archived or args.workspace is not None or args.color or args.pinned or args.tag)
            return ns, "\n".join(fmt_note_line(n) for n in ns) or (
                "no notes" if filtered or store.list(all=True, limit=1) else FIRST_RUN)
        if c == "search":
            hits = getattr(args, "served", None)
            if hits is None:
                hits = store.search(" ".join(args.query), limit=args.limit, archived=args.archived,
                                    mode=args.mode, tags=args.tag)
            return hits, "\n".join(fmt_hit(h) for h in hits) or "no matches"
        if c == "ask":
            return run_ask(store, args)
        if c == "apply":
            raw = args.proposal if args.proposal is not None else sys.stdin.read()
            try:
                p = json.loads(raw)
            except ValueError as e:
                raise StickiesError(f"proposal is not JSON: {e}")
            r = apply_proposal(store, p)
            what = (f"note #{r['note']['id']}" if "note" in r else "hub todo")
            return r, f"applied {r['action']}: {what}"
        if c == "backfill":
            emb = store.embedder
            if emb is None:
                raise StickiesError("can't embed: " + (embedder_problem() or "model failed to load"))
            t = time.perf_counter()
            n = store.embed_notes(emb)
            ms = round((time.perf_counter() - t) * 1000, 1)
            r = dict(store.embedding_stats(emb.name), newly_embedded=n, ms=ms)
            return r, (f"embedded {n} notes with {emb.name} in {ms / 1000:.1f} s "
                       f"({r['embedded']} of {r['notes']} done)")
        if c in ("rm", "restore"):
            fn = store.archive if c == "rm" else store.restore
            ns = [fn(i) for i in args.ids]
            verb = "archived" if c == "rm" else "restored"
            return ns, "\n".join(f"{verb} #{n['id']}" for n in ns)
        if c == "purge":
            ids = list(args.ids)
            if args.all_archived:
                ids += [n["id"] for n in store.list(archived=True)]
            if not ids:
                raise StickiesError("purge needs ids or --all-archived")
            res = [store.purge(i, force=args.force) for i in ids]
            return res, "\n".join(f"purged #{r['id']}" for r in res)
        if c in ("tag", "untag"):
            n = (store.tag if c == "tag" else store.untag)(args.id, args.tags)
            return n, f"#{n['id']} tags: " + (" ".join(n["tags"]) or "(none)")
        if c == "tags":
            ts = store.all_tags(archived=args.archived)
            return ts, "\n".join(f"{t['count']:>5}  {t['tag']}" for t in ts) or \
                "no tags yet (stickies tag <id> work, or stickies add --tag work ...)"
        if c in ("pin", "unpin"):
            on = c == "pin" and not args.off
            n = store.pin(args.id, on)
            return n, f"{'pinned' if on else 'unpinned'} #{n['id']}"
        if c == "color":
            n = store.color(args.id, args.color)
            return n, f"#{n['id']} is {n['color']}"
        if c == "move":
            n = store.move(args.id, x=args.x, y=args.y, w=args.w, h=args.h, z=args.z,
                           workspace=args.workspace, monitor=args.monitor, raise_=args.raise_)
            return n, f"#{n['id']} at {n['x']},{n['y']} {n['w']}x{n['h']} z={n['z']}"
        if c == "clean":
            ids = store.discard_empty(older_than=args.older_than)
            return {"discarded": ids}, ("deleted " + " ".join(f"#{i}" for i in ids)
                                        if ids else "no empty notes")
        if c == "layout":
            return run_layout(store, args)
        if c == "undo":
            n = store.undo_archive()
            return n, f"restored #{n['id']}: {_first_line(n['body']) or '(empty)'}"
        if c == "roll":
            n = store.roll(args.id, not args.off)
            return n, f"#{n['id']} {'rolled up' if n['rolled'] else 'unrolled'}"
        if c == "tidy":
            if args.undo:
                r = store.tidy_undo()
                k = len(r["restored"])
                return r, f"put {k} note{'' if k == 1 else 's'} back"
            scr = hypr_screen(args.monitor)
            ws = args.workspace if args.workspace is not None else scr["workspace"]
            r = store.tidy(ws, scr["monitor"], scr["width"], scr["height"], gaps=hypr_gaps_out(),
                           reserved=scr["reserved"], primary=scr["primary"])
            return r, (f"arranged {len(r['moved'])} notes on workspace {ws} ({scr['monitor']}); "
                       "`stickies tidy --undo` puts them back" if r["moved"]
                       else f"no unpinned notes on workspace {ws} ({scr['monitor']})")
        if c == "reminders":
            rs = store.reminders()
            return rs, "\n".join(f"#{r['note_id']:<4} {_local_stamp(r['due'])}  {r['text']}" for r in rs) \
                or "no reminders (write `@tomorrow 9:00` or `@ 2026-10-07 09:00` in a note)"
        if c == "colors":
            return COLORS, "\n".join(f"{k:<7} {v}" for k, v in COLORS.items())
        if c == "hub":
            if args.dry_run:
                s = hub_summary(store)
                cmd = ["hub", *hub_args(s)]
                return dict(s, published=False, command=cmd), " ".join(shlex.quote(a) for a in cmd)
            s = publish_hub(store, explicit=True)
            return s, (f"hub card: {s['count']} notes, {s['pinned']} pinned" if s["published"]
                       else "hub publishing is off ($STICKIES_HUB is empty)")
        raise StickiesError(f"unknown command {c}")
    finally:
        if args.cmd in WRITES:
            publish_hub(store, wait=False)
        store.close()


def fmt_layout(st):
    if st["layout"] == "free":
        return "layout: free"
    return (f"layout: {st['layout']} ({st['waterfall_width']} px, "
            f"reserve {'on' if st['waterfall_reserve'] else 'off'}, "
            f"{'collapsed' if st['waterfall_collapsed'] else 'expanded'})")


def run_layout(store, args):
    st = store.settings()
    values = {}
    if args.layout == "next":
        values["layout"] = LAYOUTS[(LAYOUTS.index(st["layout"]) + 1) % len(LAYOUTS)]
    elif args.layout:
        values["layout"] = args.layout
    if args.width is not None:
        values["waterfall_width"] = args.width
    if args.reserve:
        values["waterfall_reserve"] = args.reserve == "on"
    if args.collapsed:
        values["waterfall_collapsed"] = (not st["waterfall_collapsed"] if args.collapsed == "toggle"
                                         else args.collapsed == "on")
    if args.order is not None:
        values["waterfall_order"] = _ids(args.order)
    if args.dock is not None or args.undock is not None:
        free = list(st["waterfall_free"])
        for nid, docked in ((args.dock, True), (args.undock, False)):
            if nid is not None:
                store.get(nid)
                free = [i for i in free if i != nid] + ([] if docked else [nid])
        values["waterfall_free"] = free
    if values:
        st = store.set_settings(values)
    return st, fmt_layout(st)


def _ids(text):
    try:
        return [int(x.strip().lstrip("#")) for x in (text or "").split(",") if x.strip()]
    except ValueError:
        raise StickiesError(f"bad note ids {text!r} (comma-separated numbers)")


def run_ask(store, args):
    """`stickies ask`. On a terminal (no --json) the answer streams to
    stdout as it arrives and the returned text is only the trailer: notes
    sent, cited, and each proposal with the `stickies apply` that applies it."""
    question = " ".join(args.question)
    ids = _ids(args.notes) if args.notes else None
    if args.exclude:
        drop = set(_ids(args.exclude))
        if ids is None:
            ids = [n["id"] for n in chat_notes(store, question, args.k + len(drop))][:args.k + len(drop)]
            ids = [i for i in ids if i not in drop][:args.k]
        else:
            ids = [i for i in ids if i not in drop]
    if args.dry_run:
        r = ask(store, question, ids=ids, k=args.k, dry_run=True)
        return r, (r["prompt"] + "\n\n(dry run: nothing sent) notes: "
                   + (" ".join(f"#{n['id']}" for n in r["sent"]) or "none"))
    on_delta = None
    if not getattr(args, "json", False):
        buf, shown = [], [""]

        def on_delta(chunk):
            buf.append(chunk)
            vis = visible_answer("".join(buf), partial=True)
            if vis.startswith(shown[0]) and len(vis) > len(shown[0]):
                sys.stdout.write(vis[len(shown[0]):])
                sys.stdout.flush()
                shown[0] = vis
    r = ask(store, question, ids=ids, k=args.k, on_delta=on_delta)
    lines = ["", "", "sent: " + (" ".join(f"#{n['id']}" for n in r["sent"]) or "no notes")
             + ("   cited: " + " ".join(f"#{i}" for i in r["citations"]) if r["citations"] else "")]
    for n, p in enumerate(r["proposals"], 1):
        q = {k: v for k, v in p.items() if k != "summary"}
        lines.append(f"proposed {n}: {p['summary']}\n  apply: stickies apply "
                     + shlex.quote(json.dumps(q, ensure_ascii=False)))
    for b in r["rejected"]:
        lines.append(f"rejected proposal: {b['error']}")
    return r, "\n".join(lines)


# Commands that change what the hub card shows.
WRITES = {"add", "edit", "rm", "restore", "undo", "purge", "tidy", "clean", "pin", "unpin", "color",
          "move", "roll", "apply", "tag", "untag"}


# Commands that load the embedding model when it is installed.
EMBEDS = {"search", "serve", "backfill", "ask"}


def maybe_reexec(args, argv):
    """Re-run this command under the .venv's Python when it needs
    onnxruntime/tokenizers, this Python lacks them, and the model is
    downloaded. Otherwise the command runs here (FTS-only if need be)."""
    if (args.cmd not in EMBEDS or getattr(args, "mode", None) == "fts"
            or os.environ.get("STICKIES_REEXEC") or os.environ.get("STICKIES_SEMANTIC") == "0"):
        return
    py = venv_python()
    try:
        if not py or missing_model_files(model_name()) or deps_importable():
            return
    except StickiesError:
        return
    os.environ["STICKIES_REEXEC"] = "1"
    os.execv(py, [py, os.path.realpath(__file__), *argv])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    if (args.cmd == "search" and args.mode != "fts" and os.environ.get("STICKIES_SEMANTIC") != "0"
            and not os.environ.get("STICKIES_REEXEC")):
        args.served = search_via_serve(" ".join(args.query), args.limit, args.archived, args.mode,
                                       tags=args.tag)
    if getattr(args, "served", None) is None:
        maybe_reexec(args, argv)
    as_json = getattr(args, "json", False)
    try:
        payload, text = run(args)
    except (StickiesError, sqlite3.Error) as e:
        if as_json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"stickies: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # a bug: the traceback goes to the log, plain words to the person
        import traceback
        Store.log_line(f"{' '.join(argv)[:200]} failed:\n{traceback.format_exc()}")
        msg = f"something went wrong ({type(e).__name__}: {e}); details in {os.path.join(state_dir(), 'stickies.log')}"
        if as_json:
            print(json.dumps({"error": msg}))
        else:
            print(f"stickies: {msg}", file=sys.stderr)
        return 1
    if args.cmd == "serve":
        return 0
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    elif text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
