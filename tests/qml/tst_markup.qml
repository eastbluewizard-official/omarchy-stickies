import QtQuick
import QtTest
import "../.." as Stickies

// MarkupStyler in a real TextEdit, offscreen (tests/test_markup.py runs it
// with qmltestrunner): the document keeps the note's exact text, markers
// show faint while editing and collapse when not, the caret and selection
// survive every restyle, Enter / undo / a rich paste behave, and positions
// (what the checkbox overlays use) follow the styled layout.
Item {
  id: root
  width: 420; height: 600

  Flickable {
    id: flick
    width: 300; height: 560
    contentHeight: ed.contentHeight
    TextEdit {
      id: ed
      width: 300
      focus: true
      wrapMode: TextEdit.Wrap
      textFormat: TextEdit.RichText
      font.pixelSize: 14
      onTextChanged: if (!st.busy) st.edited()
      onInputMethodComposingChanged: if (!inputMethodComposing) st.edited()
    }
  }
  Stickies.MarkupStyler { id: st; editor: ed; flick: flick; ink: "#202020"; hidden: false }

  TestCase {
    name: "markup"
    when: windowShown

    function init() {
      st.hidden = false
      st.reset()
      st.setText("")
      ed.forceActiveFocus()
    }
    function px(i) { return ed.positionToRectangle(i).x }
    function rect(i) { return ed.positionToRectangle(i) }
    function settle() { wait(30) }

    function test_text_round_trips() {
      var t = "  two  spaces\n\n\ttab <b>x</b> &amp; \"q\" **b**\n# h\n- [ ] **milk**\n"
      st.setText(t)
      compare(st.read().replace(/\u2029/g, "\n"), t)
      compare(ed.length, t.length)
      compare(st.plain, t)
      st.hidden = true
      settle()
      compare(st.read().replace(/\u2029/g, "\n"), t)
    }

    function test_markers_faint_while_editing_and_collapsed_otherwise() {
      var t = "a **bold** b"
      st.setText(t)
      verify(px(4) - px(2) > 6, "markers visible while editing")
      st.hidden = true
      settle()
      verify(px(4) - px(2) < 2, "markers collapse: " + (px(4) - px(2)))
      compare(st.plain, t)
    }

    function test_looks() {
      st.setText("bold **bold**\niiii `iiii`\nplain\n# Head\n~~s~~ __u__ *i*")
      // Bold glyphs are wider than regular ones.
      verify(px(13 - 2) - px(7) > px(4) - px(0), "bold wider")
      // Code is monospace: "iiii" gets wider.
      var line2 = 14
      verify(px(line2 + 10) - px(line2 + 6) > px(line2 + 4) - px(line2) + 2, "monospace code")
      // A heading line is taller.
      verify(rect(t2(3)).height > rect(t2(2)).height, "heading taller")
    }
    // Start of line n of the styler's text.
    function t2(n) {
      var ls = st.plain.split("\n"), s = 0
      for (var i = 0; i < n; i++) s += ls[i].length + 1
      return s
    }

    function test_hanging_indent() {
      var t = "- " + "word ".repeat(30) + "\n1. " + "word ".repeat(30) + "\nplain " + "word ".repeat(30)
      st.setText(t)
      var lines = t.split("\n")
      for (var n = 0; n < 3; n++) {
        var s = t2(n), y0 = rect(s).y, w = -1
        for (var i = s; i < s + lines[n].length; i++) if (rect(i).y > y0 + 1) { w = rect(i).x; break }
        verify(w >= 0, "line " + n + " wraps")
        if (n < 2) fuzzyCompare(w, px(s + (n === 0 ? 2 : 3)), 1.5)  // under the text, not the bullet
        else fuzzyCompare(w, 0, 0.5)
      }
    }

    function test_backgrounds_and_dots() {
      st.setText("x ==hi== and `code`\n- bullet")
      compare(st.decos.length, 2)
      fuzzyCompare(st.decos[0].x, px(4) - 1, 0.5)
      verify(!st.decos[0].code && st.decos[1].code)
      compare(st.dots.length, 0)  // dots only while the markers are hidden
      st.hidden = true
      settle()
      compare(st.dots.length, 1)
    }

    function test_typing_styles_live() {
      ed.cursorPosition = 0
      for (var c of "say **hi** now") keyClick(c)
      compare(st.plain, "say **hi** now")
      compare(ed.cursorPosition, 14)
      verify(px(8) - px(6) > 0)
      var plainHi = st.metrics.advanceWidth("hi")
      verify(px(8) - px(6) > plainHi, "typed text turned bold")
      verify(st.lastMs < 50, "restyle " + st.lastMs + " ms")
      // Deleting a marker unstyles it again.
      ed.cursorPosition = 10
      keyClick(Qt.Key_Backspace)
      compare(st.plain, "say **hi* now")
      fuzzyCompare(px(8) - px(6), plainHi, 0.6)
    }

    function test_enter_in_a_list_keeps_caret_and_fixes_indent() {
      var t = "- one two\nafter"
      st.setText(t)
      ed.cursorPosition = 5
      keyClick(Qt.Key_Return)
      compare(st.plain, "- one\n two\nafter")
      compare(ed.cursorPosition, 6)
      compare(st.shape, [2 * 0 + st.hangPx("- one", 2), 0, 0])
      keyClick(Qt.Key_Backspace)
      compare(st.plain, t)
      compare(ed.cursorPosition, 5)
      // Enter in plain text: no rebuild needed, still consistent.
      ed.cursorPosition = t.length
      keyClick(Qt.Key_Return)
      keyClick("x")
      compare(st.plain, t + "\nx")
      compare(st.lines.join("\n"), st.plain)
      compare(st.read().replace(/\u2029/g, "\n"), st.plain)
    }

    function test_undo_redo() {
      ed.cursorPosition = 0
      for (var c of "one two") keyClick(c)
      // A word at a time: the space starts the next step.
      verify(st.undo())
      compare(st.plain, "one")
      verify(st.undo())
      compare(st.plain, "")
      verify(!st.undo())
      verify(st.redo())
      compare(st.plain, "one")
      compare(ed.cursorPosition, 3)
      verify(st.redo())
      compare(st.plain, "one two")
      st.replace("**one** two", 2, 5)
      compare(ed.selectedText, "one")
      verify(st.undo())
      compare(st.plain, "one two")
    }

    function test_caret_and_selection_survive_a_restyle() {
      st.setText("some **bold** and `code` here")
      ed.select(20, 8)  // backwards: anchor 20, caret 8
      st.hidden = true
      settle()
      compare(ed.selectionStart, 8)
      compare(ed.selectionEnd, 20)
      compare(ed.cursorPosition, 8)
      st.hidden = false
      settle()
      compare(ed.cursorPosition, 8)
      fuzzyCompare(ed.cursorRectangle.x, px(8), 0.5)
    }

    function test_a_click_on_hidden_markup_lands_on_the_letter() {
      st.setText("a **bold** word")
      st.hidden = true
      settle()
      var r = rect(5)  // the "o" of bold, markers collapsed
      ed.focus = false
      mouseClick(ed, r.x + 1, r.y + r.height / 2)
      compare(ed.cursorPosition, 5)
      settle()
      compare(ed.cursorPosition, 5)
    }

    function test_rich_paste_is_flattened() {
      st.setText("ab")
      ed.insert(1, "<h1 style='margin-left:40px'>BIG</h1><p><b>bold</b> two</p>")
      settle()
      verify(st.plain.indexOf("BIG") >= 0)
      compare(st.read().replace(/\u2029/g, "\n"), st.plain)
      fuzzyCompare(rect(2).height, rect(0).height, 1)
      compare(st.shape.length, st.plain.split("\n").length)
    }

    function test_checkbox_position_follows_the_layout() {
      st.setText("# **Title**\n- [ ] **milk** and ==eggs==\n[x] done")
      var box = st.plain.indexOf("[ ]")
      var before = rect(box)
      st.hidden = true
      settle()
      var after = rect(box)
      // The hidden bullet keeps its width: the box (and the indent) stay put.
      fuzzyCompare(after.x, before.x, 0.5)
      compare(after.y, rect(t2(1)).y)
      compare(st.plain.charAt(box), "[")
    }

    // Whatever path an edit took (nothing, a line restyled, a block
    // replaced), the result is what building the whole text gives.
    function test_incremental_equals_a_full_build() {
      st.setText("# Title\n- one **two** and ==hi== `c`\nplain *it* text\n1. num\n- [ ] box ~~x~~")
      function at(text, offset) { ed.cursorPosition = st.plain.indexOf(text) + offset }
      at("two", 1); for (var c of "xy") keyClick(c)         // inside bold: inherits
      at("hi==", 4); keyClick("z")                            // after a marker
      at("plain", 0); keyClick("*")                           // opens nothing (yet)
      at("text", 4); keyClick("*")                            // now *plain *it* text* pairs differently
      at("num", 3); keyClick(Qt.Key_Return); keyClick("-"); keyClick(" "); keyClick("n")  // a new bullet
      at("box", 0); keyClick(Qt.Key_Backspace)                // joins nothing: just a space gone
      at("# Title", 7); keyClick(Qt.Key_Return)               // a line after the heading
      at("- one", 0); keyClick(Qt.Key_Delete)                 // no longer a bullet
      st.hidden = true
      settle()
      at("Title", 2); keyClick("q")
      var text = st.plain
      compare(st.read().replace(/\u2029/g, "\n"), text)
      var rects = [], n = ed.length
      for (var i = 0; i <= n; i++) { var r = rect(i); rects.push([r.x, r.y, r.height].join()) }
      var decos = JSON.stringify(st.decos), shape = JSON.stringify(st.shape)
      st.setText(text)
      for (i = 0; i <= n; i++) {
        var r2 = rect(i)
        compare([r2.x, r2.y, r2.height].join(), rects[i], "position " + i + " of " + JSON.stringify(text))
      }
      compare(JSON.stringify(st.decos), decos)
      compare(JSON.stringify(st.shape), shape)
    }

    function test_restyle_cost_on_a_big_note() {
      var parts = [], i = 0
      while (parts.join("\n").length < 2000) {
        parts.push(["# Heading " + i, "- item with **bold** and *it* and `code` here " + i,
                    "1. numbered ==highlight== and ~~gone~~ and __under__ words " + i,
                    "- [ ] check **milk** and eggs, a lone * star " + i,
                    "plain text line with some \\*escaped\\* stars and <b>&amp; " + i][i % 5])
        i++
      }
      st.setText(parts.join("\n"))
      ed.cursorPosition = st.plain.indexOf("and *it*") + 3
      wait(50)  // painted once, as a note on screen is
      var worst = 0
      var all = []
      for (var c of "typing *more* here") { keyClick(c); all.push(st.lastMs); worst = Math.max(worst, st.lastMs) }

      verify(worst < 16, "restyle per key (ms): " + JSON.stringify(all))
      compare(st.read().replace(/\u2029/g, "\n"), st.plain)
    }
  }
}
