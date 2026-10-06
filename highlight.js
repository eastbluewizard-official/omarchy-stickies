.pragma library
.import "markup.js" as Markup

// Search-hit snippets for the overlay. Note text is user text: it is
// escaped before it is wrapped in StyledText markup, so "<b>" or "&" in a
// note shows literally. Highlights come from `stickies serve` as [start,
// end) offsets in Unicode code points; JS strings are UTF-16, so the
// snippet is split into code points (Array.from) before slicing.

function styled(snippet, spans, color) {
  // One code point in, one out: offsets stay valid.
  var cps = Array.from(String(snippet || "").replace(/[\r\n\t]/g, " "))
  var c = Markup.safeColor(color)
  var sorted = (spans || []).slice().sort(function(a, b) { return a[0] - b[0] })
  var out = "", last = 0
  for (var i = 0; i < sorted.length; i++) {
    var a = Math.max(last, sorted[i][0] | 0), b = Math.min(cps.length, sorted[i][1] | 0)
    if (b <= a) continue
    out += Markup.escapeHtml(cps.slice(last, a).join("")) + "<b><font color=\"" + c + "\">"
      + Markup.escapeHtml(cps.slice(a, b).join("")) + "</font></b>"
    last = b
  }
  return out + Markup.escapeHtml(cps.slice(last).join(""))
}
