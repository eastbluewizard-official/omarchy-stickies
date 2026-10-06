import QtQuick
import Quickshell
import "stickies"

// Chat overlay harness for bench_chat.py: the real Service (and so the real
// ChatOverlay and `stickies serve`), in its own `quickshell -p` with a temp
// STICKIES_STATE and, unless --real, a fake agent. Prints `BENCH {...}`
// (and `SHOT` when the answer is on screen, for an optional screenshot).
ShellRoot {
  id: harness

  readonly property string question: Quickshell.env("BENCH_QUESTION") || "When is the consignment payout due?"
  readonly property bool shot: Quickshell.env("BENCH_SHOT") === "1"

  property var result: ({})
  ElapsedTimer { id: clock }
  function ms() { return clock.elapsed() * 1000 }
  function log(obj) { console.log("BENCH " + JSON.stringify(obj)) }

  Service { id: svc }
  readonly property var ov: svc.chatOverlay

  function stats(xs) {
    var s = xs.slice().sort((a, b) => a - b)
    if (!s.length) return null
    function q(p) { return s[Math.min(s.length - 1, Math.floor(p * (s.length - 1) + 0.5))] }
    return { n: s.length, p50: +q(0.5).toFixed(1), p95: +q(0.95).toFixed(1), max: +s[s.length - 1].toFixed(1) }
  }

  property int step: 0
  property real t0: 0
  property real waitUntil: 0
  property var opens: []
  property bool waitingFrame: false
  property int notesBefore: 0

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
    } else if (step === 21) {
      result.enter_to_preview_on_screen_ms = +(ms() - t0).toFixed(1)
      step = 3
    }
  }

  Timer { interval: 4; repeat: true; running: true; onTriggered: harness.tick() }

  function tick() {
    if (ms() < waitUntil) return
    if (waitingFrame) { hook(); return }
    if (ms() > 240000) { result.error = "timed out at step " + step; finish(); return }
    if (step === 0) {
      if (!svc.ready || svc.notes.count === 0 || svc.agentName === "claude" && ms() < 1500) return
      result.agent = svc.agentName
      result.agent_problem = svc.agentProblem || null
      result.notes = svc.notes.count
      step = 1
    } else if (step === 1) {
      // open -> the overlay's first swapped frame, 5x
      if (opens.length >= 5) { result.open_ms = stats(opens); step = 2; return }
      t0 = ms()
      waitingFrame = true
      ov.open()
    } else if (step === 2) {
      ov.open()
      ov.field.text = question
      t0 = ms()
      ov.submit()
      step = 20
    } else if (step === 20) {
      if (!ov.draft) return
      result.enter_to_notes_listed_ms = +(ms() - t0).toFixed(1)
      result.listed = ov.draft.notes.map(n => n.id)
      waitingFrame = true
      step = 21
    } else if (step === 3) {
      // untick the last listed note: it must not reach the agent
      var listed = ov.draft.notes
      if (listed.length > 1) {
        result.unticked = listed[listed.length - 1].id
        ov.toggleTick(result.unticked)
      }
      notesBefore = svc.notes.count
      t0 = ms()
      ov.send()
      step = 4
    } else if (step === 4) {
      if (ov.liveAnswer !== "" && result.send_to_first_text_ms === undefined)
        result.send_to_first_text_ms = +(ms() - t0).toFixed(1)
      if (ov.answersSeq < 1) return
      result.send_to_done_ms = +(ms() - t0).toFixed(1)
      var t = ov.turns[0]
      result.turn = { sent: t.sent.map(n => n.id), citations: t.citations, unknown: t.unknown,
                      proposals: t.proposals.map(p => p.summary), rejected: t.rejected.length,
                      error: t.error || null, answer: t.answer }
      result.applied_before_click = svc.notes.count - notesBefore
      waitUntil = ms() + 300
      step = 5
    } else if (step === 5) {
      if (shot) { console.log("SHOT " + harness.rect(ov.card)); waitUntil = ms() + 2500 }
      step = 6
    } else if (step === 6) {
      var t6 = ov.turns[0]
      var i = t6.proposals.findIndex(p => p.action === "new_note")
      if (i < 0) { step = 8; return }
      t0 = ms()
      ov.apply(0, i)
      result.applied_index = i
      step = 7
    } else if (step === 7) {
      var st = ov.propState["0:" + result.applied_index]
      if (!st || st.state === "applying") return
      result.apply = { state: st.state, msg: st.msg, ms: +(ms() - t0).toFixed(1),
                       notes_added: svc.notes.count - notesBefore }
      step = 8
      waitUntil = ms() + 200
    } else if (step === 8) {
      finish()
    }
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
}
