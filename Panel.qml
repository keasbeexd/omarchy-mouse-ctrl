import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "keasbeexd.mousectrl"
  ipcTarget: "keasbeexd.mousectrl"
  manageIpc: false

  property int cursorIndex: 0
  property bool cursorActive: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color hoverFill: bar ? Style.hoverFillFor(bar.foreground, Color.accent) : "transparent"
  readonly property color selectedFill: bar ? Style.selectedFillFor(bar.foreground, Color.accent) : "transparent"

  readonly property var rows: hsk.rows
  // Only stage 1 is ever mapped (see Model.dpiStage1Info) -- both profiles
  // this plugin ships deliberately expose no stage picker.
  readonly property bool hasDpi: hsk.has("dpiStage1")
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
    : (hsk.ready ? barForeground : Qt.darker(barForeground, 1.55))

  // When the mouse is plugged in, the percentage recolours to the theme's
  // accent so "charging" is visible at a glance -- the accent tracks whatever
  // omarchy theme is active, so it always sits with the rest of the bar.
  readonly property color barLabelColor: hsk.lowBattery
    ? root.urgent
    : (hsk.ready && hsk.value("charging") === true
       ? Color.accent
       : (hsk.ready ? barForeground : Qt.darker(barForeground, 1.55)))

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

  // Left/right nudges the value of whatever row the cursor is on, so a polling
  // rate or lift-off distance can be changed without reaching for the mouse
  // that is currently being reconfigured.
  function adjustCurrent(direction) {
    var row = currentRow()
    if (!row) return
    if (row.kind === "dpiStage") {
      // One step per press, matching the sensor's 50 DPI granularity; hold
      // shift-free repeat and it walks smoothly.
      var wanted = Model.clampDpi(row.dpi + direction * Model.DPI_STEP)
      // Coalesced, like the slider -- holding an arrow key is one write.
      if (wanted !== row.dpi) hsk.setSoon("dpiStage1", wanted)
    } else if (row.kind === "pollingRate") {
      var options = Model.allowedRatesFor(hsk.allowed)
      var current = hsk.value("pollingRate")
      var index = options.indexOf(current)
      if (index < 0) index = 0
      var next = Math.max(0, Math.min(options.length - 1, index + direction))
      if (options[next] !== current) hsk.set("pollingRate", options[next])
    } else if (row.kind === "liftOffDistance") {
      hsk.set("liftOffDistance", hsk.value("liftOffDistance") === "1mm" ? "2mm" : "1mm")
    } else if (row.kind === "toggle") {
      hsk.toggle(row.field)
    }
  }

  function activateCursor() {
    var row = currentRow()
    if (!row) return
    if (row.kind === "toggle") {
      hsk.toggle(row.field)
    } else {
      adjustCurrent(1)
    }
  }

  // `c` cycles the DPI stage's colour, when the cursor is on it.
  function cycleCurrentColor() {
    var row = currentRow()
    if (!row || row.kind !== "dpiStage") return
    if (!hsk.canWrite("dpiStage1Color")) return
    hsk.set("dpiStage1Color", Model.nextStageColor(row.color))
  }

  function scrollItemIntoView(item) {
    if (!panelFlick || !item) return
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
    if (!row || row.kind !== "dpiStage" || !dpiRow) return
    scrollItemIntoView(dpiRow)
  }

  function setCursor(index) {
    cursorActive = true
    cursorIndex = index
    clampCursor()
  }

  function rowIndexOf(kind, key) {
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].kind !== kind) continue
      if (kind === "toggle" && rows[i].field !== key) continue
      return i
    }
    return -1
  }

  onOpenedChanged: if (opened) {
    cursorActive = false
    if (panelFlick) panelFlick.contentY = 0
    hsk.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Connections {
    // `hsk`, not `mouse` -- this one survived the rename and has been pointing
    // at nothing ever since, so the cursor was never re-clamped on a refresh.
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
    function setDpi(dpi: string): string {
      hsk.set("dpiStage1", Model.clampDpi(parseInt(dpi, 10)))
      return "ok"
    }
    function setPollingRate(rate: string): string {
      hsk.set("pollingRate", parseInt(rate, 10))
      return "ok"
    }
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
            // are carried by the percentage's colour beside it, not by swapping
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
        if (t === "c" || t === "C") root.cycleCurrentColor()
        else if (t === "r" || t === "R") hsk.refresh()
        else if (t === "m" || t === "M") hsk.toggle("motionSync")
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
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          PanelHero {
            id: hero
            width: parent.width
            // PanelHero.title and .meta land in a shared host Text with the
            // default AutoText format we cannot override from a plugin, so
            // strip < > & (and controls) and cap before assignment -- the
            // model name comes from the mouse's own product string.
            title: Model.plain(hsk.model, 60)
            meta: Model.plain(hsk.summary, 160)
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
                                               : "hskctl unavailable")
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
                font.family: "monospace"
                font.pixelSize: Style.font.caption
              }
            }
          }

          // --- DPI --------------------------------------------------------

          PanelSeparator {
            visible: root.hasDpi
            foreground: root.foreground
          }

          Column {
            visible: root.hasDpi
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: "DPI"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            DpiStageRow {
              id: dpiRow
              width: parent.width
            }
          }

          // --- performance ------------------------------------------------

          PanelSeparator {
            visible: hsk.canWrite("pollingRate") || hsk.canWrite("liftOffDistance")
            foreground: root.foreground
          }

          Column {
            visible: hsk.canWrite("pollingRate") || hsk.canWrite("liftOffDistance")
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: "PERFORMANCE"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Column {
              visible: hsk.canWrite("pollingRate")
              width: parent.width
              spacing: Style.space(6)

              Text {
                textFormat: Text.PlainText
                text: "Polling rate"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              // ButtonGroup is a Row -- it sizes to its chips, so no explicit
              // width here or the group stretches past its content.
              ButtonGroup {
                options: Model.pollingOptions(hsk.value("pollingRate"), hsk.allowed)
                value: String(hsk.value("pollingRate"))
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                cursorIndex: root.cursorActive && root.currentRow() && root.currentRow().kind === "pollingRate" ? 0 : -1
                onChanged: function(v) { hsk.set("pollingRate", parseInt(v, 10)) }
                onHovered: function(index, on) {
                  if (on) root.setCursor(root.rowIndexOf("pollingRate", null))
                }
              }
            }

            Column {
              visible: hsk.canWrite("liftOffDistance")
              width: parent.width
              spacing: Style.space(6)

              Text {
                textFormat: Text.PlainText
                text: "Lift-off distance"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              ButtonGroup {
                options: [{ value: "1mm", label: "1 mm" }, { value: "2mm", label: "2 mm" }]
                value: String(hsk.value("liftOffDistance"))
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                onChanged: function(v) { hsk.set("liftOffDistance", v) }
                onHovered: function(index, on) {
                  if (on) root.setCursor(root.rowIndexOf("liftOffDistance", null))
                }
              }
            }
          }

          // --- sensor -----------------------------------------------------

          PanelSeparator {
            visible: toggleColumn.hasAny
            foreground: root.foreground
          }

          Column {
            width: parent.width
            spacing: Style.space(10)
            visible: toggleColumn.hasAny

            PanelSectionHeader {
              text: "SENSOR"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Column {
              id: toggleColumn
              width: parent.width
              spacing: Style.space(6)
              readonly property bool hasAny: hsk.canWrite("motionSync")
                || hsk.canWrite("angleSnap")
                || hsk.canWrite("rippleControl")

              Repeater {
                model: ["motionSync", "angleSnap", "rippleControl"]
                Toggle {
                  required property var modelData
                  visible: hsk.canWrite(modelData)
                  width: toggleColumn.width
                  label: Model.toggleLabel(modelData)
                  description: Model.toggleDescription(modelData)
                  checked: hsk.value(modelData) === true
                  foreground: root.foreground
                  accent: Color.accent
                  fontFamily: root.fontFamily
                  hasCursor: {
                    var row = root.currentRow()
                    return root.cursorActive && row && row.kind === "toggle" && row.field === modelData
                  }
                  onClicked: hsk.toggle(modelData)
                  onHovered: function(on) {
                    if (on) root.setCursor(root.rowIndexOf("toggle", modelData))
                  }
                }
              }
            }
          }

          // --- footer -----------------------------------------------------
          // Which build is actually running. Read from manifest.json rather
          // than hardcoded, so it is by construction the version that was
          // published -- a hardcoded one drifts, and a version label you
          // cannot trust is worse than none. Hovering shows the firmware and
          // the CLI in use, which is the rest of "what am I running".

          Item {
            visible: hsk.pluginVersion !== ""
            width: parent.width
            implicitHeight: versionLabel.implicitHeight + Style.space(6)

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
                  var parts = ["Mouse Control v" + Model.plain(hsk.pluginVersion, 32)]
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

  // --- components ---------------------------------------------------------

  // The mouse's one DPI stage: a draggable slider plus a colour swatch and a
  // hex field. There is deliberately no stage picker here -- see the
  // profiles' own notes on why switching which stage is live does not work.
  component DpiStageRow: CursorSurface {
    id: stageRow

    // Read straight from the service rather than being handed a snapshot, so
    // the row survives a refresh instead of being torn down and rebuilt.
    readonly property int dpi: {
      var v = hsk.value("dpiStage1")
      return v === undefined || v === null ? Model.DPI_MIN : v
    }
    readonly property int dpiY: {
      var v = hsk.value("dpiStage1Y")
      return v === undefined || v === null ? stageRow.dpi : v
    }
    readonly property string swatch: {
      var v = hsk.value("dpiStage1Color")
      return v === undefined || v === null ? "" : String(v)
    }
    readonly property bool split: stageRow.dpiY !== stageRow.dpi
    readonly property bool dpiWritable: hsk.canWrite("dpiStage1")
    readonly property bool colorWritable: hsk.canWrite("dpiStage1Color")

    readonly property bool rowHasCursor: {
      var row = root.currentRow()
      return root.cursorActive && row && row.kind === "dpiStage"
    }

    hasCursor: rowHasCursor
    foreground: root.foreground
    fill: root.hoverFill
    currentFill: root.selectedFill
    implicitHeight: stageInner.implicitHeight + Style.spacing.lg

    Column {
      id: stageInner
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(6)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(6)

      RowLayout {
        width: parent.width
        spacing: Style.space(8)

        // Typing a value commits like the slider does -- on
        // editingFinished, not per keystroke, so a half-typed number is
        // never sent to the mouse.
        TextField {
          id: dpiField
          Layout.preferredWidth: Style.space(70)
          enabled: stageRow.dpiWritable
          selectByMouse: true
          horizontalAlignment: Text.AlignRight
          color: stageRow.split ? root.urgent : root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
          validator: IntValidator { bottom: Model.DPI_MIN; top: Model.DPI_MAX }

          // Only follow the device (and the slider) while not focused, or a
          // refresh mid-edit would overwrite what is being typed.
          Binding {
            target: dpiField
            property: "text"
            value: String(stageRow.dpi)
            when: !dpiField.activeFocus
          }

          onEditingFinished: {
            var n = parseInt(text, 10)
            if (!isNaN(n)) hsk.set("dpiStage1", Model.clampDpi(n))
            focus = false
          }
        }

        Item { Layout.fillWidth: true }
      }

      Slider {
        id: dpiSlider
        width: parent.width
        from: Model.DPI_MIN
        to: Model.DPI_MAX
        stepSize: Model.DPI_STEP
        snapMode: Slider.SnapAlways
        enabled: stageRow.dpiWritable

        // Track the device only while not being dragged -- otherwise a
        // refresh mid-drag yanks the handle back under the pointer and
        // `released` never arrives where the user let go.
        Binding {
          target: dpiSlider
          property: "value"
          value: stageRow.dpi
          when: !dpiSlider.pressed
        }

        onMoved: hsk.setSoon("dpiStage1", Model.clampDpi(value))
        onPressedChanged: root.setCursor(root.rowIndexOf("dpiStage"))
      }

      RowLayout {
        width: parent.width
        spacing: Style.space(8)
        visible: stageRow.swatch !== ""

        // Three one-click presets, then the current colour (click cycles the
        // firmware's full palette for anything not one of the three), then
        // the hex field for typing an exact colour.
        Repeater {
          model: Model.DPI_PRESET_COLORS

          Rectangle {
            id: presetBox
            required property string modelData
            visible: stageRow.colorWritable
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
              onEntered: root.setCursor(root.rowIndexOf("dpiStage"))
              onClicked: hsk.set("dpiStage1Color", presetBox.modelData)
            }
          }
        }

        // Current colour. Clicking cycles the firmware's full palette, for
        // reaching a colour that isn't one of the three presets above.
        Rectangle {
          id: swatchBox
          visible: stageRow.colorWritable
          Layout.preferredWidth: Style.space(14)
          Layout.preferredHeight: Style.space(14)
          Layout.alignment: Qt.AlignVCenter
          radius: Style.cornerRadius > 0 ? Style.space(3) : 0
          color: stageRow.swatch
          border.width: 1
          border.color: swatchMouse.containsMouse ? root.foreground : root.dim

          MouseArea {
            id: swatchMouse
            anchors.fill: parent
            anchors.margins: -Style.space(3)
            hoverEnabled: true
            enabled: !hsk.busy
            cursorShape: hsk.busy ? Qt.BusyCursor : Qt.PointingHandCursor
            onEntered: root.setCursor(root.rowIndexOf("dpiStage"))
            onClicked: hsk.set("dpiStage1Color", Model.nextStageColor(stageRow.swatch))
          }

          PanelToolTip {
            visible: swatchMouse.containsMouse
            text: "Current colour -- click to cycle"
            fontFamily: root.fontFamily
          }
        }

        TextField {
          id: hexField
          Layout.fillWidth: true
          enabled: stageRow.colorWritable
          selectByMouse: true
          color: root.foreground
          font.family: "monospace"
          font.pixelSize: Style.font.caption
          validator: RegularExpressionValidator { regularExpression: /#?[0-9a-fA-F]{0,6}/ }

          // Only follow the device while the field is not focused, or a
          // refresh mid-edit would overwrite what is being typed.
          Binding {
            target: hexField
            property: "text"
            value: stageRow.swatch
            when: !hexField.activeFocus
          }

          onEditingFinished: {
            var v = text.trim()
            if (v !== "" && v[0] !== "#") v = "#" + v
            if (Model.isValidHexColor(v)) hsk.set("dpiStage1Color", v.toLowerCase())
            focus = false
          }
        }
      }
    }

    PanelToolTip {
      visible: stageRow.split && stageRow.hasCursor
      text: "X and Y axes differ on this stage"
      fontFamily: root.fontFamily
    }
  }
}
