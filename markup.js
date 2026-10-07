.pragma library

// Escaping shared by highlight.js (search snippets) and chat.js (answers):
// note and model text is user text, escaped before any StyledText markup
// is added, so "<b>" or "&" in it shows literally.

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")
}

// A colour that is safe inside a font tag's attribute, or the default accent.
function safeColor(color) {
  return /^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$/.test(String(color)) ? String(color) : "#e0a030"
}

// ---------------------------------------------------------------- note markup
// A note body stays plain text with the markers in it. parseLine() says
// how one line looks; stickies.py's parse_line() is the same parser (the
// tests run one table through both). Everything is per line: markers pair
// on the same line, so an edit only ever restyles the lines it touched.
//
//   **bold**  *italic*  __underline__  ~~strike~~  ==highlight==  `code`
//   "# " / "## " heading, "- " / "* " bullet, "1. " / "1) " numbered item,
//   "[ ]" / "- [x]" checkbox (NoteCard draws the box).
//
// An opener is followed by a non-space and its closer (same marker, same
// line) follows a non-space (past any more of the marker's character, so
// "** x **" stays literal); what they hold isn't only that character. A marker without a partner stays literal; \ before ASCII
// punctuation makes it literal; inside `code` nothing else is parsed.

// Style bits of a character.
var BOLD = 1, ITALIC = 2, UNDERLINE = 4, STRIKE = 8, HIGHLIGHT = 16, CODE = 32, H1 = 64, H2 = 128
// What a character is: text, an inline marker (hidden when the note isn't
// being edited), a heading's "# " (hidden too) or a bullet's "-" / "*"
// (drawn as a dot instead).
var TEXT = 0, MARK = 1, HEAD = 2, BULLET = 3

var PAIRS = [["**", BOLD], ["__", UNDERLINE], ["~~", STRIKE], ["==", HIGHLIGHT], ["*", ITALIC]]
var PUNCT = "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"

function isSpace(c) { return c === " " || c === "\t" || c === "\u00a0" }

// The line-level part: { kind, lead, hang, bullet, box }. lead: where the
// inline text starts; hang: the prefix a wrapped line aligns under (0:
// none); bullet / box: index of the bullet character / the "[", or -1.
function lineKind(line) {
  var m = /^([ \t]*)(?:([-*+])[ \t]+)?\[[ xX]\][ \t]*/.exec(line)
  if (m) return { kind: "check", lead: m[0].length, hang: m[0].length,
                  bullet: m[2] ? m[1].length : -1, box: m[0].indexOf("[") }
  m = /^(#{1,2})[ \t]+/.exec(line)
  if (m) return { kind: m[1].length === 1 ? "h1" : "h2", lead: m[0].length, hang: 0, bullet: -1, box: -1 }
  m = /^([ \t]*)([-*])[ \t]+/.exec(line)
  if (m) return { kind: "bullet", lead: m[0].length, hang: m[0].length, bullet: m[1].length, box: -1 }
  m = /^([ \t]*)\d{1,9}[.)][ \t]+/.exec(line)
  if (m) return { kind: "number", lead: m[0].length, hang: m[0].length, bullet: -1, box: -1 }
  return { kind: "", lead: 0, hang: 0, bullet: -1, box: -1 }
}

