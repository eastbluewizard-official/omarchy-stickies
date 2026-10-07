import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import "stickies"

// Harness that runs the stickies plugin outside the live shell: copied to
// <tmp>/shell.qml next to symlinks `Commons` (the system shell's) and
// `stickies` (this repo: the plugin is its root), then run with
// `quickshell -p <tmp>` and a temp STICKIES_STATE. Results are printed as one
// `BENCH {...}` line.
ShellRoot {
  id: harness

  readonly property string mode: Quickshell.env("BENCH_MODE") || "bench"
  readonly property int nid: parseInt(Quickshell.env("BENCH_NID") || "1")
  readonly property string wallpaper: Quickshell.env("BENCH_WALL") || ""
  readonly property int dragFrames: parseInt(Quickshell.env("BENCH_DRAG_FRAMES") || "300")
  readonly property bool typing: Quickshell.env("BENCH_TYPE") !== "0"
  readonly property string typeText: Quickshell.env("BENCH_TYPE_TEXT") || "the quick brown fox jumps over the lazy dog again and again"

  property var card: null
  property var result: ({})

  ElapsedTimer { id: clock }

  // Screenshot mode only: the wallpaper on an Overlay surface mapped before
  // the plugin's, so grim sees notes on the desktop and nothing private.
  Variants {
    model: harness.mode === "shot" ? Quickshell.screens.slice(0, 1) : []
    PanelWindow {
      required property var modelData
      screen: modelData
      anchors { top: true; bottom: true; left: true; right: true }
      exclusionMode: ExclusionMode.Ignore
      WlrLayershell.layer: WlrLayer.Overlay
      WlrLayershell.namespace: "stickies-bench-backdrop"
      color: Color.background
      Image {
        anchors.fill: parent
        source: harness.wallpaper ? "file://" + harness.wallpaper : ""
        fillMode: Image.PreserveAspectCrop
      }
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

  // Wait for the card to exist and be on screen.
  Timer {
    id: waitCard
    running: true
    repeat: true
    interval: 16
    property int tries: 0
    onTriggered: {
      tries++
      // First-run shot (BENCH_NID=0): nothing to wait for but the list.
      if (harness.nid <= 0 && svc.listed) { stop(); settle.start(); return }
      if (svc.desktops.length) {
        var c = svc.desktops[0].cardFor(harness.nid)
        if (c && c.visible) {
          stop()
          harness.card = c
          harness.result.notes = svc.notes.count
          harness.result.startup_to_card_ms = Math.round(clock.elapsed() * 1000)
          settle.start()
          return
        }
      }
      if (tries > 600) { stop(); harness.log({ error: "card never appeared" }); Qt.quit() }
    }
  }

  Timer {
    id: settle
    interval: 1200
    onTriggered: {
      if (harness.mode === "shot") {
        // Dark variant: override the theme colours in this throwaway
        // instance only (the live theme is untouched).
        if (Quickshell.env("BENCH_DARK") === "1") {
          Color.background = "#1b1d1f"
          Color.foreground = "#d6d6d2"
          Color.accent = "#8fb8de"
        } else if (Quickshell.env("BENCH_LIGHT") === "1") {
          Color.background = "#efece6"
          Color.foreground = "#2a2926"
          Color.accent = "#3d6e9c"
        }
        darkSettle.start()
        return
      }
      harness.startDrag()
    }
  }

  Timer {
    id: darkSettle
    interval: 300
    onTriggered: console.log("BENCH_READY")
  }

  // ---------------------------------------------------------------- drag
  // Drives the same beginMove/moveTo/endMove path the header MouseArea
  // uses, one step per animation tick, and records the interval between
  // frames the window actually swapped.
  property var swapTimes: []
  property var tickTimes: []
  property bool recording: false
  property int dragTick: 0
  property real cx: 0
  property real cy: 0

  Connections {
    target: harness.card ? harness.card.Window.window : null
    function onFrameSwapped() { if (harness.recording) harness.swapTimes.push(clock.elapsed()) }
  }

  FrameAnimation {
    id: dragAnim
    running: false
    onTriggered: {
      var t0 = clock.elapsed()
      var a = harness.dragTick / 60 * Math.PI
      harness.card.moveTo(harness.cx + 260 * Math.sin(a), harness.cy + 160 * Math.sin(2 * a))
      harness.tickTimes.push((clock.elapsed() - t0) * 1000)
      harness.dragTick++
      if (harness.dragTick >= harness.dragFrames) harness.endDrag()
    }
  }

  function startDrag() {
    cx = card.x; cy = card.y
    card.beginMove()
    swapTimes = []; tickTimes = []
    recording = true
    dragTick = 0
    dragAnim.start()
  }

  function endDrag() {
    dragAnim.stop()
    recording = false
    card.endMove()
    var iv = []
    for (var i = 1; i < swapTimes.length; i++) iv.push((swapTimes[i] - swapTimes[i - 1]) * 1000)
    var budget = 1000 / 60.02
    var missed = iv.filter(x => x > budget * 1.5).length
    result.drag = { frames: swapTimes.length, ticks: dragTick,
                    frame_interval_ms: stats(iv), missed_frames: missed,
                    js_work_per_tick_ms: stats(tickTimes) }
    if (typing) typeStart.start()
    else finish()
  }

  // ---------------------------------------------------------------- typing
  // Real key events from wtype (virtual-keyboard protocol) into the note's
  // editor; per keystroke: time from the text changing to the next frame
  // swap that contains it.
  property var keyTimes: []
  property var keyLat: []
  property real pendingKey: -1
  property real changeAt: -1

  Timer {
    id: typeStart
    interval: 300
    onTriggered: {
      harness.card.focusEditor()
      typeGo.start()
    }
  }

  Timer {
    id: typeGo
    interval: 600
    onTriggered: {
      var w = harness.card.Window.window
      if (!harness.card.editor.activeFocus || !w || !w.active) {
        harness.result.typing = { error: "editor has no keyboard focus; not typing (wtype would go elsewhere)" }
        harness.finish()
        return
      }
      harness.recording = false
      wtype.running = true
    }
  }

  Connections {
    target: harness.card ? harness.card.editor : null
    // If focus goes elsewhere mid-run, stop typing at once so no stray
    // keys land in whatever window got it.
    function onActiveFocusChanged() {
      if (!harness.card.editor.activeFocus && wtype.running) {
        harness.result.focus_lost_after_keys = harness.keyTimes.length
        wtype.running = false
      }
    }
  }
  // A keystroke counts from the moment the editor's text changed (the
  // styler's `changing`, before it reads and styles the line); its own
  // formatting changes `text` too, so textChanged would count those.
  Connections {
    target: harness.card ? harness.card.styler : null
    function onChanging() {
      if (wtype.running) harness.changeAt = clock.elapsed()
    }
    function onPlainChanged() {
      if (!wtype.running || harness.changeAt < 0) return
      harness.pendingKey = harness.changeAt
      harness.changeAt = -1
      harness.keyTimes.push(harness.pendingKey)
    }
  }
  Connections {
    target: harness.card ? harness.card.Window.window : null
    function onFrameSwapped() {
      if (harness.pendingKey < 0) return
      harness.keyLat.push((clock.elapsed() - harness.pendingKey) * 1000)
      harness.pendingKey = -1
    }
  }

  Process {
    id: wtype
    command: ["wtype", "-d", "45", harness.typeText]
    property int code: -1
    stderr: StdioCollector { id: wtypeErr }
    onExited: (code, status) => { wtype.code = code; flushWait.start() }
  }

  Timer {
    id: flushWait
    interval: 800  // > 300 ms debounce, so the edit reaches serve
    onTriggered: {
      harness.result.typing = { keys_sent: harness.typeText.length, keys_seen: harness.keyTimes.length,
                                text_to_swap_ms: harness.stats(harness.keyLat),
                                slowest_keys: harness.keyLat.map((v, i) => [i, +v.toFixed(1)])
                                                    .sort((a, b) => b[1] - a[1]).slice(0, 3),
                                body_saved: harness.card.body.indexOf(harness.typeText) >= 0,
                                wtype_exit: wtype.code, wtype_stderr: wtypeErr.text,
                                focus_at_end: harness.card.editor.activeFocus,
                                window_active_at_end: harness.card.Window.window.active,
                                editor_text: harness.card.styler.plain }
      harness.finish()
    }
  }

  // ---------------------------------------------------------------- external
  // A write from another process (an agent running the CLI) must show up
  // on the desktop by itself: time from the CLI call to the card existing.
  property real extStart: 0
  property int extId: -1

  function finish() { externalAdd() }

  function externalAdd() {
    extStart = clock.elapsed()
    extProc.running = true
  }

  Process {
    id: extProc
    command: ["sh", "-c", "exec $STICKIES_CMD --json add 'added by an agent' --x 300 --y 640"]
    stdout: StdioCollector {
      onStreamFinished: {
        try { harness.extId = JSON.parse(text).id } catch (e) { harness.extId = -1 }
        harness.result.external = { cli_exit_ms: Math.round((clock.elapsed() - harness.extStart) * 1000) }
        extWait.start()
      }
    }
  }

  Timer {
    id: extWait
    interval: 8
    repeat: true
    property int tries: 0
    onTriggered: {
      tries++
      var c = svc.desktops[0].cardFor(harness.extId)
      if (c || tries > 250) {
        stop()
        harness.result.external.appeared = !!c
        harness.result.external.cli_start_to_card_ms = Math.round((clock.elapsed() - harness.extStart) * 1000)
        harness.done()
      }
    }
  }

  function done() {
    log(result)
    Qt.quit()
  }
}
