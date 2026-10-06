import QtQuick
import Quickshell
import Quickshell.Hyprland
import qs.Ui

// Bar entry point: the note count, and a click toggles BarPanel (pinned +
// recent notes, quick capture, search). It owns no data and starts no
// process: everything goes through this plugin's own service (Service.qml,
// the single `stickies serve` client), looked up through the scoped shell
// facade the bar hands every third-party widget.
BarWidget {
  id: root
  moduleName: "eastbluewizard.stickies"

  // The service is created asynchronously at shell start and replaced on
  // plugin hot-reload; a QtObject property goes null when it is destroyed,
  // and the timer looks it up again until it exists.
  property QtObject service: null
  function resolveService() {
    var sh = bar ? bar.shell : null
    service = sh && typeof sh.serviceFor === "function" ? sh.serviceFor(moduleName) : null
  }
  Timer {
    interval: 500
    repeat: true
    running: !root.service
    triggeredOnStart: true
    onTriggered: root.resolveService()
  }

  readonly property int count: service ? service.notes.count : 0
  readonly property int pinnedCount: service ? service.pinnedCount : 0
  readonly property bool hiddenAll: service ? service.hidden : false
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }

  // `omarchy-shell stickies search` reaches every bar (one per
  // monitor); only the one on the focused monitor answers.
  readonly property string screenName: {
    var w = button.QsWindow.window
    return w && w.screen ? w.screen.name : ""
  }
  Connections {
    target: root.service
    function onSearchRequested() {
      var mon = Hyprland.focusedMonitor
      if (root.screenName && mon && mon.name !== root.screenName) return
      if (panelLoader.item) panelLoader.item.openSearch()
    }
  }

  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.anchorItem = button
    panelLoader.item.hostWidget = root
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: { resolveService(); injectPanel() }

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("BarPanel.qml")
    visible: false
    onLoaded: {
      item.service = Qt.binding(() => root.service)
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // nf-md-note_outline when hidden, nf-md-note otherwise.
    text: (root.hiddenAll ? "󰎛 " : "󰎚 ") + root.count
    dimmed: root.hiddenAll
    tooltipText: "Stickies: " + root.count + " notes, " + root.pinnedCount + " pinned"
      + (root.hiddenAll ? " (hidden)" : "")
      + "\nclick: list + quick note, right-click: new note, middle-click: show/hide"
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) { if (root.service) root.service.newNoteHere("") }
      else if (buttonCode === Qt.MiddleButton) { if (root.service) root.service.hidden = !root.service.hidden }
      else root.toggle()
    }
  }
}
