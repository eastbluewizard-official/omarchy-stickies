import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "highlight.js" as Highlight

// One-key search over every note (SUPER + ALT + J, `omarchy-shell stickies
// find`): a box on the focused monitor; results update as you type, by
// words (FTS5) and by meaning (local embeddings) through the service's
// `stickies serve` (hybrid mode). Up/Down pick, Enter jumps to the note on
// its workspace and flashes it. Esc, a click outside the card or the key
// again closes; losing the keyboard does not (OverlayFocus.qml takes it
// back: right after the map Hyprland can hand it to the window under the
// pointer).
//
// Built hidden when the service starts, so opening only maps a surface.
PanelWindow {
  id: win

  property QtObject service
  readonly property bool opened: keeper.opened
  property var hits: []
  property int selected: 0
  property string searchedFor: ""
  // At most one search in flight; keys typed meanwhile collapse into the
  // newest query, sent when the answer comes back.
  property bool inFlight: false
  property string pending: ""
  // Bumped each time a result list lands (for timing it).
  property int resultsSeq: 0
  // The list on screen is words only because the model was still loading.
  property bool wordsOnly: false
  readonly property alias field: field
  readonly property alias card: card  // benches: where the panel is
  readonly property alias keeper: keeper  // tests/shell/focus_harness.qml

  visible: opened
  color: "transparent"
  anchors { top: true; bottom: true; left: true; right: true }
  exclusionMode: ExclusionMode.Ignore
  WlrLayershell.namespace: "eastbluewizard-stickies-search"
  WlrLayershell.layer: WlrLayer.Overlay
  WlrLayershell.keyboardFocus: keeper.keyboardFocus

  OverlayFocus {
    id: keeper
    window: win
    field: field
  }

  // Hidden, so moving it to the monitor that has focus (rotated ones too)
  // only picks where the next surface maps.
  // serve dropped the model while idle; a search loads it again and is
  // answered with words meanwhile. Once it is back, ask again: by-meaning
  // hits fill in without a key press.
  Connections {
    target: win.service
    function onSemanticReady() {
      if (win.opened && win.wordsOnly && field.text.trim()) win.runSearch(field.text)
    }
  }

  function prepare() {
    var s = service ? service.screenWithFocus() : null
    if (s && screen !== s) screen = s
    field.text = ""
    hits = []
    searchedFor = ""
    selected = 0
    if (service) service.leaveFront()  // one focus grab at a time
  }
  function open() {
    prepare()
    keeper.open()
  }
  function close() { keeper.close() }
  // `find`: the key. A second delivery of the same press is ignored.
  function toggle() {
    if (!opened) prepare()
    keeper.toggle()
  }

  function runSearch(text) {
    var q = text.trim()
    if (!q || !service) {
      pending = ""
      hits = []
      searchedFor = ""
      return
    }
    if (inFlight) { pending = q; return }
    inFlight = true
    service.search(q, function(result) {
      win.inFlight = false
      var next = win.pending
      win.pending = ""
      if (field.text.trim() !== q) {
        // typed on meanwhile: skip this stale list, ask for the newest
        if (next) win.runSearch(next)
        else if (field.text.trim()) win.runSearch(field.text)
        return
      }
      win.hits = result
      win.wordsOnly = service.semantic && !service.semanticLoaded
      win.searchedFor = q
      win.selected = 0
      win.resultsSeq++
    })
  }

  function pick(i) {
    if (i < 0 || i >= hits.length || !service) return
    var nid = hits[i].id
    close()
    service.goTo(nid, true)
  }

  function move(delta) {
    if (!hits.length) return
    selected = (selected + delta + hits.length) % hits.length
    list.positionViewAtIndex(selected, ListView.Contain)
  }

  // ---------------------------------------------------------------- scrim
  Rectangle {
    anchors.fill: parent
    color: Color.menu.scrim
    MouseArea { anchors.fill: parent; onClicked: keeper.clickOutside() }
  }

  // ---------------------------------------------------------------- card
  Rectangle {
    id: card
    width: Math.min(Style.space(640), win.width - Style.gapsOut * 4)
    height: column.implicitHeight + Style.spacing.panelPadding * 2
    anchors.horizontalCenter: parent.horizontalCenter
    y: Math.round(win.height * 0.16)
    radius: Style.cornerRadius
    color: Color.menu.background
    border.color: Color.menu.border
    border.width: Math.max(1, Style.space(2))

    MouseArea { anchors.fill: parent }  // clicks on the card don't close it

    Column {
      id: column
      anchors { left: parent.left; right: parent.right; top: parent.top; margins: Style.spacing.panelPadding }
      spacing: Style.spacing.md

      TextField {
        id: field
        width: parent.width
        placeholderText: "Search notes: words or meaning"
        foreground: Color.menu.text
        onTextChanged: win.runSearch(text)
        onAccepted: win.pick(win.selected)
        Keys.onEscapePressed: keeper.pressEscape()
        Keys.onDownPressed: win.move(1)
        Keys.onUpPressed: win.move(-1)
        Keys.onTabPressed: win.move(1)
        Keys.onBacktabPressed: win.move(-1)
      }

      Text {
        id: status
        width: parent.width
        visible: text !== ""
        textFormat: Text.PlainText
        text: {
          var s = ""
          if (win.searchedFor !== "" && field.text.trim() !== "")
            s = win.hits.length ? win.hits.length + (win.hits.length === 1 ? " note" : " notes") : "No matches"
          if (win.service && !win.service.semantic)
            s += (s ? "  ·  " : "") + "words only (run `stickies setup` to also search by meaning)"
          else if (win.wordsOnly && s)
            s += "  ·  by meaning in a moment"
          return s
        }
        color: Qt.darker(Color.menu.text, 1.4)
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }

      ListView {
        id: list
        width: parent.width
        height: Math.min(contentHeight, Style.space(440))
        visible: win.hits.length > 0
        clip: true
        model: win.hits
        boundsBehavior: Flickable.StopAtBounds
        spacing: Style.space(2)

        delegate: Rectangle {
          id: row
          required property var modelData
          required property int index
          readonly property bool current: index === win.selected
          width: list.width
          height: Math.max(snippet.implicitHeight, Style.space(22)) + Style.space(12)
          radius: Style.cornerRadius
          color: current ? Color.menu.selectedBackground : "transparent"

          Rectangle {
            id: swatch
            anchors { left: parent.left; leftMargin: Style.space(8); top: parent.top; topMargin: Style.space(10) }
            width: Style.space(10)
            height: width
            radius: Style.space(2)
            color: win.service ? win.service.fillFor(row.modelData.color) : "#fff3a3"
            border.width: 1
            border.color: Qt.rgba(0, 0, 0, 0.25)
          }

          // Escaped note text with the matched words emphasised.
          Text {
            id: snippet
            anchors {
              left: swatch.right; leftMargin: Style.space(10)
              right: meta.left; rightMargin: Style.space(10)
              verticalCenter: parent.verticalCenter
            }
            textFormat: Text.StyledText
            text: Highlight.styled(row.modelData.snippet || row.modelData.body, row.modelData.highlights,
                                   String(Color.menu.selectedText))
            wrapMode: Text.Wrap
            maximumLineCount: 2
            elide: Text.ElideRight
            color: Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.body
          }

          Column {
            id: meta
            anchors { right: parent.right; rightMargin: Style.space(8); verticalCenter: parent.verticalCenter }
            Text {
              anchors.right: parent.right
              textFormat: Text.PlainText
              text: !row.modelData.pinned && row.modelData.workspace > 0 ? "ws " + row.modelData.workspace : "all"
              color: Qt.darker(Color.menu.text, 1.4)
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
            Text {
              anchors.right: parent.right
              visible: row.modelData.match === "semantic"
              textFormat: Text.PlainText
              text: "≈ meaning"
              color: Qt.darker(Color.menu.text, 1.4)
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              font.italic: true
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
    }
  }
}
