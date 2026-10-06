import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// All notes (SUPER + ALT + O, `stickies list --open [--tag X]`, the bar
// panel's All notes, a click on a note's tag): every live note on every
// workspace, pinned first then most recently edited, as one list on the
// focused monitor. A row of tag chips on top filters ("All", or one or more
// tags: notes having all of them; a click toggles one) and the field
// filters by text as you type. Up/Down pick, Enter jumps to the note on its
// workspace and flashes it, Delete archives it (Undo below the list, or
// Ctrl+Z), Esc / a click outside / the key again closes. Same surface and
// keyboard handling as the search overlay (OverlayFocus.qml).
//
// Built hidden when the service starts. Opening maps the surface with the
// rows it had (none the first time) and asks serve for the current ones
// (`list` with brief rows: no bodies or geometry over the pipe).
PanelWindow {
  id: win

  property QtObject service
  readonly property bool opened: keeper.opened
  // serve's brief rows: { id, title, hay (lower-cased text to filter on),
  // color, pinned, workspace, tags, remind_at, updated_at, checks }.
  property var rows: []
  // Selected tag chips: notes must have every one of them.
  property var filterTags: []
  property string query: ""
  property int selected: 0
  // Bumped when serve's rows land / when they differ from the shown ones
  // (for timing both).
  property int rowsSeq: 0
  property int rowsChanges: 0
  readonly property alias field: field
  readonly property alias card: card
  readonly property alias view: view
  readonly property alias keeper: keeper

  readonly property var shown: {
    var q = query.trim().toLowerCase()
    var tags = filterTags
    if (!q && !tags.length) return rows
    var words = q ? q.split(/\s+/) : []
    return rows.filter(function(r) {
      for (var i = 0; i < tags.length; i++) if (r.tags.indexOf(tags[i]) < 0) return false
      for (var j = 0; j < words.length; j++) if (r.hay.indexOf(words[j]) < 0) return false
      return true
    })
  }
  // [{ tag, count }] over every live note, most used first; a selected tag
  // no note has (yet) still gets its chip.
  readonly property var tagCounts: {
    var counts = {}
    for (var i = 0; i < rows.length; i++) {
      var ts = rows[i].tags
      for (var j = 0; j < ts.length; j++) counts[ts[j]] = (counts[ts[j]] || 0) + 1
    }
    for (var k = 0; k < filterTags.length; k++) if (!(filterTags[k] in counts)) counts[filterTags[k]] = 0
    return Object.keys(counts).map(t => ({ tag: t, count: counts[t] }))
                 .sort((a, b) => b.count - a.count || (a.tag < b.tag ? -1 : 1))
  }

  visible: opened
  color: "transparent"
  anchors { top: true; bottom: true; left: true; right: true }
  exclusionMode: ExclusionMode.Ignore
  WlrLayershell.namespace: "eastbluewizard-stickies-list"
  WlrLayershell.layer: WlrLayer.Overlay
  WlrLayershell.keyboardFocus: keeper.keyboardFocus

  OverlayFocus {
    id: keeper
    window: win
    field: field
  }

  // Hidden, so moving it to the monitor that has focus only picks where
  // the next surface maps.
  function prepare(tags) {
    var s = service ? service.screenWithFocus() : null
    if (s && screen !== s) screen = s
    field.text = ""
    setFilter(tags)
    selected = 0
    view.contentY = 0
    if (service) service.leaveFront()  // one focus grab at a time
    // Fresh rows once the first frame (with the rows it had) is out:
    // parsing 2,000 of them before it held that frame back ~30 ms.
    pendingRefresh = true
    refreshFallback.restart()
  }
  property bool pendingRefresh: false
  function refreshNow() {
    if (!pendingRefresh) return
    pendingRefresh = false
    refresh()
  }
  Connections {
    target: field.Window.window
    enabled: win.pendingRefresh
    function onFrameSwapped() { win.refreshNow() }
  }
  Timer { id: refreshFallback; interval: 150; onTriggered: win.refreshNow() }
  // Already open (`stickies list --open --tag X` again): just refilter.
  // A new array only when it differs: each one refilters (and rebuilds the
  // view of) every row.
  function setFilter(tags) {
    tags = tags || []
    if (tags.join(" ") !== filterTags.join(" ")) filterTags = tags
  }
  function open(tags) {
    if (opened) { setFilter(tags); selected = 0; return }
    prepare(tags)
    keeper.open()
  }
  function close() { keeper.close() }
  function toggle(tags) {
    if (!opened) prepare(tags)
    keeper.toggle()
  }

  // What the rows showed last time: an unchanged list isn't reassigned (a
  // new model rebuilds the view and loses the scroll position).
  property string rowsSig: ""
  function refresh() {
    if (!service) return
    service.request("list", { brief: true }, function(ok, r) {
      if (!ok) return
      win.rowsSeq++
      var sig = r.map(x => x.id + " " + x.updated_at + " " + x.remind_at + " " + x.tags.join(",")).join("\n")
      if (sig === win.rowsSig) return
      win.rowsSig = sig
      var keep = win.shown.length ? win.shown[Math.min(win.selected, win.shown.length - 1)].id : -1
      var y = view.contentY
      win.rows = r
      win.rowsChanges++
      if (keep >= 0) {
        var at = win.shown.findIndex(x => x.id === keep)
        if (at >= 0) win.selected = at
      }
      win.clampSelected()
      view.contentY = Math.max(0, Math.min(y, view.contentHeight - view.height))
    })
  }
  // Notes change while it is open (an archive, an edit, an agent's add).
  Timer { id: refreshTimer; interval: 150; onTriggered: win.refresh() }
  Connections {
    target: win.service
    function onRevisionChanged() { if (win.opened) refreshTimer.restart() }
  }

  function clampSelected() {
    if (selected >= shown.length) selected = Math.max(0, shown.length - 1)
  }
  onShownChanged: clampSelected()

  function toggleTag(tag) {
    var i = filterTags.indexOf(tag)
    filterTags = i >= 0 ? filterTags.filter(t => t !== tag) : filterTags.concat([tag])
    selected = 0
    field.forceActiveFocus()
  }

  function move(delta) {
    if (!shown.length) return
    selected = Math.max(0, Math.min(shown.length - 1, selected + delta))
    view.positionViewAtIndex(selected, ListView.Contain)
  }

  function pick(i) {
    if (i < 0 || i >= shown.length || !service) return
    var nid = shown[i].id
    close()
    service.goTo(nid, true)
  }

  function archive(i) {
    if (i < 0 || i >= shown.length || !service) return
    var nid = shown[i].id
    rows = rows.filter(r => r.id !== nid)
    service.archive(nid, screen ? screen.name : "")
  }
  readonly property bool canUndo: !!service && !!service.notice && service.notice.undo === "archive"

  // Reminder time for the bell, as the note's header shows it.
  function remindLabel(iso) {
    if (!iso) return ""
    var d = new Date(iso)
    if (isNaN(d.getTime())) return ""
    var now = new Date()
    var hm = Qt.formatTime(d, "HH:mm")
    if (d.toDateString() === now.toDateString()) return hm
    if (d - now < 6 * 86400000 && d > now) return Qt.formatDate(d, "ddd") + " " + hm
    return Qt.formatDate(d, "d MMM") + " " + hm
  }

  readonly property color dim: Qt.darker(Color.menu.text, 1.4)

  // ---------------------------------------------------------------- scrim
  Rectangle {
    anchors.fill: parent
    color: Color.menu.scrim
    MouseArea { anchors.fill: parent; onClicked: keeper.clickOutside() }
  }

  // ---------------------------------------------------------------- card
  Rectangle {
    id: card
    width: Math.min(Style.space(720), win.width - Style.gapsOut * 4)
    height: column.implicitHeight + Style.spacing.panelPadding * 2
    anchors.horizontalCenter: parent.horizontalCenter
    y: Math.round(win.height * 0.12)
    radius: Style.cornerRadius
    color: Color.menu.background
    border.color: Color.menu.border
    border.width: Math.max(1, Style.space(2))

    MouseArea { anchors.fill: parent }  // clicks on the card don't close it

    Column {
      id: column
      anchors { left: parent.left; right: parent.right; top: parent.top; margins: Style.spacing.panelPadding }
      spacing: Style.spacing.md

      // Tag filter chips: All, then every tag with its count.
      Flow {
        id: chips
        width: parent.width
        spacing: Style.space(6)
        visible: win.tagCounts.length > 0
        TagChip {
          tag: "All"
          count: String(win.rows.length)
          ink: Color.menu.text
          pixelSize: Style.font.caption
          clickable: true
          selected: win.filterTags.length === 0
          onClicked: { win.filterTags = []; win.selected = 0; field.forceActiveFocus() }
        }
        Repeater {
          model: win.tagCounts
          TagChip {
            required property var modelData
            tag: modelData.tag
            count: String(modelData.count)
            ink: Color.menu.text
            pixelSize: Style.font.caption
            clickable: true
            selected: win.filterTags.indexOf(modelData.tag) >= 0
            onClicked: win.toggleTag(modelData.tag)
          }
        }
      }

      TextField {
        id: field
        width: parent.width
        placeholderText: "Filter notes"
        foreground: Color.menu.text
        onTextChanged: { win.query = text; win.selected = 0; view.positionViewAtBeginning() }
        onAccepted: win.pick(win.selected)
        Keys.onEscapePressed: keeper.pressEscape()
        Keys.onDownPressed: win.move(1)
        Keys.onUpPressed: win.move(-1)
        Keys.onTabPressed: win.move(1)
        Keys.onBacktabPressed: win.move(-1)
        Keys.onDeletePressed: win.archive(win.selected)
        Keys.onPressed: event => {
          if (event.key === Qt.Key_PageDown) { win.move(10); event.accepted = true }
          else if (event.key === Qt.Key_PageUp) { win.move(-10); event.accepted = true }
          else if (event.key === Qt.Key_Z && (event.modifiers & Qt.ControlModifier) && win.canUndo) {
            win.service.undoNotice()
            event.accepted = true
          }
        }
      }

      Text {
        width: parent.width
        textFormat: Text.PlainText
        text: {
          var n = win.shown.length
          var s = n + (n === 1 ? " note" : " notes")
          if (win.filterTags.length || win.query.trim()) s += " of " + win.rows.length
          return s + "  ·  Enter: go to  ·  Delete: archive"
        }
        color: win.dim
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }

      ListView {
        id: view
        width: parent.width
        // A fixed height: the card doesn't jump as the filter narrows.
        height: Math.min(Style.space(520), win.height * 0.62)
        clip: true
        model: win.shown
        reuseItems: true
        cacheBuffer: 0
        boundsBehavior: Flickable.StopAtBounds
        currentIndex: win.selected
        highlightFollowsCurrentItem: false

        Text {
          anchors.centerIn: parent
          visible: win.shown.length === 0
          textFormat: Text.PlainText
          text: win.rows.length ? "No notes match" : "No notes yet"
          color: win.dim
          font.family: Style.font.family
          font.pixelSize: Style.font.body
        }

        delegate: Rectangle {
          id: row
          required property var modelData
          required property int index
          readonly property bool current: index === win.selected
          width: view.width
          height: Style.space(34)
          radius: Style.cornerRadius
          color: current ? Color.menu.selectedBackground : "transparent"

          Rectangle {
            id: swatch
            anchors { left: parent.left; leftMargin: Style.space(8); verticalCenter: parent.verticalCenter }
            width: Style.space(10)
            height: width
            radius: Style.space(2)
            color: win.service ? win.service.fillFor(row.modelData.color) : "#fff3a3"
            border.width: 1
            border.color: Qt.rgba(0, 0, 0, 0.25)
          }

          Text {
            id: title
            anchors {
              left: swatch.right; leftMargin: Style.space(10)
              verticalCenter: parent.verticalCenter
            }
            width: Math.max(0, meta.x - x - tagsRow.width - Style.space(16))
            textFormat: Text.PlainText
            text: (row.modelData.pinned ? "\u{f0403} " : "") + (row.modelData.title || "(empty note)")
            elide: Text.ElideRight
            maximumLineCount: 1
            color: Color.menu.text
            opacity: row.modelData.title ? 1 : 0.6
            font.family: Style.font.family
            font.pixelSize: Style.font.body
          }

          Row {
            id: tagsRow
            anchors { right: meta.left; rightMargin: Style.space(10); verticalCenter: parent.verticalCenter }
            width: Math.min(implicitWidth, row.width * 0.38)
            clip: true
            spacing: Style.space(4)
            Repeater {
              model: row.modelData.tags
              TagChip {
                required property string modelData
                tag: modelData
                ink: Color.menu.text
                selected: win.filterTags.indexOf(modelData) >= 0
                pixelSize: Style.font.caption - 1
              }
            }
          }

          Row {
            id: meta
            anchors { right: parent.right; rightMargin: Style.space(8); verticalCenter: parent.verticalCenter }
            spacing: Style.space(10)
            Text {
              visible: text !== ""
              textFormat: Text.PlainText
              // nf-md-bell_outline + when it rings
              text: row.modelData.remind_at ? "\u{f009c} " + win.remindLabel(row.modelData.remind_at) : ""
              color: win.dim
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
            Text {
              visible: !!row.modelData.checks
              textFormat: Text.PlainText
              text: row.modelData.checks ? row.modelData.checks[0] + "/" + row.modelData.checks[1] : ""
              color: win.dim
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
            Text {
              width: Style.space(40)
              horizontalAlignment: Text.AlignRight
              textFormat: Text.PlainText
              text: !row.modelData.pinned && row.modelData.workspace > 0 ? "ws " + row.modelData.workspace : "all"
              color: win.dim
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
          }

          MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onEntered: win.selected = row.index
            onClicked: win.pick(row.index)
          }
        }
      }

      // The archive just made (Delete), with its Undo: the desktop's toast
      // sits under this overlay, so the same undo is offered here too.
      Item {
        width: parent.width
        height: undoText.implicitHeight
        visible: win.canUndo
        Text {
          id: undoText
          textFormat: Text.PlainText
          text: "Archived  ·  "
          color: win.dim
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }
        Text {
          anchors.left: undoText.right
          textFormat: Text.PlainText
          text: "Undo (Ctrl+Z)"
          color: Color.accent
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          font.underline: undoMouse.containsMouse
          MouseArea {
            id: undoMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: win.service.undoNotice()
          }
        }
      }
    }
  }
}