// One line -> { kind, hang, bullet, box, style: [bits per char], role:
// [TEXT/MARK/HEAD/BULLET per char] }.
function parseLine(line) {
  var s = String(line)
  var n = s.length
  var k = lineKind(s)
  var style = new Array(n), role = new Array(n)
  var base = k.kind === "h1" ? H1 : k.kind === "h2" ? H2 : 0
  for (var i = 0; i < n; i++) { style[i] = base; role[i] = TEXT }
  if (base) for (i = 0; i < k.lead; i++) role[i] = HEAD
  if (k.bullet >= 0) role[k.bullet] = BULLET

  var memo = {}
  // Only these can start a marker, an escape or code: anything else is
  // stepped over at once (the scans below are per character).
  function special(i) { var c = s.charCodeAt(i); return c === 42 || c === 95 || c === 126 || c === 61 || c === 96 || c === 92 }
  function escapeAt(i, end) { return s[i] === "\\" && i + 1 < end && PUNCT.indexOf(s[i + 1]) >= 0 }
  // `code` from i: index of the closing backtick, or -1.
  function codeEnd(i, end) {
    if (s[i] !== "`") return -1
    var j = s.indexOf("`", i + 1)
    return j > i + 1 && j < end ? j : -1
  }
  // The whole run of the marker's character counts: "** x" opens nothing.
  function opens(m, i, end) {
    if (s.substr(i, m.length) !== m) return false
    var j = i + m.length
    while (j < end && s[j] === m[0]) j++
    return j < end && !isSpace(s[j])
  }
  function closes(m, k, from) {
    var j = k - 1
    while (j >= from && s[j] === m[0]) j--
    return j >= from && !isSpace(s[j])
  }
  function onlyMarker(m, a, b) {
    for (var j = a; j < b; j++) if (s[j] !== m[0]) return false
    return true
  }
  // Index of the closer of marker m opened just before `from`, or -1.
  function closer(m, from, end) {
    var key = m + ":" + from + ":" + end
    if (key in memo) return memo[key]
    memo[key] = -1  // no cycles: a nested search never comes back here
    var k = from, found = -1
    while (k < end) {
      if (!special(k)) { k++; continue }
      if (escapeAt(k, end)) { k += 2; continue }
      var c = codeEnd(k, end)
      if (c >= 0) { k = c + 1; continue }
      var skipped = false
      // A longer marker that starts here and pairs up is nested: skip it.
      for (var p = 0; p < PAIRS.length && !skipped; p++) {
        var o = PAIRS[p][0]
        if (o.length > m.length && o.indexOf(m) === 0 && opens(o, k, end)) {
          var e = closer(o, k + o.length, end)
          if (e >= 0) { k = e + o.length; skipped = true }
        }
      }
      if (skipped) continue
      if (s.substr(k, m.length) === m && closes(m, k, from) && !onlyMarker(m, from, k)) {
        found = k
        break
      }
      for (p = 0; p < PAIRS.length && !skipped; p++) {
        o = PAIRS[p][0]
        if (opens(o, k, end)) {
          e = closer(o, k + o.length, end)
          if (e >= 0) { k = e + o.length; skipped = true }
        }
      }
      if (!skipped) k++
    }
    memo[key] = found
    return found
  }
  function mark(a, b) { for (var j = a; j < b; j++) role[j] = MARK }
  function add(a, b, bit) { for (var j = a; j < b; j++) style[j] |= bit }
  function inline(from, end) {
    var i = from
    while (i < end) {
      if (!special(i)) { i++; continue }
      if (escapeAt(i, end)) { mark(i, i + 1); i += 2; continue }
      var c = codeEnd(i, end)
      if (c >= 0) { mark(i, i + 1); mark(c, c + 1); add(i + 1, c, CODE); i = c + 1; continue }
      var done = false
      for (var p = 0; p < PAIRS.length && !done; p++) {
        var m = PAIRS[p][0]
        if (!opens(m, i, end)) continue
        var e = closer(m, i + m.length, end)
        if (e < 0) continue
        mark(i, i + m.length)
        mark(e, e + m.length)
        add(i + m.length, e, PAIRS[p][1])
        inline(i + m.length, e)
        i = e + m.length
        done = true
      }
      if (!done) i++
    }
  }
  inline(k.lead, n)
  return { kind: k.kind, hang: k.hang, bullet: k.bullet, box: k.box, style: style, role: role }
}

// The line as runs of one look: [{ a, b, style, role }].
function runs(p) {
  var out = []
  for (var i = 0; i < p.style.length; i++) {
    var last = out.length ? out[out.length - 1] : null
    if (last && last.style === p.style[i] && last.role === p.role[i]) last.b = i + 1
    else out.push({ a: i, b: i + 1, style: p.style[i], role: p.role[i] })
  }
  return out
}

// A line's runs as HTML for the editor: the text escaped first, then each
// run that has a look of its own (cssOf(run): CSS, or "") in a span.
function lineHtml(line, rs, cssOf) {
  var out = ""
  for (var r = 0; r < rs.length; r++) {
    var text = escapeHtml(line.slice(rs[r].a, rs[r].b))
    var css = cssOf(rs[r])
    out += css ? "<span style=\"" + escapeHtml(css) + "\">" + text + "</span>" : text
  }
  return out
}

