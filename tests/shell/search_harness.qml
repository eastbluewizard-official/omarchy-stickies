import QtQuick
import Quickshell
import "stickies"

// Search overlay harness for bench_search.py: the real Service (and so the
// real SearchOverlay and `stickies serve`), in its own `quickshell -p`
// with a temp STICKIES_STATE whose notes are already embedded. Prints one
// `BENCH {...}` line.
ShellRoot {
  id: harness

  readonly property string target: Quickshell.env("BENCH_TARGET") || "dentist appointment"
  readonly property int targetNid: parseInt(Quickshell.env("BENCH_TARGET_NID") || "1")
  readonly property bool shot: Quickshell.env("BENCH_SHOT") === "1"
  property bool shotDone: false

  property var result: ({})
  ElapsedTimer { id: clock }
  function ms() { return clock.elapsed() * 1000 }  // elapsed() is in seconds
  function log(obj) { console.log("BENCH " + JSON.stringify(obj)) }

  Service { id: svc }
  readonly property var ov: svc.searchOverlay

  function stats(xs) {
    var s = xs.slice().sort((a, b) => a - b)
    if (!s.length) return null
    function q(p) { return s[Math.min(s.length - 1, Math.floor(p * (s.length - 1) + 0.5))] }
    return { n: s.length, p50: +q(0.5).toFixed(1), p95: +q(0.95).toFixed(1), max: +s[s.length - 1].toFixed(1) }
  }

  // Keystrokes, as typed: every prefix of these.
  readonly property var words: ["cardmarket fees", "tandarts", "dentist appointment",
                                "money owed to people", "btw aangifte", "zzzz"]
  property var prefixes: {
    var out = []
    for (var i = 0; i < words.length; i++)
      for (var j = 1; j <= words[i].length; j++) out.push(words[i].slice(0, j))
    return out
  }

  property int step: 0
  property real t0: 0
  property real waitUntil: 0
  property var opens: []
  property bool waitingFrame: false
  property var keyToResults: []
  property var keyToFrame: []
  property string lastShown: ""
  property int k: 0
  property int seqAt: 0
  property real landed: 0
  property var bursts: []
  property int burstWord: 0

  // Frames of the overlay window: closes "open" and "results on screen".
  property var hooked: null
  function hook() {
    var w = ov.field.Window.window
    if (w && w !== hooked) { w.frameSwapped.connect(harness.frame); hooked = w }
  }
  function frame() {
    if (!waitingFrame) return
    waitingFrame = false
    if (step === 1) {
      opens.push(ms() - t0)
      ov.close()
      waitUntil = ms() + 200
    } else if (step === 2) {
      keyToFrame.push(ms() - t0)
      k++
      waitUntil = ms() + 5
    }
  }

  Timer {
    interval: 4
    repeat: true
    running: true
    onTriggered: harness.tick()
  }

  function tick() {
    if (ms() < waitUntil) return
    if (waitingFrame) { hook(); return }
    if (step === 0) {
      if (!svc.ready || svc.notes.count === 0) return
      if (!svc.semanticLoaded) {
        if (ms() > 20000) { result.semantic = false; step = 1 }
        return  // serve pushes {"event": "semantic", "loaded": true} once the model is loaded
      }
      result.semantic = true
      result.notes = svc.notes.count
      result.ready_ms = Math.round(ms())
      step = 1
    } else if (step === 1) {
      // open -> the overlay's first swapped frame, 5x
      if (opens.length >= 5) { result.open_ms = stats(opens); step = 2; ov.open(); waitUntil = ms() + 300; return }
      t0 = ms()
      waitingFrame = true
      ov.open()
    } else if (step === 2) {
      // one key at a time: text change -> results landed -> next frame
      if (k >= prefixes.length) {
        result.key_to_results_ms = stats(keyToResults)
        result.changed_list_key_to_frame_ms = stats(keyToFrame)
        step = 3
        burstWord = 0
        return
      }
      if (seqAt === 0) {
        seqAt = ov.resultsSeq + 1
        t0 = ms()
        ov.field.text = prefixes[k]
        if (!prefixes[k].trim()) { seqAt = 0; k++ }
        return
      }
      if (ov.resultsSeq < seqAt || ov.searchedFor !== prefixes[k].trim()) {
        if (ms() - t0 > 5000) { result.error = "no results for " + prefixes[k]; finish() }
        return
      }
      keyToResults.push(ms() - t0)
      seqAt = 0
      // Only a changed list has to reach the screen; an unchanged one
      // repaints nothing, so there is no frame to wait for.
      var shown = JSON.stringify(ov.hits.map(h => [h.id, h.snippet, h.highlights]))
      if (shown !== lastShown) { lastShown = shown; waitingFrame = true }
      else k++
    } else if (step === 3) {
      // burst typing, 60 ms per key: last key -> final results landed
      if (burstWord >= 3) { result.burst_last_key_to_results_ms = stats(bursts); step = 4; return }
      var w = ["money owed to people whose cards I sell", "cardmarket commission", "afspraak bij de dokter"][burstWord]
      ov.field.text = ""
      burstWord++
      typeBurst(w, 0)
      step = 31
    } else if (step === 31) {
      if (burstTyping || !burstText) return
      if (ov.searchedFor === burstText && !ov.inFlight) {
        bursts.push(ms() - burstLastKey)
        burstText = ""
        step = 3
      } else if (ms() - burstLastKey > 5000) {
        result.error = "burst never settled: " + burstText
        finish()
      }
    } else if (step === 4) {
      // Enter on the target query: jump + flash
      ov.field.text = target
      if (ov.searchedFor !== target) return
      if (shot && !shotDone) { shotDone = true; console.log("SHOT " + harness.rect(ov.card)); waitUntil = ms() + 2500; return }
      result.target_top_hit = ov.hits.length ? ov.hits[0].id : null
      result.target_top_match = ov.hits.length ? ov.hits[0].match : null
      t0 = ms()
      ov.pick(0)
      step = 5
    } else if (step === 5) {
      var c = svc.desktops.length ? svc.desktops[0].cardFor(targetNid) : null
      if (c && c.flashing) {
        result.enter_to_flash_ms = +(ms() - t0).toFixed(1)
        result.overlay_closed = !ov.opened
        step = 6
        waitUntil = ms() + 400
      } else if (ms() - t0 > 3000) {
        result.enter_to_flash_ms = null
        step = 6
      }
    } else if (step === 6) {
      finish()
    }
  }

  property real burstLastKey: 0
  property string burstText: ""
  property bool burstTyping: false
  function typeBurst(word, i) {
    if (i < word.length) {
      burstTyping = true
      ov.field.text = word.slice(0, i + 1)
      burstLastKey = ms()
      if (i + 1 < word.length) {
        burstTimer.next = () => harness.typeBurst(word, i + 1)
        burstTimer.restart()
        return
      }
    }
    burstText = word
    burstTyping = false
  }
  Timer {
    id: burstTimer
    interval: 60
    property var next: null
    onTriggered: if (next) next()
  }

  // "x y w h" of an item in its window, for a cropped screenshot.
  function rect(item) {
    var p = item.mapToItem(null, 0, 0)
    return [Math.round(p.x), Math.round(p.y), Math.round(item.width), Math.round(item.height)].join(" ")
  }

  function finish() {
    log(result)
    Qt.quit()
  }

  Timer {
    interval: 120000
    running: true
    onTriggered: { harness.result.error = "timeout at step " + harness.step; harness.finish() }
  }
}
