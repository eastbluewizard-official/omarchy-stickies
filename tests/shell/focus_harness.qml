import QtQuick
import QtTest
import Quickshell
import Quickshell.Wayland
import "stickies"

// Overlay keyboard harness for tests/test_overlay_focus.py (live session
// only, opt-in): the real Service, so the real SearchOverlay and
// ChatOverlay and their OverlayFocus, in its own `quickshell -p` with a
// temp STICKIES_STATE. For each overlay, as SUPER + ALT + J / A do:
//   toggle (the IPC method) twice at once (one press delivered twice),
//   while another Exclusive layer maps for 250 ms (what Hyprland's
//   window-under-the-pointer does during the map) -> open, the field has
//   the keyboard
//   settled, a spurious focus-out: that layer maps again and takes the
//   keyboard for 250 ms, then the field itself loses focus -> still open,
//   keyboard back in the field each time
//   type "abc" -> it lands in the field
//   Esc -> closed
// Prints one `FOCUS {...}` line. Keys are Qt events into this process
// (QtTest), never into the session.
ShellRoot {
  id: harness

  Service { id: svc }

  ElapsedTimer { id: clock }
  function ms() { return Math.round(clock.elapsed() * 1000) }

  TestCase { id: keys; name: "focus"; when: false }

  // Takes the keyboard while shown, like whatever Hyprland hands it to.
  PanelWindow {
    id: thief
    visible: false
    implicitWidth: 40
    implicitHeight: 40
    color: "#80ff0000"
    anchors { top: true; left: true }
    exclusionMode: ExclusionMode.Ignore
    WlrLayershell.namespace: "eastbluewizard-stickies-test-thief"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
  }

  property var result: ({})
  property var names: ["search", "chat"]
  property int which: 0
  property int step: 0
  property real t0: 0
  property real waitUntil: 0
  property var cur: ({})
  readonly property var ov: which === 0 ? svc.searchOverlay : svc.chatOverlay

  function fail(why) {
    cur.error = why + " (step " + step + ")"
    next()
  }
  function next() {
    result[names[which]] = cur
    if (ov.opened) ov.close()
    thief.visible = false
    cur = ({})
    step = 0
    which++
    waitUntil = ms() + 300
    if (which >= names.length) finish()
  }

  Timer {
    interval: 10
    repeat: true
    running: true
    onTriggered: harness.tick()
  }

  function tick() {
    if (which >= names.length || ms() < waitUntil) return
    var k = ov.keeper
    if (step === 0) {
      if (!svc.ready) return
      t0 = ms()
      ov.toggle()
      ov.toggle()  // the same press delivered twice (fallback, repeat)
      cur.duplicate_ignored = ov.opened
      thief.visible = true  // competes for the keyboard during the map
      waitUntil = ms() + 250
      step = 1
    } else if (step === 1) {
      thief.visible = false
      step = 2
    } else if (step === 2) {
      if (!k.hasKeyboard) { if (ms() - t0 > 3000) fail("keyboard never reached the field"); return }
      cur.open_to_keyboard_ms = ms() - t0
      cur.survived_map_steal = ov.opened
      waitUntil = ms() + 600  // settled: the map-time grab is gone
      step = 3
    } else if (step === 3) {
      thief.visible = true
      t0 = ms()
      step = 4
    } else if (step === 4) {
      if (k.hasKeyboard) { if (ms() - t0 > 1000) fail("the other surface never took the keyboard"); return }
      cur.lost_keyboard = true
      waitUntil = ms() + 250
      step = 5
    } else if (step === 5) {
      thief.visible = false
      t0 = ms()
      step = 6
    } else if (step === 6) {
      if (!k.hasKeyboard) { if (ms() - t0 > 3000) fail("keyboard not back after the other surface"); return }
      cur.survived_surface_focus_out = ov.opened
      cur.surface_refocus_ms = ms() - t0
      // the field itself loses focus
      ov.card.forceActiveFocus()
      t0 = ms()
      step = 7
    } else if (step === 7) {
      if (!k.hasKeyboard) { if (ms() - t0 > 3000) fail("field focus not taken back"); return }
      cur.survived_field_focus_out = ov.opened
      cur.field_refocus_ms = ms() - t0
      cur.refocuses = k.refocuses
      ov.field.text = ""
      keys.keyClick(Qt.Key_A)
      keys.keyClick(Qt.Key_B)
      keys.keyClick(Qt.Key_C)
      step = 8
    } else if (step === 8) {
      cur.typed = ov.field.text
      keys.keyClick(Qt.Key_Escape)
      t0 = ms()
      step = 9
    } else if (step === 9) {
      if (ov.opened) { if (ms() - t0 > 1000) fail("Esc did not close"); return }
      cur.closed_on_escape = true
      next()
    }
  }

  function finish() {
    console.log("FOCUS " + JSON.stringify(result))
    Qt.quit()
  }

  Timer {
    interval: 30000
    running: true
    onTriggered: { harness.result.error = "timeout"; harness.finish() }
  }
}
