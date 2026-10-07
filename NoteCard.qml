import QtQuick
import Quickshell
import qs.Commons
import "markup.js" as Markup

// One sticky. Position and size bind to the model except while a drag or
// resize is in flight, when they follow the pointer locally (no round trip
// per frame) and are written once on release. Typing is debounced into
// `edit` requests (300 ms) and flushed when the editor loses focus.
// `docked`: the card sits in the waterfall column (Waterfall.qml), which
// owns its position; a drag there reorders or, out on the desktop, frees
// the note, and the free position in the DB is left alone otherwise.
Item {
  id: card

  // Set from the model by Desktop's slot (the card is Loader-made, so
  // these are plain properties rather than required model roles).
  property int nid: -1
  property string body: ""
  property string color: "yellow"
  property bool pinned: false
  property int workspace: -1
  property string monitor: ""
  property int x0: 0
  property int y0: 0
  property int w: 240
  property int h: 200
  // Rolled up to the header line (double-click the header).
  property bool rolled: false
  // Next reminder (UTC ISO) from an `@ <when>` line, or "".
  property string remindAt: ""
  // Tags, space-separated (a ListModel role is one type: a string).
  property string tags: ""
  readonly property var tagList: tags ? tags.split(" ") : []
  // The tag editor is open (the header's tag button).
  property bool tagEditing: false

  property var service
  property var desk
  readonly property alias editor: editor
  // The note's text as typed (`styler.plain`, markers and all) and its
  // live styling.
  readonly property alias styler: styler
  property bool docked: false
  // Animate position changes (the column's layout switch / reorder glide).
  property bool glide: false

  readonly property int headerHeight: 26
  readonly property int minW: 150
  readonly property int minH: 110
  readonly property real dragThreshold: 4
  // Drag snapping: an 8 px grid, or another note's edge within snapReach.
  readonly property int grid: 8
  readonly property int snapReach: 6

  // Local geometry while the pointer owns the note.
  property bool moving: false
  property bool resizing: false
  property real liveX: 0
  property real liveY: 0
  property real liveW: 0
  property real liveH: 0
  // Other notes' edges, read once when a drag starts.
  property var snapEdges: null

  // What we last sent / received for the body, to tell echoes from news.
  property string sentBody: ""
  property bool showPalette: false

  x: moving ? liveX : x0
  y: moving ? liveY : y0
  // Position only: animating the size re-wraps the text every frame
  // (measured: 8 dropped frames per 20 glides with 40 notes). The column's
  // glide (130 ms) or a tidy / its undo (220 ms); drags never animate.
  readonly property int glideMs: 130
  readonly property bool gliding: (glide || service.arranging) && !moving
  readonly property int glideFor: glide ? glideMs : 220
  Behavior on x { enabled: card.gliding; NumberAnimation { duration: card.glideFor; easing.type: Easing.OutCubic } }
  Behavior on y { enabled: card.gliding; NumberAnimation { duration: card.glideFor; easing.type: Easing.OutCubic } }
  width: resizing ? liveW : w
  height: resizing ? liveH : rolled ? headerHeight : h

  readonly property color fill: service.fillFor(color)
  readonly property color ink: service.ink
  readonly property color rule: Qt.rgba(ink.r, ink.g, ink.b, 0.10)
  // ==highlight==: the theme's accent as a marker pen over the note colour.
  readonly property color markColor: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, service.dark ? 0.45 : 0.38)

  // Markers show faint while the note is being edited, hidden otherwise.
  MarkupStyler {
    id: styler
    editor: editor
    flick: flick
    ink: card.ink
    hidden: !editor.activeFocus
    // Typing, undo, a shortcut or a new body from outside: recount the
    // checkboxes; save unless it is what was last sent or received.
    onPlainChanged: {
      card.scanChecks()
      if (plain !== card.sentBody) saveTimer.restart()
    }
  }

  // ------------------------------------------------------------ geometry API
  // Used by the pointer handlers below and by the frame-time bench.
  function beginMove() {
    liveX = x; liveY = y
    // Snapping is the free layout's: in a waterfall the column owns places.
    snapEdges = docked || service.waterfall ? null : desk.snapEdges(nid)
    moving = true
    if (!docked) service.raise(nid)
  }
  // free: Shift held, no snapping.
  function moveTo(nx, ny, free) {
    if (docked) {  // the column clamps where it lands
      liveX = Math.round(nx); liveY = Math.round(ny)
      desk.dragMoved(card)
      return
    }
    var x = Math.max(-width + 60, Math.min(desk.width - 60, nx))
    var y = Math.max(0, Math.min(desk.height - headerHeight, ny))
    var gx = -1, gy = -1
    if (!free && snapEdges) {
      var sx = snapAxis(x, width, snapEdges.xs)
      if (sx) { x = sx.v; gx = sx.line } else x = Math.round(x / grid) * grid
      var sy = snapAxis(y, height, snapEdges.ys)
      if (sy) { y = sy.v; gy = sy.line } else y = Math.round(y / grid) * grid
    }
    liveX = Math.round(x)
    liveY = Math.round(y)
    desk.guideX = gx
    desk.guideY = gy
  }
  // The nearest edge line within snapReach of this note's start or end edge:
  // { v: new start, line: where the guide goes }, or null.
  function snapAxis(pos, size, lines) {
    var best = null, bestD = snapReach + 1
    for (var i = 0; i < lines.length; i++) {
      var l = lines[i]
      var d = Math.abs(pos - l)
      if (d < bestD) { bestD = d; best = { v: l, line: l } }
      d = Math.abs(pos + size - l)
      if (d < bestD) { bestD = d; best = { v: l - size, line: l } }
    }
    return best
  }
  function endMove() {
    if (!moving) return
    if (docked) {
      desk.dragEnded(card)
      moving = false
      return
    }
    // A free note dropped on the waterfall column joins it (its free spot
    // stays where it was before the drag).
    if (service.waterfall && service.column && service.column.screenName === desk.screenName
        && service.column.accepts(liveX + width / 2, liveY + headerHeight / 2)) {
      moving = false
      service.dockNote(nid)
      return
    }
    var fields = { x: Math.round(liveX), y: Math.round(liveY) }
    if (monitor !== desk.screenName) fields.monitor = desk.screenName
    service.moveNote(nid, fields)
    moving = false
    snapEdges = null
    desk.guideX = -1
    desk.guideY = -1
  }
  function beginResize() {
    liveW = w; liveH = h
    resizing = true
  }
  function resizeTo(nw, nh) {
    liveW = Math.round(Math.max(minW, nw))
    liveH = Math.round(Math.max(minH, nh))
  }
  function endResize() {
    if (!resizing) return
    service.moveNote(nid, { w: Math.round(liveW), h: Math.round(liveH) })
    resizing = false
  }

  function focusEditor() {
    if (rolled) return
    if (docked) desk.reveal(card)
    editor.forceActiveFocus()
    editor.cursorPosition = editor.length
  }

  function toggleRoll() {
    if (!rolled) {
      flush()
      editor.focus = false
      showPalette = false
      closeTags()
    }
    service.toggleRoll(nid)
  }

  // ------------------------------------------------------------ tags
  // The editor: chips with an x, a field that autocompletes from the tags
  // in use. Enter adds (the highlighted suggestion, else what was typed;
  // Shift+Enter: exactly what was typed), Backspace on an empty field
  // removes the last tag, Esc or a click elsewhere closes it.
  function openTags() {
    if (rolled) toggleRoll()
    showPalette = false
    tagEditing = true
    service.loadTags()
    Qt.callLater(() => tagInput.forceActiveFocus())
  }
  function closeTags() {
    if (!tagEditing) return
    var typed = tagInput.text
    tagInput.text = ""
    tagEditing = false
    if (typed.trim()) addTag(typed)
  }
  function cleanTag(t) {
    t = String(t || "").trim().replace(/^#/, "").toLowerCase()
    return /^[a-z0-9-]{1,32}$/.test(t) ? t : ""
  }
  function addTag(t) {
    t = cleanTag(t)
    if (!t || tagList.indexOf(t) >= 0) return
    service.setTags(nid, tagList.concat([t]))
  }
  function removeTag(t) {
    service.setTags(nid, tagList.filter(x => x !== t))
  }
  property int suggestIndex: 0
  readonly property var suggestions: {
    if (!tagEditing) return []
    var typed = cleanTag(tagInput.text)
    var all = service.allTags || []
    var out = []
    for (var i = 0; i < all.length && out.length < 5; i++) {
      var t = all[i].tag
      if (tagList.indexOf(t) < 0 && (!typed || t.indexOf(typed) === 0)) out.push(all[i])
    }
    return out
  }
  // -1: nothing highlighted (empty field: Enter closes the editor).
  onSuggestionsChanged: suggestIndex = suggestions.length && tagInput.text.trim() ? 0 : -1
  function tagEnter(exact) {
    var typed = tagInput.text
    if (!exact && suggestIndex >= 0 && suggestIndex < suggestions.length) addTag(suggestions[suggestIndex].tag)
    else if (typed.trim()) addTag(typed)
    else { closeTags(); editor.forceActiveFocus(); return }
    tagInput.text = ""
  }

  // ------------------------------------------------------------ body sync
  function flush() {
    saveTimer.stop()
    if (styler.plain !== sentBody) {
      sentBody = styler.plain
      service.editBody(nid, styler.plain)
    }
  }

  // An external change (CLI, agent) lands in the editor unless the person
  // is mid-edit here; then their text wins and is flushed on blur.
  onBodyChanged: {
    if (body === styler.plain) { sentBody = body; return }
    if (!editor.activeFocus && !saveTimer.running) {
      sentBody = body
      styler.setText(body)
      styler.reset()
    }
  }

  Timer {
    id: saveTimer
    interval: 300
    onTriggered: card.flush()
  }

  // ------------------------------------------------------------ checklists
  // A line starting with "[ ]" / "[x]" (or "- [ ]", "* [x]") is a checkbox:
  // the box is drawn over the brackets, a click flips the character in the
  // text (so the CLI, search and chat see the same thing), and the header
  // shows done/total. Notes without a "[" cost one indexOf per keystroke.
  property var checks: []          // [{ pos: index of "[", done }]
  property int checkDone: 0
  property int layoutRev: 0        // bumped when wrapping may have moved lines

  function scanChecks() {
    var t = styler.plain
    var out = []
    if (t.indexOf("[") >= 0) {
      var re = /^([ \t]*(?:[-*+][ \t]+)?)\[([ xX])\]/gm
      var m
      while ((m = re.exec(t)) !== null) out.push({ pos: m.index + m[1].length, done: m[2] !== " " })
    }
    if (out.length === checks.length && out.every((c, i) => c.pos === checks[i].pos && c.done === checks[i].done)) return
    checks = out
    checkDone = out.filter(c => c.done).length
    // The overlays follow in place: a key typed above them moves them,
    // it doesn't make new ones.
    for (var i = 0; i < out.length; i++) {
      if (i >= checkModel.count) checkModel.append(out[i])
      else if (checkModel.get(i).pos !== out[i].pos || checkModel.get(i).done !== out[i].done) checkModel.set(i, out[i])
    }
    if (checkModel.count > out.length) checkModel.remove(out.length, checkModel.count - out.length)
  }
  ListModel { id: checkModel }

  // Where a checkbox goes (not while the styler rebuilds the document,
  // which is empty for that moment).
  function boxRect(pos) {
    return !styler.busy && pos <= editor.length ? editor.positionToRectangle(pos) : Qt.rect(0, 0, 0, 0)
  }

  // The flip goes through the styler: one undo step, the caret kept.
  function toggleCheck(pos) {
    var t = styler.plain
    var mark = t.charAt(pos + 1) === " " ? "x" : " "
    styler.replace(t.slice(0, pos + 1) + mark + t.slice(pos + 2), styler.anchorPos(), editor.cursorPosition)
    flush()
  }

  // Rolled-up title: the first line with text, without a checkbox, in
  // plain words (no markup).
  readonly property string title: {
    var lines = body.split("\n")
    for (var i = 0; i < lines.length; i++) {
      var l = Markup.plainLine(lines[i].replace(/^[ \t]*(?:[-*+][ \t]+)?\[[ xX]\][ \t]*/, "")).trim()
      if (l) return l
    }
    return "(empty note)"
  }

  // ------------------------------------------------------------ markup keys
  // Ctrl+B / I / U, Ctrl+Shift+X / H and Ctrl+E wrap the selection in a
  // marker (or unwrap it), Ctrl+L puts a checkbox on the line (or takes it
  // off). Ctrl+Z and Ctrl+Shift+Z / Ctrl+Y use the styler's history: the
  // TextEdit's own would replay the styling too.
  readonly property var wrapKeys: ({ [Qt.Key_B]: "**", [Qt.Key_I]: "*", [Qt.Key_U]: "__", [Qt.Key_E]: "`" })
  readonly property var wrapShiftKeys: ({ [Qt.Key_X]: "~~", [Qt.Key_H]: "==" })
  function markupKey(event) {
    var mods = event.modifiers & (Qt.ControlModifier | Qt.ShiftModifier | Qt.AltModifier | Qt.MetaModifier)
    if (editor.inputMethodComposing || !(mods & Qt.ControlModifier) || (mods & (Qt.AltModifier | Qt.MetaModifier)))
      return false
    var shift = (mods & Qt.ShiftModifier) !== 0
    var k = event.key
    if (k === Qt.Key_Z && !shift) { styler.undo(); return true }
    if ((k === Qt.Key_Z && shift) || (k === Qt.Key_Y && !shift)) { styler.redo(); return true }
    var m = (shift ? wrapShiftKeys : wrapKeys)[k]
    if (m) {
      var r = Markup.toggleWrap(styler.plain, editor.selectionStart, editor.selectionEnd, m)
      styler.replace(r.text, r.a, r.b)
      return true
    }
    if (k === Qt.Key_L && !shift) {
      var c = Markup.toggleCheckbox(styler.plain, editor.cursorPosition)
      styler.replace(c.text, c.pos, c.pos)
      return true
    }
    return false
  }

  // Reminder time for the bell: "09:00" today, "Tue 09:00" this week,
  // "7 Oct 09:00" otherwise.
  readonly property string remindLabel: {
    if (!remindAt) return ""
    var d = new Date(remindAt)
    if (isNaN(d.getTime())) return ""
    var now = new Date()
    var hm = Qt.formatTime(d, "HH:mm")
    if (d.toDateString() === now.toDateString()) return hm
    if (d - now < 6 * 86400000 && d > now) return Qt.formatDate(d, "ddd") + " " + hm
    return Qt.formatDate(d, "d MMM") + " " + hm
  }

  // The search overlay jumped here: pulse an accent ring a few times.
  function flash() {
    if (docked) desk.reveal(card)
    flashAnim.restart()
  }
  readonly property bool flashing: flashAnim.running

  Connections {
    target: card.service
    function onFlashRequestChanged() {
      if (card.service.flashRequest === card.nid && card.visible) {
        card.service.flashRequest = -1
        card.flash()
      }
    }
    function onFocusRequestChanged() {
      if (card.service.focusRequest === card.nid && card.visible) {
        card.service.focusRequest = -1
        card.focusEditor()
      }
    }
  }

  Component.onCompleted: {
    sentBody = body
    styler.setText(body)
    desk.addRegion(region)
    if (service.focusRequest === nid && visible) {
      service.focusRequest = -1
      Qt.callLater(focusEditor)
    }
    // Built just now because the jump switched to this workspace.
    if (service.flashRequest === nid && visible) {
      service.flashRequest = -1
      Qt.callLater(flash)
    }
  }
  Component.onDestruction: {
    if (styler.plain !== sentBody) flush()
    if (discardTimer.running && isEmpty()) service.discardIfEmpty(nid)
    desk.removeRegion(region)
  }

  // A note left empty goes when it loses focus. Checked a moment later, so
  // focus that only blinks away (the surface remapping as front mode
  // starts, a hop between monitors) doesn't count.
  function isEmpty() { return styler.plain.trim() === "" && tags === "" }
  Timer {
    id: discardTimer
    interval: 400
    onTriggered: if (!editor.activeFocus && !tagInput.activeFocus && card.isEmpty()) card.service.discardIfEmpty(card.nid)
  }

  // Input region: the card's committed rect (cards only exist while
  // shown). It deliberately ignores the live drag/resize geometry: the
  // pointer's implicit grab keeps events coming while a button is held, so
  // there is no need to re-send the surface's input region every frame.
  Region {
    id: region
    x: card.x0
    y: card.y0
    width: card.visible ? card.w : 0   // hidden: in the waterfall column
    height: card.visible ? (card.rolled ? card.headerHeight : card.h) : 0
  }

  // ------------------------------------------------------------ visuals
  Rectangle {
    // Cheap shadow: one offset translucent rect, no shader pass per frame.
    x: 2; y: 3
    width: parent.width; height: parent.height
    radius: 4
    color: Qt.rgba(0, 0, 0, card.service.dark ? 0.35 : 0.16)
  }

  Rectangle {
    id: paper
    anchors.fill: parent
    radius: 4
    color: card.fill
    border.width: editor.activeFocus ? 1 : 0
    border.color: Qt.rgba(card.ink.r, card.ink.g, card.ink.b, 0.35)
  }

  // Header: drag strip with buttons layered above it. The drag MouseArea
  // takes the press but only claims the pointer (preventStealing) once the
  // pointer has travelled dragThreshold px, so a press-release on the strip
  // still focuses the note and the buttons above get their own clicks.
  // Double-click rolls the note up to this line (and back).
  Item {
    id: header
    anchors { left: parent.left; right: parent.right; top: parent.top }
    height: card.headerHeight

    Rectangle {
      visible: !card.rolled
      anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
      height: 1
      color: card.rule
    }

    MouseArea {
      id: dragArea
      anchors.fill: parent
      cursorShape: card.moving ? Qt.ClosedHandCursor : Qt.OpenHandCursor
      preventStealing: card.moving
      property real pressX: 0
      property real pressY: 0
      property real grabX: 0
      property real grabY: 0

      onPressed: mouse => {
        var p = mapToItem(card.desk, mouse.x, mouse.y)
        pressX = p.x; pressY = p.y
        grabX = p.x - card.x; grabY = p.y - card.y
      }
      onPositionChanged: mouse => {
        var p = mapToItem(card.desk, mouse.x, mouse.y)
        if (!card.moving) {
          if (Math.abs(p.x - pressX) < card.dragThreshold && Math.abs(p.y - pressY) < card.dragThreshold) return
          card.beginMove()
        }
        card.moveTo(p.x - grabX, p.y - grabY, (mouse.modifiers & Qt.ShiftModifier) !== 0)
      }
      onReleased: {
        if (card.moving) card.endMove()
        else { if (!card.docked) card.service.raise(card.nid); card.focusEditor() }
      }
      onDoubleClicked: card.toggleRoll()
      onCanceled: card.endMove()
    }

    // Left side: the title when rolled up, checklist progress, the bell.
    Row {
      id: info
      anchors { left: parent.left; leftMargin: 9; right: buttons.left; rightMargin: 6; verticalCenter: parent.verticalCenter }
      spacing: 8
      readonly property real fixed: (progress.visible ? progress.implicitWidth + spacing : 0)
                                    + (bell.visible ? bell.implicitWidth + spacing : 0)
                                    + (rolledTags.visible ? rolledTags.width + spacing : 0)

      Text {
        id: titleText
        visible: card.rolled
        width: Math.min(implicitWidth, Math.max(0, info.width - info.fixed))
        elide: Text.ElideRight
        text: card.title
        textFormat: Text.PlainText
        color: card.ink
        font.family: Style.font.family
        font.pixelSize: 13
        font.bold: true
      }
      // Rolled up, the tags ride in the header (at most 45% of it).
      Row {
        id: rolledTags
        visible: card.rolled && card.tagList.length > 0
        anchors.verticalCenter: parent.verticalCenter
        width: Math.min(implicitWidth, info.width * 0.45)
        clip: true
        spacing: 3
        Repeater {
          model: rolledTags.visible ? card.tagList : []
          TagChip {
            required property string modelData
            tag: modelData
            ink: card.ink
            pixelSize: 10
          }
        }
      }
      Text {
        id: progress
        visible: card.checks.length > 0
        text: card.checkDone + "/" + card.checks.length
        color: card.ink
        opacity: card.checkDone === card.checks.length ? 0.85 : 0.55
        font.family: Style.font.family
        font.pixelSize: 12
      }
      Text {
        id: bell
        visible: card.remindLabel !== ""
        // nf-md-bell_outline + the time it rings.
        text: "\u{f009c} " + card.remindLabel
        color: card.ink
        opacity: 0.6
        font.family: Style.font.family
        font.pixelSize: 12
      }
    }

    Row {
      id: buttons
      anchors { right: parent.right; rightMargin: 4; verticalCenter: parent.verticalCenter }
      spacing: 2

      HeaderButton {
        glyph: card.pinned ? "\u{f0403}" : "\u{f0931}"
        tip: card.pinned ? "Pinned to all workspaces" : "Pin to all workspaces"
        active: card.pinned
        ink: card.ink
        onClicked: card.service.togglePin(card.nid)
      }
      HeaderButton {
        // nf-md-tag / tag_outline
        glyph: card.tagList.length ? "\u{f04f9}" : "\u{f04fc}"
        tip: "Tags"
        active: card.tagEditing
        ink: card.ink
        onClicked: card.tagEditing ? card.closeTags() : card.openTags()
      }
      HeaderButton {
        glyph: "\u{f03d8}"
        tip: "Colour"
        active: card.showPalette
        ink: card.ink
        onClicked: {
          if (card.rolled) card.toggleRoll()
          card.closeTags()
          card.showPalette = !card.showPalette
        }
      }
      HeaderButton {
        glyph: "\u{f003c}"
        tip: "Archive"
        ink: card.ink
        onClicked: {
          card.flush()
          card.service.archive(card.nid, card.desk.screenName)
        }
      }
    }
  }

  // Colour swatches, dropped under the header when the palette is open.
  Row {
    id: palette
    visible: card.showPalette && !card.rolled
    z: 2
    anchors { top: header.bottom; left: parent.left; topMargin: 6; leftMargin: 8 }
    spacing: 6
    Repeater {
      model: card.service.paletteNames
      Rectangle {
        required property string modelData
        width: 20; height: 20; radius: 10
        color: card.service.fillFor(modelData)
        border.width: modelData === card.color ? 2 : 1
        border.color: Qt.rgba(card.ink.r, card.ink.g, card.ink.b, modelData === card.color ? 0.8 : 0.3)
        MouseArea {
          anchors.fill: parent
          cursorShape: Qt.PointingHandCursor
          onClicked: {
            card.service.setColor(card.nid, parent.modelData)
            card.showPalette = false
          }
        }
      }
    }
  }

  // Tags under the header: quiet chips (a click opens All notes filtered
  // by that tag) or, while editing, chips with an x and the field.
  Flow {
    id: tagRow
    visible: !card.rolled && (card.tagList.length > 0 || card.tagEditing)
    z: 2
    anchors {
      top: palette.visible ? palette.bottom : header.bottom
      left: parent.left; right: parent.right
      topMargin: 5; leftMargin: 8; rightMargin: 8
    }
    spacing: 4
    Repeater {
      model: tagRow.visible ? card.tagList : []
      TagChip {
        required property string modelData
        tag: modelData
        ink: card.ink
        removable: card.tagEditing
        clickable: !card.tagEditing
        onClicked: card.service.openList([modelData])
        onRemoved: card.removeTag(modelData)
      }
    }
    TextInput {
      id: tagInput
      visible: card.tagEditing
      width: Math.max(60, contentWidth + 4)
      height: 18
      verticalAlignment: TextInput.AlignVCenter
      color: card.ink
      selectionColor: Qt.rgba(card.ink.r, card.ink.g, card.ink.b, 0.25)
      font.family: Style.font.family
      font.pixelSize: 12
      maximumLength: 33
      validator: RegularExpressionValidator { regularExpression: /#?[A-Za-z0-9-]*/ }
      Text {
        visible: tagInput.text === ""
        anchors.verticalCenter: parent.verticalCenter
        text: card.tagList.length ? "add tag…" : "tag…"
        color: Qt.rgba(card.ink.r, card.ink.g, card.ink.b, 0.4)
        font: tagInput.font
      }
      Keys.onReturnPressed: event => card.tagEnter((event.modifiers & Qt.ShiftModifier) !== 0)
      Keys.onEnterPressed: event => card.tagEnter((event.modifiers & Qt.ShiftModifier) !== 0)
      Keys.onEscapePressed: { card.closeTags(); card.editor.forceActiveFocus() }
      Keys.onDownPressed: if (card.suggestions.length) card.suggestIndex = (card.suggestIndex + 1) % card.suggestions.length
      Keys.onUpPressed: if (card.suggestions.length) card.suggestIndex = (card.suggestIndex - 1 + card.suggestions.length) % card.suggestions.length
      Keys.onTabPressed: if (card.suggestions.length) tagInput.text = card.suggestions[Math.max(0, card.suggestIndex)].tag
      Keys.onPressed: event => {
        if (event.key === Qt.Key_Backspace && tagInput.text === "" && card.tagList.length) {
          card.removeTag(card.tagList[card.tagList.length - 1])
          event.accepted = true
        }
      }
      onActiveFocusChanged: {
        if (activeFocus) {
          discardTimer.stop()
          if (card.docked) card.service.columnEditing = true
          else card.service.raise(card.nid)
        } else {
          card.closeTags()
          if (card.isEmpty()) discardTimer.restart()
        }
      }
    }
  }

  // Autocomplete: the most used tags this note doesn't have yet.
  Rectangle {
    id: suggestBox
    visible: card.tagEditing && card.suggestions.length > 0 && tagInput.activeFocus
    z: 5
    anchors { top: tagRow.bottom; left: parent.left; topMargin: 3; leftMargin: 8 }
    width: Math.min(card.width - 16, Math.max(120, suggestCol.implicitWidth + 8))
    height: suggestCol.implicitHeight + 6
    radius: 4
    color: Qt.tint(card.fill, Qt.rgba(card.ink.r, card.ink.g, card.ink.b, 0.06))
    border.width: 1
    border.color: Qt.rgba(card.ink.r, card.ink.g, card.ink.b, 0.25)
    Column {
      id: suggestCol
      anchors { left: parent.left; right: parent.right; top: parent.top; margins: 3 }
      Repeater {
        model: suggestBox.visible ? card.suggestions : []
        Rectangle {
          required property var modelData
          required property int index
          width: suggestCol.width
          height: 20
          radius: 3
          color: index === card.suggestIndex ? Qt.rgba(card.ink.r, card.ink.g, card.ink.b, 0.14) : "transparent"
          Text {
            anchors { left: parent.left; leftMargin: 5; verticalCenter: parent.verticalCenter }
            text: parent.modelData.tag
            textFormat: Text.PlainText
            color: card.ink
            font.family: Style.font.family
            font.pixelSize: 12
          }
          Text {
            anchors { right: parent.right; rightMargin: 5; verticalCenter: parent.verticalCenter }
            text: parent.modelData.count
            color: card.ink
            opacity: 0.45
            font.family: Style.font.family
            font.pixelSize: 11
          }
          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            // Keep the field's focus: adding by click shouldn't close the editor.
            onPressed: mouse => { mouse.accepted = true; card.addTag(parent.modelData.tag); tagInput.text = "" }
          }
        }
      }
    }
  }

  Flickable {
    id: flick
    visible: !card.rolled
    anchors {
      left: parent.left; right: parent.right; bottom: parent.bottom
      top: tagRow.visible ? tagRow.bottom : palette.visible ? palette.bottom : header.bottom
      leftMargin: 10; rightMargin: 10; topMargin: 6; bottomMargin: 10
    }
    clip: true
    contentWidth: width
    contentHeight: editor.contentHeight
    boundsBehavior: Flickable.StopAtBounds
    interactive: contentHeight > height

    function ensureVisible(r) {
      if (contentY >= r.y) contentY = r.y
      else if (contentY + height <= r.y + r.height) contentY = r.y + r.height - height
    }

    // Under the text: ==highlight== and `code` backgrounds, and the dots
    // drawn for "- " bullets while the markers are hidden.
    Repeater {
      model: styler.decoModel
      Rectangle {
        required property real dx
        required property real dy
        required property real dw
        required property real dh
        required property bool code
        x: dx
        y: dy + 1
        width: dw
        height: dh - 2
        radius: code ? 3 : 2
        color: code ? Qt.rgba(card.ink.r, card.ink.g, card.ink.b, card.service.dark ? 0.14 : 0.09) : card.markColor
      }
    }
    Repeater {
      model: styler.dotModel
      Rectangle {
        required property real dx
        required property real dy
        required property real dw
        required property real dh
        width: 5; height: 5; radius: 2.5
        x: dx + Math.round((dw - width) / 2)
        y: dy + Math.round((dh - height) / 2)
        color: Qt.rgba(card.ink.r, card.ink.g, card.ink.b, 0.75)
      }
    }

    TextEdit {
      id: editor
      width: flick.width
      height: Math.max(flick.height, contentHeight)
      wrapMode: TextEdit.Wrap
      color: card.ink
      selectionColor: Qt.rgba(card.ink.r, card.ink.g, card.ink.b, 0.25)
      selectedTextColor: card.ink
      selectByMouse: true
      persistentSelection: false
      font.family: Style.font.family
      font.pixelSize: 14
      // Rich text only for the look: the document holds the note's plain
      // text (MarkupStyler); `text` would be HTML, so read styler.plain.
      textFormat: TextEdit.RichText

      // The styler's own formatting changes `text` too (busy then).
      onTextChanged: if (!styler.busy) styler.edited()
      onInputMethodComposingChanged: if (!inputMethodComposing) styler.edited()
      Keys.onPressed: event => { if (card.markupKey(event)) event.accepted = true }
      onWidthChanged: {
        if (card.checks.length) card.layoutRev++
        styler.remeasure()  // rewrapped: backgrounds and dots move
      }
      onContentHeightChanged: if (card.checks.length) card.layoutRev++
      onActiveFocusChanged: {
        if (activeFocus) {
          discardTimer.stop()
          if (card.docked) card.service.columnEditing = true
          else card.service.raise(card.nid)
        }
        else {
          card.flush()
          card.showPalette = false
          if (card.isEmpty()) discardTimer.restart()
        }
      }
      onCursorRectangleChanged: flick.ensureVisible(cursorRectangle)
      // Esc: done with this note; in front mode, back to the desktop too.
      Keys.onEscapePressed: {
        editor.focus = false
        card.service.leaveFront(true)
      }

      Text {
        visible: editor.length === 0 && !editor.activeFocus
        text: "Click to write…"
        color: Qt.rgba(card.ink.r, card.ink.g, card.ink.b, 0.4)
        font: editor.font
      }

      // Checkboxes over the "[ ]" / "[x]" of each checklist line.
      Repeater {
        model: checkModel
        Item {
          id: check
          required property int pos
          required property bool done
          readonly property rect at: { card.layoutRev; styler.rev; return card.boxRect(pos) }
          readonly property rect end: { card.layoutRev; styler.rev; return card.boxRect(pos + 3) }
          x: at.x
          y: at.y
          width: Math.max(end.y === at.y ? end.x - at.x : 0, 16)
          height: at.height

          Rectangle {  // hides the brackets
            anchors.fill: parent
            color: card.fill
          }
          Rectangle {
            id: box
            anchors.centerIn: parent
            width: 13; height: 13; radius: 3
            color: check.done ? Qt.rgba(card.ink.r, card.ink.g, card.ink.b, 0.75) : "transparent"
            border.width: 1.5
            border.color: Qt.rgba(card.ink.r, card.ink.g, card.ink.b, boxMouse.containsMouse ? 0.9 : 0.6)
            Text {
              anchors.centerIn: parent
              visible: check.done
              text: "✓"
              color: card.fill
              font.pixelSize: 11
              font.bold: true
            }
          }
          MouseArea {
            id: boxMouse
            anchors.fill: parent
            anchors.margins: -2
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: card.toggleCheck(check.pos)
          }
        }
      }
    }
  }

  Rectangle {
    id: flashRing
    anchors.fill: parent
    anchors.margins: -4
    z: 10
    radius: 7
    color: "transparent"
    border.width: 3
    border.color: Color.accent
    opacity: 0
    visible: opacity > 0

    SequentialAnimation {
      id: flashAnim
      loops: 3
      NumberAnimation { target: flashRing; property: "opacity"; to: 1; duration: 140; easing.type: Easing.OutQuad }
      NumberAnimation { target: flashRing; property: "opacity"; to: 0; duration: 260; easing.type: Easing.InQuad }
    }
  }

  // Resize grip, bottom-right corner (not in the column: fixed width there).
  Item {
    id: grip
    visible: !card.docked && !card.rolled
    width: 18; height: 18
    anchors { right: parent.right; bottom: parent.bottom }
    z: 3

    Canvas {
      id: gripCanvas
      anchors.fill: parent
      opacity: gripArea.containsMouse || card.resizing ? 0.6 : 0.3
      onPaint: {
        var ctx = getContext("2d")
        ctx.clearRect(0, 0, width, height)
        ctx.strokeStyle = card.ink
        ctx.lineWidth = 1
        for (var i = 0; i < 3; i++) {
          ctx.beginPath()
          ctx.moveTo(width - 4 - i * 4, height - 3)
          ctx.lineTo(width - 3, height - 4 - i * 4)
          ctx.stroke()
        }
      }
      Connections {
        target: card
        function onInkChanged() { gripCanvas.requestPaint() }
      }
    }

    MouseArea {
      id: gripArea
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.SizeFDiagCursor
      preventStealing: card.resizing
      property real pressX: 0
      property real pressY: 0
      onPressed: mouse => {
        var p = mapToItem(card.desk, mouse.x, mouse.y)
        pressX = p.x; pressY = p.y
        card.beginResize()
      }
      onPositionChanged: mouse => {
        var p = mapToItem(card.desk, mouse.x, mouse.y)
        card.resizeTo(card.w + p.x - pressX, card.h + p.y - pressY)
      }
      onReleased: card.endResize()
      onCanceled: card.endResize()
    }
  }
}
