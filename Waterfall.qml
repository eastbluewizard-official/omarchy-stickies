import QtQuick
import Quickshell
import qs.Commons
import "waterfall.js" as W

// The waterfall column: the focused workspace's notes stacked at the left
// or right edge of one screen, each its own height, a fixed width, the
// overflow scrolling inside the column. Geometry comes from waterfall.js;
// this file only feeds it and places NoteCards where it says. A note's
// free position (x/y/w/h in the DB) is never written here, except when a
// note is dragged out onto the desktop, which makes it free right there.
Item {
  id: wf

  property var service
  property var window: null
  property string screenName: ""
  property bool primary: false
  property var knownScreens: []
  property int reservedTop: 0
  property int reservedBottom: 0

  readonly property int glideMs: 130   // NoteCard.glideMs

  // nid -> {nid, h, pinned, updated} for every note in the column. Kept up
  // to date by the slots below (cheap per-note Items), so a keystroke in
  // one of 2,000 notes doesn't walk the model. `updated` is frozen when the
  // note joins, so editing a note doesn't make it jump to the top while
  // you type; the order refreshes on the next workspace visit or switch.
  property var members: ({})
  property int memberRev: 0
  property var sorted: []
  property var slotMap: ({})
  property real contentH: 0
  property real scroll: 0
  property var drag: null      // {nid, index} while a column note is dragged

  // A layout switch / collapse / reorder / new note: cards animate to their
  // new places for glideMs. Scrolling and dragging move them directly.
  property bool gliding: false
  function startGlide() { gliding = true; glideTimer.restart() }
  Timer { id: glideTimer; interval: wf.glideMs + 40; onTriggered: wf.gliding = false }

  readonly property var geo: W.geometry({ screenW: width, screenH: height, side: service.colSide,
                                          width: service.colWidth, collapsed: service.colCollapsed,
                                          top: reservedTop, bottom: reservedBottom, contentH: contentH })
  readonly property real maxScroll: Math.max(0, contentH - geo.column.h)

  function memberIds() { return service.waterfall ? sorted.map(n => n.nid) : [] }
  // No card on its way anywhere (benches: when a glide has finished).
  function settled() {
    for (var i = 0; i < cards.count; i++) {
      var c = cards.itemAt(i) ? cards.itemAt(i).card : null
      if (c && (Math.abs(c.x - c.x0) > 0.5 || Math.abs(c.y - c.y0) > 0.5)) return false
    }
    return true
  }
  function cardFor(nid) {
    for (var i = 0; i < cards.count; i++) {
      var s = cards.itemAt(i)
      if (s && s.nid === nid) return s.card
    }
    return null
  }

  function setMember(nid, row) {
    // A delegate's bindings can run before its required roles are set.
    if (!(nid > 0)) return
    var m = members
    if (row) {
      var old = m[nid]
      row.updated = old ? old.updated : row.updated
      if (old && old.h === row.h && old.rolled === row.rolled && old.pinned === row.pinned) return
      m[nid] = row
    } else {
      if (!(nid in m)) return
      delete m[nid]
    }
    memberRev++
    relayoutSoon()
  }

  // New workspace: forget the frozen edit times, so the most recently
  // edited notes go to the top for this visit.
  Connections {
    target: wf.service
    function onFocusedWsChanged() {
      var m = wf.members
      for (var k in m) m[k].updated = m[k].fresh
      wf.scroll = 0
      wf.relayoutSoon()
    }
    function onColOrderChanged() { wf.relayoutSoon() }
    function onLayoutChanged() { wf.relayoutSoon() }
  }
  onGeoChanged: relayoutSoon()

  // The column's first frame after a switch is up: the desktop can stop
  // drawing those notes (see service.entering).
  Connections {
    target: wf.Window.window
    function onFrameSwapped() { if (wf.service.entering) wf.service.entering = false }
  }

  property bool relayoutQueued: false
  function relayoutSoon() {
    if (relayoutQueued) return
    relayoutQueued = true
    Qt.callLater(relayout)
  }
  function relayout() {
    relayoutQueued = false
    var list = []
    for (var k in members) list.push(members[k])
    sorted = W.sortColumn(list, service.colOrder)
    var r = W.slots(sorted, geo, drag)
    var map = {}
    for (var i = 0; i < r.slots.length; i++) map[r.slots[i].nid] = r.slots[i]
    contentH = r.contentH
    slotMap = map   // builds the cards of new members, synchronously
    scroll = W.clampScroll(scroll, contentH, geo.column.h)
  }

  function scrollBy(dy) {
    scroll = W.clampScroll(scroll + dy, contentH, geo.column.h)
  }

  // ------------------------------------------------------------ drag (from NoteCard)
  // `content` is the cards' desk: a note dragged in the column reports here.
  function dragMoved(card) {
    var cy = card.liveY + card.height / 2
    var idx = W.dropIndex(sorted, geo, card.nid, cy)
    if (!drag || drag.nid !== card.nid || drag.index !== idx) {
      drag = { nid: card.nid, index: idx }
      startGlide()
      relayout()
    }
  }
  function dragEnded(card) {
    var d = drag
    drag = null
    var ax = card.liveX, ay = card.liveY + geo.column.y - scroll   // screen coordinates
    if (W.leftColumn(geo, ax + card.width / 2)) {
      // Out on the desktop: free from now on, exactly there.
      var x = Math.round(Math.max(0, Math.min(width - 60, ax)))
      var y = Math.round(Math.max(0, Math.min(height - card.headerHeight, ay)))
      var fields = { x: x, y: y }
      if (card.monitor !== screenName) fields.monitor = screenName
      service.undock(card.nid, fields)
    } else if (d) {
      var ids = memberIds()
      if (ids.indexOf(card.nid) !== d.index)
        service.setColumnOrder(W.reorder(service.colOrder, ids, card.nid, d.index))
    }
    startGlide()
    relayout()
  }

  // A free note dropped at (x, y) on this screen lands in the column?
  function accepts(x, y) {
    if (!service.waterfall) return false
    var c = geo.column
    return service.colCollapsed ? W.inside(geo.strip, x, y) || W.inside(geo.handle, x, y)
                                : x >= c.x && x < c.x + c.w && y >= c.y && y < c.y + c.h
  }

  // ------------------------------------------------------------ input region
  Region { id: columnRegion; x: wf.geo.column.x; y: wf.geo.column.y; width: wf.geo.column.w
           height: Math.max(0, Math.min(wf.contentH, wf.geo.column.h)) }
  Region { id: stripRegion; x: wf.geo.strip.x; y: wf.geo.strip.y; width: wf.geo.strip.w; height: wf.geo.strip.h }
  Region { id: handleRegion; item: handle }
  readonly property bool hasNotes: sorted.length > 0
  readonly property var regions: !service.waterfall ? []
                                 : service.colCollapsed ? [stripRegion, handleRegion]
                                 : hasNotes ? [columnRegion, handleRegion] : []

  // ------------------------------------------------------------ cards
  // Clipped to the column's height so scrolled-away notes don't paint over
  // the bar; unclipped while gliding, so notes travel from anywhere.
  Item {
    id: viewport
    x: 0
    y: wf.geo.column.y
    width: wf.width
    height: wf.geo.column.h
    clip: !wf.gliding && wf.drag === null

    WheelHandler {
      target: null
      acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
      onWheel: event => {
        var d = event.pixelDelta.y !== 0 ? event.pixelDelta.y : event.angleDelta.y / 2
        wf.scrollBy(-d)
      }
    }

    Item {
      id: content
      y: -wf.scroll
      width: wf.width
      height: wf.height
      // The NoteCard "desk" API.
      readonly property string screenName: wf.screenName
      function addRegion(r) {}
      function removeRegion(r) {}
      function dragMoved(card) { wf.dragMoved(card) }
      function dragEnded(card) { wf.dragEnded(card) }
      // Scroll a card (caret or search jump) fully into the column's view.
      function reveal(card) {
        var t = wf.slotMap[card.nid]
        if (!t) return
        if (t.y < wf.scroll) wf.scroll = t.y
        else if (t.y + t.h > wf.scroll + wf.geo.column.h) wf.scroll = W.clampScroll(t.y + t.h - wf.geo.column.h, wf.contentH, wf.geo.column.h)
      }

      Repeater {
        id: cards
        model: wf.service.notes

        Item {
          id: slot
          required property int nid
          required property string body
          required property string color
          required property bool pinned
          required property int workspace
          required property string monitor
          required property int x0
          required property int y0
          required property int w
          required property int h
          required property string updated
          required property bool rolled
          required property string remindAt
          required property string tags

          // Would be in the column (this workspace, not dragged out), in
          // any layout: its card is built ahead of time, in the background
          // while the layout is free, so a switch only has to show it and
          // glide (building 40 cards on the key press cost ~90 ms).
          readonly property bool member: wf.service.dockable(nid, workspace, pinned)
          readonly property bool docked: member && wf.service.waterfall
          // Set for the column's notes just before a switch to free, so the
          // card stays shown while it glides home.
          readonly property bool leaving: wf.service.leaving[nid] === true
          readonly property var target: member ? wf.slotMap[nid] : null

          // Its free spot on this screen in content coordinates (a note
          // from another monitor comes from / goes to the column's edge).
          readonly property bool here: monitor === wf.screenName
                                       || ((monitor === "" || wf.knownScreens.indexOf(monitor) < 0) && wf.primary)
          readonly property real freeX: here ? x0 : wf.geo.parkX
          readonly property real freeY: (here ? y0 : (target ? target.y + wf.geo.column.y : wf.geo.column.y))
                                        - wf.geo.column.y + wf.scroll
          readonly property bool inSlot: docked && !leaving && !!target

          z: card && card.moving ? 1000 : 0
          readonly property var card: loader.item

          function sync() {
            wf.setMember(nid, member ? { nid: nid, h: h, rolled: rolled, pinned: pinned, updated: updated, fresh: updated } : null)
          }
          onMemberChanged: sync()
          onHChanged: if (member) sync()
          onRolledChanged: if (member) sync()
          onPinnedChanged: if (member) sync()
          onUpdatedChanged: if (member && wf.members[nid]) wf.members[nid].fresh = updated
          Component.onCompleted: if (member) sync()
          Component.onDestruction: wf.setMember(nid, null)

          Loader {
            id: loader
            active: (slot.member && !!slot.target) || slot.leaving
            // In the background while free (no frame waits for it); at
            // once in a waterfall (a new note, another workspace).
            asynchronous: !wf.service.waterfall
            sourceComponent: NoteCard {
              service: wf.service
              desk: content
              docked: true
              visible: slot.docked || slot.leaving
              // Glides between its free spot (and size) and its slot.
              glide: wf.gliding
              nid: slot.nid
              body: slot.body
              color: slot.color
              pinned: slot.pinned
              workspace: slot.workspace
              monitor: slot.monitor
              x0: slot.inSlot ? slot.target.x : slot.freeX
              y0: slot.inSlot ? slot.target.y : slot.freeY
              // Column width throughout (also while gliding home): the
              // desktop's card takes over at the note's own size on landing.
              w: slot.target ? slot.target.w : wf.geo.column.w
              h: slot.target ? slot.target.h : slot.h
              rolled: slot.rolled
              remindAt: slot.remindAt
              tags: slot.tags
            }
          }
        }
      }
    }
  }

  // Scroll position, while there is something to scroll.
  Rectangle {
    visible: wf.maxScroll > 0 && !wf.service.colCollapsed && wf.service.waterfall
    x: wf.geo.side === "right" ? wf.geo.column.x + wf.geo.column.w + 3 : wf.geo.column.x - 6
    width: 3
    radius: 1.5
    color: Qt.rgba(wf.service.ink.r, wf.service.ink.g, wf.service.ink.b, 0.35)
    height: Math.max(24, wf.geo.column.h * wf.geo.column.h / Math.max(1, wf.contentH))
    y: wf.geo.column.y + (wf.geo.column.h - height) * (wf.maxScroll > 0 ? wf.scroll / wf.maxScroll : 0)
  }

  // ------------------------------------------------------------ collapse
  // The collapsed column: a thin strip at the screen edge. Click it (or
  // the handle, or SUPER + ALT + W) to bring the column back.
  Rectangle {
    id: strip
    visible: wf.service.colCollapsed && wf.service.waterfall
    x: wf.geo.strip.x; y: wf.geo.strip.y; width: wf.geo.strip.w; height: wf.geo.strip.h
    color: Color.accent
    opacity: stripMouse.containsMouse ? 0.9 : 0.55
    radius: 2
    MouseArea {
      id: stripMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: wf.service.toggleCollapsed()
    }
  }

  Rectangle {
    id: handle
    visible: wf.service.waterfall && (wf.hasNotes || wf.service.colCollapsed)
    x: wf.geo.handle.x; y: wf.geo.handle.y; width: wf.geo.handle.w; height: wf.geo.handle.h
    radius: 4
    color: Color.popups.background
    border.color: Color.popups.border
    border.width: 1
    opacity: handleMouse.containsMouse ? 1 : 0.7

    Text {
      anchors.centerIn: parent
      // Points the way the click moves the column.
      text: (wf.geo.side === "right") !== wf.service.colCollapsed ? "›" : "‹"
      color: Color.popups.text
      font.pixelSize: 16
      font.family: Style.font.family
    }
    MouseArea {
      id: handleMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: wf.service.toggleCollapsed()
    }
  }
}
