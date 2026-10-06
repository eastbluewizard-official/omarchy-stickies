import QtQuick
import qs.Commons

// Small glyph button for a note's header. Sits above the header's drag
// strip, so it receives its own press and click.
Item {
  id: button

  property string glyph: ""
  property string tip: ""
  property bool active: false
  property color ink: "black"
  signal clicked()

  width: 22; height: 22

  Rectangle {
    anchors.fill: parent
    radius: 4
    color: Qt.rgba(button.ink.r, button.ink.g, button.ink.b,
                   mouse.pressed ? 0.18 : mouse.containsMouse ? 0.10 : 0)
  }

  Text {
    anchors.centerIn: parent
    text: button.glyph
    color: button.ink
    opacity: button.active || mouse.containsMouse ? 0.9 : 0.5
    font.family: Style.font.family
    font.pixelSize: 14
  }

  MouseArea {
    id: mouse
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    onClicked: button.clicked()
  }
}
