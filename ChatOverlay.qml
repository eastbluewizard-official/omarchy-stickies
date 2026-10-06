import QtQuick
import Quickshell
import Quickshell.Hyprland
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "chat.js" as Chat

// Ask your notes (SUPER + ALT + A, `omarchy-shell stickies chat`). Enter
// once: the question's top notes (serve's hybrid search) are listed, all
// ticked; untick any. Enter again (or Send): only the question, the ticked
// notes and this chat's earlier turns go to Omarchy's default agent
// (`claude -p`, through serve), and the answer streams in. Citations are
// links/chips that jump to the note. Proposed actions (new note, append,
// hub todo) are cards; nothing happens until Apply. The conversation lives
// in this object only: per shell session, cleared by New chat.
//
// Built hidden when the service starts, like the search overlay.
PanelWindow {
  id: win

  property QtObject service
  readonly property bool opened: keeper.opened

  // [{ chat, question, sent: [{id,color,title,body}], answer, error,
  //    citations, unknown, proposals, rejected, done }]
  property var turns: []
  // Preview before sending: { question, notes: [...], ticked: {id: bool} }
  property var draft: null
  property bool loadingNotes: false
  property bool busy: false
  property string liveAnswer: ""
  property string liveChat: ""
  property int chatSeq: 0
  // "<turn>:<proposal>" -> { state: "applying"|"applied"|"dismissed"|"error", msg }
  property var propState: ({})
  // Bumped when an answer finishes (bench_chat.py times it).
  property int answersSeq: 0
  readonly property alias field: field
  readonly property alias card: card  // benches: where the panel is
  readonly property alias keeper: keeper  // tests/shell/focus_harness.qml

  visible: opened
  color: "transparent"
  anchors { top: true; bottom: true; left: true; right: true }
  exclusionMode: ExclusionMode.Ignore
  WlrLayershell.namespace: "eastbluewizard-stickies-chat"
  WlrLayershell.layer: WlrLayer.Overlay
  WlrLayershell.keyboardFocus: keeper.keyboardFocus

  // Esc, the key again or a click outside the card closes; losing the
  // keyboard does not (see OverlayFocus.qml). While an answer streams the
  // field is disabled, so the key catcher holds the keyboard for Esc.
  OverlayFocus {
    id: keeper
    window: win
    field: field.enabled ? field : keyCatcher
  }

  readonly property color muted: Qt.darker(Color.menu.text, 1.4)
  readonly property int tickedCount: {
    if (!draft) return 0
    var n = 0
    for (var i = 0; i < draft.notes.length; i++) if (draft.ticked[draft.notes[i].id]) n++
    return n
  }

  // Hidden, so this only picks the monitor (the focused one, rotated ones
  // too) the next surface maps on.
  function prepare() {
    var s = service ? service.screenWithFocus() : null
    if (s && screen !== s) screen = s
    if (service) service.leaveFront()  // one focus grab at a time
    Qt.callLater(() => transcript.positionViewAtEnd())
  }
  function open() {
    prepare()
    keeper.open()
  }
  function close() { keeper.close() }
  // `chat`: the key. A second delivery of the same press is ignored.
  function toggle() {
    if (!opened) prepare()
    keeper.toggle()
  }

  function newChat() {
    if (busy && service) service.cancelChat(liveChat)
    turns = []
    draft = null
    propState = ({})
    liveAnswer = ""
    field.text = ""
    field.forceActiveFocus()
  }

  // Enter: preview the notes for a new question, or send the previewed one.
  function submit() {
    var q = field.text.trim()
    if (!q || busy || !service) return
    if (draft && draft.question === q) { send(); return }
    preview(q)
  }

  function preview(q) {
    loadingNotes = true
    service.chatNotes(q, function(ok, notes) {
      win.loadingNotes = false
      if (field.text.trim() !== q) return  // edited meanwhile
      var ticked = {}
      var list = ok ? notes : []
      for (var i = 0; i < list.length; i++) ticked[list[i].id] = true
      // words: picked by words only, the model was still loading
      win.draft = { question: q, notes: list, ticked: ticked,
                    words: service.semantic && !service.semanticLoaded }
    })
  }

  // The model is back (serve drops it when idle): pick the notes again,
  // by meaning too, unless the person already unticked some.
  Connections {
    target: win.service
    function onSemanticReady() {
      if (win.draft && win.draft.words && !win.busy) win.preview(win.draft.question)
    }
  }

  function toggleTick(nid) {
    if (!draft) return
    var t = Object.assign({}, draft.ticked)
    t[nid] = !t[nid]
    draft = { question: draft.question, notes: draft.notes, ticked: t }
  }

  function history() {
    var out = []
    for (var i = 0; i < turns.length; i++)
      if (turns[i].done && !turns[i].error) out.push({ question: turns[i].question, answer: turns[i].answer })
    return out.slice(-6)
  }

  function send() {
    if (!draft || busy || !service) return
    var ids = draft.notes.filter(n => draft.ticked[n.id]).map(n => n.id)
    var cid = "chat-" + (++chatSeq)
    var turn = { chat: cid, question: draft.question, sent: [], answer: "", error: "",
                 citations: [], unknown: [], proposals: [], rejected: [], done: false }
    var hist = history()
    turns = turns.concat([turn])
    draft = null
    field.text = ""
    busy = true
    liveChat = cid
    liveAnswer = ""
    service.ask(cid, turn.question, ids, hist, function(ok, r) {
      var i = win.indexOfChat(cid)
      if (i < 0) return  // New chat was pressed meanwhile
      var t = Object.assign({}, win.turns[i])
      t.done = true
      if (ok) {
        t.sent = r.sent; t.answer = r.answer; t.citations = r.citations
        t.unknown = r.unknown_citations; t.proposals = r.proposals; t.rejected = r.rejected
      } else {
        t.answer = win.liveAnswer
        t.error = String(r)
      }
      var copy = win.turns.slice()
      copy[i] = t
      win.busy = false
      win.liveChat = ""
      win.turns = copy
      win.answersSeq++
      Qt.callLater(() => transcript.positionViewAtEnd())
    })
    Qt.callLater(() => transcript.positionViewAtEnd())
  }

  function indexOfChat(cid) {
    for (var i = 0; i < turns.length; i++) if (turns[i].chat === cid) return i
    return -1
  }

  // serve's {"event": "chat"} lines, routed here by the service.
  function onChatEvent(ev) {
    var i = indexOfChat(ev.chat)
    if (i < 0) return
    if (ev.kind === "sent") {
      var copy = turns.slice()
      copy[i] = Object.assign({}, turns[i], { sent: ev.sent })
      turns = copy
    } else if (ev.kind === "delta" && ev.chat === liveChat) {
      liveAnswer = ev.answer
      if (transcript.atYEnd) Qt.callLater(() => transcript.positionViewAtEnd())
    }
  }

  // serve went away: whatever was in flight is lost.
  function serveLost() {
    if (!busy) return
    var i = indexOfChat(liveChat)
    if (i >= 0) {
      var copy = turns.slice()
      copy[i] = Object.assign({}, turns[i], { done: true, answer: liveAnswer, error: "stickies serve restarted" })
      turns = copy
    }
    busy = false
    liveChat = ""
    loadingNotes = false
  }

  function stop() { if (busy && service) service.cancelChat(liveChat) }

  function setProp(key, state, msg) {
    var p = Object.assign({}, propState)
    p[key] = { state: state, msg: msg || "" }
    propState = p
  }

  function apply(ti, pi) {
    var key = ti + ":" + pi
    var st = propState[key]
    if (st && (st.state === "applying" || st.state === "applied")) return
    setProp(key, "applying")
    service.applyProposal(turns[ti].proposals[pi], function(ok, r) {
      if (!ok) { win.setProp(key, "error", String(r)); return }
      win.setProp(key, "applied", r.note ? "#" + r.note.id : (r.todo && r.todo.id ? "todo " + r.todo.id : ""))
    })
  }

  function dismiss(ti, pi) {
    var key = ti + ":" + pi
    var st = propState[key]
    if (st && (st.state === "applying" || st.state === "applied")) return
    setProp(key, "dismissed")
  }

  function jump(nid) {
    if (nid < 0 || !service) return
    close()
    service.goTo(nid, true)
  }

  component NoteChip: Rectangle {
    id: chip
    required property var note
    property bool dimmed: false
    width: Math.min(chipText.implicitWidth + Style.space(30), Style.space(300))
    height: Style.space(24)
    radius: Style.space(12)
    color: chipMouse.containsMouse ? Color.menu.selectedBackground : "transparent"
    border.width: 1
    border.color: Qt.rgba(Color.menu.text.r, Color.menu.text.g, Color.menu.text.b, 0.25)
    opacity: dimmed ? 0.5 : 1
    Rectangle {
      id: chipSwatch
      anchors { left: parent.left; leftMargin: Style.space(8); verticalCenter: parent.verticalCenter }
      width: Style.space(9); height: width; radius: Style.space(2)
      color: win.service ? win.service.fillFor(chip.note.color) : "#fff3a3"
      border.width: 1
      border.color: Qt.rgba(0, 0, 0, 0.25)
    }
    Text {
      id: chipText
      anchors { left: chipSwatch.right; leftMargin: Style.space(6); right: parent.right
                rightMargin: Style.space(8); verticalCenter: parent.verticalCenter }
      textFormat: Text.PlainText
      text: "#" + chip.note.id + "  " + (chip.note.title || Chat.title(chip.note.body, 40) || "(empty)")
      elide: Text.ElideRight
      color: Color.menu.text
      font.family: Style.font.family
      font.pixelSize: Style.font.caption
    }
    MouseArea {
      id: chipMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: win.jump(chip.note.id)
    }
  }

  component Caption: Text {
    textFormat: Text.PlainText
    color: win.muted
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    wrapMode: Text.Wrap
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

    Item {
      id: keyCatcher
      Keys.onEscapePressed: keeper.pressEscape()
    }
    width: Math.min(Style.space(760), win.width - Style.gapsOut * 4)
    height: Math.min(win.height * 0.8, column.implicitHeight + Style.spacing.panelPadding * 2)
    anchors.horizontalCenter: parent.horizontalCenter
    y: Math.round(win.height * 0.1)
    radius: Style.cornerRadius
    color: Color.menu.background
    border.color: Color.menu.border
    border.width: Math.max(1, Style.space(2))

    MouseArea { anchors.fill: parent }  // clicks on the card don't close it

    Column {
      id: column
      anchors { left: parent.left; right: parent.right; top: parent.top; margins: Style.spacing.panelPadding }
      spacing: Style.spacing.md

      // ------------------------------------------------ header
      Item {
        width: parent.width
        height: Math.max(heading.implicitHeight, headerButtons.implicitHeight)
        Text {
          id: heading
          anchors { left: parent.left; verticalCenter: parent.verticalCenter }
          textFormat: Text.PlainText
          text: "Ask your notes"
          color: Color.menu.text
          font.family: Style.font.family
          font.pixelSize: Style.font.title
          font.bold: true
        }
        Row {
          id: headerButtons
          anchors { right: parent.right; verticalCenter: parent.verticalCenter }
          spacing: Style.spacing.controlGap
          Button { text: "Stop"; visible: win.busy; onClicked: win.stop() }
          Button { text: "New chat"; visible: win.turns.length > 0 || win.draft !== null; onClicked: win.newChat() }
          Button { text: "Close"; onClicked: win.close() }
        }
      }

      // ------------------------------------------------ transcript
      ListView {
        id: transcript
        width: parent.width
        height: Math.min(contentHeight, win.height * 0.8 - Style.space(260) - draftBox.height)
        visible: win.turns.length > 0
        clip: true
        spacing: Style.space(14)
        boundsBehavior: Flickable.StopAtBounds
        model: win.turns

        delegate: Column {
          id: turn
          required property var modelData
          required property int index
          readonly property bool live: !modelData.done && modelData.chat === win.liveChat
          width: transcript.width
          spacing: Style.space(6)

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: turn.modelData.question
            wrapMode: Text.Wrap
            color: Color.menu.text
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            font.bold: true
          }

          Caption {
            width: parent.width
            text: turn.modelData.sent.length
                  ? "Sent " + turn.modelData.sent.length + (turn.modelData.sent.length === 1 ? " note" : " notes")
                    + " to " + (win.service ? win.service.agentName : "the agent") + ":"
                  : (turn.live ? "Sending…" : "Sent no notes, only the question.")
          }
          Flow {
            width: parent.width
            spacing: Style.space(6)
            visible: turn.modelData.sent.length > 0
            Repeater {
              model: turn.modelData.sent
              NoteChip {
                required property var modelData
                note: modelData
                dimmed: turn.modelData.done && turn.modelData.citations.indexOf(modelData.id) < 0
              }
            }
          }

          Text {
            width: parent.width
            textFormat: Text.StyledText
            text: Chat.answerHtml(turn.live ? win.liveAnswer : turn.modelData.answer,
                                  turn.modelData.sent.map(n => n.id), String(Color.menu.selectedText))
                  + (turn.live ? " …" : "")
            visible: text !== ""
            wrapMode: Text.Wrap
            color: Color.menu.text
            linkColor: Color.menu.selectedText
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            onLinkActivated: link => win.jump(Chat.noteFromLink(link))
            HoverHandler { cursorShape: parent.hoveredLink ? Qt.PointingHandCursor : Qt.ArrowCursor }
          }

          Caption {
            width: parent.width
            visible: turn.modelData.error !== ""
            text: "⚠ " + turn.modelData.error
            color: Color.urgent
          }
          Caption {
            width: parent.width
            visible: turn.modelData.unknown.length > 0
            text: "Cited notes that were not sent (ignored): "
                  + turn.modelData.unknown.map(i => "#" + i).join(" ")
          }

          // proposed actions: nothing happens until Apply
          Repeater {
            model: turn.modelData.proposals
            Rectangle {
              id: prop
              required property var modelData
              required property int index
              readonly property string key: turn.index + ":" + index
              readonly property var st: win.propState[key] || ({ state: "pending", msg: "" })
              width: turn.width
              height: propRow.implicitHeight + Style.space(16)
              radius: Style.cornerRadius
              color: "transparent"
              border.width: 1
              border.color: Qt.rgba(Color.menu.text.r, Color.menu.text.g, Color.menu.text.b, 0.3)
              Item {
                id: propRow
                anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter
                          leftMargin: Style.space(10); rightMargin: Style.space(8) }
                implicitHeight: Math.max(propText.implicitHeight, propButtons.implicitHeight)
                Column {
                  id: propText
                  anchors { left: parent.left; right: propButtons.left; rightMargin: Style.space(8)
                            verticalCenter: parent.verticalCenter }
                  Caption { text: "Proposed"; font.italic: true }
                  Text {
                    width: parent.width
                    textFormat: Text.PlainText
                    text: prop.modelData.summary
                    wrapMode: Text.Wrap
                    maximumLineCount: 3
                    elide: Text.ElideRight
                    color: Color.menu.text
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                  }
                  Caption {
                    width: parent.width
                    visible: prop.st.state === "error"
                    text: "⚠ " + prop.st.msg
                    color: Color.urgent
                  }
                }
                Row {
                  id: propButtons
                  anchors { right: parent.right; verticalCenter: parent.verticalCenter }
                  spacing: Style.spacing.controlGap
                  Caption {
                    anchors.verticalCenter: parent.verticalCenter
                    visible: prop.st.state === "applied" || prop.st.state === "dismissed" || prop.st.state === "applying"
                    wrapMode: Text.NoWrap
                    text: prop.st.state === "applied" ? "Applied " + prop.st.msg
                          : prop.st.state === "applying" ? "Applying…" : "Dismissed"
                  }
                  Button {
                    text: prop.st.state === "error" ? "Retry" : "Apply"
                    bordered: true
                    visible: prop.st.state === "pending" || prop.st.state === "error"
                    onClicked: win.apply(turn.index, prop.index)
                  }
                  Button {
                    text: "Dismiss"
                    visible: prop.st.state === "pending" || prop.st.state === "error"
                    onClicked: win.dismiss(turn.index, prop.index)
                  }
                }
              }
            }
          }
          Caption {
            width: parent.width
            visible: turn.modelData.rejected.length > 0
            text: turn.modelData.rejected.length + " proposed action"
                  + (turn.modelData.rejected.length === 1 ? " was" : "s were") + " invalid and ignored ("
                  + turn.modelData.rejected.map(r => r.error).join("; ") + ")"
          }
        }
      }

      // ------------------------------------------------ preview: what goes out
      Column {
        id: draftBox
        width: parent.width
        spacing: Style.space(4)
        visible: win.draft !== null
        height: visible ? implicitHeight : 0

        Caption {
          width: parent.width
          text: !win.draft ? "" : win.draft.notes.length
                ? "Only the question and the ticked notes go to " + (win.service ? win.service.agentName : "the agent")
                  + ". Untick any you don't want sent; Enter sends."
                : "No notes match; only the question will be sent. Enter sends."
        }
        Repeater {
          model: win.draft ? win.draft.notes : []
          Rectangle {
            id: drow
            required property var modelData
            readonly property bool ticked: !!(win.draft && win.draft.ticked[modelData.id])
            width: draftBox.width
            height: Math.max(dtext.implicitHeight, Style.space(20)) + Style.space(8)
            radius: Style.cornerRadius
            color: dmouse.containsMouse ? Color.menu.selectedBackground : "transparent"
            Rectangle {
              id: box
              anchors { left: parent.left; leftMargin: Style.space(6); verticalCenter: parent.verticalCenter }
              width: Style.space(14); height: width; radius: Style.space(3)
              color: drow.ticked ? Color.menu.selectedText : "transparent"
              border.width: 1
              border.color: drow.ticked ? Color.menu.selectedText : win.muted
              Text {
                anchors.centerIn: parent
                text: "✓"
                visible: drow.ticked
                color: Color.menu.background
                font.pixelSize: Style.font.caption
                font.bold: true
              }
            }
            Rectangle {
              id: dswatch
              anchors { left: box.right; leftMargin: Style.space(8); verticalCenter: parent.verticalCenter }
              width: Style.space(9); height: width; radius: Style.space(2)
              color: win.service ? win.service.fillFor(drow.modelData.color) : "#fff3a3"
              border.width: 1
              border.color: Qt.rgba(0, 0, 0, 0.25)
            }
            Text {
              id: dtext
              anchors { left: dswatch.right; leftMargin: Style.space(8); right: parent.right
                        rightMargin: Style.space(6); verticalCenter: parent.verticalCenter }
              textFormat: Text.PlainText
              text: "#" + drow.modelData.id + "  " + drow.modelData.body.replace(/\s+/g, " ").trim()
              elide: Text.ElideRight
              maximumLineCount: 2
              wrapMode: Text.Wrap
              color: Color.menu.text
              opacity: drow.ticked ? 1 : 0.5
              font.family: Style.font.family
              font.pixelSize: Style.font.body
            }
            MouseArea {
              id: dmouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: win.toggleTick(drow.modelData.id)
            }
          }
        }
        Row {
          spacing: Style.spacing.controlGap
          Button {
            text: "Send " + win.tickedCount + (win.tickedCount === 1 ? " note" : " notes")
            bordered: true
            onClicked: win.send()
          }
          Button { text: "Cancel"; onClicked: { win.draft = null; field.forceActiveFocus() } }
        }
      }

      // ------------------------------------------------ input
      TextField {
        id: field
        width: parent.width
        enabled: !win.busy
        placeholderText: win.busy ? "Answering…" : (win.turns.length ? "Ask a follow-up" : "Ask your notes, e.g. what did I write about the Cardmarket fees?")
        foreground: Color.menu.text
        onAccepted: win.submit()
        onTextChanged: if (win.draft && text.trim() !== win.draft.question) win.draft = null
        Keys.onEscapePressed: win.draft ? win.draft = null : keeper.pressEscape()
      }

      Caption {
        width: parent.width
        text: win.loadingNotes ? "Finding notes…"
              : win.busy ? "Esc closes; the answer keeps coming. Stop cancels it."
              : "Enter shows which notes would be sent · nothing is changed without Apply · this chat is kept until the shell restarts"
      }
    }
  }
}
