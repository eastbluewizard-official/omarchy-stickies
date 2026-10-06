import QtQuick
import qs.Commons

// A tag as a small, quiet chip: on a note (under its header, or in the
// header when rolled up), in the tag editor (removable: an x) and as a
// filter in the All notes view (count, selected).
Rectangle {
  id: chip

  property string tag: ""
  property color ink: "black"
  property bool removable: false
  property bool selected: false
  property bool clickable: false
  // Shown after the tag in a lighter weight ("" for none).
  property string count: ""
  property int pixelSize: 11
  signal clicked()
  signal removed()

  implicitHeight: label.implicitHeight + 4
  implicitWidth: label.implicitWidth + 12 + (removable ? close.width + 2 : 0)
  height: implicitHeight
  width: implicitWidth
  radius: height / 2
  color: Qt.rgba(ink.r, ink.g, ink.b, selected ? 0.24 : mouse.containsMouse && clickable ? 0.14 : 0.08)
  border.width: selected ? 1 : 0
  border.color: Qt.rgba(ink.r, ink.g, ink.b, 0.45)

  Text {
    id: label
    anchors { left: parent.left; leftMargin: 6; verticalCenter: parent.verticalCenter }
    textFormat: Text.StyledText
    // Tags are a-z, 0-9 and "-" (the CLI checks), so nothing to escape.
    text: chip.tag + (chip.count !== "" ? "<font color='" + Qt.rgba(chip.ink.r, chip.ink.g, chip.ink.b, 0.5)
                                          + "'> " + chip.count + "</font>" : "")
    color: chip.ink
    opacity: chip.selected ? 1 : 0.75
    font.family: Style.font.family
    font.pixelSize: chip.pixelSize
  }

  MouseArea {
    id: mouse
    anchors.fill: parent
    enabled: chip.clickable
    hoverEnabled: chip.clickable
    cursorShape: Qt.PointingHandCursor
    onClicked: chip.clicked()
  }

  Text {
    id: close
    visible: chip.removable
    anchors { right: parent.right; rightMargin: 5; verticalCenter: parent.verticalCenter }
    text: "×"
    color: chip.ink
    opacity: closeMouse.containsMouse ? 0.9 : 0.5
    font.family: Style.font.family
    font.pixelSize: chip.pixelSize + 1
    MouseArea {
      id: closeMouse
      anchors.fill: parent
      anchors.margins: -3
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: chip.removed()
    }
  }
}
