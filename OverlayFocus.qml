import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Wayland
import "overlayfocus.js" as Focus

// Keyboard for a summoned overlay (search, chat), one full-screen surface
// on the Overlay layer. Decisions are in overlayfocus.js.
//  - Exclusive keyboard focus always, so the surface already asks for the
//    keyboard in the commit that maps it (switching None -> Exclusive on
//    open raced the map). Hidden, it is unmapped and asks for nothing.
//  - While it maps, a HyprlandFocusGrab on the surface as well (front
//    mode's tool, Service.qml), so the window under the pointer can't take
//    the keyboard; dropped once the field has held it for a moment, so a
//    menu or the launcher opened on top still gets the keyboard.
//  - Losing focus never closes. During the map the field takes it back
//    (re-asking for Exclusive if the surface lost it); later, another
//    surface keeps it until it goes, then Exclusive brings it back.
// `field` is what should hold the keyboard; it may change (chat: a key
// catcher while its text field is disabled). Put this inside the
// PanelWindow (it reads Window.active) and bind
//   visible: keeper.opened
//   WlrLayershell.keyboardFocus: keeper.keyboardFocus
Item {
  id: keeper

  property var window
  property Item field
  readonly property bool opened: openState
  readonly property int keyboardFocus: bouncing ? WlrKeyboardFocus.None : WlrKeyboardFocus.Exclusive
  readonly property bool hasKeyboard: windowActive && fieldFocused
  // Focus-outs taken back since the last open (tests, logs).
  property int refocuses: 0

  property bool openState: false
  property bool bouncing: false
  property bool gaveUp: false
  property var st: Focus.create()
  readonly property bool windowActive: Window.active
  readonly property bool fieldFocused: field ? field.activeFocus : false

  function now() { return Date.now() }

  function apply(action) {
    if (action === "open") {
      refocuses = 0
      gaveUp = false
      openState = true
      grab.active = true
      Qt.callLater(take)
    } else if (action === "close") {
      grab.active = false
      refocusTimer.stop()
      openState = false
    }
    return action
  }

  function open() { return apply(Focus.open(st, now())) }
  function close() { return apply(Focus.close(st)) }
  function toggle() { return apply(Focus.toggle(st, now())) }
  function pressEscape() { return apply(Focus.pressEscape(st)) }
  function clickOutside() { return apply(Focus.clickOutside(st)) }

  function take() { if (openState && field) field.forceActiveFocus() }

  onHasKeyboardChanged: {
    if (!openState) return
    if (hasKeyboard) {
      Focus.focusGained(st, now())
      gaveUp = false
    } else {
      lost()
    }
  }

  function lost() {
    var a = Focus.focusLost(st, now(), !windowActive)
    refocuses = st.refocuses
    if (a === "refocus") refocusTimer.restart()
    else if (a === "giveup" && !gaveUp) {
      gaveUp = true
      console.warn("stickies overlay: keyboard taken away", refocuses, "times, not asking again")
    }
  }

  // Checked while open: catches a keyboard that never arrived (nothing
  // changed to react to) and ends the map-time grab once settled.
  Timer {
    interval: 100
    repeat: true
    running: keeper.openState
    onTriggered: {
      if (grab.active && !Focus.wantGrab(keeper.st, keeper.now())) grab.active = false
      if (!keeper.hasKeyboard && !keeper.gaveUp && !refocusTimer.running && !bounceTimer.running) keeper.lost()
    }
  }

  // A moment later, so a focus that only blinks away comes back by itself.
  Timer {
    id: refocusTimer
    interval: 60
    onTriggered: {
      if (!keeper.openState || keeper.hasKeyboard) return
      if (!keeper.windowActive) {
        // Ask for Exclusive again: Hyprland focuses an exclusive layer
        // when it asks, not only when it maps.
        keeper.bouncing = true
        bounceTimer.restart()
      } else {
        keeper.take()
      }
    }
  }
  Timer {
    id: bounceTimer
    interval: 30
    onTriggered: {
      keeper.bouncing = false
      Qt.callLater(keeper.take)
    }
  }

  HyprlandFocusGrab {
    id: grab
    windows: keeper.window ? [keeper.window] : []
    onCleared: {
      if (Focus.grabCleared(keeper.st, keeper.now()) === "regrab")
        Qt.callLater(() => { if (Focus.wantGrab(keeper.st, keeper.now())) grab.active = true })
    }
  }
}
