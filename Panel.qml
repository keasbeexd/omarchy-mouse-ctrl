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
  moduleName: "io.github.keasbeexd.hsk"
  ipcTarget: "io.github.keasbeexd.hsk"
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
  readonly property var dpiStages: Model.dpiStages(hsk.effectiveValues)
  // Delegate count only. Binding the Repeater to the stage *array* rebuilt
  // every row whenever any value changed -- which destroyed the slider you
  // were dragging, so `released` never arrived and the knob snapped back.
  readonly property int stageCount: dpiStages.length
  readonly property bool needsSetup: hsk.state === "undiscovered"
  readonly property bool hasError: hsk.state === "error"

  // Missing permissions is by far the most common first-run failure, and the
  // rawest form of it -- EACCES opening /dev/hidraw* -- is unreadable to
  // anyone who has not just read the udev docs. Every exchange needs the node
  // opened read-write, so this is not a degraded mode: nothing works at all,
  // including the battery. Say so, and say what to run.
  readonly property bool looksLikePermissions: root.hasError
    && Model.isPermissionError(hsk.lastError)

  readonly property color barIconColor: hsk.lowBattery
    ? root.urgent
    : (hsk.ready ? barForeground : Qt.darker(barForeground, 1.55))

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // NOT `id: mouse`. MouseArea.onClicked carries an implicit `mouse`
  // parameter (the MouseEvent), which silently shadows an id of that name --
  // so `mouse.setDpiStage(...)` inside a click handler resolved to the event,
  // not the service, and did nothing. Handlers for parameterless signals
  // (Toggle.clicked, PanelActionButton.clicked) were unaffected, which is why
  // the toggles worked and clicking a DPI stage did not.
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
      // Coalesced, like the step buttons -- holding an arrow key is one write.
      if (wanted !== row.dpi) hsk.setSoon("dpiStage" + row.stage, wanted)
    } else if (row.kind === "pollingRate") {
      var options = Model.POLLING_RATES
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
    if (row.kind === "dpiStage") {
      if (row.selectable) hsk.setDpiStage(row.stage)
    } else if (row.kind === "toggle") {
      hsk.toggle(row.field)
    } else {
      adjustCurrent(1)
    }
  }

  // `c` cycles the colour of the stage under the cursor.
  function cycleCurrentColor() {
    var row = currentRow()
    if (!row || row.kind !== "dpiStage") return
    if (!hsk.canWrite("dpiStage" + row.stage + "Color")) return
    hsk.set("dpiStage" + row.stage + "Color", Model.nextStageColor(row.color))
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
    if (!row || row.kind !== "dpiStage" || !stageColumn) return
    // Index by position among the stage rows, not by stage number -- a profile
    // that omits a stage would otherwise scroll to the wrong row.
    var seen = 0
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].kind !== "dpiStage") continue
      if (rows[i].stage === row.stage) break
      seen++
    }
    if (seen < stageColumn.children.length) scrollItemIntoView(stageColumn.children[seen])
  }

  function setCursor(index) {
    cursorActive = true
    cursorIndex = index
    clampCursor()
  }

  function rowIndexOf(kind, key) {
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].kind !== kind) continue
      if (kind === "dpiStage" && rows[i].stage !== key) continue
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
    function cycleDpi(): string { hsk.cycleDpiStage(); return "ok" }
    function setDpiStage(stage: string): string {
      hsk.setDpiStage(parseInt(stage, 10))
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
    text: hsk.barText
    slotSize: hsk.barText !== "" ? Style.bar.statusSlot : Style.bar.iconSlot
    active: hsk.lowBattery
    tooltipText: hsk.model + " — " + hsk.summary
    iconComponent: Component {
      Item {
        Text {
          anchors.centerIn: parent
          text: hsk.ready
            ? Model.batteryGlyph(hsk.value("batteryPercent"), hsk.value("charging") === true)
            : "󰍽"
          color: root.barIconColor
          font.family: root.fontFamily
          font.pixelSize: Style.bar.iconFont
          opacity: hsk.ready ? 1.0 : 0.55
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) hsk.cycleDpiStage()
      else if (buttonCode === Qt.MiddleButton) hsk.refresh()
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
        else if (t === "d" || t === "D") hsk.cycleDpiStage()
        else if (t === "m" || t === "M") hsk.toggle("motionSync")
        // 1..7, not 1..6 -- the mouse has seven stages and stage 7 was
        // unreachable from the keyboard.
        else if (t >= "1" && t <= "7") hsk.setDpiStage(parseInt(t, 10))
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
            title: hsk.model
            meta: hsk.summary
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: hsk.ready ? 1.0 : 0.5
            iconComponent: Component {
              Text {
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
              anchors.centerIn: parent
              text: "󰏫  Writing to the mouse…"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }
          }

          Text {
            visible: hsk.actionStatus !== "" || (hsk.lastError !== "" && !root.needsSetup)
            width: parent.width
            text: hsk.actionStatus !== "" ? hsk.actionStatus : hsk.lastError
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
                width: parent.width
                text: {
                  if (root.looksLikePermissions)
                    return "Configuring the mouse uses HID feature reports, and those need "
                         + "read-write access to /dev/hidraw*, which is root-only by default. "
                         + "Install the udev rule, then unplug and replug the mouse or its dongle."
                  if (root.hasError) return hsk.lastError
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
                width: parent.width
                visible: root.needsSetup || root.looksLikePermissions
                text: root.looksLikePermissions
                  ? "~/.config/omarchy/plugins/io.github.keasbeexd.hsk/install.sh --udev"
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
            visible: root.dpiStages.length > 0
            foreground: root.foreground
          }

          Column {
            visible: root.dpiStages.length > 0
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: "DPI"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Column {
              id: stageColumn
              width: parent.width
              spacing: Style.space(4)

              Repeater {
                model: root.stageCount
                StageRow {
                  required property int index
                  width: stageColumn.width
                  stage: index + 1
                }
              }
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
                text: "Polling rate"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              // ButtonGroup is a Row -- it sizes to its chips, so no explicit
              // width here or the group stretches past its content.
              ButtonGroup {
                options: Model.pollingOptions(hsk.value("pollingRate"))
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
        }
      }
    }
  }

  // --- components ---------------------------------------------------------

  // A repeat-capable step button. Holding it repeats, because stepping from
  // 400 to 3200 in fifties is 56 clicks otherwise -- and repeats cost nothing,
  // since the service coalesces them into a single write.
  component StepButton: Rectangle {
    id: stepButton
    property string glyph: ""
    property bool enabled: true
    signal stepped()
    signal hovered()

    Layout.preferredWidth: Style.space(20)
    Layout.preferredHeight: Style.space(20)
    Layout.alignment: Qt.AlignVCenter
    radius: Style.cornerRadius > 0 ? Style.space(4) : 0
    color: stepMouse.pressed && stepButton.enabled
      ? root.selectedFill
      : (stepMouse.containsMouse && stepButton.enabled ? root.hoverFill : "transparent")
    border.width: 1
    border.color: stepButton.enabled ? root.dim : "transparent"
    opacity: stepButton.enabled ? 1 : 0.35

    Text {
      anchors.centerIn: parent
      text: stepButton.glyph
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    MouseArea {
      id: stepMouse
      anchors.fill: parent
      hoverEnabled: true
      enabled: stepButton.enabled
      cursorShape: Qt.PointingHandCursor
      onEntered: stepButton.hovered()
      onPressed: {
        stepButton.stepped()
        repeatDelay.restart()
      }
      onReleased: { repeatDelay.stop(); repeatRun.stop() }
      onCanceled: { repeatDelay.stop(); repeatRun.stop() }
    }

    Timer {
      id: repeatDelay
      interval: 400
      onTriggered: repeatRun.start()
    }

    Timer {
      id: repeatRun
      interval: 60
      repeat: true
      onTriggered: {
        if (!stepButton.enabled) { stop(); return }
        stepButton.stepped()
      }
    }
  }

  component StageRow: CursorSurface {
    id: stageRow
    property int stage: 0

    // Read straight from the service rather than being handed a snapshot, so
    // the row survives a refresh instead of being torn down and rebuilt.
    readonly property int dpi: {
      var v = hsk.value("dpiStage" + stageRow.stage)
      return v === undefined || v === null ? 0 : v
    }
    readonly property int dpiY: {
      var v = hsk.value("dpiStage" + stageRow.stage + "Y")
      return v === undefined || v === null ? stageRow.dpi : v
    }
    readonly property string swatch: {
      var v = hsk.value("dpiStage" + stageRow.stage + "Color")
      return v === undefined || v === null ? "" : String(v)
    }
    readonly property bool isActive: hsk.value("activeDpiStage") === stageRow.stage
    readonly property bool split: stageRow.dpiY !== stageRow.dpi

    readonly property bool rowHasCursor: {
      var row = root.currentRow()
      return root.cursorActive && row && row.kind === "dpiStage" && row.stage === stageRow.stage
    }

    // One click, one step. The service holds the value for a moment and writes
    // once, so holding down "+" costs one exchange rather than one per click.
    function nudge(delta) {
      if (!hsk.canWrite("dpiStage" + stageRow.stage)) return
      var wanted = Model.clampDpi(stageRow.dpi + delta)
      if (wanted === stageRow.dpi) return
      hsk.setSoon("dpiStage" + stageRow.stage, wanted)
      root.setCursor(root.rowIndexOf("dpiStage", stageRow.stage))
    }

    hasCursor: rowHasCursor
    current: isActive
    foreground: root.foreground
    fill: root.hoverFill
    currentFill: root.selectedFill
    implicitHeight: stageInner.implicitHeight + Style.spacing.lg

    RowLayout {
      id: stageInner
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(6)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(8)

      // Selector. Filled when this is the stage the mouse is currently using.
      Text {
        text: stageRow.isActive ? "\uf111" : "\uf10c"
        color: stageRow.isActive ? root.foreground : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        Layout.alignment: Qt.AlignVCenter
        Layout.preferredWidth: Style.space(14)
        horizontalAlignment: Text.AlignHCenter

        MouseArea {
          anchors.fill: parent
          anchors.margins: -Style.space(4)
          hoverEnabled: true
          // Refuse the click rather than queue it. Selecting a stage twice in a
          // row is not something anyone means to do, and letting it queue is
          // what produced "I have to wait a moment before the next click".
          enabled: !hsk.busy
          cursorShape: hsk.busy ? Qt.BusyCursor : Qt.PointingHandCursor
          onEntered: root.setCursor(root.rowIndexOf("dpiStage", stageRow.stage))
          onClicked: hsk.setDpiStage(stageRow.stage)
        }
      }

      Text {
        text: stageRow.stage
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: stageRow.isActive
        Layout.alignment: Qt.AlignVCenter
        Layout.preferredWidth: Style.space(10)
      }

      Item { Layout.fillWidth: true }

      // A stepper rather than a slider. A slider asks you to land a drag on a
      // 50-DPI boundary, and every intermediate position is a value you did not
      // want -- so it either writes constantly or it writes on release and the
      // number lies until you let go. Two buttons and a number cannot be
      // misread, and a click is exactly one step.
      StepButton {
        glyph: ""
        enabled: hsk.canWrite("dpiStage" + stageRow.stage)
                 && stageRow.dpi > Model.DPI_MIN
        onStepped: stageRow.nudge(-Model.DPI_STEP)
        onHovered: root.setCursor(root.rowIndexOf("dpiStage", stageRow.stage))
      }

      Text {
        text: stageRow.dpi
        color: stageRow.split ? root.urgent : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignHCenter
        Layout.preferredWidth: Style.space(40)
        Layout.alignment: Qt.AlignVCenter
      }

      StepButton {
        glyph: ""
        enabled: hsk.canWrite("dpiStage" + stageRow.stage)
                 && stageRow.dpi < Model.DPI_MAX
        onStepped: stageRow.nudge(Model.DPI_STEP)
        onHovered: root.setCursor(root.rowIndexOf("dpiStage", stageRow.stage))
      }

      // Colour swatch. Clicking steps through the firmware's stage palette.
      Rectangle {
        id: swatchBox
        visible: stageRow.swatch !== "" && hsk.canWrite("dpiStage" + stageRow.stage + "Color")
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
          onEntered: root.setCursor(root.rowIndexOf("dpiStage", stageRow.stage))
          onClicked: hsk.set(
            "dpiStage" + stageRow.stage + "Color",
            Model.nextStageColor(stageRow.swatch)
          )
        }

        PanelToolTip {
          visible: swatchMouse.containsMouse
          text: "Cycle stage colour"
          fontFamily: root.fontFamily
        }
      }
    }

    MouseArea {
      id: stageMouse
      anchors.fill: parent
      acceptedButtons: Qt.LeftButton
      hoverEnabled: true
      // Sits behind the slider and swatch, so it only catches the row's margins
      // and the stage label -- but that is most of the row, and clicking any of
      // it should select the stage rather than only the small radio glyph.
      z: -1
      enabled: !hsk.busy
      cursorShape: hsk.busy ? Qt.BusyCursor : Qt.PointingHandCursor
      onContainsMouseChanged: if (containsMouse) root.setCursor(root.rowIndexOf("dpiStage", stageRow.stage))
      onClicked: hsk.setDpiStage(stageRow.stage)
    }

    PanelToolTip {
      visible: stageRow.split && stageMouse.containsMouse
      text: "X and Y axes differ on this stage"
      fontFamily: root.fontFamily
    }
  }
}
