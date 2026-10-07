import QtQuick
import Quickshell
import qs.Commons

// One monitor's worth of stickies: the notes that belong here, the
// new-note button, the undo toast, and the list of input regions the
// layer surface uses as its mask.
Item {
  id: desktop

  property var service
  property string screenName: ""
  property bool primary: false
  property var knownScreens: []
  property int workspaceId: -1
  // The layer surface this lives on (front mode's focus grab lists them).
  property var window: null

  // Region objects collected from the cards plus our own controls.
  property var cardRegions: []
  readonly property var regions: cardRegions.concat([newRegion],
                                                    toast.visible ? [toastRegion] : [],
                                                    setupToast.visible ? [setupRegion] : [])

  // The service's toast shows on the screen it names (unknown -> primary).
  readonly property bool toastHere: !!service.notice
    && (service.notice.screen === screenName
        || (primary && knownScreens.indexOf(service.notice.screen) < 0))

  // Snap guides while a note is dragged: x / y of the edge it snapped to.
  property int guideX: -1
  property int guideY: -1

  // Edges of every other note on this screen (committed geometry), for
  // the drag's snapping: { xs: [left, right, ...], ys: [top, bottom, ...] }.
  function snapEdges(nid) {
    var xs = [], ys = []
    for (var i = 0; i < cards.count; i++) {
      var s = cards.itemAt(i)
      if (!s || !s.shown || s.nid === nid) continue
      var h = s.rolled ? 26 : s.h
      xs.push(s.x0, s.x0 + s.w)
      ys.push(s.y0, s.y0 + h)
    }
    return { xs: xs, ys: ys }
  }

  // A note lives on its monitor (unknown or unset -> the primary one) and
  // its workspace; pinned notes and notes without a workspace show on all.
  function shows(monitor, workspace, pinned) {
    var mine = monitor === "" || knownScreens.indexOf(monitor) < 0 ? primary : monitor === screenName
    return mine && (pinned || workspace < 0 || workspace === workspaceId)
  }

  function cardFor(nid) {
    for (var i = 0; i < cards.count; i++) {
      var s = cards.itemAt(i)
      if (s && s.nid === nid) return s.card
    }
    return null
  }

  function addRegion(r) { cardRegions = cardRegions.concat([r]) }
  function removeRegion(r) { cardRegions = cardRegions.filter(x => x !== r) }

  // One cheap slot per note in the model; the card itself is only built
  // while the note shows on this monitor + workspace, so thousands of notes
  // elsewhere cost a few empty Items, not thousands of cards and regions.
  Repeater {
    id: cards
    model: desktop.service.notes

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
      required property int z0
      required property bool rolled
      required property string remindAt
      required property string tags
      readonly property bool shown: desktop.shows(monitor, workspace, pinned)
      // In the waterfall column (or gliding back from it) it is drawn there;
      // the card stays built, hidden, so switching layouts costs no rebuild.
      readonly property bool inColumn: !desktop.service.entering && desktop.service.docked(nid, workspace, pinned)
                                       || desktop.service.leaving[nid] === true
      readonly property var card: loader.item

      z: z0

      Loader {
        id: loader
        active: slot.shown
        sourceComponent: NoteCard {
          service: desktop.service
          desk: desktop
          visible: !slot.inColumn
          nid: slot.nid
          body: slot.body
          color: slot.color
          pinned: slot.pinned
          workspace: slot.workspace
          monitor: slot.monitor
          x0: slot.x0
          y0: slot.y0
          w: slot.w
          h: slot.h
          rolled: slot.rolled
          remindAt: slot.remindAt
          tags: slot.tags
        }
      }
    }
  }

  // -------------------------------------------------------- new note
  Rectangle {
    id: newButton
    width: 36; height: 36; radius: 18
    anchors { right: parent.right; bottom: parent.bottom; rightMargin: 18; bottomMargin: 18 }
    z: 100000
    color: desktop.service.fillFor("yellow")
    border.width: 1
    border.color: Qt.rgba(0, 0, 0, newMouse.containsMouse ? 0.35 : 0.18)
    opacity: newMouse.containsMouse ? 1 : 0.75

    Text {
      anchors.centerIn: parent
      text: "+"
      color: desktop.service.ink
      font.pixelSize: 22
      font.family: Style.font.family
    }

    MouseArea {
      id: newMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: {
        var n = 0
        for (var i = 0; i < cards.count; i++) if (cards.itemAt(i) && cards.itemAt(i).shown) n++
        // Cascade up-left from the button so new notes don't pile up.
        var x = Math.max(16, desktop.width - 300 - (n % 6) * 28)
        var y = Math.max(16, desktop.height - 300 - (n % 6) * 28)
        desktop.service.newNote(desktop.screenName, desktop.workspaceId, x, y)
      }
    }
  }
  Region { id: newRegion; item: newButton }

  // -------------------------------------------------------- first run
  // No live notes at all: a quiet hint next to the + button on the primary
  // screen. Not an input region, so clicks go straight through it.
  readonly property bool showHint: desktop.primary && desktop.service.listed
                                   && desktop.service.notes.count === 0 && !desktop.service.hidden
  Rectangle {
    id: hint
    visible: desktop.showHint
    z: 99999
    anchors { right: newButton.left; bottom: parent.bottom; rightMargin: 12; bottomMargin: 18 }
    width: hintText.implicitWidth + 24
    height: hintText.implicitHeight + 16
    radius: 6
    color: Color.popups.background
    border.color: Color.popups.border
    border.width: 1
    opacity: 0.9

    Text {
      id: hintText
      anchors.centerIn: parent
      textFormat: Text.StyledText
      text: "<b>No sticky notes yet</b><br>"
            + "+ or SUPER + ALT + N &nbsp; new note<br>"
            + "SUPER + ALT + V &nbsp; note from the clipboard<br>"
            + "SUPER + ALT + J &nbsp; find a note<br>"
            + "SUPER + ALT + A &nbsp; ask your notes"
      color: Color.popups.text
      font.family: Style.font.family
      font.pixelSize: 12
      lineHeight: 1.25
    }
  }

  // -------------------------------------------------------- snap guides
  // Faint lines along the edge a dragged note snapped to. Drawn only, never
  // part of the input region. Parked off-screen rather than hidden: flipping
  // `visible` many times a second re-batched every card (drag p95 17.4 ->
  // 18.6 ms at 151 notes); moving costs nothing.
  Rectangle {
    x: desktop.guideX >= 0 ? desktop.guideX : -4
    width: 1
    height: parent.height
    z: 99998
    color: Color.accent
    opacity: 0.45
  }
  Rectangle {
    y: desktop.guideY >= 0 ? desktop.guideY : -4
    width: parent.width
    height: 1
    z: 99998
    color: Color.accent
    opacity: 0.45
  }

  // -------------------------------------------------------- toast
  // "Archived - Undo", "Tidied 6 notes - Undo", "Reminder: ... - Show",
  // or plain words when something didn't work (never a traceback). Gone
  // after 6 s (a reminder: 15 s).
  Rectangle {
    id: toast
    visible: desktop.toastHere
    z: 100001
    anchors { horizontalCenter: parent.horizontalCenter; bottom: parent.bottom; bottomMargin: 24 }
    width: Math.min(toastRow.implicitWidth + 28, desktop.width - 32)
    height: 38
    radius: 6
    color: Color.popups.background
    border.color: Color.popups.border
    border.width: 1

    Row {
      id: toastRow
      anchors.centerIn: parent
      spacing: 14
      Text {
        anchors.verticalCenter: parent.verticalCenter
        width: Math.min(implicitWidth, desktop.width - 120)
        elide: Text.ElideRight
        text: desktop.service.notice ? desktop.service.notice.text : ""
        textFormat: Text.PlainText
        color: Color.popups.text
        font.family: Style.font.family
        font.pixelSize: 13
      }
      Text {
        anchors.verticalCenter: parent.verticalCenter
        visible: !!desktop.service.notice && desktop.service.notice.undo !== ""
        text: desktop.service.notice && desktop.service.notice.undo === "reminder" ? "Show" : "Undo"
        color: Color.accent
        font.family: Style.font.family
        font.pixelSize: 13
        font.bold: true
        font.underline: undoMouse.containsMouse
        MouseArea {
          id: undoMouse
          anchors.fill: parent
          anchors.margins: -8
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: desktop.service.undoNotice()
        }
      }
    }

    Timer {
      id: toastTimer
      running: toast.visible
      interval: desktop.service.reminding ? 15000 : 6000
      onTriggered: desktop.service.notice = null
    }
    Connections {
      target: desktop.service
      function onNoticeChanged() { if (desktop.toastHere) toastTimer.restart() }
    }
  }
  Region { id: toastRegion; item: toast }

  // -------------------------------------------------------- set-up offer
  // First start after `omarchy plugin add`: ask once whether to add the
  // keys and the `stickies` command (Service.setupState). Never silent.
  Rectangle {
    id: setupToast
    visible: desktop.primary && desktop.service.setupState !== "" && !desktop.service.hidden
    z: 100001
    anchors { horizontalCenter: parent.horizontalCenter; bottom: parent.bottom; bottomMargin: 72 }
    width: Math.min(desktop.width - 48, setupCol.implicitWidth + 32)
    height: setupCol.implicitHeight + 24
    radius: 6
    color: Color.popups.background
    border.color: Color.popups.border
    border.width: 1

    Column {
      id: setupCol
      anchors.centerIn: parent
      spacing: 10
      Text {
        width: Math.min(implicitWidth, desktop.width - 80)
        wrapMode: Text.Wrap
        textFormat: Text.StyledText
        text: desktop.service.setupState === "offer"
              ? "<b>Set up Stickies?</b><br>"
                + "Adds the SUPER + ALT keys: N new note, SHIFT + N notes above windows, V note from the clipboard,<br>"
                + "J find, A ask, L layout, W collapse the column, for this session and every next one,<br>"
                + "and a <tt>stickies</tt> command in ~/.local/bin. Undo any time: <tt>stickies uninstall</tt>."
              : desktop.service.setupText
        color: Color.popups.text
        font.family: Style.font.family
        font.pixelSize: 13
        lineHeight: 1.2
      }
      Row {
        spacing: 18
        visible: desktop.service.setupState === "offer"
        Repeater {
          model: [{ label: "Set up", yes: true }, { label: "Not now", yes: false }]
          Text {
            required property var modelData
            text: modelData.label
            color: modelData.yes ? Color.accent : Color.popups.text
            font.family: Style.font.family
            font.pixelSize: 13
            font.bold: modelData.yes
            font.underline: answerMouse.containsMouse
            MouseArea {
              id: answerMouse
              anchors.fill: parent
              anchors.margins: -8
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: desktop.service.answerSetup(parent.modelData.yes)
            }
          }
        }
      }
    }
  }
  Region { id: setupRegion; item: setupToast }
}
