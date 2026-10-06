import QtQuick
import Quickshell
import "stickies"

// All notes overlay harness for bench_list.py: the real Service (and so the
// real List.qml and `stickies serve`), in its own `quickshell -p` with a
// temp STICKIES_STATE of generated, tagged notes. Prints one `BENCH {...}`.
ShellRoot {
  id: harness

  readonly property int targetNid: parseInt(Quickshell.env("BENCH_TARGET_NID") || "1")
  readonly property string targetTag: Quickshell.env("BENCH_TARGET_TAG") || "cardmarket"
  readonly property bool shot: Quickshell.env("BENCH_SHOT") === "1"

  property var result: ({})
  ElapsedTimer { id: clock }
  function ms() { return clock.elapsed() * 1000 }  // elapsed() is in seconds
  function log(obj) { console.log("BENCH " + JSON.stringify(obj)) }

  Service { id: svc }
  readonly property var ov: svc.listOverlay

  function stats(xs) {
    var s = xs.slice().sort((a, b) => a - b)
    if (!s.length) return null
    function q(p) { return s[Math.min(s.length - 1, Math.floor(p * (s.length - 1) + 0.5))] }
    return { n: s.length, p50: +q(0.5).toFixed(1), p95: +q(0.95).toFixed(1), max: +s[s.length - 1].toFixed(1) }
  }

  readonly property var words: ["cardmarket", "fees stock", "zzzz"]
  property var prefixes: {
    var out = []
    for (var i = 0; i < words.length; i++)
      for (var j = 1; j <= words[i].length; j++) out.push(words[i].slice(0, j))
    return out
  }

  property int step: 0
  property real t0: 0
  property real waitUntil: 0
  property bool waitingFrame: false
  property string waitFor: ""   // "frame", or "rows" (a fresh list landed, then a frame)
  property int seqAt: 0
  property int chAt: 0
  property bool landing: false
  property var landed: []
  // serve's rows for an open landed (a request + 2,000 brief rows parsed).
  Connections {
    target: harness.ov
    function onRowsSeqChanged() {
      if (harness.landing) { harness.landed.push(harness.ms() - harness.t0); harness.landing = false }
    }
  }
  property var opens: []
  property var openRows: []
  property var keys: []
  property var chips: []
  property var scrollGaps: []
  property real lastFrame: 0
  property bool scrolling: false
  property int k: 0
  property int round: 0

  property var hooked: null
  function hook() {
    var w = ov.field.Window.window
    if (w && w !== hooked) { w.frameSwapped.connect(harness.frame); hooked = w }
  }
  function frame() {
    var now = ms()
    if (scrolling) {
      scrollGaps.push(now - lastFrame)
      lastFrame = now
      var v = ov.view
      if (v.contentY + v.height >= v.contentHeight - 1 || scrollGaps.length >= 300) { scrolling = false; return }
      v.contentY = Math.min(v.contentHeight - v.height, v.contentY + 24)  // a brisk wheel scroll
      return
    }
    if (!waitingFrame) return
    if (waitFor === "rows" && ov.rowsChanges < chAt) return
    waitingFrame = false
    if (step === 1) {
      if (waitFor === "frame") {
        opens.push(now - t0)
        // The first (cold) open has no rows yet: also time them on screen.
        if (opens.length === 1) {
          if (ov.rowsChanges >= chAt) openRows.push(now - t0)
          else {
            waitFor = "rows"
            waitingFrame = true
            return
          }
        }
      } else {
        openRows.push(now - t0)
      }
      step = 11  // close once serve's rows have landed
    } else if (step === 3) {
      keys.push(now - t0)
      k++
      waitUntil = now + 5
    } else if (step === 4) {
      chips.push(now - t0)
      waitUntil = now + 30
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
    if (waitingFrame) {
      hook()
      if (ms() - t0 > 5000) { result.error = "no frame at step " + step; finish() }
      return
    }
    if (step === 0) {
      if (!svc.ready || !svc.listed) return
      result.notes = svc.notes.count
      result.ready_ms = Math.round(ms())
      step = 1
    } else if (step === 1) {
      // open -> first frame (the rows it had) and -> fresh rows on screen, 6x
      if (opens.length >= 7) {
        result.cold_open_first_frame_ms = +opens[0].toFixed(1)
        result.cold_open_rows_on_screen_ms = +openRows[0].toFixed(1)
        result.open_first_frame_ms = stats(opens.slice(1))
        result.open_rows_landed_ms = stats(landed.slice(1))
        result.rows = ov.rows.length
        step = 2
        ov.open([])
        waitUntil = ms() + 600
        return
      }
      seqAt = ov.rowsSeq + 1
      chAt = ov.rowsChanges + 1
      waitFor = "frame"
      t0 = ms()
      landing = true
      waitingFrame = true
      ov.open([])
    } else if (step === 11) {
      if (ov.rowsSeq < seqAt) {
        if (ms() - t0 > 5000) { result.error = "rows never landed"; finish() }
        return
      }
      ov.close()
      waitUntil = ms() + 250
      step = 1
    } else if (step === 2) {
      // scroll the full list one frame at a time; the gaps between frames
      if (!scrollGaps.length && !scrolling) {
        hook()
        ov.view.positionViewAtBeginning()
        lastFrame = ms()
        scrolling = true
        ov.view.contentY = 1
        return
      }
      if (scrolling) return
      scrollGaps.shift()  // the first gap includes the jump back to the top
      result.scroll_frame_gap_ms = stats(scrollGaps)
      result.scroll_frames = scrollGaps.length
      ov.view.positionViewAtBeginning()
      step = 3
    } else if (step === 3) {
      // filter as you type: text change -> next frame
      if (k >= prefixes.length) {
        result.filter_key_to_frame_ms = stats(keys)
        ov.field.text = ""
        step = 4
        waitUntil = ms() + 100
        return
      }
      t0 = ms()
      waitFor = "frame"
      waitingFrame = true
      ov.field.text = prefixes[k]
    } else if (step === 4) {
      // a tag chip on and off: click -> next frame
      if (chips.length >= 10) {
        result.tag_chip_to_frame_ms = stats(chips)
        result.tags = ov.tagCounts.length
        ov.toggleTag(targetTag)
        result.tag_filtered = ov.shown.length
        result.tag_filtered_all_have_it = ov.shown.every(r => r.tags.indexOf(targetTag) >= 0)
        step = 5
        waitUntil = ms() + 300
        return
      }
      t0 = ms()
      waitFor = "frame"
      waitingFrame = true
      ov.toggleTag(targetTag)
    } else if (step === 5) {
      if (shot && !result.shot) {
        result.shot = true
        console.log("SHOT " + rect(ov.card))
        waitUntil = ms() + 2500
        return
      }
      // Enter on the target: the overlay closes, the note flashes
      var at = ov.shown.findIndex(r => r.id === targetNid)
      result.target_listed = at >= 0
      if (at < 0) { finish(); return }
      ov.selected = at
      t0 = ms()
      ov.pick(at)
      step = 6
    } else if (step === 6) {
      var c = svc.desktops.length ? svc.desktops[0].cardFor(targetNid) : null
      if (c && c.flashing) {
        result.enter_to_flash_ms = +(ms() - t0).toFixed(1)
        result.overlay_closed = !ov.opened
        step = 7
        waitUntil = ms() + 400
      } else if (ms() - t0 > 3000) {
        result.enter_to_flash_ms = null
        step = 7
      }
    } else if (step === 7) {
      finish()
    }
  }

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
