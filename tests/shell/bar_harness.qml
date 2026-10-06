import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import "stickies"

// Bar widget harness for bench_bar.py: the real Service + BarWidget, with a
// stub standing in for omarchy.bar (the scoped shell facade's serviceFor,
// tooltip/popout no-ops). Runs in its own `quickshell -p` with a temp
// STICKIES_STATE; prints one `BENCH {...}` line.
ShellRoot {
  id: harness

  property var result: ({})
  ElapsedTimer { id: clock }
  function ms() { return clock.elapsed() * 1000 }  // elapsed() is in seconds

  Service { id: svc }

  QtObject {
    id: stubShell
    function serviceFor(id) { return id === "eastbluewizard.stickies" ? svc : null }
  }

  QtObject {
    id: stubBar
    property var shell: stubShell
    property color barForeground: Color.foreground
    property color foreground: Color.foreground
    property color urgent: Color.urgent
    property string fontFamily: Style.font.family
    property string position: "top"
    property bool vertical: false
    property int barSize: 28
    property bool foregroundAnimationEnabled: true
    property var activePopout: null
    property var clickTargets: []
    function registerClickTarget(t) {}
    function unregisterClickTarget(t) {}
    function showTooltip(t, s) {}
    function hideTooltip(t) {}
    function requestPopout(o) { activePopout = o }
    function releasePopout(o) { if (activePopout === o) activePopout = null }
    function switchPanelFrom(o, d) { return false }
    function targetBelongsToWindow(t, w) { return false }
    function moduleWidgets(id) { return [widget] }
  }

  PanelWindow {
    id: barWindow
    screen: Quickshell.screens[0]
    anchors { top: true; right: true }
    implicitWidth: 120
    implicitHeight: stubBar.barSize
    color: Color.background
    exclusionMode: ExclusionMode.Ignore
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.namespace: "stickies-bench-bar"

    BarWidget {
      id: widget
      anchors.right: parent.right
      height: parent.height
      bar: stubBar
    }
  }

  function log(obj) { console.log("BENCH " + JSON.stringify(obj)) }

  function panel() { return widget.children.length ? findPanel() : null }
  function findPanel() {
    for (var i = 0; i < widget.children.length; i++) {
      var c = widget.children[i]
      if (c.item && c.item.popup) return c.item
    }
    return null
  }

  function stats(xs) {
    var s = xs.slice().sort((a, b) => a - b)
    if (!s.length) return null
    function q(p) { return s[Math.min(s.length - 1, Math.floor(p * (s.length - 1) + 0.5))] }
    return { n: s.length, p50: +q(0.5).toFixed(1), p95: +q(0.95).toFixed(1), max: +s[s.length - 1].toFixed(1) }
  }

  // ---- steps, driven by the service's state
  property int step: 0
  property var opens: []
  property real t0: 0
  property bool waitingFrame: false
  property int expectCount: -1
  property var captures: []
  property var searches: []

  Timer {
    id: ticker
    interval: 16
    repeat: true
    running: svc.ready
    onTriggered: harness.tick()
  }

  property real waitUntil: 0

  function tick() {
    var p = findPanel()
    if (!p || !p.service || harness.ms() < waitUntil) return
    if (step === 0) {
      if (svc.notes.count === 0) return  // first `list` not back yet
      result.count_shown = widget.count
      result.pinned_shown = widget.pinnedCount
      result.service_resolved = widget.service === svc
      step = 1
    } else if (step === 1) {
      // open/close 5 times: open() -> the popup's next swapped frame
      if (opens.length >= 5) { step = 2; return }
      if (p.opened || waitingFrame) return
      waitingFrame = true
      t0 = harness.ms()
      widget.toggle()
    } else if (step === 2) {
      result.open_ms = stats(opens)
      // open -> pinned + recent lists filled (a serve `list` round-trip)
      p.pinnedRows = []
      p.recentRows = []
      t0 = harness.ms()
      widget.open()
      step = 21
    } else if (step === 21) {
      if (!p.recentRows.length) return
      result.rows_ms = +(harness.ms() - t0).toFixed(1)
      result.rows_pinned = p.pinnedRows.length
      result.rows_recent = p.recentRows.length
      // quick capture through the panel's own path, panel open
      expectCount = svc.notes.count + 1
      t0 = harness.ms()
      p.service.newNoteHere("captured from the bar harness")
      step = 3
    } else if (step === 3) {
      if (svc.notes.count < expectCount) return
      captures.push(harness.ms() - t0)
      if (captures.length < 5) {
        expectCount = svc.notes.count + 1
        t0 = harness.ms()
        svc.newNoteHere("capture " + captures.length)
        return
      }
      result.capture_ms = stats(captures)
      result.count_after_capture = widget.count
      step = 4
      nextSearch()
    } else if (step === 5) {
      result.search_ms = stats(searches)
      result.search_hits = p.hits.length
      // show/hide all
      svc.hidden = true
      result.hidden_label = widget.hiddenAll
      svc.hidden = false
      widget.close()
      step = 6
      waitUntil = harness.ms() + 300
    } else if (step === 6) {
      log(result)
      Qt.quit()
    }
  }

  readonly property var queries: ["capt", "capture", "cardm", "fees", "note", "bar harness", "c", "ca"]
  function nextSearch() {
    var p = findPanel()
    if (searches.length >= queries.length * 3) { step = 5; return }
    var q = queries[searches.length % queries.length]
    var start = harness.ms()
    svc.search(q, function(hits) {
      searches.push(harness.ms() - start)
      if (searches.length === 1) p.hits = hits
      Qt.callLater(harness.nextSearch)
    })
  }

  // Popup frames: the first swap after an open() closes the timing. The
  // popup's QQuickWindow only exists once it is mapped, so hook it lazily.
  property var hooked: null
  function hookFrames() {
    var p = findPanel()
    var w = p && p.popup.focusTarget ? p.popup.focusTarget.Window.window : null
    if (w && w !== hooked) { w.frameSwapped.connect(harness.popupFrame); hooked = w }
  }
  Timer { interval: 1; repeat: true; running: harness.waitingFrame; onTriggered: harness.hookFrames() }

  function popupFrame() {
    if (!waitingFrame) return
    var p = findPanel()
    if (!p.opened) return
    opens.push(harness.ms() - t0)
    waitingFrame = false
    widget.close()
    waitUntil = harness.ms() + 250  // let the fade-out finish
  }

  Timer {
    interval: 30000
    running: true
    onTriggered: {
      var p = harness.findPanel()
      harness.log({ error: "timeout", step: harness.step, partial: harness.result,
                    opened: p ? p.opened : null, popupVisible: p ? p.popup.visible : null,
                    hooked: !!harness.hooked, opens: harness.opens })
      Qt.quit()
    }
  }
}
