"""Markup in notes: one parser in markup.js (the desktop's live styling and
plain titles) and its twin in stickies.py (snippets, titles, reminders,
embeddings), run over the same table; the shortcuts' text edits; what
search, titles and reminders show (plain words); and the shape of the live
editor. The styling itself, the caret and the checkbox overlays are
checked in a real TextEdit by tests/qml/tst_markup.qml (qmltestrunner,
offscreen)."""

import json
import os
import re
import shutil
import subprocess
import sys
import unittest

from helpers import ROOT, SCRIPT, TempState, stickies
from test_shell import qml_sources

NODE = shutil.which("node")
# Qt 6's (a plain `qmltestrunner` on PATH may be Qt 5's).
QMLTEST = next((p for p in ("/usr/lib/qt6/bin/qmltestrunner", shutil.which("qmltestrunner6"),
                            shutil.which("qmltestrunner-qt6")) if p and os.path.exists(p)), None)


def markup_js():
    with open(os.path.join(ROOT, "markup.js")) as f:
        return "\n".join(l for l in f.read().splitlines() if not l.startswith(".pragma"))


def node(script):
    p = subprocess.run([NODE, "-e", markup_js() + "\n" + script], capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError(p.stderr)
    return json.loads(p.stdout)


# line -> its plain words (titles, snippets, embeddings).
TABLE = [
    ("**bold** and *it* x", "bold and it x"),
    ("__under__ ~~gone~~ ==hl== `code`", "under gone hl code"),
    # Pairs only, on one line; a lone marker stays literal.
    ("a lone * star and ** two", "a lone * star and ** two"),
    ("**not closed", "**not closed"),
    ("2 * 3 * 4", "2 * 3 * 4"),
    ("a == b == c", "a == b == c"),
    ("****", "****"),
    ("** spaced **", "** spaced **"),
    ("snake_case_name", "snake_case_name"),
    # Escapes.
    ("\\*not\\* it", "*not* it"),
    ("a \\` tick", "a ` tick"),
    ("C:\\Users\\me", "C:\\Users\\me"),
    ("\\", "\\"),
    # Inside code nothing else is parsed.
    ("`**code**` **b**", "**code** b"),
    ("`a\\*b` c", "a\\*b c"),
    ("`` empty", "`` empty"),
    # Nesting.
    ("***bi***", "bi"),
    ("*a **b** c*", "a b c"),
    ("**bold *it***", "bold it"),
    ("**a*", "*a"),
    ("*a**b**", "*ab"),
    ("__init__ x", "init x"),
    # Line level.
    ("# Heading **x**", "Heading x"),
    ("## Sub", "Sub"),
    ("### three", "### three"),
    ("#tag", "#tag"),
    ("- item *i*", "item i"),
    ("* item", "item"),
    ("  - nested", "  nested"),
    ("-not a bullet", "-not a bullet"),
    ("---", "---"),
    ("1. one **1**", "1. one 1"),
    ("12) twelve", "12) twelve"),
    # Checkboxes keep their box (the card draws it) and take markup.
    ("[ ] **milk**", "[ ] milk"),
    ("- [x] done ~~old~~", "[x] done old"),
    ("+ [ ] plus", "[ ] plus"),
    # User text that looks like HTML is just text.
    ("<b>&amp;</b> **x**", "<b>&amp;</b> x"),
    ("café **crème** 😀 *ok*", "café crème 😀 ok"),
    ("", ""),
]


class ParserTest(unittest.TestCase):
    def test_python_table(self):
        for line, want in TABLE:
            self.assertEqual(stickies.plain_line(line), want, line)

    @unittest.skipUnless(NODE, "node not installed")
    def test_js_matches_python(self):
        lines = [l for l, _ in TABLE] + ["- [ ] **a** *b* `c` ==d== ~~e~~ __f__", "# **x** and `y`"]
        js = node("console.log(JSON.stringify(" + json.dumps(lines) + ".map(l => { const p = parseLine(l);"
                  " return [plainLine(l), p.kind, p.hang, p.bullet, p.box, p.style, p.role] })))")
        for line, got in zip(lines, js):
            kind, bullet, box, hang, style, role = stickies.parse_line(line)
            self.assertEqual(got[0], stickies.plain_line(line), line)
            self.assertEqual(got[1:5], [kind, hang, bullet, box], line)
            if line.isascii():  # JS indexes UTF-16 units, Python code points
                self.assertEqual(got[5], style, line)
                self.assertEqual(got[6], role, line)

    @unittest.skipUnless(NODE, "node not installed")
    def test_styles(self):
        got = node(r"""const l = "# **b** *i* __u__ ~~s~~ ==h== `c` \\*";
const p = parseLine(l);
console.log(JSON.stringify(runs(p).map(r => [l.slice(r.a, r.b), r.style, r.role])))""")
        H = 64
        self.assertEqual(got, [
            ["# ", H, 2], ["**", H, 1], ["b", H | 1, 0], ["**", H, 1], [" ", H, 0],
            ["*", H, 1], ["i", H | 2, 0], ["*", H, 1], [" ", H, 0],
            ["__", H, 1], ["u", H | 4, 0], ["__", H, 1], [" ", H, 0],
            ["~~", H, 1], ["s", H | 8, 0], ["~~", H, 1], [" ", H, 0],
            ["==", H, 1], ["h", H | 16, 0], ["==", H, 1], [" ", H, 0],
            ["`", H, 1], ["c", H | 32, 0], ["`", H, 1], [" ", H, 0],
            ["\\", H, 1], ["*", H, 0]])

    @unittest.skipUnless(NODE, "node not installed")
    def test_hanging_indent_prefix(self):
        got = node(r"""console.log(JSON.stringify(["- a", "  * b", "1. c", "10) d", "- [ ] e", "[x] f", "# g", "h"]
  .map(l => { const k = lineKind(l); return [k.kind, k.hang] })))""")
        self.assertEqual(got, [["bullet", 2], ["bullet", 4], ["number", 3], ["number", 4], ["check", 6],
                               ["check", 4], ["h1", 0], ["", 0]])

    @unittest.skipUnless(NODE, "node not installed")
    def test_html_is_escaped_before_tags(self):
        got = node(r"""const l = '<b>x</b> & "q" **<i>y</i>**';
console.log(JSON.stringify(lineHtml(l, runs(parseLine(l)), r => r.style ? "font-weight:700;" : "")))""")
        self.assertNotIn("<b>", got)
        self.assertNotIn("<i>", got)
        self.assertIn("&lt;b&gt;x&lt;/b&gt; &amp; &quot;q&quot; **", got)
        self.assertIn('<span style="font-weight:700;">&lt;i&gt;y&lt;/i&gt;</span>', got)
        # Markers stay in the document (positions are the note's own).
        self.assertTrue(got.endswith("**"), got)

    def test_long_line_is_quick(self):
        import time
        line = "*a " * 700  # every opener searches the rest of the line
        t = time.perf_counter()
        stickies.plain_line(line)
        self.assertLess(time.perf_counter() - t, 2.0)


@unittest.skipUnless(NODE, "node not installed")
class ShortcutTest(unittest.TestCase):
    def wrap(self, text, a, b, m):
        return node(f"console.log(JSON.stringify(toggleWrap({json.dumps(text)}, {a}, {b}, {json.dumps(m)})))")

    def test_wrap_and_unwrap(self):
        r = self.wrap("buy milk now", 4, 8, "**")
        self.assertEqual(r, {"text": "buy **milk** now", "a": 6, "b": 10})
        self.assertEqual(self.wrap(r["text"], r["a"], r["b"], "**"), {"text": "buy milk now", "a": 4, "b": 8})
        # Selected with its markers: unwrapped too.
        self.assertEqual(self.wrap("buy **milk** now", 4, 12, "**"), {"text": "buy milk now", "a": 4, "b": 8})
        # Spaces at the ends stay outside the markers.
        self.assertEqual(self.wrap("buy milk now", 3, 9, "*")["text"], "buy *milk* now")
        # Italic inside bold is wrapped, not mistaken for half of the bold.
        self.assertEqual(self.wrap("**milk**", 2, 6, "*")["text"], "***milk***")
        self.assertEqual(self.wrap("***milk***", 3, 7, "*")["text"], "**milk**")
        self.assertEqual(self.wrap("x ==hi== y", 4, 6, "==")["text"], "x hi y")

    def test_no_selection_inserts_a_pair(self):
        self.assertEqual(self.wrap("ab", 1, 1, "`"), {"text": "a``b", "a": 2, "b": 2})
        self.assertEqual(self.wrap("a``b", 2, 2, "`"), {"text": "ab", "a": 1, "b": 1})
        self.assertEqual(self.wrap("", 0, 0, "~~"), {"text": "~~~~", "a": 2, "b": 2})

    def test_lines_wrap_one_by_one(self):
        r = self.wrap("one\n\n  two", 0, 10, "**")
        self.assertEqual(r["text"], "**one**\n\n  **two**")
        self.assertEqual(self.wrap(r["text"], r["a"], r["b"], "**")["text"], "one\n\n  two")

    def test_checkbox_toggle(self):
        def cb(text, pos):
            return node(f"console.log(JSON.stringify(toggleCheckbox({json.dumps(text)}, {pos})))")
        self.assertEqual(cb("milk", 2), {"text": "[ ] milk", "pos": 6})
        self.assertEqual(cb("[ ] milk", 6), {"text": "milk", "pos": 2})
        self.assertEqual(cb("a\n- bread\nc", 5), {"text": "a\n- [ ] bread\nc", "pos": 9})
        self.assertEqual(cb("a\n- [x] bread", 13), {"text": "a\n- bread", "pos": 9})
        self.assertEqual(cb("x\n", 2), {"text": "x\n[ ] ", "pos": 6})


class PlainWordsTest(TempState):
    """Titles, snippets, reminders and the All notes filter show plain
    words; the body, --json and chat keep the markers."""

    def setUp(self):
        super().setUp()
        self.s = stickies.Store(embedder=None)

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def test_fts_matches_and_snippet_has_no_markers(self):
        n = self.s.add("# Groceries\n- [ ] **milk** and ==eggs==\n`code` bits")
        hits = self.s.search("milk", mode="fts")
        self.assertEqual([h["id"] for h in hits], [n["id"]])
        snip, spans = hits[0]["snippet"], hits[0]["highlights"]
        self.assertNotIn("*", snip)
        self.assertNotIn("=", snip)
        self.assertNotIn("#", snip)
        self.assertEqual([snip[a:b] for a, b in spans], ["milk"])
        self.assertEqual(self.s.search("eggs", mode="fts")[0]["highlights"],
                         [[snip.index("eggs"), snip.index("eggs") + 4]])
        self.assertEqual(hits[0]["body"], "# Groceries\n- [ ] **milk** and ==eggs==\n`code` bits")

    def test_snippet_window_cuts_a_pair(self):
        # The window starts after "**" opens: still no stray asterisk at its end.
        body = "intro " + " ".join(f"w{i}" for i in range(40)) + " **bold " + " ".join(
            f"x{i}" for i in range(40)) + " target end** tail"
        self.s.add(body)
        h = self.s.search("target", mode="fts", tokens=6)[0]
        self.assertNotIn("*", h["snippet"])
        self.assertEqual([h["snippet"][a:b] for a, b in h["highlights"]], ["target"])

    def test_plain_hit_snippet_maps_offsets(self):
        body = "a **milk** b"
        text, spans = stickies.plain_hit_snippet(body, "a **milk** b", [[4, 8]])
        self.assertEqual((text, spans), ("a milk b", [[2, 6]]))
        text, spans = stickies.plain_hit_snippet("x " * 50 + "**milk** y", "…**milk** y", [[3, 7]])
        self.assertEqual((text, [text[a:b] for a, b in spans]), ("…milk y", ["milk"]))

    def test_titles_and_hay(self):
        n = self.s.add("- [ ] **Pay** the *invoice*\nmore")
        self.assertEqual(stickies.note_title(n["body"]), "Pay the invoice")
        row = stickies.brief_row(self.s.get(n["id"]))
        self.assertEqual(row["title"], "Pay the invoice")
        self.assertIn("pay", row["hay"])  # the filter is a substring match: markers can stay
        self.assertEqual(stickies._first_line("\n# **Big** plan\n"), "Big plan")

    def test_cli_list_shows_plain_titles_and_json_keeps_markers(self):
        n = self.s.add("## **Fees** went up")
        env = dict(os.environ, STICKIES_STATE=self.state, STICKIES_HUB="")
        p = subprocess.run([sys.executable, SCRIPT, "list"], capture_output=True, text=True, env=env, input="")
        self.assertIn("Fees went up", p.stdout)
        p = subprocess.run([sys.executable, SCRIPT, "show", str(n["id"]), "--json"], capture_output=True,
                           text=True, env=env, input="")
        self.assertEqual(json.loads(p.stdout)["body"], "## **Fees** went up")

    def test_reminder_text_is_plain(self):
        body = "**Call** the plumber @ 17:30 about the *leak*\nx"
        spec = stickies.find_reminders(body)[0][0]
        self.assertEqual(stickies.reminder_text(body, spec), "about the leak")
        body = "# **Dentist**\n@ 2030-01-01 09:00"
        spec = stickies.find_reminders(body)[0][0]
        self.assertEqual(stickies.reminder_text(body, spec), "Dentist")

    def test_embeddings_use_plain_words_and_backfill(self):
        a = self.s.add("**milk** and eggs")["id"]
        b = self.s.add("no markup here")["id"]
        self.assertEqual(stickies.doc_text("**milk** and eggs"), "milk and eggs")
        # Vectors an older version stored from the raw body.
        for nid, raw in ((a, "**milk** and eggs"), (b, "no markup here")):
            self.s.db.execute("INSERT INTO embeddings(note_id, model, body_hash, vec) VALUES (?, 'm', ?, x'00')",
                              (nid, stickies.body_hash("m", raw)))
        stale = self.s.stale_embeddings("m")
        self.assertEqual([(i, t) for i, t, _ in stale], [(a, "milk and eggs")])  # only the one with markup


class EditorShapeTest(unittest.TestCase):
    """How the card's editor is put together (the behaviour is in
    tests/qml/tst_markup.qml)."""

    def setUp(self):
        src = qml_sources()
        self.card, self.styler = src["NoteCard.qml"], src["MarkupStyler.qml"]

    def test_editor_is_rich_but_holds_plain_text(self):
        self.assertIn("textFormat: TextEdit.RichText", self.card)
        self.assertIn("MarkupStyler {", self.card)
        self.assertIn("hidden: !editor.activeFocus", self.card)
        # Nothing reads the editor's text (HTML in rich mode) or sets it raw.
        self.assertNotRegex(self.card, r"editor\.text\b(?!Document)")
        self.assertEqual(re.findall(r"editor\.text\b(?!Document).*", self.styler),
                         ['editor.text = html + "</body></html>"'])
        self.assertIn("Markup.lineHtml(line, info(line).runs, cssOf)", self.styler)

    def test_keys(self):
        self.assertIn("Keys.onPressed: event => { if (card.markupKey(event)) event.accepted = true }", self.card)
        for key, m in (("Key_B", '"**"'), ("Key_I", '"*"'), ("Key_U", '"__"'), ("Key_E", '"`"'),
                       ("Key_X", '"~~"'), ("Key_H", '"=="')):
            self.assertIn(f"[Qt.{key}]: {m}", self.card)
        self.assertIn("Markup.toggleCheckbox(styler.plain, editor.cursorPosition)", self.card)
        self.assertIn("styler.undo()", self.card)
        self.assertIn("styler.redo()", self.card)

    def test_titles_strip(self):
        self.assertIn("Markup.plainLine(lines[i]", self.card)
        self.assertIn("Markup.plainLine(", qml_sources()["NoteList.qml"])


@unittest.skipUnless(QMLTEST, "qmltestrunner not installed")
class LiveEditorTest(unittest.TestCase):
    """MarkupStyler in a real TextEdit with real key events, offscreen."""

    def test_qml(self):
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", QT_FORCE_STDERR_LOGGING="1")
        p = subprocess.run([QMLTEST, "-input", os.path.join(ROOT, "tests", "qml", "tst_markup.qml")],
                           capture_output=True, text=True, env=env, timeout=120)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, 0, out[-4000:])
        self.assertIn("0 failed", out)


if __name__ == "__main__":
    unittest.main()
