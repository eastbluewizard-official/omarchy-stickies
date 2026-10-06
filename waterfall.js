.pragma library

// Waterfall layout: the current workspace's notes as one column docked to
// the left or right edge of a screen. Pure functions (no QML), so
// tests/test_shell.py runs them under node; Waterfall.qml only feeds them
// numbers and places cards where they say.

var LAYOUTS = ["free", "waterfall-right", "waterfall-left"]

var MARGIN = 12     // column <-> screen edge, bar and bottom
var GAP = 10        // between cards
var STRIP = 6       // the collapsed column
var HANDLE_W = 12   // the collapse handle on the column's inner edge
var HANDLE_H = 56
var MIN_H = 110     // NoteCard.minH
var ROLLED_H = 26   // NoteCard.headerHeight: a rolled-up note is its header line

// SUPER + ALT + L: free -> waterfall-right -> waterfall-left -> free.
function nextLayout(cur) {
  var i = LAYOUTS.indexOf(cur)
  return LAYOUTS[(i + 1) % LAYOUTS.length]
}

function sideOf(layout) {
  return layout === "waterfall-left" ? "left" : layout === "waterfall-right" ? "right" : ""
}

// Column order. notes: [{nid, pinned, updated}], order: the stored ids (a
// drag in the column writes it). Notes not in it come first, pinned before
// the rest, most recently edited first; then the stored ones, in order.
function sortColumn(notes, order) {
  var at = {}
  for (var i = 0; i < (order || []).length; i++) at[order[i]] = i
  var fresh = [], kept = []
  for (var j = 0; j < notes.length; j++) (notes[j].nid in at ? kept : fresh).push(notes[j])
  fresh.sort(function(a, b) {
    if (!!a.pinned !== !!b.pinned) return a.pinned ? -1 : 1
    if (a.updated !== b.updated) return a.updated > b.updated ? -1 : 1
    return b.nid - a.nid
  })
  kept.sort(function(a, b) { return at[a.nid] - at[b.nid] })
  return fresh.concat(kept)
}

// Where everything goes on a screen of screenW x screenH logical px (a
// rotated monitor just has screenW < screenH). top/bottom: space other
// surfaces reserve (the bar). Returns rects in screen coordinates:
//   column  where cards sit, full usable height
//   strip   the collapsed column, at the screen edge
//   handle  the collapse/expand tab (inner edge of the column, or the strip)
//   parkX   card x while collapsed (just off screen)
//   reserve exclusive zone when `reserve` is on
function geometry(o) {
  var sw = Math.max(0, o.screenW), sh = Math.max(0, o.screenH)
  var right = o.side !== "left"
  var top = (o.top || 0) + MARGIN
  var h = Math.max(0, sh - top - (o.bottom || 0) - MARGIN)
  var w = Math.max(60, Math.min(o.width || 320, sw - 2 * MARGIN - HANDLE_W))
  var cx = right ? sw - MARGIN - w : MARGIN
  var hy = top + Math.max(0, Math.round((Math.min(h, o.contentH === undefined ? h : o.contentH) - HANDLE_H) / 2))
  var handle = o.collapsed
      ? { x: right ? sw - STRIP - HANDLE_W : STRIP, y: hy, w: HANDLE_W, h: HANDLE_H }
      : { x: right ? cx - HANDLE_W - 2 : cx + w + 2, y: hy, w: HANDLE_W, h: HANDLE_H }
  return {
    side: right ? "right" : "left",
    collapsed: !!o.collapsed,
    column: { x: cx, y: top, w: w, h: h },
    strip: { x: right ? sw - STRIP : 0, y: top, w: STRIP, h: h },
    handle: handle,
    parkX: right ? sw + 8 : -w - 8,
    reserve: o.collapsed ? STRIP : w + 2 * MARGIN + HANDLE_W
  }
}

// Card rects for sorted notes ([{nid, h}]) in column coordinates (y = 0 is
// the column's top; scrolling is applied by the caller). drag: {nid, index}
// leaves a gap of the dragged note's height at `index`. Heights are each
// note's own, capped to the column (a taller note scrolls inside itself);
// a note with `rolled` set takes its header line only.
function slots(notes, geo, drag) {
  var list = notes.slice()
  if (drag && drag.nid !== undefined) {
    var from = -1
    for (var i = 0; i < list.length; i++) if (list[i].nid === drag.nid) from = i
    if (from >= 0) {
      var moved = list.splice(from, 1)[0]
      list.splice(Math.max(0, Math.min(list.length, drag.index)), 0, moved)
    }
  }
  var out = [], y = 0
  var x = geo.collapsed ? geo.parkX : geo.column.x
  var cap = Math.max(MIN_H, geo.column.h)
  for (var j = 0; j < list.length; j++) {
    var h = list[j].rolled ? ROLLED_H : Math.max(MIN_H, Math.min(cap, list[j].h || 200))
    out.push({ nid: list[j].nid, x: x, y: y, w: geo.column.w, h: h })
    y += h + GAP
  }
  var contentH = list.length ? y - GAP : 0
  return { slots: out, contentH: contentH, maxScroll: Math.max(0, contentH - geo.column.h),
           regionH: Math.min(contentH, geo.column.h) }
}

function clampScroll(scroll, contentH, viewH) {
  return Math.max(0, Math.min(Math.max(0, contentH - viewH), scroll))
}

// Index the dragged note would drop at, from its centre y in column
// coordinates: the number of other notes whose middle is above it.
function dropIndex(notes, geo, nid, centerY) {
  var others = notes.filter(function(n) { return n.nid !== nid })
  var s = slots(others, geo).slots
  var k = 0
  for (var i = 0; i < s.length; i++) if (s[i].y + s[i].h / 2 < centerY) k = i + 1
  return k
}

// The stored order after a drag: the column's ids in their new order, kept
// after the ids of notes elsewhere (their place relative to these doesn't
// matter: only notes in the same column are ever compared).
function reorder(order, columnIds, nid, index) {
  var ids = columnIds.filter(function(i) { return i !== nid })
  ids.splice(Math.max(0, Math.min(ids.length, index)), 0, nid)
  var inCol = {}
  for (var i = 0; i < ids.length; i++) inCol[ids[i]] = true
  return (order || []).filter(function(i) { return !inCol[i] }).concat(ids)
}

function inside(r, x, y) {
  return x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h
}

// A card dropped with its centre at (cx, cy) on the screen: still in the
// column (reorder) or out on the desktop (it goes free there)? A little
// slack either side so a sloppy vertical drag still reorders.
function leftColumn(geo, cx) {
  var c = geo.column
  return cx < c.x - 40 || cx > c.x + c.w + 40
}
