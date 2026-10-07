import QtQuick
import "markup.js" as Markup

// Live markup for a note's TextEdit (textFormat RichText). The document
// holds exactly the note's text, markers and all: one character per
// character, so cursor positions, positionToRectangle and the checkbox
// overlays stay valid; only the look changes.
//
// A keystroke restyles the line it touched through a TextSelection (fonts
// and colours set on the document; the caret is never moved), and not even
// that when the typed character already took the look of its neighbour
// (most keys). A hanging indent is a block format, which nothing in QML
// sets on an existing block, so a line whose indent changes (Enter in a
// list, a line becoming a bullet, a shortcut) is replaced by a new HTML
// paragraph in place; only a paste, a soft line break or a new text from
// outside builds the whole document again. The caret, selection and
// scroll position are put back after either. Backgrounds (==highlight==,
// `code`) and bullet dots are drawn under the text from `decoModel` /
// `dotModel`, updated line by line.
//
// Formatting edits the document, so the TextEdit's own undo stack would
// replay it: undo and redo are text snapshots kept here instead.
Item {
  id: st
  visible: false

  property TextEdit editor
  property Flickable flick
  property color ink: "black"
  property string family: editor ? editor.font.family : ""
  property real pixelSize: editor ? editor.font.pixelSize : 14
  // Markers hidden (the note isn't being edited) or shown faint.
  property bool hidden: true

  // The note's text as the editor holds it, without formatting.
  property string plain: ""
  // Background rects { x, y, w, h, code } and bullet dots { x, y, w, h }
  // in editor coordinates; the models hold the same as rows (dx, dy, dw,
  // dh, code) and change only where a row did.
  property var decos: []
  property var dots: []
  property ListModel decoModel: ListModel {}
  property ListModel dotModel: ListModel {}
  // Bumped after every restyle: overlays placed from the layout refresh.
  property int rev: 0
  // The last restyle's cost in ms (the benches read it).
  property real lastMs: 0
  // The text changed (before it is read and styled; `plain` follows if it
  // really did): benches time a keystroke from here.
  signal changing()

  readonly property string mono: "monospace"
  readonly property int h1Size: Math.round(pixelSize * 1.4)
  readonly property int h2Size: Math.round(pixelSize * 1.2)

  // The document as it stands: its lines, the indent each block has, and
  // per line where it starts on screen and what is drawn under it.
  property var lines: []
  property var shape: []
  property var tops: []
  property var lineDecos: []
  property var lineDots: []
  property bool busy: false
  // Parses by line text, and the look of each (style, role).
  property var cache: new Map()
  property var looks: ({})

  property TextSelection sel: TextSelection { document: st.editor ? st.editor.textDocument : null }
  property FontMetrics metrics: FontMetrics { font.family: st.family; font.pixelSize: st.pixelSize }

  // Any markup in the document; any line restyled in place since it was
  // built (those set the ink on plain text too). A note without markup
  // follows the TextEdit's own colour and has nothing to show or hide.
  property bool styled: false
  property bool painted: false

  onHiddenChanged: restyleAll(styled)
  onInkChanged: restyleAll(styled || painted)
  onFamilyChanged: restyleAll(true)
  onPixelSizeChanged: restyleAll(true)
  // Rebuilt on the next turn of the event loop: focus moves on a mouse
  // press before the press places the caret, which has to land in the
  // layout the person clicked on.
  function restyleAll(needed) {
    looks = {}
    if (needed && editor && lines.length) later.restart()
  }
  Timer { id: later; interval: 0; onTriggered: st.rebuild() }

  // ------------------------------------------------------------ parsing
  function info(line) {
    var hit = cache.get(line)
    if (hit) return hit
    var p = Markup.parseLine(line)
    var rs = Markup.runs(p)
    // Stretches of highlight or code text (markers excluded): [a, b, code].
    var bg = []
    for (var i = 0; i < rs.length; i++) {
      var r = rs[i]
      var b = r.role === Markup.TEXT ? r.style & (Markup.HIGHLIGHT | Markup.CODE) : 0
      if (!b) continue
      var code = (b & Markup.CODE) !== 0
      var last = bg.length ? bg[bg.length - 1] : null
      if (last && last[1] === r.a && last[2] === code) last[1] = r.b
      else bg.push([r.a, r.b, code])
    }
    hit = { p: p, runs: rs, bg: bg, styled: rs.length > 1 || (rs.length === 1 && (rs[0].style || rs[0].role) !== 0) }
    cache.set(line, hit)
    return hit
  }
  function keyAt(inf, i) { return inf.p.style[i] * 4 + inf.p.role[i] }
  function indentOf(line) {
    var inf = info(line)
    return inf.p.hang ? hangPx(line, inf.p.hang) : 0
  }

  // ------------------------------------------------------------ look
  function look(style, role) {
    var key = style * 4 + role
    var l = looks[key]
    if (l) return l
    var size = style & Markup.H1 ? h1Size : style & Markup.H2 ? h2Size : pixelSize
    var text = role === Markup.TEXT
    // Hidden markers shrink to nothing; a bullet keeps its width (so the
    // indent stays put) and a dot is drawn over it.
    var tiny = hidden && (role === Markup.MARK || role === Markup.HEAD)
    var f = {
      family: style & Markup.CODE ? mono : family,
      pixelSize: tiny ? 1 : style & Markup.CODE ? Math.round(size * 0.92) : size,
      bold: text && (style & (Markup.BOLD | Markup.H1 | Markup.H2)) !== 0,
      italic: text && (style & Markup.ITALIC) !== 0,
      underline: text && (style & Markup.UNDERLINE) !== 0,
      strikeout: text && (style & Markup.STRIKE) !== 0
    }
    var c = text ? ink : hidden ? Qt.rgba(ink.r, ink.g, ink.b, 0) : Qt.rgba(ink.r, ink.g, ink.b, 0.38)
    var deco = (f.underline ? " underline" : "") + (f.strikeout ? " line-through" : "")
    var css = (f.family !== family ? "font-family:'" + f.family + "';" : "")
      + (f.pixelSize !== pixelSize ? "font-size:" + f.pixelSize + "px;" : "")
      + (f.bold ? "font-weight:700;" : "") + (f.italic ? "font-style:italic;" : "")
      + (deco ? "text-decoration:" + deco + ";" : "")
      + (!text ? "color:rgba(" + Math.round(c.r * 255) + "," + Math.round(c.g * 255) + ","
                     + Math.round(c.b * 255) + "," + c.a.toFixed(2) + ");" : "")
    l = { font: Qt.font(f), color: c, css: css, text: text }
    looks[key] = l
    return l
  }
  function cssOf(run) { return look(run.style, run.role).css }

  // Does the character at pos already look like `key`? (A typed one takes
  // its neighbour's format; a pasted one may not.)
  function looksLike(pos, key) {
    var l = look(key >> 2, key & 3)
    sel.selectionStart = pos
    sel.selectionEnd = pos + 1
    var f = sel.font
    if (f.bold !== l.font.bold || f.italic !== l.font.italic || f.underline !== l.font.underline
        || f.strikeout !== l.font.strikeout || f.pixelSize !== l.font.pixelSize || f.family !== l.font.family)
      return false
    return l.text ? sel.color.a > 0.99 : Qt.colorEqual(sel.color, l.color)
  }

  // Width of a line's first n characters for its hanging indent (a tab
  // goes to the next stop).
  function hangPx(line, n) {
    var x = 0
    for (var i = 0; i < n; i++) {
      if (line[i] === "\t") { var stop = editor.tabStopDistance || 80; x = (Math.floor(x / stop) + 1) * stop }
      else x += metrics.advanceWidth(line[i])
    }
    return Math.round(x)
  }

  // ------------------------------------------------------------ reading
  function read() {
    if (!editor || editor.length === 0) return ""
    sel.selectionStart = 0
    sel.selectionEnd = editor.length
    return sel.text
  }
  function clean(raw) { return raw.replace(/[\u2029\u2028]/g, "\n").replace(/\u00a0/g, " ") }
  function lineStart(i) {
    var s = 0
    for (var k = 0; k < i; k++) s += lines[k].length + 1
    return s
  }

  // ------------------------------------------------------------ building
  // One line as an HTML paragraph: white space kept, its hanging indent.
  function paragraph(line, w) {
    var block = "margin-top:0px; margin-bottom:0px; margin-left:" + w + "px; margin-right:0px; text-indent:" + (-w) + "px;"
    if (!line.length) return "<p style=\"-qt-paragraph-type:empty; " + block + "\"><br /></p>"
    return "<p style=\"white-space:pre-wrap; " + block + "\">" + Markup.lineHtml(line, info(line).runs, cssOf) + "</p>"
  }

  // The whole document from `text`. With anchor/pos the caret and
  // selection go there; the scroll position stays.
  function setText(text, anchor, pos) {
    if (!editor) return
    var t0 = Date.now()
    text = String(text || "")
    var cy = flick ? flick.contentY : 0
    var ls = text.split("\n")
    var old = cache
    cache = new Map()
    // Plain text has no colour or font of its own: the TextEdit's apply.
    var html = "<html><body>"
    var sh = [], any = false
    for (var i = 0; i < ls.length; i++) {
      var inf = old.get(ls[i])
      if (inf) cache.set(ls[i], inf)
      inf = info(ls[i])
      any = any || inf.styled
      sh.push(inf.p.hang ? hangPx(ls[i], inf.p.hang) : 0)
      html += paragraph(ls[i], sh[i])
    }
    busy = true
    editor.text = html + "</body></html>"
    busy = false
    lines = ls
    shape = sh
    styled = any
    painted = false
    plain = text
    // A TextEdit still being created keeps the text for later: measure
    // (and put the caret back) once it holds it.
    if (editor.length !== text.length) { later.restart(); return }
    restore(anchor, pos, cy)
    decorate()
    lastMs = Date.now() - t0
  }
  function restore(anchor, pos, cy) {
    if (anchor !== undefined) {
      var n = editor.length
      editor.select(Math.max(0, Math.min(n, anchor)), Math.max(0, Math.min(n, pos)))
    }
    if (flick) flick.contentY = cy
  }

  // Block i (its text in the document `curLen` long, from `start`) becomes
  // a fresh paragraph for `line`. A paragraph inserted into a block merges
  // into it, so the new one goes in after a throwaway first paragraph (an
  // "X" that lands on the neighbouring block and is taken off again).
  function replaceBlock(i, start, curLen, line) {
    var w = indentOf(line)
    var p = paragraph(line.length ? line : "Y", w)  // an empty one would be dropped
    busy = true
    if (start > 0) {
      editor.remove(start - 1, start + curLen)
      editor.insert(start - 1, "<p>X</p>" + p)
      editor.remove(start - 1, start)
    } else {
      editor.remove(0, curLen + 1)
      editor.insert(0, p + "<p>X</p>")
      editor.remove(Math.max(1, line.length) + 1, Math.max(1, line.length) + 2)
    }
    if (!line.length) editor.remove(start, start + 1)
    busy = false
    shape[i] = w
    styled = styled || info(line).styled
  }

  // The same text built again (markers shown or hidden, another ink).
  function rebuild() {
    if (editor) setText(plain, anchorPos(), editor.cursorPosition)
  }
  function anchorPos() {
    return editor.cursorPosition === editor.selectionStart ? editor.selectionEnd : editor.selectionStart
  }

  // Restyle one line in place (start: its offset in the document); with
  // `stale` (a flag per character), only the runs that hold one.
  function styleLine(start, line, stale) {
    var inf = info(line), rs = inf.runs
    styled = styled || inf.styled
    painted = true
    busy = true
    for (var r = 0; r < rs.length; r++) {
      if (stale && !anyIn(stale, rs[r].a, rs[r].b)) continue
      var l = look(rs[r].style, rs[r].role)
      sel.selectionStart = start + rs[r].a
      sel.selectionEnd = start + rs[r].b
      sel.font = l.font
      sel.color = l.color
    }
    busy = false
  }

  function anyIn(flags, a, b) {
    for (var i = a; i < b; i++) if (flags[i]) return true
    return false
  }

  function count(s, a, b) {
    var n = 0
    for (var i = a; i < b; i++) if (s.charCodeAt(i) === 10) n++
    return n
  }

  // ------------------------------------------------------------ edits
  // From the editor's onTextChanged. Formatting changes come through here
  // too and are told apart by the text being the same.
  function edited() {
    if (busy || !editor || editor.inputMethodComposing) return
    changing()
    var t0 = Date.now()
    var raw = read()
    var soft = raw.indexOf("\u2028") >= 0
    var text = clean(raw)
    if (text === plain && !soft) return
    var old = plain
    record(old, text)
    // The changed stretch: [a, old.length - b) became [a, text.length - b).
    var a = 0, max = Math.min(old.length, text.length)
    while (a < max && old.charCodeAt(a) === text.charCodeAt(a)) a++
    var b = 0
    while (b < max - a && old.charCodeAt(old.length - 1 - b) === text.charCodeAt(text.length - 1 - b)) b++
    var added = text.length - b - a, removed = old.length - b - a
    // A paste, an IME commit or a soft break may bring formats of their
    // own: build it all again. Typing, Enter and Backspace don't.
    if (soft || added > 1 || lines.length !== count(old, 0, old.length) + 1) {
      setText(text, anchorPos(), editor.cursorPosition)
      return
    }
    var i0 = count(text, 0, a)
    var oldN = count(old, a, old.length - b)
    var start = a > 0 ? text.lastIndexOf("\n", a - 1) + 1 : 0
    var e = text.indexOf("\n", text.length - b)
    var fresh = text.slice(start, e < 0 ? text.length : e).split("\n")
    // The touched blocks keep the format of the first one (Qt splits and
    // joins blocks that way).
    var keep = shape[i0]
    var oldLine = lines[i0]
    lines.splice.apply(lines, [i0, oldN + 1].concat(fresh))
    shape.splice.apply(shape, [i0, oldN + 1].concat(fresh.map(() => keep)))
    plain = text
    var anchor = anchorPos(), caret = editor.cursorPosition
    // Replacing the only block needs a neighbour: a one-line note is
    // quick to build anew.
    if (lines.length < 2 && indentOf(fresh[0]) !== keep) {
      setText(text, anchor, caret)
      return
    }
    var cy = flick ? flick.contentY : 0
    var s = start, moved = false
    for (var i = 0; i < fresh.length; i++) {
      var line = fresh[i]
      if (indentOf(line) !== shape[i0 + i]) {
        replaceBlock(i0 + i, s, line.length, line)
        moved = true
      } else {
        var st = fresh.length === 1 && oldN === 0 ? staleChars(oldLine, line, a - start, added, removed, a) : null
        if (!st || st.some(x => x)) styleLine(s, line, st)
      }
      s += line.length + 1
    }
    if (moved) restore(anchor, caret, cy)
    decorateLines(i0, oldN + 1, fresh.length)
    lastMs = Date.now() - t0
  }

  // Typing one character into a line (or deleting) at offset p: which
  // characters don't look yet as the new parse wants (a flag each), or
  // null if that can't be told. The typed one took the look of the one
  // before it, unless it came some other way (checked).
  function staleChars(oldLine, line, p, added, removed, pos) {
    if (!(added === 1 && removed === 0 && p > 0) && !(added === 0 && removed > 0)) return null
    var o = info(oldLine), n = info(line)
    var out = new Array(line.length)
    for (var j = 0; j < line.length; j++) {
      var oj = j < p ? j : j < p + added ? p - 1 : j - added + removed
      out[j] = keyAt(o, oj) !== keyAt(n, j)
    }
    if (added && !out[p]) out[p] = !looksLike(pos, keyAt(n, p))
    return out
  }

  // ------------------------------------------------------------ decorations
  // Everything drawn under one line, and where the line starts.
  function lineDeco(i, start) {
    var line = lines[i]
    var inf = info(line)
    var out = [], ds = []
    if (hidden && inf.p.kind === "bullet") {
      var r = editor.positionToRectangle(start + inf.p.bullet)
      var r2 = editor.positionToRectangle(start + inf.p.bullet + 1)
      ds.push({ x: r.x, y: r.y, w: Math.max(4, r2.x - r.x), h: r.height })
    }
    for (var k = 0; k < inf.bg.length; k++) spans(out, start + inf.bg[k][0], start + inf.bg[k][1], inf.bg[k][2])
    lineDecos[i] = out
    lineDots[i] = ds
  }
  function decorate() {
    tops = []
    lineDecos = []
    lineDots = []
    var start = 0
    for (var i = 0; i < lines.length; i++) {
      if (lines[i].length && /[`=]/.test(lines[i]) || (hidden && /^[ \t]*[-*][ \t]/.test(lines[i]))) {
        tops[i] = editor.positionToRectangle(start).y
        lineDeco(i, start)
      } else {
        tops[i] = null
        lineDecos[i] = []
        lineDots[i] = []
      }
      start += lines[i].length + 1
    }
    publish()
  }
  // The layout changed without the text (a resize): measure again, if
  // anything is drawn at all.
  function remeasure() {
    if (editor && lines.length && (decos.length || dots.length)) decorate()
  }
  // After an edit that replaced `oldCount` lines from i0 by `newCount`:
  // the new lines are measured again, and the lines below, laid out the
  // same as before, move by however much the edited ones grew or shrank.
  function decorateLines(i0, oldCount, newCount) {
    var nulls = []
    for (var k = 0; k < newCount; k++) nulls.push(null)
    tops.splice.apply(tops, [i0, oldCount].concat(nulls))
    lineDecos.splice.apply(lineDecos, [i0, oldCount].concat(nulls.map(() => [])))
    lineDots.splice.apply(lineDots, [i0, oldCount].concat(nulls.map(() => [])))
    var start = lineStart(i0)
    for (k = i0; k < i0 + newCount; k++) {
      tops[k] = editor.positionToRectangle(start).y
      lineDeco(k, start)
      start += lines[k].length + 1
    }
    var below = i0 + newCount
    if (below < lines.length) {
      // Lines below are all measured, or none (a later line with nothing
      // under it has a null top): find the first measured one.
      var j = below
      while (j < lines.length && tops[j] === null) { start += lines[j].length + 1; j++ }
      if (j < lines.length) {
        var dy = editor.positionToRectangle(start).y - tops[j]
        if (dy !== 0) {
          for (k = j; k < lines.length; k++) {
            if (tops[k] === null) continue
            tops[k] += dy
            lineDecos[k].forEach(d => d.y += dy)
            lineDots[k].forEach(d => d.y += dy)
          }
        }
      }
    }
    publish()
  }
  function publish() {
    decos = [].concat.apply([], lineDecos)
    dots = [].concat.apply([], lineDots)
    sync(decoModel, decos)
    sync(dotModel, dots)
    rev++
  }
  function sync(model, rows) {
    for (var i = 0; i < rows.length; i++) {
      var d = rows[i]
      var row = { dx: d.x, dy: d.y, dw: d.w, dh: d.h, code: !!d.code }
      if (i >= model.count) { model.append(row); continue }
      var m = model.get(i)
      if (m.dx !== row.dx || m.dy !== row.dy || m.dw !== row.dw || m.dh !== row.dh || m.code !== row.code)
        model.set(i, row)
    }
    if (model.count > rows.length) model.remove(rows.length, model.count - rows.length)
  }
  // Rects for [a, b), one per visual line it covers.
  function spans(out, a, b, code) {
    var r0 = editor.positionToRectangle(a)
    var x0 = r0.x, y = r0.y, h = r0.height
    var rb = editor.positionToRectangle(b)
    var k = Math.abs(rb.y - y) > 1 ? a + 1 : b  // on one line: no walk
    for (; k <= b; k++) {
      var r = k === b ? rb : editor.positionToRectangle(k)
      var wrapped = Math.abs(r.y - y) > 1
      if (k < b && !wrapped) continue
      // A piece cut by a wrap ends after the last glyph of its line.
      var x1 = wrapped ? editor.positionToRectangle(k - 1).x + metrics.advanceWidth(plain[k - 1]) : r.x
      out.push({ x: x0 - 1, y: y, w: Math.max(2, x1 - x0 + 2), h: h, code: code })
      x0 = r.x; y = r.y; h = r.height
    }
  }

  // ------------------------------------------------------------ undo
  // Snapshots of the text before each change; a run of typing (or of
  // Backspace) within 1.5 s is one step, a word at a time.
  property var undoStack: []
  property var redoStack: []
  property real lastEditAt: 0
  property int lastEditEnd: -1
  property bool replaying: false

  function record(old, text) {
    if (replaying) return
    var now = Date.now()
    var d = text.length - old.length
    var pos = editor.cursorPosition
    var joins = Math.abs(d) === 1 && now - lastEditAt < 1500 && undoStack.length > 0
                && (d > 0 ? pos - 1 === lastEditEnd : pos === lastEditEnd - 1 || pos === lastEditEnd)
                && !(d > 0 && /\s/.test(text[pos - 1] || "") && !/\s/.test(text[pos - 2] || ""))
    if (!joins) push(undoStack, old)
    redoStack = []
    lastEditAt = Math.abs(d) === 1 ? now : 0
    lastEditEnd = pos
  }
  function push(stack, text) {
    stack.push(text)
    if (stack.length > 200) stack.shift()
  }
  function reset() { undoStack = []; redoStack = []; lastEditAt = 0 }
  function undo() { return swap(undoStack, redoStack) }
  function redo() { return swap(redoStack, undoStack) }
  function swap(from, to) {
    if (!from.length) return false
    var text = from.pop()
    push(to, plain)
    // The caret goes to the end of what changed.
    var cur = plain, b = 0
    var max = Math.min(cur.length, text.length)
    var a = 0
    while (a < max && cur[a] === text[a]) a++
    while (b < max - a && cur[cur.length - 1 - b] === text[text.length - 1 - b]) b++
    replaying = true
    apply(text, text.length - b, text.length - b)
    replaying = false
    lastEditAt = 0
    return true
  }

  // A change made by code (a shortcut, a checkbox): one undo step.
  function replace(text, anchor, pos) {
    if (text !== plain) {
      push(undoStack, plain)
      redoStack = []
      lastEditAt = 0
      apply(text, anchor, pos)
    } else editor.select(anchor, pos)
  }

  // A new text with the same number of lines: only the lines that differ
  // are replaced (a shortcut touches one); anything else is built anew.
  function apply(text, anchor, pos) {
    var ls = text.split("\n")
    if (ls.length !== lines.length || ls.length < 2) { setText(text, anchor, pos); return }
    var changed = []
    for (var i = 0; i < ls.length; i++) if (ls[i] !== lines[i]) changed.push(i)
    if (changed.length > 3) { setText(text, anchor, pos); return }
    var t0 = Date.now()
    var cy = flick ? flick.contentY : 0
    for (var c = 0; c < changed.length; c++) {
      var k = changed[c]
      // Earlier replaced lines may have changed length: offsets from ls.
      var start = 0
      for (var j = 0; j < k; j++) start += ls[j].length + 1
      replaceBlock(k, start, lines[k].length, ls[k])
      lines[k] = ls[k]
    }
    plain = text
    restore(anchor, pos, cy)
    decorate()
    lastMs = Date.now() - t0
  }
}