// A line's plain words: every marker gone (and a bullet's "- ").
function plainLine(line) {
  var s = String(line)
  if (!/[*_~=`\\#+\-]/.test(s)) return s
  var p = parseLine(s)
  // A bullet goes with the spaces after it (up to the text or the box).
  var gapEnd = p.bullet < 0 ? -1 : p.kind === "check" ? p.box : p.hang
  var out = ""
  for (var i = 0; i < s.length; i++) {
    if (p.role[i] !== TEXT || (i > p.bullet && i < gapEnd)) continue
    out += s[i]
  }
  return out
}

// The whole body without markers, line by line.
function plain(text) {
  return String(text || "").split("\n").map(plainLine).join("\n")
}

// ---------------------------------------------------------------- shortcuts
// Ctrl+B and friends: wrap the selection [a, b) in marker m, or unwrap it
// if it is wrapped already (markers just outside it or at its ends). No
// selection: an empty pair with the caret between (or, between an empty
// pair, the pair goes). A selection over several lines wraps each line
// (markers pair per line). Spaces at the selection's ends stay outside.
// Returns { text, a, b }: the new text and selection.

// How many of m's character run up to i (dir -1) or from i (dir 1).
function runOf(text, i, ch, dir) {
  var n = 0
  if (dir < 0) { while (i - 1 - n >= 0 && text[i - 1 - n] === ch) n++ }
  else { while (i + n < text.length && text[i + n] === ch) n++ }
  return n
}
// Do runs of l and r marker characters close m exactly? A single "*" must
// not eat half of a "**" (odd runs only); a doubled marker needs two.
function fits(m, l, r) {
  if (m === "*") return l % 2 === 1 && r % 2 === 1
  return l >= m.length && r >= m.length
}

function toggleWrap(text, a, b, m) {
  text = String(text)
  if (a > b) { var t = a; a = b; b = t }
  var n = m.length, ch = m[0]
  if (a === b) {
    if (text.substr(a - n, n) === m && text.substr(a, n) === m && fits(m, runOf(text, a, ch, -1), runOf(text, a, ch, 1)))
      return { text: text.slice(0, a - n) + text.slice(a + n), a: a - n, b: a - n }
    return { text: text.slice(0, a) + m + m + text.slice(a), a: a + n, b: a + n }
  }
  // Leave white space at either end outside the markers.
  while (a < b && /\s/.test(text[a])) a++
  while (b > a && /\s/.test(text[b - 1])) b--
  if (a === b) return { text: text, a: a, b: b }
  var sel = text.slice(a, b)
  if (sel.indexOf("\n") >= 0) return wrapLines(text, a, b, m)
  if (runOf(text, a, ch, -1) >= n && runOf(text, b, ch, 1) >= n && fits(m, runOf(text, a, ch, -1), runOf(text, b, ch, 1)))
    return { text: text.slice(0, a - n) + sel + text.slice(b + n), a: a - n, b: b - n }
  var l = runOf(sel, 0, ch, 1), r = runOf(sel, sel.length, ch, -1)
  if (sel.length > 2 * n && l >= n && r >= n && l < sel.length && fits(m, l, r))
    return { text: text.slice(0, a) + sel.slice(n, sel.length - n) + text.slice(b), a: a, b: b - 2 * n }
  return { text: text.slice(0, a) + m + sel + m + text.slice(b), a: a + n, b: b + n }
}

// Each line of the selection on its own: unwrap them all if every one is
// wrapped (at its ends), else wrap the ones that aren't.
function wrapLines(text, a, b, m) {
  var parts = text.slice(a, b).split("\n")
  var n = m.length, ch = m[0]
  function core(p) {
    var s = 0, e = p.length
    while (s < e && /\s/.test(p[s])) s++
    while (e > s && /\s/.test(p[e - 1])) e--
    return [s, e]
  }
  function wrapped(p) {
    var c = core(p), x = p.slice(c[0], c[1])
    var l = runOf(x, 0, ch, 1), r = runOf(x, x.length, ch, -1)
    return x.length > 2 * n && l < x.length && fits(m, l, r)
  }
  var all = parts.every(p => core(p)[0] === core(p)[1] || wrapped(p))
  var out = parts.map(function(p) {
    var c = core(p)
    if (c[0] === c[1]) return p
    var x = p.slice(c[0], c[1])
    if (all) x = x.slice(n, x.length - n)
    else if (!wrapped(p)) x = m + x + m
    return p.slice(0, c[0]) + x + p.slice(c[1])
  }).join("\n")
  return { text: text.slice(0, a) + out + text.slice(b), a: a, b: a + out.length }
}

// Ctrl+L: a "[ ] " checkbox on the caret's line, or off again. Returns
// { text, pos } with the caret moved along with the text.
function toggleCheckbox(text, pos) {
  text = String(text)
  var start = pos > 0 ? text.lastIndexOf("\n", pos - 1) + 1 : 0
  var end = text.indexOf("\n", start)
  var line = text.slice(start, end < 0 ? text.length : end)
  var m = /^([ \t]*(?:[-*+][ \t]+)?)\[[ xX]\][ \t]?/.exec(line)
  if (m) {
    var at = start + m[1].length, len = m[0].length - m[1].length
    return { text: text.slice(0, at) + text.slice(at + len), pos: pos > at ? Math.max(at, pos - len) : pos }
  }
  var lead = /^[ \t]*(?:[-*][ \t]+)?/.exec(line)[0].length
  var ins = start + lead
  return { text: text.slice(0, ins) + "[ ] " + text.slice(ins), pos: pos >= ins ? pos + 4 : pos }
}
