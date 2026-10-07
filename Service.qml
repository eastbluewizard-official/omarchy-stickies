import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Hyprland
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "waterfall.js" as W

// Desktop stickies. One `stickies serve` process is the only way in or out
// of the notes DB: this file keeps a ListModel mirror of the live notes,
// updated from the change events serve pushes (our own writes and anybody
// else's, e.g. an agent running `stickies add`). Each monitor gets a
// layer-shell surface on the Bottom layer -- above the wallpaper, below
// windows -- whose input region covers only the notes, so clicks anywhere
// else still reach the desktop. Front mode (SUPER+ALT+N, SUPER+ALT+SHIFT+N)
// moves those surfaces to the Top layer, above tiled windows, until Esc or
// a click outside the notes sends them back. In a waterfall layout the
// focused workspace's notes leave the desktop for one more surface: a
// column on the Top layer at the focused monitor's left or right edge.
Item {
  id: root

  // Dev/bench knobs, never set in a normal session (see docs/shell.md).
  readonly property string layerName: Quickshell.env("STICKIES_LAYER") || "bottom"
  readonly property bool exclusiveFocus: Quickshell.env("STICKIES_FOCUS") === "exclusive"

  property bool ready: false
  // The first `list` after (re)start has landed: the model is the DB.
  property bool listed: false
  property int requestSeq: 0
  property var callbacks: ({})
  property var desktops: []

  // The desktop's one toast, on one screen: { text, screen, undo, nid }.
  // undo: "" (just words), "archive" (restore nid), "tidy" or "reminder"
  // (its button shows nid).
  property var notice: null
  // A note the UI just created and wants the caret in.
  property int focusRequest: -1
  // A note the search overlay jumped to; its card flashes when it shows.
  property int flashRequest: -1
  // Search by meaning is installed (serve loads the model when it is used).
  property bool semantic: false
  // ... and the model is loaded right now (serve drops it when idle).
  property bool semanticLoaded: false
  // The model just loaded: open overlays ask again for by-meaning results.
  signal semanticReady()
  // Who chat talks to (Omarchy's default agent), from serve's chat_info.
  property string agentName: "claude"
  property string agentProblem: ""
  // First-run offer (Desktop's set-up toast): "" (nothing to show),
  // "offer" (the question) or "result" (what Set up did, shown briefly).
  property string setupState: ""
  property string setupText: ""
  // Lua that removes the runtime binds this plugin added (from serve's
  // keys_on); run when the plugin is unloaded.
  property string unbindLua: ""

  // Show/hide all stickies (bar panel, middle-click). Session-only.
  property bool hidden: false
  // Front mode: every surface on the Top layer, above windows, holding the
  // keyboard. On in newNote and SUPER+ALT+SHIFT+N; off on Esc, the key
  // again, a click outside the notes, or a workspace change.
  property bool front: false
  // ---------------------------------------------------------------- layout
  // Mirrored from serve's settings (the CLI is the only writer; every
  // surface follows its "settings" events, whoever changed them).
  property string layout: "free"
  property int colWidth: 320
  property bool colReserve: false
  property bool colCollapsed: false
  property var colOrder: []
  property var colFree: []
  readonly property var freeSet: {
    var o = {}
    for (var i = 0; i < colFree.length; i++) o[colFree[i]] = true
    return o
  }
  readonly property bool waterfall: layout !== "free"
  readonly property string colSide: W.sideOf(layout) || "right"
  // The column lives on the focused monitor (STICKIES_COLUMN_SCREEN pins it
  // to one output: a bench knob) and holds that monitor's workspace.
  readonly property string columnScreenPin: Quickshell.env("STICKIES_COLUMN_SCREEN") || ""
  readonly property string focusedScreen: columnScreenPin || (Hyprland.focusedMonitor ? Hyprland.focusedMonitor.name : "")
  readonly property var focusedHyprMonitor: {
    var mons = Hyprland.monitors.values
    for (var i = 0; i < mons.length; i++) if (mons[i].name === focusedScreen) return mons[i]
    return Hyprland.focusedMonitor
  }
  readonly property int focusedWs: focusedHyprMonitor && focusedHyprMonitor.activeWorkspace
                                   ? focusedHyprMonitor.activeWorkspace.id : -1
  // Notes gliding from the column back to their free spots (nid -> true);
  // the desktop draws them again once they land.
  property var leaving: ({})
  // Switching to a waterfall: the desktop keeps drawing the notes until the
  // column surface's first frame is up (no blank frame between the two).
  property bool entering: false
  // A column note has the caret: the focus grab keeps the keyboard there.
  property bool columnEditing: false
  // The column (Waterfall.qml), for the desktop's drop-to-dock.
  property var column: null

  // In the column: waterfall layout, not dragged out, and on the column
  // monitor's workspace (pinned notes and notes without a workspace are on all).
  function docked(nid, workspace, pinned) {
    return waterfall && dockable(nid, workspace, pinned)
  }
  // Would be in the column if the layout were a waterfall.
  function dockable(nid, workspace, pinned) {
    return !freeSet[nid] && (pinned || workspace < 0 || workspace === focusedWs)
  }

  function applySettings(st) {
    if (!st) return
    var wasWaterfall = waterfall
    // Glide first: everything below moves cards.
    if (column && (st.layout !== layout || st.waterfall_collapsed !== colCollapsed)) column.startGlide()
    if (wasWaterfall && st.layout === "free" && column) {
      var l = {}
      var ids = column.memberIds()
      for (var i = 0; i < ids.length; i++) l[ids[i]] = true
      leaving = l
      leaveTimer.restart()
    }
    if (!wasWaterfall && st.layout !== "free") entering = true
    layout = st.layout
    colWidth = st.waterfall_width
    colReserve = st.waterfall_reserve
    colCollapsed = st.waterfall_collapsed
    colOrder = st.waterfall_order
    colFree = st.waterfall_free
  }
  Timer {
    id: leaveTimer
    interval: 150   // the 130 ms glide home, then the desktop takes over
    onTriggered: root.leaving = ({})
  }

  function currentSettings() {
    return { layout: layout, waterfall_width: colWidth, waterfall_reserve: colReserve,
             waterfall_collapsed: colCollapsed, waterfall_order: colOrder, waterfall_free: colFree }
  }
  // Optimistic: apply here at once (the animation starts on the key press),
  // then store; serve's settings event confirms or corrects.
  function setSettings(values) {
    var st = currentSettings()
    for (var k in values) st[k] = values[k]
    applySettings(st)
    request("set", { values: values })
  }
  function setLayout(name) { if (W.LAYOUTS.indexOf(name) >= 0) setSettings({ layout: name }) }
  function cycleLayout() { hidden = false; setLayout(W.nextLayout(layout)) }
  function toggleCollapsed() { setSettings({ waterfall_collapsed: !colCollapsed }) }
  function setColumnOrder(order) { setSettings({ waterfall_order: order }) }
  // Dragged out of the column: free from now on, right where it was dropped.
  function undock(nid, fields) {
    colFree = colFree.filter(i => i !== nid).concat([nid])
    moveNote(nid, fields)
    request("dock", { id: nid, docked: false })
  }
  // Dropped on the column from the desktop: back in it.
  function dockNote(nid) {
    if (!freeSet[nid]) return
    colFree = colFree.filter(i => i !== nid)
    request("dock", { id: nid, docked: true })
  }

  // A short look at the notes above the windows without taking the
  // keyboard: a clipboard note just made, a workspace just tidied.
  property bool peeking: false
  // A reminder's toast is up: the notes stay above the windows (without
  // the keyboard) until it goes, so its text is seen.
  readonly property bool reminding: !!notice && notice.undo === "reminder"
  // Notes glide to their places while a tidy (or its undo) lands.
  property bool arranging: false
  // This session's last tidy can be undone (cleared by the undo or a drag).
  property bool tidyUndoable: false
  // Bumped on every model change; the bar panel's lists depend on it.
  property int revision: 0
  property int pinnedCount: 0
  // `omarchy-shell stickies search`: the bar widget on the focused monitor
  // opens its panel with the search field focused. (The hub card and
  // SUPER + ALT + J use `find`, the search overlay.)
  signal searchRequested()

  // ---------------------------------------------------------------- palette
  // Six named colours. A note stores the name, so "pink = money" keeps its
  // meaning; the active Omarchy theme decides how each name looks: the hue
  // tinted toward the theme's background (Color.background, which
  // omarchy-shell swaps live on a theme switch, so every card recolours
  // with it). Dark themes get deep tints of the hue, light ones pastels.
  readonly property var paletteNames: ["yellow", "pink", "blue", "green", "orange", "purple"]
  readonly property var hues: ({
    yellow: "#ffe45c", pink: "#ff8fb1", blue: "#7cc0ff",
    green: "#8fdc7c", orange: "#ffb35c", purple: "#b894ff", gray: "#c8c8c8"
  })
  readonly property bool dark: luminance(Color.background) < 0.45
  readonly property color ink: Qt.tint(dark ? "#ecebe6" : "#24221d",
                                       Qt.rgba(Color.foreground.r, Color.foreground.g, Color.foreground.b, 0.3))

  function luminance(c) {
    return 0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b
  }

  function fillFor(name) {
    var hue = hues[name] || (/^#[0-9a-fA-F]{6}$/.test(name) ? name : hues.yellow)
    var bg = Color.background
    return Qt.tint(hue, Qt.rgba(bg.r, bg.g, bg.b, dark ? 0.72 : 0.42))
  }

  // ---------------------------------------------------------------- toast
  function say(text, undo, nid, screenName) {
    var mon = Hyprland.focusedMonitor
    notice = { text: text, undo: undo || "", nid: nid === undefined ? -1 : nid,
               screen: screenName || (mon ? mon.name : "") }
  }
  // serve's error, as a sentence for the person.
  function sentence(err) {
    var s = String(err || "something went wrong").replace(/^stickies: /, "")
    return s.charAt(0).toUpperCase() + s.slice(1)
  }
  function notReady() {
    if (ready) return false
    say("Stickies is still starting; try again in a moment")
    return true
  }
  function undoNotice() {
    var n = notice
    notice = null
    if (!n) return
    if (n.undo === "archive") undoArchive(n.nid)
    else if (n.undo === "tidy") undoTidy()
    else if (n.undo === "reminder") showNote(n.nid)
  }
  // A reminder came due. The system notification only says which note
  // (the notification server keeps what it shows where others can read
  // it); the words show here, in the toast, above the windows.
  function showReminder(ev) {
    hidden = false
    say((ev.missed ? "Missed reminder: " : "Reminder: ") + (ev.text || "note #" + ev.id), "reminder", ev.id)
  }
  // A click on the reminder's toast (ours, or the system one through
  // `omarchy-shell stickies showNote <id>`): the note, raised and flashed.
  function showNote(nid) {
    if (indexOf(nid) < 0) return
    if (notice && notice.undo === "reminder" && notice.nid === nid) notice = null
    goTo(nid, true)
    peek()
  }

  function peek() {
    hidden = false
    peeking = true
    peekTimer.restart()
  }
  Timer {
    id: peekTimer
    interval: 1800
    onTriggered: root.peeking = false
  }
  function arrange() {
    arranging = true
    arrangeTimer.restart()
  }
  Timer {
    id: arrangeTimer
    interval: 400
    onTriggered: root.arranging = false
  }

  // ---------------------------------------------------------------- serve client
  // The command bundled with this plugin, by path: nothing needs to be on
  // PATH. STICKIES_CMD (tests, benches) replaces it.
  readonly property string command: decodeURIComponent(String(Qt.resolvedUrl("stickies")).replace(/^file:\/\//, ""))

  Process {
    id: serve
    command: ["sh", "-c", "if [ -n \"$STICKIES_CMD\" ]; then exec $STICKIES_CMD serve; else exec \"$0\" serve; fi",
              root.command]
    stdinEnabled: true
    running: true
    stdout: SplitParser {
      onRead: data => root.handleLine(data)
    }
    stderr: SplitParser {
      onRead: data => console.warn("stickies serve:", data)
    }
    onExited: (code, status) => {
      root.ready = false
      root.listed = false
      root.semantic = false
      root.semanticLoaded = false
      root.callbacks = ({})
      chatOverlay.serveLost()
      console.warn("stickies serve exited", code, "- restarting")
      restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    interval: 1500
    onTriggered: serve.running = true
  }

  function request(op, args, cb) {
    if (!serve.running) return
    var id = ++requestSeq
    if (cb) callbacks[id] = cb
    serve.write(JSON.stringify({ id: id, op: op, args: args || {} }) + "\n")
  }

  function handleLine(line) {
    var msg
    try { msg = JSON.parse(line) } catch (e) { console.warn("stickies: bad line", line); return }
    if (msg.event === "ready") {
      ready = true
      request("settings", {}, function(ok, st) { if (ok) applySettings(st) })
      // Notes left empty (a session that ended before they lost focus, or
      // the ones made before empty notes were discarded) go, once per start.
      request("clean", { older_than: 10 }, function() {
        request("list", {}, function(ok, notes) { if (ok) resetNotes(notes) })
      })
      request("chat_info", {}, function(ok, r) {
        if (!ok) return
        root.agentName = r.agent || "the agent"
        root.agentProblem = r.error || ""
      })
      request("integration", {}, function(ok, r) {
        if (!ok) return
        if (!r.asked) root.setupState = "offer"
        else if (r.keys === "runtime") root.bindKeys()
      })
    } else if (msg.event === "semantic") {
      // At start (installed or not), then each time serve loads the model
      // (~0.7 s, off the request loop) or drops it after its idle time.
      semantic = !!msg.on
      var loaded = semantic && (msg.loaded === undefined || !!msg.loaded)
      var arrived = loaded && !semanticLoaded
      semanticLoaded = loaded
      if (arrived) semanticReady()
    } else if (msg.event === "changed") {
      applyChange(msg)
    } else if (msg.event === "settings") {
      applySettings(msg.settings)
    } else if (msg.event === "chat") {
      chatOverlay.onChatEvent(msg)
    } else if (msg.event === "reminder") {
      showReminder(msg)
    } else if (msg.id !== undefined) {
      var cb = callbacks[msg.id]
      delete callbacks[msg.id]
      if (!msg.ok) console.warn("stickies:", msg.error)
      if (cb) cb(msg.ok, msg.ok ? msg.result : msg.error)
    }
  }

  // ---------------------------------------------------------------- model
  // Roles are fixed-type (ListModel infers types from the first row), so
  // null workspace/monitor become -1 / "". x/y/z would shadow Item
  // properties in the delegate, hence x0/y0/z0.
  ListModel { id: notesModel }
  readonly property alias notes: notesModel

  function toRow(n) {
    return {
      nid: n.id, body: n.body || "", color: n.color || "yellow", pinned: !!n.pinned,
      workspace: n.workspace === null || n.workspace === undefined ? -1 : n.workspace,
      monitor: n.monitor || "", x0: n.x, y0: n.y, w: n.w, h: n.h, z0: n.z,
      updated: n.updated_at || "", rolled: !!n.rolled, remindAt: n.remind_at || "",
      tags: (n.tags || []).join(" ")
    }
  }

  function indexOf(nid) {
    for (var i = 0; i < notesModel.count; i++) if (notesModel.get(i).nid === nid) return i
    return -1
  }

  function recountPinned() {
    var n = 0
    for (var i = 0; i < notesModel.count; i++) if (notesModel.get(i).pinned) n++
    pinnedCount = n
  }

  function resetNotes(list) {
    notesModel.clear()
    notesModel.append(list.map(toRow))
    recountPinned()
    listed = true
    revision++
  }

  function upsert(n) {
    var row = toRow(n)
    var i = indexOf(row.nid)
    if (i < 0) {
      notesModel.append(row)
      if (row.pinned) pinnedCount++
      revision++
      return
    }
    var cur = notesModel.get(i)
    var changed = false
    for (var k in row) if (cur[k] !== row[k]) {
      if (k === "pinned") pinnedCount += row.pinned ? 1 : -1
      notesModel.setProperty(i, k, row[k])
      changed = true
    }
    if (changed) revision++
  }

  function removeNote(nid) {
    var i = indexOf(nid)
    if (i < 0) return
    if (notesModel.get(i).pinned) pinnedCount--
    notesModel.remove(i)
    revision++
  }

  function applyChange(ev) {
    if (ev.kind === "archive" || ev.kind === "purge" || !ev.note || ev.note.archived_at) removeNote(ev.id)
    else upsert(ev.note)
  }

  // Optimistic local update (drag end, colour, pin) before serve echoes it.
  readonly property var roleFor: ({ x: "x0", y: "y0", z: "z0" })
  function setLocal(nid, fields) {
    var i = indexOf(nid)
    if (i < 0) return
    if ("pinned" in fields && fields.pinned !== notesModel.get(i).pinned) pinnedCount += fields.pinned ? 1 : -1
    for (var k in fields) notesModel.setProperty(i, roleFor[k] || k, fields[k])
    revision++
  }

  function topZ() {
    var z = 0
    for (var i = 0; i < notesModel.count; i++) z = Math.max(z, notesModel.get(i).z0)
    return z
  }

  // ---------------------------------------------------------------- actions
  // body empty -> the caret goes into the new note; otherwise (quick
  // capture) it is just placed.
  function newNote(screenName, workspaceId, x, y, body) {
    hidden = false
    if (notReady()) return
    // In a waterfall layout the new note lands in the column (which takes
    // the keyboard when the caret goes in); free notes need front mode.
    if (!body && !waterfall) enterFront()
    if (waterfall && column) column.startGlide()
    request("add", { body: body || "", monitor: screenName || null,
                     workspace: workspaceId > 0 ? workspaceId : null, x: x, y: y },
            function(ok, n) { if (ok) { upsert(n); if (!body) root.focusRequest = n.id } })
  }

  // The screen Hyprland has focused (the overlays open there).
  function screenWithFocus() {
    var mon = Hyprland.focusedMonitor
    var screens = Quickshell.screens
    for (var i = 0; i < screens.length; i++) if (mon && screens[i].name === mon.name) return screens[i]
    return screens.length ? screens[0] : null
  }

  function desktopFor(screenName) {
    for (var i = 0; i < desktops.length; i++) if (desktops[i].screenName === screenName) return desktops[i]
    return desktops.length ? desktops[0] : null
  }

  // A note on the focused monitor's current workspace, near the middle of
  // the screen, cascading so repeated presses don't stack exactly.
  function placeHere() {
    var mon = Hyprland.focusedMonitor
    var name = mon ? mon.name : ""
    var d = desktopFor(name)
    var step = (notesModel.count % 6) * 28
    return { name: name, ws: mon && mon.activeWorkspace ? mon.activeWorkspace.id : -1,
             x: d ? Math.max(16, Math.round(d.width / 2 - 120) + step) : null,
             y: d ? Math.max(16, Math.round(d.height / 3 - 100) + step) : null }
  }
  function newNoteHere(body) {
    var p = placeHere()
    newNote(p.name, p.ws, p.x, p.y, body)
  }

  // SUPER + ALT + V: the clipboard's text as a note (serve runs wl-paste:
  // text only, trimmed, at most 10,000 characters), flashed during a short
  // look above the windows. An image or an empty clipboard says so.
  function pasteHere() {
    if (notReady()) return
    var p = placeHere()
    request("paste", { monitor: p.name || null, workspace: p.ws > 0 ? p.ws : null, x: p.x, y: p.y },
            function(ok, r) {
      if (!ok) { root.say(root.sentence(r)); return }
      root.upsert(r.note)
      root.peek()
      root.flashRequest = -1
      root.flashRequest = r.note.id
      if (r.truncated) root.say("Clipboard note cut to its first 10,000 characters")
    })
  }

  // Tidy: this workspace's unpinned notes in rows, Hyprland's gaps_out
  // apart (serve asks hyprctl for the gaps and the bar's space). Undo once.
  function tidyHere() {
    if (notReady()) return
    // The waterfall column owns its notes' places; tidy is the free layout's.
    if (waterfall) { say("Tidy is for the free layout; the column already lines the notes up"); return }
    var mon = Hyprland.focusedMonitor
    var d = desktopFor(mon ? mon.name : "")
    if (!d) return
    request("tidy", { workspace: d.workspaceId, monitor: d.screenName, width: Math.round(d.width),
                      height: Math.round(d.height), primary: d.primary }, function(ok, r) {
      if (!ok) { root.say(root.sentence(r)); return }
      if (!r.moved.length) { root.say("No loose notes to tidy on this workspace"); return }
      root.arrange()
      r.moved.forEach(n => root.upsert(n))
      root.tidyUndoable = true
      root.peek()
      root.say("Tidied " + r.moved.length + (r.moved.length === 1 ? " note" : " notes"), "tidy", -1, d.screenName)
    })
  }
  function undoTidy() {
    request("tidy_undo", {}, function(ok, r) {
      root.tidyUndoable = false
      if (!ok) { root.say(root.sentence(r)); return }
      root.arrange()
      r.restored.forEach(n => root.upsert(n))
      root.peek()
    })
  }

  // Roll-up (double-click a header): the note shrinks to its header line.
  function toggleRoll(nid) {
    var i = indexOf(nid)
    if (i < 0) return
    var rolled = !notesModel.get(i).rolled
    setLocal(nid, { rolled: rolled })
    request("roll", { id: nid, rolled: rolled })
  }

  // Bring a note into view: its workspace, raised, then the caret in it
  // (bar panel) or, with flash (search overlay), a short highlight.
  function goTo(nid, flash) {
    var i = indexOf(nid)
    if (i < 0) return
    var r = notesModel.get(i)
    hidden = false
    var mon = Hyprland.focusedMonitor
    var here = mon && mon.activeWorkspace ? mon.activeWorkspace.id : -1
    // Hyprland's dispatchers are Lua here (same form omarchy.bar's
    // workspace buttons send through hyprctl).
    if (!r.pinned && r.workspace > 0 && r.workspace !== here)
      Hyprland.dispatch("hl.dsp.focus({ workspace = \"" + r.workspace + "\" })")
    raise(nid)
    if (flash) {
      flashRequest = -1
      flashRequest = nid
    } else {
      focusRequest = -1
      focusRequest = nid
    }
  }

  function search(query, cb) {
    request("search", { query: query, limit: 20 }, function(ok, hits) { cb(ok ? hits : []) })
  }

  // ---------------------------------------------------------------- chat
  // The question's top notes (hybrid search, live and non-empty only).
  function chatNotes(question, cb) {
    request("chat_notes", { question: question, k: 6 }, function(ok, notes) { cb(ok, ok ? notes : []) })
  }
  // One turn: exactly these note ids go out. serve streams {"event":
  // "chat"} lines (routed to the overlay) and answers when it is done.
  function ask(chatId, question, ids, history, cb) {
    request("ask", { chat: chatId, question: question, ids: ids, history: history }, cb)
  }
  function cancelChat(chatId) {
    request("chat_cancel", { chat: chatId })
  }
  // The person clicked Apply; serve validates again and applies.
  function applyProposal(proposal, cb) {
    request("apply", { proposal: proposal }, cb)
  }

  // Bar panel: every pinned note, then the `recent` most recently updated
  // others. serve sorts (pinned, updated_at) off an index; walking the
  // ListModel here cost ~70 ms per pass at 2,000 notes.
  function pinnedAndRecent(recent, cb) {
    request("list", { limit: pinnedCount + recent }, function(ok, notes) {
      if (!ok) return
      var rows = notes.map(n => ({ nid: n.id, body: n.body, color: n.color, pinned: n.pinned,
                                   workspace: n.workspace === null ? -1 : n.workspace }))
      cb(rows.filter(r => r.pinned), rows.filter(r => !r.pinned).slice(0, recent))
    })
  }

  // ---------------------------------------------------------------- tags
  // Every tag in use with its count, most used first (the tag editor's
  // autocomplete); loaded when an editor opens.
  property var allTags: []
  function loadTags() {
    request("tags", {}, function(ok, tags) { if (ok) root.allTags = tags })
  }
  function setTags(nid, tags) {
    setLocal(nid, { tags: tags.join(" ") })
    request("set_tags", { id: nid, tags: tags }, function(ok, r) {
      if (!ok) { root.say(root.sentence(r)); return }
      root.upsert(r)
      root.loadTags()
    })
  }

  function editBody(nid, body) {
    setLocal(nid, { body: body })
    request("edit", { id: nid, body: body })
  }

  function moveNote(nid, fields) {
    tidyUndoable = false
    setLocal(nid, fields)
    var args = { id: nid }
    for (var k in fields) args[k] = fields[k]
    request("move", args)
  }

  function raise(nid) {
    var i = indexOf(nid)
    if (i < 0) return
    var top = topZ()
    var z = notesModel.get(i).z0
    var shared = 0
    for (var j = 0; j < notesModel.count; j++) if (notesModel.get(j).z0 === top) shared++
    if (z === top && shared === 1) return
    setLocal(nid, { z: top + 1 })
    request("move", { id: nid, raise: true })
  }

  function setColor(nid, name) {
    setLocal(nid, { color: name })
    request("color", { id: nid, color: name })
  }

  function togglePin(nid) {
    var i = indexOf(nid)
    if (i < 0) return
    var pinned = !notesModel.get(i).pinned
    setLocal(nid, { pinned: pinned })
    request("pin", { id: nid, pinned: pinned })
  }

  // A note that lost focus with no text in it: serve deletes it if it is
  // still empty there too (an edit from elsewhere wins).
  function discardIfEmpty(nid) {
    request("discard", { id: nid })
  }

  // ---------------------------------------------------------------- keys
  // Runtime binds (serve runs `hyprctl eval`); a Hyprland config reload
  // drops them, so they are added again on every reload.
  function bindKeys(cb) {
    request("keys_on", {}, function(ok, r) {
      if (ok && r.unbind_lua) root.unbindLua = r.unbind_lua
      if (cb) cb(ok, r)
    })
  }
  Connections {
    target: Hyprland
    function onRawEvent(event) {
      if (event.name === "configreloaded" && root.ready && root.unbindLua !== "") root.bindKeys()
    }
  }
  Component.onDestruction: {
    // Plugin disabled or removed: take its keys with it. (A reload of the
    // plugin adds them again when the new instance starts.)
    if (unbindLua !== "") Quickshell.execDetached(["hyprctl", "eval", unbindLua])
  }

  function answerSetup(yes) {
    if (!yes) {
      setupState = ""
      request("integrate", { yes: false })
      return
    }
    request("integrate", { yes: true }, function(ok, r) {
      if (!ok) {
        setupText = "Set up failed: " + r
      } else {
        var res = r.result || {}
        if (res.unbind_lua) root.unbindLua = res.unbind_lua
        var lines = []
        if (res.keys === "unavailable") lines.push("No Hyprland session found: the keys were not added.")
        else lines.push("Keys ready: SUPER + ALT + N new note, J find, O all notes, A ask, V from the clipboard, L layout.")
        for (var i = 0; i < (res.taken || []).length; i++)
          lines.push(res.taken[i].keys + " is already used (" + res.taken[i].by + "); left as it is.")
        lines.push(r.link.state === "ours" ? "Command: " + r.link.path
                   : "Not added: " + r.link.path + " already exists.")
        setupText = lines.join("<br>")
      }
      setupState = "result"
      setupDone.restart()
    })
  }
  Timer {
    id: setupDone
    interval: 8000
    onTriggered: root.setupState = ""
  }

  // ---------------------------------------------------------------- front mode
  // Keyboard: a plain on-demand layer surface loses the keyboard to the
  // window under the pointer the moment the pointer moves (follow_mouse),
  // and an exclusive one never lets a click on a window take it back. A
  // Hyprland focus grab over the stickies surfaces does both halves: the
  // keyboard stays on the notes, and a click anywhere outside them clears
  // the grab, which ends front mode.
  // The same grab holds the keyboard on the waterfall column while one of
  // its notes has the caret (columnEditing).
  HyprlandFocusGrab {
    id: frontFocus
    active: root.front || (root.columnEditing && columnPanel.visible)
    // The focused monitor's surface last: the grab hands it the keyboard
    // (the column when a column note is being edited).
    windows: {
      var mon = Hyprland.focusedMonitor
      var here = mon ? mon.name : ""
      var ds = !root.front ? [] : root.desktops.filter(d => d.screenName !== here).concat(root.desktops.filter(d => d.screenName === here))
      var ws = ds.map(d => d.window)
      return columnPanel.visible ? (root.columnEditing ? ws.concat([columnPanel]) : [columnPanel].concat(ws)) : ws
    }
    onCleared: root.leaveFront()
  }

  function enterFront() {
    hidden = false
    front = true
  }
  // release: we still hold the keyboard (Esc, the key): hand it back to the
  // windows instead of keeping it on a surface that is now below them all.
  function leaveFront(release) {
    if (!front && !columnEditing) return
    front = false
    columnEditing = false
    if (release) {
      keyboardReleased = true
      releaseTimer.restart()
    }
  }
  function toggleFront() { if (front) leaveFront(true); else enterFront() }
  property bool keyboardReleased: false
  Timer {
    id: releaseTimer
    interval: 150
    onTriggered: root.keyboardReleased = false
  }
  Connections {
    target: Hyprland
    function onFocusedWorkspaceChanged() { root.leaveFront(true) }
  }

  function archive(nid, screenName) {
    removeNote(nid)
    say("Archived", "archive", nid, screenName)
    request("rm", { id: nid }, function(ok, err) { if (!ok) root.say(root.sentence(err)) })
  }

  function undoArchive(nid) {
    request("restore", { id: nid }, function(ok, n) {
      if (ok) root.upsert(n)
      else root.say(root.sentence(n))
    })
  }

  // ---------------------------------------------------------------- search overlay
  SearchOverlay {
    id: searchOverlay
    service: root
  }
  readonly property alias searchOverlay: searchOverlay

  ChatOverlay {
    id: chatOverlay
    service: root
  }
  readonly property alias chatOverlay: chatOverlay

  // All notes (SUPER + ALT + O, `stickies list --open`, the bar panel, a
  // click on a note's tag): every live note, filtered by tags and text.
  List {
    id: listOverlay
    service: root
  }
  readonly property alias listOverlay: listOverlay
  // tags: the filter to start with ([] for all).
  function openList(tags) {
    hidden = false
    listOverlay.open(tags || [])
  }

  // `omarchy-shell stickies <method>`; the keybindings (hypr/stickies.lua,
  // falling back to `stickies shell <method>`, which logs failures) call
  // newNote, front, paste, find and chat; the hub card calls find.
  IpcHandler {
    target: "stickies"
    function newNote(): void { root.newNoteHere("") }
    function paste(): void { root.pasteHere() }
    function tidy(): void { root.tidyHere() }
    function front(): void { root.toggleFront() }
    function toggle(): void { root.hidden = !root.hidden; if (root.hidden) root.leaveFront() }
    function show(): void { root.hidden = false }
    function hide(): void { root.hidden = true; root.leaveFront() }
    function search(): void { root.searchRequested() }
    // A click on a reminder's system notification (its omarchy-exec-argv).
    function showNote(id: int): void { root.showNote(id) }
    function find(): void { searchOverlay.toggle() }
    // SUPER + ALT + O (toggles), and `stickies list --open [--tag X]`
    // (listTag: tags separated by spaces or commas).
    function list(): void { listOverlay.toggle([]) }
    function listTag(tags: string): void { root.openList(tags.split(/[\s,]+/).filter(t => t !== "")) }
    function chat(): void { chatOverlay.toggle() }
    // SUPER + ALT + L / SUPER + ALT + W, and `layout` for scripts.
    function cycleLayout(): void { root.cycleLayout() }
    function collapse(): void { root.toggleCollapsed() }
    function layout(name: string): void { root.setLayout(name) }
    function reload(): void {
      root.request("list", {}, function(ok, notes) { if (ok) root.resetNotes(notes) })
    }
  }

  // ---------------------------------------------------------------- waterfall
  // The column: one surface on the focused monitor (it moves when focus
  // does), on the Top layer so it floats above tiled windows on every
  // workspace. Full-screen so notes can glide between their free spots and
  // the column, but the input region is only the column (or the collapsed
  // strip) and its handle; everything else clicks through. It never pushes
  // windows aside (Ignore); `reserve` adds the spacer below for that.
  readonly property var columnScreen: {
    var ss = Quickshell.screens
    for (var i = 0; i < ss.length; i++) if (ss[i].name === focusedScreen) return ss[i]
    return ss.length ? ss[0] : null
  }
  readonly property bool leavingAny: Object.keys(leaving).length > 0
  readonly property alias columnWindow: columnPanel

  PanelWindow {
    id: columnPanel
    screen: root.columnScreen
    visible: (root.waterfall || root.leavingAny) && !root.hidden && !!root.columnScreen && !columnRemap.remapping
    color: "transparent"
    anchors { top: true; bottom: true; left: true; right: true }
    exclusionMode: ExclusionMode.Ignore

    ScreenMoveRemap {
      id: columnRemap
      window: columnPanel
    }

    WlrLayershell.namespace: "eastbluewizard-stickies-column"
    WlrLayershell.layer: root.layerName === "overlay" ? WlrLayer.Overlay : WlrLayer.Top
    WlrLayershell.keyboardFocus: root.exclusiveFocus ? WlrKeyboardFocus.Exclusive
                                 : root.keyboardReleased ? WlrKeyboardFocus.None : WlrKeyboardFocus.OnDemand

    mask: Region { regions: waterfallItem.regions }

    Waterfall {
      id: waterfallItem
      anchors.fill: parent
      service: root
      window: columnPanel
      screenName: root.columnScreen ? root.columnScreen.name : ""
      primary: Quickshell.screens.length > 0 && Quickshell.screens[0].name === screenName
      knownScreens: Quickshell.screens.map(s => s.name)
      reservedTop: root.reservedFor(screenName)[1]
      reservedBottom: root.reservedFor(screenName)[3]
      Component.onCompleted: root.column = waterfallItem
    }
  }

  // Space other layers reserve on a monitor ([left, top, right, bottom],
  // e.g. the bar's 26 px on top), so the column starts below the bar.
  function reservedFor(name) {
    var mons = Hyprland.monitors.values
    for (var i = 0; i < mons.length; i++) {
      var o = mons[i].lastIpcObject
      if (mons[i].name === name && o && o.reserved) return o.reserved
    }
    return [0, 0, 0, 0]
  }

  // `reserve` on: an invisible strip on the column's edge whose exclusive
  // zone pushes tiled windows aside, like a bar. No input, nothing drawn.
  PanelWindow {
    id: reservePanel
    screen: root.columnScreen
    visible: root.waterfall && root.colReserve && !root.hidden && !!root.columnScreen
    color: "transparent"
    anchors { top: true; bottom: true; left: root.colSide === "left"; right: root.colSide === "right" }
    implicitWidth: 1
    exclusionMode: ExclusionMode.Normal
    exclusiveZone: waterfallItem.geo.reserve
    mask: Region {}
    WlrLayershell.namespace: "eastbluewizard-stickies-reserve"
    WlrLayershell.layer: WlrLayer.Bottom
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
  }

  // ---------------------------------------------------------------- surfaces
  // One per screen Quickshell knows, including rotated ones; Variants adds
  // and drops surfaces as monitors come and go, and ScreenMoveRemap remaps
  // a surface whose monitor moved in the layout (Hyprland would otherwise
  // leave it at the old position, e.g. off-screen after a hotplug).
  Variants {
    model: Quickshell.screens

    PanelWindow {
      id: panel
      required property var modelData
      screen: modelData
      visible: !root.hidden && !remapGuard.remapping
      color: "transparent"
      anchors { top: true; bottom: true; left: true; right: true }
      exclusionMode: ExclusionMode.Ignore

      ScreenMoveRemap {
        id: remapGuard
        window: panel
      }

      WlrLayershell.namespace: "eastbluewizard-stickies"
      WlrLayershell.layer: root.front || root.peeking || root.reminding ? WlrLayer.Top
                           : root.layerName === "overlay" ? WlrLayer.Overlay : WlrLayer.Bottom
      WlrLayershell.keyboardFocus: root.exclusiveFocus ? WlrKeyboardFocus.Exclusive
                                   : root.keyboardReleased ? WlrKeyboardFocus.None : WlrKeyboardFocus.OnDemand

      // Input only where something is drawn: every visible note, the
      // new-note button and (while shown) the undo toast.
      mask: Region { regions: desktop.regions }

      Desktop {
        id: desktop
        anchors.fill: parent
        service: root
        screenName: panel.modelData.name
        primary: Quickshell.screens.length > 0 && Quickshell.screens[0].name === panel.modelData.name
        knownScreens: Quickshell.screens.map(s => s.name)
        workspaceId: {
          var mon = Hyprland.monitorFor(panel.modelData)
          return mon && mon.activeWorkspace ? mon.activeWorkspace.id : -1
        }
        window: panel
        Component.onCompleted: root.desktops = root.desktops.concat([desktop])
        Component.onDestruction: root.desktops = root.desktops.filter(d => d !== desktop)
      }
    }
  }
}
