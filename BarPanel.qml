import QtQuick
import qs.Commons
import qs.Ui

// The bar widget's popup: a quick-capture field (Enter makes a note on the
// current workspace), search-as-you-type through `stickies serve`, and the
// pinned + most recent notes, and the layout picker. Clicking a note brings
// it into view: its workspace, raised, caret in it. All reads/writes go
// through the service.
Panel {
  id: root
  moduleName: "eastbluewizard.stickies"
  // The bar exists once per monitor and an IPC target routes to a single
  // handler, so open/close come from the widget, not a per-panel target.
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property QtObject service: null
  // For timing the popup window's first frame.
  property alias popup: panel

  readonly property int recentLimit: 8
  property var hits: []
  property string searchedFor: ""
  property string flash: ""

  function open() { root.controller.show() }
  function close() { root.controller.hide() }
  function openSearch() {
    open()
    Qt.callLater(() => searchField.forceActiveFocus())
  }

  onOpenedChanged: {
    if (opened) refresh()
    else { searchField.text = ""; flash = "" }
  }

  // Pinned + recent come from serve, refreshed on open and (while open,
  // debounced) whenever the service's model changes -- which includes each
  // keystroke in a desktop note. The last lists are kept between opens so
  // the panel never paints empty while the request is in flight.
  property var pinnedRows: []
  property var recentRows: []
  function refresh() {
    if (!service) return
    service.pinnedAndRecent(recentLimit, function(pinned, recent) {
      root.pinnedRows = pinned
      root.recentRows = recent
    })
  }
  Timer { id: refreshTimer; interval: 150; onTriggered: root.refresh() }
  Connections {
    target: root.service
    function onRevisionChanged() { if (root.opened) refreshTimer.restart() }
  }
  readonly property bool searching: searchField.text.trim() !== ""

  function runSearch() {
    var q = searchField.text.trim()
    if (!q || !service) { hits = []; searchedFor = ""; return }
    service.search(q, function(result) {
      if (searchField.text.trim() !== q) return  // a newer query is on its way
      searchedFor = q
      // A snippet is plain words already (serve strips the markup).
      hits = result.map(h => ({ nid: h.id, body: h.snippet || h.body, plain: !!h.snippet, color: h.color, pinned: h.pinned,
                                workspace: h.workspace === null ? -1 : h.workspace }))
    })
  }

  function capture() {
    var text = captureField.text.trim()
    if (!text || !service) return
    service.newNoteHere(text)
    captureField.text = ""
    flash = "Added to this workspace"
    flashTimer.restart()
  }

  function goTo(nid) {
    close()
    if (service) service.goTo(nid)
  }

  Timer { id: flashTimer; interval: 2500; onTriggered: root.flash = "" }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    focusTarget: captureField
    contentWidth: panel.fittedContentWidth(Style.space(340))
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(560))

    Flickable {
      id: flick
      anchors.fill: parent
      contentWidth: width
      contentHeight: content.implicitHeight
      clip: true
      boundsBehavior: Flickable.StopAtBounds

      Column {
        id: content
        width: flick.width
        spacing: Style.spacing.panelGap

        Item {
          width: parent.width
          height: Math.max(title.implicitHeight, actions.implicitHeight)

          Text {
            id: title
            anchors.verticalCenter: parent.verticalCenter
            text: "Stickies"
            color: root.barForeground
            font.family: Style.font.family
            font.pixelSize: Style.font.subtitle
            font.bold: true
          }

          Row {
            id: actions
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.spacing.controlGap
            Button {
              text: "New"
              onClicked: { root.close(); if (root.service) root.service.newNoteHere("") }
            }
            // Every note, filterable by tag (SUPER + ALT + O).
            Button {
              text: "All notes"
              onClicked: { root.close(); if (root.service) root.service.openList([]) }
            }
            // This workspace's unpinned notes in a grid; once more undoes it.
            // Free layout only: in a waterfall the column owns the places.
            Button {
              visible: !!root.service && !root.service.waterfall
              text: root.service && root.service.tidyUndoable ? "Undo tidy" : "Tidy"
              onClicked: {
                if (!root.service) return
                if (root.service.tidyUndoable) root.service.undoTidy()
                else root.service.tidyHere()
              }
            }
            Button {
              text: root.service && root.service.hidden ? "Show all" : "Hide all"
              onClicked: if (root.service) root.service.hidden = !root.service.hidden
            }
          }
        }

        // Layout picker: free, or the waterfall column on the left / right
        // edge (same as SUPER + ALT + L and `stickies layout`).
        Item {
          width: parent.width
          height: layoutRow.implicitHeight

          Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "Layout"
            color: root.barForeground
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }

          Row {
            id: layoutRow
            anchors.right: parent.right
            spacing: Style.spacing.controlGap
            Repeater {
              model: [{ name: "free", label: "Free" }, { name: "waterfall-left", label: "Left" },
                      { name: "waterfall-right", label: "Right" }]
              Button {
                required property var modelData
                text: modelData.label
                tooltipText: modelData.name === "free" ? "Notes where you put them"
                             : "This workspace's notes in a column on the " + (modelData.name === "waterfall-left" ? "left" : "right") + " edge"
                selected: !!root.service && root.service.layout === modelData.name
                onClicked: if (root.service) root.service.setLayout(modelData.name)
              }
            }
          }
        }

        TextField {
          id: captureField
          width: parent.width
          placeholderText: "Quick note, Enter to add"
          onAccepted: root.capture()
          Keys.onEscapePressed: root.close()
          Keys.onDownPressed: searchField.forceActiveFocus()
        }

        TextField {
          id: searchField
          width: parent.width
          placeholderText: "Search notes"
          onTextChanged: root.runSearch()
          onAccepted: if (root.hits.length) root.goTo(root.hits[0].nid)
          Keys.onEscapePressed: text ? text = "" : root.close()
          Keys.onUpPressed: captureField.forceActiveFocus()
        }

        Text {
          width: parent.width
          visible: root.flash !== ""
          text: root.flash
          color: Color.accent
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
        }

        // ---- search results, or pinned + recent
        NoteList {
          visible: root.searching
          label: root.hits.length ? root.hits.length + (root.hits.length === 1 ? " match" : " matches")
                 : (root.searchedFor ? "No matches" : "")
          rows: root.searching ? root.hits : []
          width: parent.width
          service: root.service
          foreground: root.barForeground
          onPicked: nid => root.goTo(nid)
        }
        NoteList {
          visible: !root.searching && root.pinnedRows.length > 0
          label: "Pinned"
          rows: root.searching ? [] : root.pinnedRows
          width: parent.width
          service: root.service
          foreground: root.barForeground
          onPicked: nid => root.goTo(nid)
        }
        NoteList {
          visible: !root.searching
          label: root.recentRows.length ? "Recent" : (root.pinnedRows.length ? "" : "No notes yet")
          rows: root.searching ? [] : root.recentRows
          width: parent.width
          service: root.service
          foreground: root.barForeground
          onPicked: nid => root.goTo(nid)
        }
      }
    }
  }
}
