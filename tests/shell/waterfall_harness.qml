import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Hyprland
import Quickshell.Wayland
import qs.Commons
import "stickies"

// Waterfall layout harness, run by bench_waterfall.py in its own
// `quickshell -p <tmp>` against a temp STICKIES_STATE (the live notes and
// shell are not touched). Modes:
//   shot   switch through BENCH_LAYOUTS, printing `BENCH_SHOT <layout>` once
//          each has settled (the script screenshots then), and the column's
//          geometry per layout in the final BENCH line
//   bench  switch-animation frame times (free <-> waterfall, both ways,
//          BENCH_SWITCHES times) and scrolling the column top -> bottom -> top
//   interact  reorder, drag out, drop back, collapse, typing into a column
//          note (real keys, only once the focus grab holds the keyboard), a
//          layout change from the CLI, and free positions after it all
//   follow  the column moves with monitor focus: focus BENCH_FOLLOW_TO,
//          then back to where focus was
ShellRoot {
  id: harness

  readonly property string mode: Quickshell.env("BENCH_MODE") || "bench"
  readonly property var layouts: (Quickshell.env("BENCH_LAYOUTS") || "free,waterfall-right,waterfall-left").split(",")
  readonly property int switches: parseInt(Quickshell.env("BENCH_SWITCHES") || "10")
  readonly property real scrollStep: parseFloat(Quickshell.env("BENCH_SCROLL_STEP") || "12")
  readonly property real budget: 1000 / parseFloat(Quickshell.env("BENCH_HZ") || "60.02")

  property var result: ({ layouts: {} })
  ElapsedTimer { id: clock }

  // Shot mode with BENCH_WALL: a plain backdrop on the Background layer of
  // the column's screen, under the windows, so nothing of the live
  // wallpaper ends up in the picture. BENCH_LIGHT: a light theme palette.
  readonly property string wallpaper: Quickshell.env("BENCH_WALL") || ""
  Variants {
    model: harness.mode === "shot" && harness.wallpaper
           ? Quickshell.screens.filter(s => s.name === Quickshell.env("STICKIES_COLUMN_SCREEN")) : []
    PanelWindow {
      required property var modelData
      screen: modelData
      anchors { top: true; bottom: true; left: true; right: true }
      exclusionMode: ExclusionMode.Ignore
      WlrLayershell.layer: WlrLayer.Background
      WlrLayershell.namespace: "stickies-bench-backdrop"
      color: "transparent"
      Image { anchors.fill: parent; source: "file://" + harness.wallpaper; fillMode: Image.PreserveAspectCrop }
    }
  }
  Component.onCompleted: {
    if (Quickshell.env("BENCH_LIGHT") === "1") {
      Color.background = "#efece6"
      Color.foreground = "#2a2926"
      Color.accent = "#3d6e9c"
    }
  }

  Service { id: svc }

  function log(obj) { console.log("BENCH " + JSON.stringify(obj)) }

  function stats(xs) {
    var s = xs.slice().sort((a, b) => a - b)
    if (!s.length) return null
    function q(p) { return s[Math.min(s.length - 1, Math.floor(p * (s.length - 1) + 0.5))] }
    var sum = 0
    for (var i = 0; i < s.length; i++) sum += s[i]
    return { n: s.length, mean: +(sum / s.length).toFixed(2), p50: +q(0.5).toFixed(2),
             p95: +q(0.95).toFixed(2), p99: +q(0.99).toFixed(2), max: +s[s.length - 1].toFixed(2) }
  }

  // Frame swaps of the column surface (the one that animates).
  property bool recording: false
  property var swaps: []
  Connections {
    target: svc.column ? svc.column.Window.window : null
    function onFrameSwapped() {
      if (!harness.recording) return
      var t = clock.elapsed() * 1000
      harness.swaps.push(t)
      if (harness.firstAt < 0) harness.firstAt = t
      // Every card at rest where it is going (column slot or free spot).
      if (harness.landedAt < 0 && t - harness.switchAt > 20 && svc.column.settled()) harness.landedAt = t
    }
  }
  function intervals(xs) {
    var iv = []
    for (var i = 1; i < xs.length; i++) iv.push(xs[i] - xs[i - 1])
    return iv
  }

  Timer {
    id: waitListed
    running: true
    repeat: true
    interval: 50
    property int tries: 0
    onTriggered: {
      if (svc.listed && svc.desktops.length) { stop(); settle.start(); return }
      if (++tries > 200) { stop(); harness.log({ error: "never listed" }); Qt.quit() }
    }
  }
  Timer {
    id: settle
    interval: 800
    onTriggered: harness.mode === "shot" ? harness.nextShot()
                 : harness.mode === "interact" ? harness.nextStep()
                 : harness.mode === "follow" ? harness.nextFollow() : harness.nextSwitch()
  }

  // ---------------------------------------------------------------- shot
  property int shotIndex: -1
  function nextShot() {
    shotIndex++
    if (shotIndex >= layouts.length) { done(); return }
    // "waterfall-right+collapsed", "waterfall-left+reserve": a layout plus flags.
    var parts = layouts[shotIndex].split("+")
    svc.setSettings({ layout: parts[0], waterfall_collapsed: parts.indexOf("collapsed") > 0,
                      waterfall_reserve: parts.indexOf("reserve") > 0 })
    shotSettle.start()
  }
  Timer {
    id: shotSettle
    interval: 700
    onTriggered: {
      var l = harness.layouts[harness.shotIndex]
      var col = svc.column
      var cards = []
      if (svc.waterfall) {
        var ids = col.memberIds()
        for (var i = 0; i < ids.length; i++) {
          var t = col.slotMap[ids[i]]
          cards.push({ nid: ids[i], x: t.x, y: t.y + col.geo.column.y - col.scroll, w: t.w, h: t.h })
        }
      }
      var shown = 0
      for (var d = 0; d < svc.desktops.length; d++)
        for (var j = 0; j < svc.notes.count; j++) {
          var dc = svc.desktops[d].cardFor(svc.notes.get(j).nid)
          if (dc && dc.visible) shown++
        }
      harness.result.layouts[l] = { column_screen: col.screenName, screen: [col.width, col.height],
                                    geometry: col.geo, column_cards: cards, desktop_cards: shown,
                                    scrollable: col.maxScroll, input_regions: col.regions.length,
                                    column_visible: svc.columnWindow.visible, collapsed: svc.colCollapsed,
                                    reserve: svc.colReserve }
      console.log("BENCH_SHOT " + l)
      shotHold.start()
    }
  }
  Timer { id: shotHold; interval: 900; onTriggered: harness.nextShot() }

  // ---------------------------------------------------------------- interact
  // The column's pointer paths through NoteCard's own drag API (the header
  // MouseArea calls the same beginMove/moveTo/endMove; there is no pointer
  // injection on this machine), checked against what serve stored.
  property var checks: ({})
  property var seeded: ({})
  property int stepNo: 0
  property var steps: [
    function() {
      for (var i = 0; i < svc.notes.count; i++) { var r = svc.notes.get(i); seeded[r.nid] = [r.x0, r.y0, r.w, r.h] }
      svc.setLayout("waterfall-right")
      return 500
    },
    function() {  // reorder: the third card dragged to the top
      var col = svc.column
      var ids = col.memberIds()
      harness.checks.order_before = ids
      var c = col.cardFor(ids[2])
      c.beginMove()
      for (var k = 1; k <= 6; k++) c.moveTo(c.liveX, c.liveY - k * 80)
      c.moveTo(c.liveX, -60)
      c.endMove()
      harness.dragged = ids[2]
      return 500
    },
    function() {
      var ids = svc.column.memberIds()
      harness.checks.order_after = ids
      harness.checks.reorder_ok = ids[0] === harness.dragged && svc.colOrder.indexOf(harness.dragged) >= 0
      return dbCheck("settings", {}, function(st) {
        harness.checks.reorder_stored = st.waterfall_order.slice(-ids.length).join(",") === ids.join(",")
      })
    },
    function() {  // drag out onto the desktop at (700, 300) on screen
      var col = svc.column
      var ids = col.memberIds()
      var c = col.cardFor(ids[1])
      harness.outNid = ids[1]
      c.beginMove()
      c.moveTo(400, c.liveY)
      c.moveTo(700, 300 - col.geo.column.y + col.scroll)
      c.endMove()
      return 500
    },
    function() {
      var col = svc.column
      var d = svc.desktopFor(col.screenName)
      var dc = d.cardFor(harness.outNid)
      harness.checks.out_not_in_column = col.memberIds().indexOf(harness.outNid) < 0
      harness.checks.out_desktop_card = !!dc && dc.visible ? [dc.x, dc.y] : null
      return dbCheck("show", { id: harness.outNid }, function(n) {
        harness.checks.out_stored_xy = [n.x, n.y]
        harness.checks.out_ok = n.x === 700 && n.y === 300
      })
    },
    function() {
      return dbCheck("settings", {}, function(st) {
        harness.checks.out_in_free_list = st.waterfall_free.indexOf(harness.outNid) >= 0
      })
    },
    function() {  // and back: dropped on the column, its free spot unchanged
      var col = svc.column
      var dc = svc.desktopFor(col.screenName).cardFor(harness.outNid)
      dc.beginMove()
      dc.moveTo(col.geo.column.x + 40, 200)
      dc.endMove()
      return 500
    },
    function() {
      harness.checks.dock_back_in_column = svc.column.memberIds().indexOf(harness.outNid) >= 0
      return dbCheck("show", { id: harness.outNid }, function(n) {
        harness.checks.dock_back_kept_free_xy = n.x === 700 && n.y === 300
      })
    },
    function() {  // collapse
      svc.toggleCollapsed()
      return 400
    },
    function() {
      var col = svc.column
      var c = col.cardFor(col.memberIds()[0])
      harness.checks.collapsed_card_parked = c.x === col.geo.parkX
      harness.checks.collapsed_regions = col.regions.length
      return dbCheck("settings", {}, function(st) { harness.checks.collapsed_stored = st.waterfall_collapsed === true })
    },
    function() { svc.toggleCollapsed(); return 400 },
    function() {  // typing: the caret in a column note, the focus grab, real keys
      var c = svc.column.cardFor(svc.column.memberIds()[0])
      harness.typeCard = c
      c.focusEditor()
      return 700
    },
    function() {
      var c = harness.typeCard
      var w = c.Window.window
      harness.checks.type_focus = { editor: c.editor.activeFocus, window_active: !!w && w.active,
                                    column_editing: svc.columnEditing }
      if (!harness.typing || !c.editor.activeFocus || !w || !w.active) {
        harness.checks.type_skipped = true
        return 50
      }
      c.editor.cursorPosition = c.editor.length
      wtype.running = true
      return 2000
    },
    function() {
      if (!harness.checks.type_skipped)
        harness.checks.type_text_in_editor = harness.typeCard.editor.text.indexOf(harness.typeText) >= 0
      svc.leaveFront(true)
      return 600
    },
    function() {
      if (harness.checks.type_skipped) return 50
      return dbCheck("show", { id: harness.typeCard.nid }, function(n) {
        harness.checks.type_saved = n.body.indexOf(harness.typeText) >= 0
      })
    },
    function() {  // an agent switches the layout from the CLI
      harness.cliAt = clock.elapsed()
      cliProc.running = true
      return 50
    },
    function() {
      if (svc.layout !== "waterfall-left" && clock.elapsed() - harness.cliAt < 3) return -30   // poll
      harness.checks.cli_to_surface_ms = Math.round((clock.elapsed() - harness.cliAt) * 1000)
      harness.checks.cli_followed = svc.layout === "waterfall-left"
      svc.setLayout("free")
      return 500
    },
    function() {  // free again: every note exactly where it was (but the one dragged out)
      return dbCheck("list", {}, function(notes) {
        var moved = []
        for (var i = 0; i < notes.length; i++) {
          var n = notes[i], s = harness.seeded[n.id]
          if (s && (n.x !== s[0] || n.y !== s[1] || n.w !== s[2] || n.h !== s[3])) moved.push(n.id)
        }
        harness.checks.free_positions_changed = moved
        harness.checks.free_ok = moved.length === 1 && moved[0] === harness.outNid
        var shown = 0
        var d = svc.desktopFor(svc.column.screenName)
        for (var j = 0; j < notes.length; j++) { var dc = d.cardFor(notes[j].id); if (dc && dc.visible) shown++ }
        harness.checks.free_desktop_cards = shown
      })
    }
  ]
  property int dragged: -1
  property int outNid: -1
  property var typeCard: null
  property real cliAt: 0
  readonly property bool typing: Quickshell.env("BENCH_TYPE") !== "0"
  readonly property string typeText: " typed in the column"
  property bool waitingDb: false

  // Ask serve directly (what is stored, not what the model shows).
  function dbCheck(op, args, cb) {
    waitingDb = true
    svc.request(op, args, function(ok, r) { if (ok) cb(r); else harness.checks["error_" + op] = r; harness.waitingDb = false })
    return 20
  }
  function nextStep() {
    if (stepNo >= steps.length) { result.interact = checks; done(); return }
    var d = steps[stepNo]()
    if (d >= 0) stepNo++
    stepTimer.interval = Math.abs(d)
    stepTimer.start()
  }
  Timer {
    id: stepTimer
    onTriggered: {
      if (harness.waitingDb) { start(); return }
      harness.nextStep()   // a step returning a negative delay runs again (polling)
    }
  }

  Process {
    id: wtype
    command: ["wtype", "-d", "30", harness.typeText]
  }
  Process {
    id: cliProc
    command: ["sh", "-c", "exec $STICKIES_CMD layout waterfall-left"]
  }

  // ---------------------------------------------------------------- follow
  readonly property string followTo: Quickshell.env("BENCH_FOLLOW_TO") || ""
  property string followFrom: ""
  property int followStep: 0
  property var follow: []
  function followState(tag) {
    var col = svc.column
    follow.push({ step: tag, focused: Hyprland.focusedMonitor ? Hyprland.focusedMonitor.name : "",
                  column_screen: col.screenName, column_size: [col.width, col.height],
                  column_x: col.geo.column.x, workspace: svc.focusedWs, cards: col.memberIds().length })
  }
  function nextFollow() {
    if (followStep === 0) {
      followFrom = Hyprland.focusedMonitor ? Hyprland.focusedMonitor.name : ""
      svc.setLayout("waterfall-right")
    } else if (followStep === 1) {
      followState("start")
      Hyprland.dispatch("hl.dsp.focus({ monitor = \"" + followTo + "\" })")
    } else if (followStep === 2) {
      followState("focused " + followTo)
      Hyprland.dispatch("hl.dsp.focus({ monitor = \"" + followFrom + "\" })")
    } else {
      followState("back on " + followFrom)
      result.follow = follow
      done()
      return
    }
    followStep++
    followTimer.start()
  }
  Timer { id: followTimer; interval: 600; onTriggered: harness.nextFollow() }

  // ---------------------------------------------------------------- switch
  property int switchNo: 0
  property var toWaterfall: []
  property var toFree: []
  property var glideFrames: []
  property var settleTimes: [[], []]   // [to waterfall, to free]
  property var firstTimes: [[], []]
  property real firstAt: -1
  property real landedAt: -1
  property real switchAt: 0
  function nextSwitch() {
    if (switchNo >= switches * 2) { startScroll(); return }
    var target = switchNo % 2 === 0 ? "waterfall-right" : "free"
    swaps = []
    recording = true
    switchAt = clock.elapsed() * 1000
    firstAt = -1
    landedAt = -1
    svc.setLayout(target)
    switchEnd.start()
  }
  Timer {
    id: switchEnd
    interval: 400   // > the 180 ms glide; swaps stop when it ends
    onTriggered: {
      harness.recording = false
      // Only the frames of the glide itself (swap times within 200 ms of
      // the switch, plus the first one after it).
      var xs = harness.swaps.filter(t => t - harness.switchAt <= 200)
      var iv = harness.intervals([harness.switchAt].concat(xs).slice(1))
      ;(harness.switchNo % 2 === 0 ? harness.toWaterfall : harness.toFree).push.apply(
        harness.switchNo % 2 === 0 ? harness.toWaterfall : harness.toFree, iv)
      harness.glideFrames.push(xs.length)
      var dirIx = harness.switchNo % 2
      if (harness.landedAt > 0) harness.settleTimes[dirIx].push(harness.landedAt - harness.switchAt)
      if (harness.firstAt > 0) harness.firstTimes[dirIx].push(harness.firstAt - harness.switchAt)
      harness.switchNo++
      switchGap.start()
    }
  }
  Timer { id: switchGap; interval: 250; onTriggered: harness.nextSwitch() }

  // ---------------------------------------------------------------- scroll
  property var tickTimes: []
  property int dir: 1
  property int ticks: 0
  function startScroll() {
    var all = harness.toWaterfall.concat(harness.toFree)
    result.switch = { switches: switches, frames_per_glide: stats(glideFrames),
                      to_waterfall_interval_ms: stats(toWaterfall), to_free_interval_ms: stats(toFree),
                      missed_frames: all.filter(x => x > budget * 1.5).length,
                      to_waterfall_first_frame_ms: stats(firstTimes[0]),
                      to_waterfall_all_landed_ms: stats(settleTimes[0]),
                      to_free_first_frame_ms: stats(firstTimes[1]),
                      to_free_all_landed_ms: stats(settleTimes[1]) }
    svc.setLayout("waterfall-right")
    scrollGo.start()
  }
  Timer {
    id: scrollGo
    interval: 600
    onTriggered: {
      var col = svc.column
      var mids = col.memberIds()
      harness.result.scroll = { column_notes: mids.length, notes: svc.notes.count, content_h: col.contentH,
                                view_h: col.geo.column.h, max_scroll: col.maxScroll }
      col.scroll = 0
      harness.swaps = []
      harness.recording = true
      scrollAnim.start()
    }
  }
  FrameAnimation {
    id: scrollAnim
    running: false
    onTriggered: {
      var col = svc.column
      var t0 = clock.elapsed()
      col.scrollBy(harness.dir * harness.scrollStep)
      harness.tickTimes.push((clock.elapsed() - t0) * 1000)
      harness.ticks++
      if (harness.dir > 0 && col.scroll >= col.maxScroll) harness.dir = -1
      else if (harness.dir < 0 && col.scroll <= 0) {
        stop()
        harness.recording = false
        var iv = harness.intervals(harness.swaps)
        harness.result.scroll.ticks = harness.ticks
        harness.result.scroll.frames = harness.swaps.length
        harness.result.scroll.frame_interval_ms = harness.stats(iv)
        harness.result.scroll.missed_frames = iv.filter(x => x > harness.budget * 1.5).length
        harness.result.scroll.js_per_tick_ms = harness.stats(harness.tickTimes)
        harness.done()
      }
    }
  }

  function done() {
    log(result)
    Qt.quit()
  }
}
