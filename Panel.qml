import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "keasbeexd.mousectrl"
  ipcTarget: "keasbeexd.mousectrl"
  manageIpc: false

  property int cursorIndex: 0
  property bool cursorActive: false

  // Every color in this panel is one of these, and every one of these comes
  // from the active Omarchy theme -- nothing below names a color of its own.
  // The only literal colors in the tree are the LED presets in Model.js,
  // which are values written to the mouse's LED, not panel styling.
  //
  // Secondary text is the foreground at reduced alpha rather than
  // Qt.darker(foreground): on a light theme the foreground is already dark,
  // and darkening it makes "dim" text *more* prominent, not less.
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color accent: Commons.Color.accent
  readonly property color dim: root.withAlpha(foreground, 0.6)
  readonly property color faint: root.withAlpha(foreground, 0.18)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color hoverFill: bar ? Style.hoverFillFor(bar.foreground, accent) : "transparent"
  readonly property color selectedFill: bar ? Style.selectedFillFor(bar.foreground, accent) : "transparent"

  // Qt.lighter(c, 1.0) is an identity that also turns a color *string* into
  // a color value, so this works whichever form the host hands over.
  function withAlpha(c, a) {
    var q = Qt.lighter(c, 1.0)
    return Qt.rgba(q.r, q.g, q.b, a)
  }

  readonly property var rows: hsk.rows
  readonly property bool needsSetup: hsk.state === "undiscovered"
  readonly property bool hasError: hsk.state === "error"

  // Missing permissions is by far the most common first-run failure, and the
  // rawest form of it -- EACCES opening /dev/hidraw* -- is unreadable to
  // anyone who has not just read the udev docs. Every exchange needs the node
  // opened read-write, so this is not a degraded mode: nothing works at all,
  // including the battery. Say so, and say what to run.
  readonly property bool looksLikePermissions: root.hasError
    && Model.isPermissionError(hsk.lastError)

  // The mouse glyph stays filled at every state -- an outlined mouse when the
  // reading is stale reads as a different device, not as "waiting". Low battery
  // still shouts (urgent), a stale reading still dims, and everything else is
  // the bar's own foreground.
  readonly property color barIconColor: hsk.lowBattery
    ? root.urgent
    : (hsk.ready ? barForeground : root.withAlpha(barForeground, 0.6))

  // When the mouse is plugged in, the percentage recolors to the theme's
  // accent so "charging" is visible at a glance -- the accent tracks whatever
  // omarchy theme is active, so it always sits with the rest of the bar.
  readonly property color barLabelColor: hsk.lowBattery
    ? root.urgent
    : (hsk.ready && hsk.value("charging") === true
       ? root.accent
       : (hsk.ready ? barForeground : root.withAlpha(barForeground, 0.6)))

  // The battery percentage drawn beside the glyph -- see the BarIconButton
  // below for why the label has to live inside the icon. Blank in a vertical
  // bar, which is one icon wide and has nowhere to put it.
  readonly property string barLabelText: (bar && bar.vertical) ? "" : hsk.barText

  // The slot is a fixed width, so it has to be told how much text it is about
  // to hold or the label renders outside the button and overlaps its neighbour.
  // Both metrics feed the slot width below -- sizing to actual content instead
  // of the full icon slot removes the padding that made the widget look loose
  // against its neighbours.
  TextMetrics {
    id: barLabelMetrics
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
    text: root.barLabelText
  }

  TextMetrics {
    id: barIconMetrics
    font.family: root.fontFamily
    font.pixelSize: Style.bar.iconFont
    text: "󰍽"
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // NOT `id: mouse`. MouseArea.onClicked carries an implicit `mouse`
  // parameter (the MouseEvent), which silently shadows an id of that name --
  // so `mouse.toggle(...)` inside a click handler resolved to the event, not
  // the service, and did nothing. Handlers for parameterless signals
  // (Toggle.clicked, PanelActionButton.clicked) were unaffected, which is why
  // the toggles worked and this one did not.
  Service {
    id: hsk
    settings: root.settings
  }

  // --- cursor -------------------------------------------------------------
  //
  // The cursor is an index into `rows` -- the flat, data-built list of
  // writable rows the connected mouse's profile lays out. Nothing here knows
  // a field name either: rows carry {field, widget}, and adjustment below is
  // per-widget behavior, not per-field names.

  // Reference to each row's rendered item, by field, for scroll-into-view.
  // Loaders register on load and clear on destruction; a section change can
  // leave a stale entry briefly, so scroll never trusts `visible`.
  property var rowItems: ({})

  function clampCursor() {
    if (rows.length === 0) {
      cursorIndex = 0
      return
    }
    cursorIndex = Math.max(0, Math.min(cursorIndex, rows.length - 1))
  }

  function currentRow() {
    if (cursorIndex < 0 || cursorIndex >= rows.length) return null
    return rows[cursorIndex]
  }

  function isRowCursor(field) {
    var row = currentRow()
    return root.cursorActive && row && row.field === field
  }

  function moveCursor(dx, dy) {
    cursorActive = true
    if (dy !== 0) {
      cursorIndex = cursorIndex + (dy > 0 ? 1 : -1)
      clampCursor()
      scrollCursorIntoView()
      return
    }
    if (dx !== 0) adjustCurrent(dx > 0 ? 1 : -1)
  }

  // Left/right nudges the value of whatever row the cursor is on, so any
  // setting can be changed without reaching for the mouse that is being
  // reconfigured. How a row nudges is decided by its widget, not its name.
  function adjustCurrent(direction) {
    var row = currentRow()
    if (!row) return
    var field = row.field
    if (row.widget === "toggle") {
      hsk.toggle(field)
    } else if (row.widget === "chips") {
      var options = Model.chipsOptions(field, hsk.allowed, hsk.unitFor(field))
      if (options.length === 0) return
      var current = String(hsk.value(field))
      var index = -1
      for (var i = 0; i < options.length; i++) {
        if (options[i].value === current) { index = i; break }
      }
      if (index < 0) index = 0
      var next = Math.max(0, Math.min(options.length - 1, index + direction))
      var nextValue = options[next].value
      if (nextValue === current) return
      // The CLI coerces on its side too; numbers travel as numbers so a
      // polling register is never asked for the string "4000".
      var numeric = parseInt(nextValue, 10)
      hsk.set(field, (!isNaN(numeric) && String(numeric) === nextValue) ? numeric : nextValue)
    } else if (row.widget === "slider") {
      var bounds = Model.sliderBounds(field, hsk.allowed, hsk.metaFor(field))
      if (!bounds) return
      var at = hsk.value(field)
      if (typeof at !== "number" || !isFinite(at)) return
      // One step per press, hold-to-repeat walks smoothly. Coalesced, like a
      // drag -- holding an arrow key is one write, not one per frame.
      var wanted = Model.clampStep(at + direction * bounds.step, bounds)
      if (wanted !== at) hsk.setSoon(field, wanted)
    } else if (row.widget === "color") {
      // The current swatch cycles the LED palette -- reaching the next color
      // is one keystroke, like one click on it is.
      var swatch = hsk.value(field) === undefined || hsk.value(field) === null ? "" : String(hsk.value(field))
      if (Model.isValidHexColor(swatch) && swatch !== "") hsk.set(field, Model.nextStageColor(swatch))
      else hsk.set(field, Model.STAGE_COLORS[0])
    }
  }

  function activateCursor() {
    // Enter on a non-toggle nudges the value one step in the natural
    // direction; on a toggle it flips it. Same primitive as the arrow keys,
    // by design -- the cursor is never idle.
    var row = currentRow()
    if (row) adjustCurrent(1)
  }

  function scrollItemIntoView(item) {
    if (!panelFlick || !item || !item.visible) return
    Qt.callLater(function() {
      if (!item) return
      var margin = Style.space(6)
      var point = item.mapToItem(panelFlick.contentItem, 0, 0)
      var top = point.y
      var bottom = top + item.height
      var viewTop = panelFlick.contentY
      var viewBottom = viewTop + panelFlick.height
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (top < viewTop + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > viewBottom - margin) panelFlick.contentY = Math.min(maxY, bottom + margin - panelFlick.height)
    })
  }

  function scrollCursorIntoView() {
    var row = currentRow()
    if (!row) return
    scrollItemIntoView(root.rowItems[row.field])
  }

  function setCursor(index) {
    cursorActive = true
    cursorIndex = index
    clampCursor()
  }

  function rowIndexOf(field) {
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].field === field) return i
    }
    return -1
  }

  // Opening the panel used to force a refresh too, on top of startup, the
  // link watcher and the explicit triggers below (this button, middle-click,
  // 'r'). Read/write is otherwise more than enough to keep `values` correct,
  // so opening the panel just shows whatever it already knows.
  onOpenedChanged: if (opened) {
    cursorActive = false
    if (panelFlick) panelFlick.contentY = 0
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Connections {
    // The cursor must be re-clamped whenever the rows list changes -- a
    // mouse unplugging, a profile swap, a different field set all move it.
    target: hsk
    function onChanged() { root.clampCursor() }
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { hsk.refresh(); return "ok" }
    function status(): string { return hsk.summary }
    // Deliberately nothing that writes to the mouse. Every setting change
    // lands in the mouse's own storage, and an IPC method is reachable by any
    // process running as this user with nobody at the panel -- so changing
    // DPI or polling rate needs the panel (or `hskctl set` in a terminal).
  }

  // --- bar item -----------------------------------------------------------

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // Deliberately no `text:`. BarIconButton renders its `text` through an
    // OpticalGlyph that is `visible: iconComponent === null`, and it forces
    // `labelVisible: false` on the WidgetButton underneath -- so a widget with
    // a custom icon has no route to a text label at all. Setting `text` here
    // is not an error and not ignored-with-a-warning; it simply draws nothing,
    // which is why `showBatteryLabel` appeared to do nothing in either
    // position. The percentage is drawn inside the icon instead.
    // Content width plus a two-pixel breathing room on each side, instead of
    // Style.bar.iconSlot -- which is padded to a square that fits any single
    // glyph, and left the widget visibly loose next to its neighbours.
    slotSize: Math.ceil(barIconMetrics.width)
      + (root.barLabelText !== "" ? Style.space(3) + Math.ceil(barLabelMetrics.width) : 0)
      + Style.space(2)
    active: hsk.lowBattery
    // BarIconButton renders its tooltip through a shared host component we
    // cannot pin to PlainText from here, so strip < > & and controls and cap
    // the length before handoff. hsk.model is set by the mouse's own firmware
    // and hsk.summary quotes it, so the values are attacker-supplyable in the
    // reviewer's threat model (a USB device sets its own product string).
    tooltipText: Model.plain(hsk.model, 60) + " — " + Model.plain(hsk.summary, 120)
    iconComponent: Component {
      Item {
        // Centred on the button, not on the 16px optical canvas this Loader
        // fills -- the canvas is itself centred, so overflowing it is
        // symmetrical, and the slot above was widened to hold the result.
        Row {
          anchors.centerIn: parent
          spacing: root.barLabelText !== "" ? Style.space(3) : 0

          Text {
            // textFormat is set on every Text in this tree without exception,
            // literal-only ones included. On Qt's default AutoText a string
            // that looks like markup renders as rich text and `<img src=...>`
            // becomes a real fetch from the shell process -- and every value
            // that ends up in a Text here comes ultimately from the mouse or
            // an hskctl error message, so the invariant has to hold end to end.
            textFormat: Text.PlainText
            anchors.verticalCenter: parent.verticalCenter
            // Always the filled mouse glyph -- charging state and battery level
            // are carried by the percentage's color beside it, not by swapping
            // the icon out from under the reader.
            text: "󰍽"
            color: root.barIconColor
            font.family: root.fontFamily
            font.pixelSize: Style.bar.iconFont
            opacity: hsk.ready ? 1.0 : 0.55
          }

          Text {
            textFormat: Text.PlainText
            anchors.verticalCenter: parent.verticalCenter
            visible: root.barLabelText !== ""
            text: root.barLabelText
            color: root.barLabelColor
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            renderType: Text.NativeRendering
          }
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.MiddleButton) hsk.refresh()
      else root.toggle()
    }
  }

  // --- panel --------------------------------------------------------------
  //
  // The content below the hero is rendered entirely from the connected
  // mouse's profile: its sections in order, their writable fields in order,
  // each field drawn by the widget its metadata asks for. There is no
  // per-field QML left -- a mouse this plugin has never heard of renders
  // itself, and one that has nothing writable renders nothing.

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "r" || t === "R") hsk.refresh()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        // A thin, square-ended rule in the theme's foreground, sitting in a
        // fixed gutter to the right of the content (see `column` below) so it
        // never draws over a control. The stock Controls style would paint
        // its own grey on top of the rows instead.
        ScrollBar.vertical: ScrollBar {
          id: panelScroll
          policy: ScrollBar.AsNeeded
          padding: 0
          contentItem: Rectangle {
            implicitWidth: Style.space(3)
            implicitHeight: Style.space(24)
            visible: panelScroll.size < 1.0
            color: panelScroll.pressed || panelScroll.hovered
              ? root.dim
              : root.withAlpha(root.foreground, 0.3)
          }
        }

        Column {
          id: column
          // The gutter is always reserved, scrollable or not: making it
          // conditional would change the width, re-wrap the text, change the
          // height, and so change whether it scrolls.
          width: panelFlick.width - Style.space(22)
          spacing: Style.space(12)

          PanelHero {
            id: hero
            width: parent.width
            // PanelHero.title and .meta land in a shared host Text with the
            // default AutoText format we cannot override from a plugin, so
            // strip < > & (and controls) and cap before assignment -- the
            // model name comes from the mouse's own product string.
            title: Model.plain(hsk.model, 60)
            // When the mouse is being read, the numbers live one line below
            // (the stat row) and the hero stays a single austere line --
            // repeating them here would be two places to look for one thing.
            // When it is NOT, this uppercased one-liner is the state: it is
            // what tells a user what went wrong before the setup card.
            meta: hsk.state === "ready" ? "" : Model.plain(hsk.summary, 160)
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: hsk.ready ? 1.0 : 0.5
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: hsk.ready
                  ? Model.batteryGlyph(hsk.value("batteryPercent"), hsk.value("charging") === true)
                  : "󰍽"
                color: hsk.lowBattery ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
            trailingControl: Component {
              PanelActionButton {
                iconText: "󰑐"
                tooltipText: "Refresh"
                foreground: root.foreground
                fontFamily: root.fontFamily
                onClicked: hsk.refresh()
              }
            }
            detail: {
              // The firmware revision in a pill beside the title. A version
              // number is not a button, so the render type -- a bordered
              // BorderSurface -- communicates "attribute", never "control".
              if (hsk.state !== "ready" || !hsk.has("firmwareVersion")) return ""
              return "fw " + Model.plain(hsk.value("firmwareVersion"), 24)
            }
          }

          // One line, no boxes: the reading a person actually glances at,
          // without the full settings tree under it. Battery carries its state
          // in color (urgent when low, accent while charging); link and
          // firmware sit beside it in dim text so attention goes where the
          // theme says it should. Values a profile has no reading for simply
          // do not appear -- an unknown number is not drawn.
          Row {
            id: statRow
            visible: hsk.state === "ready"
              && (hsk.has("batteryPercent") || linkLabel !== "")
            width: parent.width
            spacing: Style.space(14)

            readonly property string linkLabel: hsk.has("connection")
              ? Model.connectionLabel(hsk.value("connection")) : ""

            Row {
              visible: hsk.has("batteryPercent")
              spacing: Style.space(4)

              Text {
                textFormat: Text.PlainText
                text: Model.batteryGlyph(hsk.value("batteryPercent"), hsk.value("charging") === true)
                // The charging bolt and the percent share the battery's color:
                // one color for one fact, and never a literal one.
                color: hsk.lowBattery ? root.urgent
                    : (hsk.value("charging") === true ? root.accent : root.foreground)
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
              }
              Text {
                textFormat: Text.PlainText
                text: hsk.value("batteryPercent") + "%"
                color: hsk.lowBattery ? root.urgent
                    : (hsk.value("charging") === true ? root.accent : root.foreground)
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
              }
            }

            Text {
              textFormat: Text.PlainText
              visible: statRow.linkLabel !== ""
              text: statRow.linkLabel
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }
          }

          // Says out loud that the mouse is being written to. Without it, the
          // 200-odd milliseconds an exchange takes read as "my click did
          // nothing", and the natural response -- click again -- is the one
          // thing that makes it worse.
          Rectangle {
            visible: hsk.working
            width: parent.width
            implicitHeight: writingLabel.implicitHeight + Style.space(10)
            radius: Style.cornerRadius > 0 ? Style.space(4) : 0
            color: root.hoverFill

            Text {
              id: writingLabel
              textFormat: Text.PlainText
              anchors.centerIn: parent
              text: "󰏫  Writing to the mouse…"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
          }

          Text {
            // hsk.lastError comes from hskctl (a trusted first-party helper
            // that we ship) but the text itself is not necessarily under our
            // control -- an exception string can carry a device name or path
            // that came from the mouse. Pin the format explicitly so an error
            // that happens to look like markup renders as text, not as a
            // remote fetch.
            textFormat: Text.PlainText
            visible: hsk.actionStatus !== "" || (hsk.lastError !== "" && !root.needsSetup)
            width: parent.width
            text: Model.plain(
              hsk.actionStatus !== "" ? hsk.actionStatus : hsk.lastError, 400
            )
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          // --- setup state ----------------------------------------------
          // The honest first-run view. The protocol is not mapped yet, so
          // rather than showing dead controls we explain exactly what is
          // missing and what to run next.
          CursorSurface {
            visible: root.needsSetup || root.hasError
            width: parent.width
            implicitHeight: setupColumn.implicitHeight + Style.spacing.xl
            foreground: root.foreground
            outline: true

            Column {
              id: setupColumn
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.space(12)
              anchors.rightMargin: Style.space(12)
              spacing: Style.space(6)

              Text {
                textFormat: Text.PlainText
                width: parent.width
                text: root.needsSetup
                  ? (hsk.detected ? "Mouse found, protocol not mapped" : "Mouse not detected")
                  : (root.looksLikePermissions ? "No permission to reach the mouse"
                                               : "Can't talk to the mouse")
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
                wrapMode: Text.WordWrap
              }

              Text {
                textFormat: Text.PlainText
                width: parent.width
                text: {
                  if (root.looksLikePermissions)
                    return "Configuring the mouse uses HID feature reports, and those need "
                         + "read-write access to /dev/hidraw*, which is root-only by default. "
                         + "Install the udev rule, then unplug and replug the mouse or its dongle."
                  if (root.hasError) return Model.plain(hsk.lastError, 400)
                  if (!hsk.detected) return "Plug in the mouse or its 2.4 GHz dongle, then refresh."
                  return "hskctl can see the device but does not know its config protocol yet. "
                       + "Run a capture to fill in the profile."
                }
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }

              Text {
                textFormat: Text.PlainText
                width: parent.width
                visible: root.needsSetup || root.looksLikePermissions
                text: root.looksLikePermissions
                  ? "~/.config/omarchy/plugins/keasbeexd.mousectrl/install.sh --udev"
                  : "hskctl probe"
                wrapMode: Text.WrapAnywhere
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          // --- the profile's own layout -----------------------------------
          Repeater {
            model: hsk.sections

            delegate: Column {
              id: sectionColumn

              required property var modelData

              // A section exists only if it has a row to render, judged by
              // the same predicate buildRows uses (hsk.rowVisible), or a
              // section of gated or read-only fields would render headers
              // above nothing.
              readonly property bool hasAny: {
                // isList, not Array.isArray: modelData comes back off the
                // Service's `property var` boundary as a wrapped sequence,
                // and only Model.isList recognizes it (see Model.js).
                if (!modelData || !Model.isList(modelData.fields)) return false
                for (var i = 0; i < modelData.fields.length; i++) {
                  if (hsk.rowVisible(modelData.fields[i])) return true
                }
                return false
              }

              visible: hasAny
              width: parent.width
              spacing: Style.space(10)

              PanelSeparator {
                width: parent.width
                foreground: root.foreground
              }

              PanelSectionHeader {
                // Section titles come from the profile, and everything user
                // visible goes through `plain()` for the same reason every
                // device string does -- but only where it may render through
                // a shared host Text. PanelSectionHeader, like the hero,
                // cannot be pinned to PlainText from a plugin.
                text: Model.plain(sectionColumn.modelData.title, 24)
                foreground: root.foreground
                fontFamily: root.fontFamily
              }

              Column {
                width: parent.width
                spacing: Style.space(6)

                Repeater {
                  model: sectionColumn.modelData.fields

                  delegate: Loader {
                    id: fieldLoader

                    required property string modelData

                    // A field renders only if hskctl says it is a row at all --
                    // writable, renderable widget, not hidden by a capability
                    // gate. The same predicate Model.buildRows applies, so a
                    // drawn row and a cursor stop never disagree.
                    readonly property bool activeField: hsk.rowVisible(modelData)
                    property string fieldName: modelData

                    active: activeField
                    width: parent.width
                    // One Loader per field, whose source component is the
                    // widget the profile's metadata asks for: this is the
                    // whole renderer. The rowToggle fallback is unreachable
                    // (the active gate above already required a known
                    // writable widget) but keeps `sourceComponent` total.
                    sourceComponent: {
                      var m = hsk.metaFor(modelData)
                      if (m && m.widget === "chips") return rowChips
                      if (m && m.widget === "slider") return rowSlider
                      if (m && m.widget === "color") return rowColor
                      return rowToggle
                    }

                    onLoaded: {
                      if (item) {
                        item.field = fieldName
                        root.rowItems[fieldName] = item
                      }
                    }
                    Component.onDestruction: {
                      if (root.rowItems[modelData] === item) delete root.rowItems[modelData]
                    }
                  }
                }
              }
            }
          }

          // --- footer -----------------------------------------------------
          // Which build is actually running. hskctl reads it from
          // manifest.json and reports it in its JSON, so it is by construction
          // the version that was published -- a hardcoded one drifts, and a
          // version label you cannot trust is worse than none. Hovering shows
          // the firmware and the device in use, which is the rest of "what am
          // I running".

          Item {
            visible: hsk.pluginVersion !== ""
            width: parent.width
            implicitHeight: Math.max(hotkeyLabel.implicitHeight, versionLabel.implicitHeight) + Style.space(6)

            // A standing reminder of the keys README documents -- arrows to
            // move the cursor and adjust the row under it, 'r' to refresh
            // (silent-by-design otherwise, see refresh()'s callers). The
            // panel is the one place someone would actually go looking.
            Text {
              id: hotkeyLabel
              textFormat: Text.PlainText
              anchors.left: parent.left
              anchors.bottom: parent.bottom
              text: "↑↓←→ navigate  ·  r refresh"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Text {
              id: versionLabel
              textFormat: Text.PlainText
              anchors.right: parent.right
              anchors.bottom: parent.bottom
              // Version comes from hskctl (which reads it from manifest.json),
              // so it is trusted -- but plain() also caps and keeps a rogue
              // build from painting a novel out of the label.
              text: "v" + Model.plain(hsk.pluginVersion, 32)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption

              MouseArea {
                id: versionMouse
                anchors.fill: parent
                anchors.margins: -Style.space(4)
                hoverEnabled: true
              }

              PanelToolTip {
                visible: versionMouse.containsMouse
                // The tooltip is rendered by a shared host component, so
                // sanitize the device-supplied pieces (firmware string, hidraw
                // path) the same way as the bar tooltip. The literal prefix
                // and the version stay as-is.
                text: {
                  var parts = ["Mouse CTRL v" + Model.plain(hsk.pluginVersion, 32)]
                  if (hsk.has("firmwareVersion"))
                    parts.push("firmware " + Model.plain(hsk.value("firmwareVersion"), 40))
                  if (hsk.devicePath !== "") parts.push(Model.plain(hsk.devicePath, 60))
                  return parts.join("  ·  ")
                }
                fontFamily: root.fontFamily
              }
            }
          }
        }
      }
    }
  }

  // --- row widgets ----------------------------------------------------------
  //
  // Four widgets cover every control a profile can ask for, and every one of
  // them is generic over `field`: it reads its value and metadata through the
  // service (which itself reads it from the mouse), never a hardcoded field
  // name. A row that says "only writable fields become controls" stays true
  // because the Loader gate above already applied it.

  // A pick-one row of Omarchy's own bordered buttons, drawn like Vigil's
  // tabs. Not the shell's ButtonGroup: that fills every unselected chip with
  // the theme background, which in some themes is a block of solid color
  // against the panel. A plain Button stays transparent until it is selected.
  component ChoiceRow: Row {
    id: choiceRow
    property var options: []
    property string value: ""
    property int cursorIndex: -1
    signal changed(string value)
    signal hovered(int index, bool on)
    spacing: Style.spacing.md

    Repeater {
      model: choiceRow.options
      Button {
        required property var modelData
        required property int index
        text: modelData.label
        selected: String(modelData.value) === choiceRow.value
        hasCursor: choiceRow.cursorIndex === index
        bordered: true
        foreground: root.foreground
        fontFamily: root.fontFamily
        fontSize: Style.font.bodySmall
        horizontalPadding: Style.space(8)
        onClicked: choiceRow.changed(String(modelData.value))
        onHovered: function(on) { choiceRow.hovered(index, on) }
      }
    }
  }

  Component {
    id: rowToggle
    Toggle {
      property string field: ""
      visible: hsk.canWrite(field) && !!hsk.metaFor(field)
      width: parent ? parent.width : 0
      label: hsk.labelFor(field)
      description: hsk.metaFor(field) ? hsk.metaFor(field).description : ""
      checked: hsk.value(field) === true
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      hasCursor: root.isRowCursor(field)
      onClicked: hsk.toggle(field)
      onHovered: function(on) { if (on) root.setCursor(root.rowIndexOf(field)) }
    }
  }

  Component {
    id: rowChips
    Column {
      property string field: ""
      width: parent ? parent.width : 0
      spacing: Style.space(6)

      Text {
        textFormat: Text.PlainText
        text: hsk.labelFor(field)
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      // ChoiceRow is a Row -- it sizes to its chips, so no explicit
      // width here or the row stretches past its content.
      ChoiceRow {
        options: Model.chipsOptions(field, hsk.allowed, hsk.unitFor(field))
        value: String(hsk.value(field))
        cursorIndex: root.isRowCursor(field) ? 0 : -1
        onChanged: function(v) {
          var numeric = parseInt(v, 10)
          hsk.set(field, (!isNaN(numeric) && String(numeric) === v) ? numeric : v)
        }
        onHovered: function(index, on) { if (on) root.setCursor(root.rowIndexOf(field)) }
      }
    }
  }

  Component {
    id: rowSlider
    CursorSurface {
      id: sliderRow

      property string field: ""
      width: parent.width

      // Read straight from the service rather than being handed a snapshot,
      // so the row survives a refresh instead of being torn down and rebuilt.
      readonly property var bounds: Model.sliderBounds(field, hsk.allowed, hsk.metaFor(field))
      readonly property real value: {
        var v = hsk.value(field)
        var n = typeof v === "number" ? v : NaN
        return isFinite(n) ? n : (bounds ? bounds.min : 0)
      }

      hasCursor: root.isRowCursor(field)
      foreground: root.foreground
      fill: root.hoverFill
      currentFill: root.selectedFill
      implicitHeight: sliderInner.implicitHeight + Style.spacing.lg

      Column {
        id: sliderInner
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.leftMargin: Style.space(6)
        anchors.rightMargin: Style.space(6)
        spacing: Style.space(6)

        Text {
          textFormat: Text.PlainText
          text: hsk.labelFor(field)
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        RowLayout {
          width: parent.width
          spacing: Style.space(8)

          // Typing a value commits like the slider does -- on
          // editingFinished, not per keystroke, so a half-typed number is
          // never sent to the mouse.
          TextField {
            id: valueField
            property var row: sliderRow
            Layout.preferredWidth: Style.space(70)
            enabled: row.bounds !== null
            selectByMouse: true
            horizontalAlignment: Text.AlignRight
            color: root.foreground
            selectionColor: root.withAlpha(root.accent, 0.4)
            selectedTextColor: root.foreground
            placeholderTextColor: root.dim
            opacity: enabled ? 1.0 : 0.5
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            maximumLength: 7
            background: Rectangle {
              implicitHeight: Style.space(28)
              radius: Style.cornerRadius > 0 ? Style.space(4) : 0
              color: "transparent"
              border.width: 1
              border.color: valueField.activeFocus ? root.accent : root.dim
            }
            validator: IntValidator {
              // The validator is a nested property-wrapper object, not part of
              // the signal-handler scope -- valueField's own `row` property is
              // not in its lookup chain (this is a real, logged error: the
              // shell showed `row is not defined` binding as soon as the row
              // rendered). Reference the row by file-scope id instead.
              bottom: sliderRow.bounds ? sliderRow.bounds.min : 0
              top: sliderRow.bounds ? sliderRow.bounds.max : 999999
            }

            // Only follow the device (and the slider) while not focused, or a
            // refresh mid-edit would overwrite what is being typed.
            Binding {
              target: valueField
              property: "text"
              value: String(Math.round(sliderRow.value))
              when: !valueField.activeFocus
            }

            onEditingFinished: {
              var n = parseInt(text, 10)
              if (!isNaN(n) && row.bounds) hsk.set(field, Model.clampStep(n, row.bounds))
              focus = false
            }
          }

          Text {
            // The unit ("Hz", "DPI", "s") sits beside the number -- both come
            // from the profile, so an unknown field still labels its value.
            textFormat: Text.PlainText
            visible: hsk.unitFor(field) !== ""
            text: hsk.unitFor(field)
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Item { Layout.fillWidth: true }
        }

        Slider {
          id: slider
          property var row: sliderRow
          width: parent.width
          from: row.bounds ? row.bounds.min : 0
          to: row.bounds ? row.bounds.max : 1
          stepSize: row.bounds ? row.bounds.step : 1
          snapMode: Slider.SnapAlways
          enabled: row.bounds !== null
          opacity: enabled ? 1.0 : 0.5

          // Track and handle drawn from the theme rather than left to the
          // Controls style, whose palette knows nothing about Omarchy themes.
          background: Rectangle {
            x: slider.leftPadding
            y: slider.topPadding + slider.availableHeight / 2 - height / 2
            implicitWidth: Style.space(200)
            implicitHeight: Style.space(4)
            width: slider.availableWidth
            height: implicitHeight
            radius: Style.cornerRadius > 0 ? height / 2 : 0
            color: root.faint

            Rectangle {
              width: slider.visualPosition * parent.width
              height: parent.height
              radius: parent.radius
              color: root.accent
            }
          }

          handle: Rectangle {
            x: slider.leftPadding + slider.visualPosition * (slider.availableWidth - width)
            y: slider.topPadding + slider.availableHeight / 2 - height / 2
            implicitWidth: Style.space(14)
            implicitHeight: Style.space(14)
            radius: Style.cornerRadius > 0 ? width / 2 : 0
            color: slider.pressed || slider.hovered ? root.accent : root.foreground
            border.width: 1
            border.color: root.accent
          }

          // Track the device only while not being dragged -- otherwise a
          // refresh mid-drag yanks the handle back under the pointer and
          // `released` never arrives where the user let go.
          Binding {
            target: slider
            property: "value"
            value: sliderRow.value
            when: !slider.pressed
          }

          onMoved: {
            if (row.bounds) hsk.setSoon(row.field, Model.clampStep(value, row.bounds))
          }
          onPressedChanged: root.setCursor(root.rowIndexOf(row.field))
        }
      }
    }
  }

  Component {
    id: rowColor
    CursorSurface {
      id: colorRow

      property string field: ""
      width: parent.width

      readonly property string swatch: {
        var v = hsk.value(field)
        return v === undefined || v === null ? "" : String(v)
      }

      hasCursor: root.isRowCursor(field)
      foreground: root.foreground
      fill: root.hoverFill
      currentFill: root.selectedFill
      implicitHeight: colorInner.implicitHeight + Style.spacing.lg

      Column {
        id: colorInner
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.leftMargin: Style.space(6)
        anchors.rightMargin: Style.space(6)
        spacing: Style.space(6)

        Text {
          textFormat: Text.PlainText
          text: hsk.labelFor(field)
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        RowLayout {
          width: parent.width
          spacing: Style.space(8)

          // Three one-click presets, then the current color (click cycles the
          // eight preset colors, for reaching one that isn't among the three),
          // then the hex field for an exact color.
          Repeater {
            model: Model.DPI_PRESET_COLORS

            Rectangle {
              id: presetBox
              required property string modelData
              Layout.preferredWidth: Style.space(14)
              Layout.preferredHeight: Style.space(14)
              Layout.alignment: Qt.AlignVCenter
              radius: Style.cornerRadius > 0 ? Style.space(3) : 0
              color: presetBox.modelData
              border.width: 1
              border.color: presetMouse.containsMouse ? root.foreground : root.dim

              MouseArea {
                id: presetMouse
                anchors.fill: parent
                anchors.margins: -Style.space(3)
                hoverEnabled: true
                enabled: !hsk.busy
                cursorShape: hsk.busy ? Qt.BusyCursor : Qt.PointingHandCursor
                onEntered: root.setCursor(root.rowIndexOf(colorRow.field))
                onClicked: hsk.set(colorRow.field, presetBox.modelData)
              }
            }
          }

          // Current color. Clicking cycles the eight preset colors above, for
          // reaching a color that isn't one of the three shown.
          Rectangle {
            id: swatchBox
            Layout.preferredWidth: Style.space(14)
            Layout.preferredHeight: Style.space(14)
            Layout.alignment: Qt.AlignVCenter
            radius: Style.cornerRadius > 0 ? Style.space(3) : 0
            // Only a validated #rrggbb reaches a color property; anything else
            // the device reports shows as an empty outline.
            color: Model.isValidHexColor(colorRow.swatch) ? colorRow.swatch : "transparent"
            border.width: 1
            border.color: swatchMouse.containsMouse ? root.foreground : root.dim

            MouseArea {
              id: swatchMouse
              anchors.fill: parent
              anchors.margins: -Style.space(3)
              hoverEnabled: true
              enabled: !hsk.busy
              cursorShape: hsk.busy ? Qt.BusyCursor : Qt.PointingHandCursor
              onEntered: root.setCursor(root.rowIndexOf(colorRow.field))
              onClicked: hsk.set(colorRow.field, Model.nextStageColor(colorRow.swatch))
            }

            PanelToolTip {
              visible: swatchMouse.containsMouse
              text: "Current color -- click to cycle"
              fontFamily: root.fontFamily
            }
          }

          TextField {
            id: hexField
            property var row: colorRow
            Layout.fillWidth: true
            selectByMouse: true
            color: root.foreground
            selectionColor: root.withAlpha(root.accent, 0.4)
            selectedTextColor: root.foreground
            placeholderTextColor: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            maximumLength: 7
            background: Rectangle {
              implicitHeight: Style.space(28)
              radius: Style.cornerRadius > 0 ? Style.space(4) : 0
              color: "transparent"
              border.width: 1
              border.color: hexField.activeFocus ? root.accent : root.dim
            }
            validator: RegularExpressionValidator { regularExpression: /#?[0-9a-fA-F]{0,6}/ }

            // Only follow the device while the field is not focused, or a
            // refresh mid-edit would overwrite what is being typed.
            Binding {
              target: hexField
              property: "text"
              value: colorRow.swatch
              when: !hexField.activeFocus
            }

            onEditingFinished: {
              var v = text.trim()
              if (v !== "" && v[0] !== "#") v = "#" + v
              if (Model.isValidHexColor(v)) hsk.set(row.field, v.toLowerCase())
              focus = false
            }
          }
        }
      }
    }
  }
}
