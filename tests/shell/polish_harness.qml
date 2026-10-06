import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "stickies"

// bench_polish.py's harness: drives the polish features through the same
// functions the pointer and keys use (there is no pointer injection here)
// and times them: checklist toggle, typing in a checklist note, drag
// snapping, roll-up, tidy + undo, archive + undo, clipboard note, a live
// theme switch. One `BENCH {...}` line at the end.
ShellRoot {
  id: harness

  readonly property int nid: parseInt(Quickshell.env("BENCH_NID") || "1")      // the checklist note
  readonly property int plainNid: parseInt(Quickshell.env("BENCH_PLAIN") || "2") // a note without one
  readonly property string clipMode: Quickshell.env("BENCH_CLIP_MODE_FILE") || ""
  property var card: null
  property var plain: null
  property var result: ({})
  property var checks: ({})

  ElapsedTimer { id: clock }
  Service { id: svc }

  function log(obj) { console.log("BENCH " + JSON.stringify(obj)) }
  function stats(xs) {
    var s = xs.slice().sort((a, b) => a - b)
    if (!s.length) return null
    function q(p) { return s[Math.min(s.length - 1, Math.floor(p * (s.length - 1) + 0.5))] }
    return { n: s.length, p50: +q(0.5).toFixed(2), p95: +q(0.95).toFixed(2), max: +s[s.length - 1].toFixed(2) }
  }
  function check(name, ok, detail) { checks[name] = ok ? true : (detail === undefined ? false : detail) }
  function show(id, cb) { svc.request("show", { id: id }, function(ok, n) { cb(ok ? n : null) }) }

  // Steps run one after another; each calls next() when done.
  property int step: -1
  property var steps: [stepWait, stepChecklist, stepTypeChecklist, stepTypePlain, stepSnap, stepRoll,
                       stepTidy, stepTidyUndo, stepArchive, stepPasteText, stepPasteImage, stepWaterfall,
                       stepTheme]
  function next() {
    step++
    if (step >= steps.length) { done(); return }
    Qt.callLater(steps[step])
  }
  Component.onCompleted: next()
  function later(ms, fn) {
    var t = Qt.createQmlObject("import QtQuick; Timer {}", harness)
    t.interval = ms
    t.triggered.connect(function() { t.destroy(); fn() })
    t.start()
  }

  function stepWait() {
    if (svc.desktops.length && svc.listed) {
      card = svc.desktops[0].cardFor(nid)
      plain = svc.desktops[0].cardFor(plainNid)
    }
    if (card && plain && card.visible) { later(800, next); return }
    if (clock.elapsed() > 20) { log({ error: "cards never appeared" }); Qt.quit(); return }
    later(16, stepWait)
  }

  // -- 1. checklists
  function stepChecklist() {
    check("checklist_parsed", card.checks.length === 3 && card.checkDone === 1,
          JSON.stringify(card.checks))
    var t0 = clock.elapsed()
    card.toggleCheck(card.checks[1].pos)
    result.check_toggle_js_ms = +((clock.elapsed() - t0) * 1000).toFixed(2)
    check("progress_after_toggle", card.checkDone === 2, card.checkDone)
    later(400, function() {
      show(nid, function(n) {
        check("toggle_saved", !!n && n.body.indexOf("- [x] 1 case") >= 0, n && n.body)
        next()
      })
    })
  }

  // -- 2. typing: text change -> swapped frame, inserting before the
  // checklist (every key shifts every box) vs in a plain note.
  property var pending: -1
  property var lat: []
  Connections {
    target: harness.card ? harness.card.Window.window : null
    function onFrameSwapped() {
      if (harness.pending < 0) return
      harness.lat.push((clock.elapsed() - harness.pending) * 1000)
      harness.pending = -1
    }
  }
  function typeInto(c, key, done) {
    lat = []
    var n = 0
    function one() {
      if (n >= 60) { result[key] = stats(lat); later(500, done); return }
      pending = clock.elapsed()
      c.editor.insert(5, "abcdefghij".charAt(n % 10))
      n++
      later(45, one)
    }
    one()
  }
  function stepTypeChecklist() { typeInto(card, "type_checklist_text_to_swap_ms", next) }
  function stepTypePlain() { typeInto(plain, "type_plain_text_to_swap_ms", next) }

  // -- 3. drag snapping (the header MouseArea's path)
  function stepSnap() {
    var d = card.desk
    var o = plain
    card.beginMove()
    card.moveTo(o.x0 + 3, 517)
    check("snaps_to_edge", card.liveX === o.x0 && d.guideX === o.x0, [card.liveX, o.x0, d.guideX])
    card.moveTo(1203, 61)  // no other note's edge within reach
    check("snaps_to_grid", card.liveX === 1200 && card.liveY === 64 && d.guideX === -1 && d.guideY === -1,
          [card.liveX, card.liveY, d.guideX, d.guideY])
    card.moveTo(1203, 517, true)
    check("shift_places_freely", card.liveX === 1203 && card.liveY === 517, [card.liveX, card.liveY])
    var t = []
    for (var i = 0; i < 300; i++) {
      var t0 = clock.elapsed()
      card.moveTo(600 + 400 * Math.sin(i / 20), 300 + 200 * Math.cos(i / 15))
      t.push((clock.elapsed() - t0) * 1000)
    }
    result.snap_move_js_ms = stats(t)
    card.endMove()
    check("guides_cleared", d.guideX === -1 && d.guideY === -1)
    later(300, next)
  }

  // -- 4. roll-up
  function stepRoll() {
    svc.toggleRoll(nid)
    check("rolled_height", card.height === card.headerHeight, card.height)
    later(400, function() {
      show(nid, function(n) {
        check("rolled_saved", !!n && n.rolled === true)
        svc.toggleRoll(nid)
        later(300, next)
      })
    })
  }

  // -- 5. tidy + undo
  property var before: ({})
  function stepTidy() {
    for (var i = 0; i < svc.notes.count; i++) { var r = svc.notes.get(i); before[r.nid] = [r.x0, r.y0] }
    var t0 = clock.elapsed()
    var c0 = 0
    svc.tidyHere()
    function wait() {
      if (svc.tidyUndoable) {
        result.tidy_roundtrip_ms = +((clock.elapsed() - t0) * 1000).toFixed(1)
        var moved = 0
        for (var i = 0; i < svc.notes.count; i++) {
          var r = svc.notes.get(i)
          if (before[r.nid] && (before[r.nid][0] !== r.x0 || before[r.nid][1] !== r.y0)) moved++
        }
        check("tidy_moved_notes", moved > 0, moved)
        check("tidy_toast", !!svc.notice && svc.notice.undo === "tidy", JSON.stringify(svc.notice))
        later(600, next)
      } else if (++c0 > 200) { check("tidy_moved_notes", "no answer: " + JSON.stringify(svc.notice)); next() }
      else later(10, wait)
    }
    wait()
  }
  function stepTidyUndo() {
    svc.undoNotice()
    later(500, function() {
      var back = 0, total = 0
      for (var i = 0; i < svc.notes.count; i++) {
        var r = svc.notes.get(i)
        if (!before[r.nid]) continue
        total++
        if (before[r.nid][0] === r.x0 && before[r.nid][1] === r.y0) back++
      }
      check("tidy_undone", back === total && !svc.tidyUndoable, [back, total])
      next()
    })
  }

  // -- 6. archive + undo toast
  function stepArchive() {
    svc.archive(plainNid, card.desk.screenName)
    check("archive_toast", !!svc.notice && svc.notice.text === "Archived" && svc.notice.undo === "archive")
    later(300, function() {
      svc.undoNotice()
      later(400, function() {
        check("archive_undone", svc.indexOf(plainNid) >= 0)
        next()
      })
    })
  }

  // -- 7. clipboard note (a fake wl-paste: text, then an image)
  property int countBefore: 0
  function stepPasteText() {
    countBefore = svc.notes.count
    var t0 = clock.elapsed()
    svc.pasteHere()
    function wait(n) {
      if (svc.notes.count > countBefore) {
        result.paste_roundtrip_ms = +((clock.elapsed() - t0) * 1000).toFixed(1)
        var r = svc.notes.get(svc.notes.count - 1)
        check("paste_note", r.body === "copied from somewhere", r.body)
        later(500, next)
      } else if (n > 300) { check("paste_note", "no note: " + JSON.stringify(svc.notice)); next() }
      else later(10, function() { wait(n + 1) })
    }
    wait(0)
  }
  FileView { id: modeFile; path: harness.clipMode }
  function stepPasteImage() {
    modeFile.setText("image")
    svc.notice = null
    later(200, function() {
      svc.pasteHere()
      later(800, function() {
        check("paste_image_refused", !!svc.notice && svc.notice.text === "The clipboard holds an image, not text",
              JSON.stringify(svc.notice))
        next()
      })
    })
  }

  // -- 8. in a waterfall column (#136): checklist and roll-up ride along,
  // tidy is refused, a column drag doesn't snap.
  function stepWaterfall() {
    svc.setLayout("waterfall-right")
    later(800, function() {
      var c = svc.column ? svc.column.cardFor(nid) : null
      check("column_card", !!c && c.visible && c.docked, !!c)
      if (!c) { svc.setLayout("free"); later(600, next); return }
      check("column_checklist", c.checks.length === 3 && c.checkDone === 2, [c.checks.length, c.checkDone])
      svc.notice = null
      svc.tidyHere()
      check("tidy_refused_in_waterfall", !!svc.notice && svc.notice.text.indexOf("free layout") >= 0
            && !svc.tidyUndoable, JSON.stringify(svc.notice))
      c.beginMove()
      check("no_snap_in_column", c.snapEdges === null)
      c.endMove()
      svc.toggleRoll(nid)
      later(600, function() {
        var slot = svc.column.slotMap[nid]
        check("column_rolled", c.height === c.headerHeight && !!slot && slot.h === 26,
              [c.height, slot && slot.h])
        svc.toggleRoll(nid)
        later(400, function() {
          var s2 = svc.column.slotMap[nid]
          check("column_unrolled", !!s2 && s2.h > 26 && c.height === s2.h, [c.height, s2 && s2.h])
          svc.setLayout("free")
          later(800, next)
        })
      })
    })
  }

  // -- 9. theme switch: recolour live (this instance only)
  function stepTheme() {
    var oldFill = "" + card.fill
    var oldBg = Color.background
    pending = clock.elapsed()
    lat = []
    Color.background = svc.dark ? "#f4f1e8" : "#1b1d1f"
    later(300, function() {
      result.theme_switch_to_swap_ms = lat.length ? +lat[0].toFixed(2) : null
      check("recoloured_live", ("" + card.fill) !== oldFill, [oldFill, "" + card.fill])
      check("name_kept", card.color === "blue", card.color)
      Color.background = oldBg
      later(200, next)
    })
  }

  function done() {
    result.checks = checks
    result.all_ok = Object.keys(checks).every(k => checks[k] === true)
    log(result)
    Qt.quit()
  }
}
