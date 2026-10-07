import QtQuick
import qs.Commons
import "markup.js" as Markup

// A titled list of notes for the bar panel: colour swatch, first line,
// where it shows (workspace or "all"). Rows are plain objects
// { nid, body, color, pinned, workspace, plain? }; clicking one emits
// picked(nid). The line shows in plain words (no markup), unless `plain`
// says the body is that already (a search snippet).
Column {
  id: list

  property string label: ""
  property var rows: []
  property QtObject service: null
  property color foreground: Color.foreground

  signal picked(int nid)

  spacing: Style.spacing.xs

  function firstLine(body, plain) {
    var lines = String(body || "").split("\n")
    for (var i = 0; i < lines.length; i++) {
      var l = (plain ? lines[i] : Markup.plainLine(lines[i])).trim()
      if (l) return l
    }
    return "(empty)"
  }

  Text {
    visible: list.label !== ""
    text: list.label
    color: Qt.darker(list.foreground, 1.3)
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    font.bold: true
  }

  Repeater {
    model: list.rows

    delegate: Rectangle {
      id: row
      required property var modelData
      width: list.width
      height: Math.max(title.implicitHeight, where.implicitHeight) + Style.space(10)
      radius: Style.cornerRadius
      color: rowMouse.containsMouse ? Qt.rgba(list.foreground.r, list.foreground.g, list.foreground.b, 0.10) : "transparent"

      Rectangle {
        id: swatch
        anchors.left: parent.left
        anchors.leftMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(10)
        height: width
        radius: Style.space(2)
        color: list.service ? list.service.fillFor(row.modelData.color) : "#fff3a3"
        border.width: 1
        border.color: Qt.rgba(0, 0, 0, 0.25)
      }

      Text {
        id: title
        anchors.left: swatch.right
        anchors.leftMargin: Style.space(8)
        anchors.right: where.left
        anchors.rightMargin: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
        // Note bodies are user text, never markup.
        textFormat: Text.PlainText
        text: list.firstLine(row.modelData.body, row.modelData.plain)
        elide: Text.ElideRight
        color: list.foreground
        font.family: Style.font.family
        font.pixelSize: Style.font.body
      }

      Text {
        id: where
        anchors.right: parent.right
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter
        textFormat: Text.PlainText
        text: !row.modelData.pinned && row.modelData.workspace > 0 ? "ws " + row.modelData.workspace : "all"
        color: Qt.darker(list.foreground, 1.4)
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
      }

      MouseArea {
        id: rowMouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: list.picked(row.modelData.nid)
      }
    }
  }
}
