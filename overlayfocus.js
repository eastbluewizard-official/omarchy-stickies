.pragma library

// Open/close/focus decisions for the search and chat overlays
// (OverlayFocus.qml holds the state and does what these return). Pure, so
// tests/test_overlay_focus.py runs it under node.
//
// What it guards against (owner report, Hyprland 0.56, follow_mouse = 1):
// the overlay maps, and right after that the keyboard goes back to the
// window under the pointer, so to the person the key did nothing. So
// focus loss never closes an overlay. While it maps, the keyboard is taken
// back; once settled (the field has had it for SETTLE_MS), a surface that
// takes it (a menu, the launcher) is left alone and the overlay's
// Exclusive focus brings it back when that surface goes. An overlay closes
// only on Esc, a click outside its card, or the same key again -- and
// "again" means a real second press, not one press delivered twice.

var SETTLE_MS = 400       // the field has held the keyboard this long: mapped
var REPEAT_MS = 450       // a toggle sooner than this after opening is a duplicate
var MAX_REFOCUS = 8       // in a row; after that the overlay stays, unfocused

function create() {
  return { opened: false, openedAt: 0, firstFocusAt: 0, gainedAt: 0, refocuses: 0 }
}

function settled(s, now) {
  return s.opened && s.firstFocusAt > 0 && now - s.firstFocusAt >= SETTLE_MS
}

function open(s, now) {
  s.opened = true
  s.openedAt = now
  s.firstFocusAt = 0
  s.gainedAt = 0
  s.refocuses = 0
  return "open"
}

function close(s) {
  if (!s.opened) return "ignore"
  s.opened = false
  return "close"
}

// The IPC method (find / chat): open, or close on a real second press.
function toggle(s, now) {
  if (!s.opened) return open(s, now)
  if (now - s.openedAt < REPEAT_MS) return "ignore"
  return close(s)
}

function focusGained(s, now) {
  if (!s.opened) return "ignore"
  if (!s.firstFocusAt) s.firstFocusAt = now
  s.gainedAt = now
  return "ignore"
}

// The keyboard left the field while open. windowLost: it left the surface
// (another client has it), not just the field (inside our own window,
// e.g. chat's field disabled while an answer streams -- always taken back).
// A keyboard held for SETTLE_MS since the last loss starts a fresh budget.
function focusLost(s, now, windowLost) {
  if (!s.opened) return "ignore"
  if (windowLost && settled(s, now)) return "wait"
  if (s.gainedAt && now - s.gainedAt >= SETTLE_MS) s.refocuses = 0
  s.gainedAt = 0
  if (s.refocuses >= MAX_REFOCUS) return "giveup"
  s.refocuses++
  return "refocus"
}

// The map-time focus grab: held until settled. Cleared before that is
// Hyprland settling: grab again.
function wantGrab(s, now) {
  return s.opened && !settled(s, now)
}
function grabCleared(s, now) {
  return wantGrab(s, now) ? "regrab" : "ignore"
}

function pressEscape(s) { return close(s) }
function clickOutside(s) { return close(s) }
