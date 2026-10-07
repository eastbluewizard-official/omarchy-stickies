.pragma library
.import "markup.js" as Markup

// Chat answers for the overlay. The answer is model text: it is escaped
// before any markup is added, so "<b>" or "&" in it shows literally. Only
// [#id] citations of notes that were actually sent become links
// (note:<id>, which the overlay turns into a jump to the note); any other
// [#id] stays plain text with a "?" so a made-up citation is visible.

function answerHtml(text, sentIds, color) {
  var sent = {}
  for (var i = 0; i < (sentIds || []).length; i++) sent[String(sentIds[i])] = true
  var c = Markup.safeColor(color)
  var out = Markup.escapeHtml(String(text || "")).replace(/\[#(\d+)\]/g, function(m, id) {
    return sent[id] ? "<a href=\"note:" + id + "\"><font color=\"" + c + "\">#" + id + "</font></a>"
                    : "#" + id + "?"
  })
  // The one bit of markdown models use all the time; the rest stays literal.
  out = out.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
  return out.replace(/\r?\n/g, "<br>")
}

// "note:12" -> 12, anything else -> -1.
function noteFromLink(link) {
  var m = /^note:(\d+)$/.exec(String(link))
  return m ? parseInt(m[1], 10) : -1
}

// First non-empty line in plain words (no markup), cut to n characters.
function title(body, n) {
  var lines = String(body || "").split(/\r?\n/)
  var t = ""
  for (var i = 0; i < lines.length; i++) {
    var l = Markup.plainLine(lines[i]).trim()
    if (l) { t = l; break }
  }
  n = n || 60
  return t.length <= n ? t : t.slice(0, n - 1) + "…"
}
