import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// Mouse CTRL: battery in the bar; in the panel, the selected mouse's DPI
// stages and settings, drawn from whatever capabilities its driver reports.
// No mouse model or setting name appears in this file.
Panel {
  id: root
  moduleName: "keasbeexd.mousectrl"
  ipcTarget: "keasbeexd.mousectrl"
  // This panel owns the one IpcHandler the target allows, to add `setup`.
  manageIpc: false

  // Parameterless only: opening the panel or its setup changes nothing.
  // `omarchy-shell keasbeexd.mousectrl setup` can sit behind a keybinding.
  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function setup(): void {
      root.open()
      root.openWizard(backend.selectedKey)
    }
  }

  // Every color comes from the active Omarchy theme. Secondary text is
  // Qt.darker(foreground), matching the first-party panels.
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color accent: Commons.Color.accent
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color hoverFill: bar ? Style.hoverFillFor(bar.foreground, accent) : "transparent"
  readonly property color selectedFill: bar ? Style.selectedFillFor(bar.foreground, accent) : "transparent"

  readonly property var caps: backend.capabilities
  readonly property var stagesCap: Model.firstOfKind(caps, "dpiStages")
  readonly property bool ready: backend.status === "ready"
  readonly property bool genericView: backend.status === "generic" && backend.generic !== null
  readonly property string contributeUrl: "https://github.com/keasbeexd/omarchy-mouse-ctrl/blob/master/docs/contributing-a-family.md"
  // The settings grid every row shares: label column, then controls, with a
  // fixed slot for units at the right edge.
  readonly property real labelWidth: column.width * 0.4
  readonly property real unitWidth: Style.space(28)

  // --- keyboard cursor -----------------------------------------------------

  property bool cursorActive: false
  property int cursorIndex: 0
  property int selectedStage: 0
  readonly property var rows: ready ? Model.buildRows(caps, backend.devices.length)
                                    : (backend.devices.length > 1 ? [{ type: "device", capId: "" }] : [])
  readonly property var cursorRow: cursorActive && cursorIndex < rows.length ? rows[cursorIndex] : null

  function rowHasCursor(type, capId) {
    return cursorRow !== null && cursorRow.type === type && cursorRow.capId === capId
  }

  function hoverRow(type, capId) {
    var i = Model.rowIndex(rows, type, capId)
    if (i < 0) return
    cursorActive = true
    cursorIndex = i
  }

  // Scroll the panel so a row the keyboard cursor lands on is on screen.
  function ensureVisible(item) {
    var top = item.mapToItem(column, 0, 0).y
    var bottom = top + item.height
    if (top < flick.contentY) flick.contentY = top
    else if (bottom > flick.contentY + flick.height) flick.contentY = bottom - flick.height
  }

  function moveCursor(delta) {
    if (rows.length === 0) return
    cursorIndex = Math.max(0, Math.min(rows.length - 1, cursorIndex + delta))
  }

  function adjust(delta) {
    var row = cursorRow
    if (!row) return
    if (row.type === "device") return backend.cycleDevice()
    if (row.type === "stages") return selectStage(selectedStage + delta)
    if (row.type === "stageDpi") return stagesView.setDpi(stagesView.selectedStage.dpi + delta * stagesCap.step)
    if (row.type === "stageColor") return stagesView.cycleColor(delta)
    var cap = Model.findCap(caps, row.capId)
    var next = cap ? Model.steppedValue(cap, backend.values[cap.id], delta) : undefined
    if (next !== undefined) backend.write(cap.id, next)
  }

  function activate() {
    var row = cursorRow
    if (!row) return
    if (row.type === "device") return backend.cycleDevice()
    if (row.type === "stages") return stagesView.pick(selectedStage)
    if (row.type === "stageColor") return stagesView.cycleColor(1)
    var cap = Model.findCap(caps, row.capId)
    if (cap && cap.kind === "toggle") backend.write(cap.id, !backend.values[cap.id])
  }

  function selectStage(index) {
    var value = stagesCap ? backend.values[stagesCap.id] : null
    if (!value) return
    selectedStage = Math.max(0, Math.min(value.stages.length - 1, index))
  }

  onRowsChanged: if (cursorIndex >= rows.length) cursorIndex = Math.max(0, rows.length - 1)

  // The setup wizard replaces the panel's content while open; it is loaded
  // fresh each time, so a cancelled run leaves nothing behind.
  property bool wizardOpen: false
  property string wizardKey: ""
  // Handed to the wizard by this name: inside `Wizard { backend: backend }`
  // the right-hand side would resolve to the wizard's own property.
  readonly property var mouseBackend: backend

  function openWizard(key) {
    wizardKey = key || ""
    cursorActive = false
    wizardOpen = true
    flick.contentY = 0
  }

  onOpenedChanged: {
    if (!opened) { wizardOpen = false; return }
    cursorActive = false
    cursorIndex = 0
    backend.refresh()
  }

  Connections {
    target: backend
    // Start editing on the live stage whenever a mouse is (re)selected.
    function onSelectedKeyChanged() { root.selectedStage = -1 }
    function onValuesChanged() {
      var value = root.stagesCap ? backend.values[root.stagesCap.id] : null
      if (!value) return
      if (root.selectedStage < 0 || root.selectedStage >= value.stages.length) root.selectedStage = value.active
    }
  }

  Backend {
    id: backend
    settings: root.settings
    panelOpen: root.opened
  }

  // --- bar -----------------------------------------------------------------

  readonly property string barLabel: (bar && bar.vertical) || !backend.setting("showBatteryLabel", true) || !backend.battery
                                     ? "" : backend.battery.percent + "%"
  readonly property color barColor: backend.lowBattery ? urgent
                                  : (backend.battery && backend.battery.charging ? accent
                                  : (ready ? barForeground : Qt.darker(barForeground, 1.6)))

  TextMetrics {
    id: glyphMetrics
    font.family: root.fontFamily
    font.pixelSize: Style.bar.iconFont
    text: "󰍽"
  }

  TextMetrics {
    id: labelMetrics
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
    text: root.barLabel
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // BarIconButton draws no `text` once it has an iconComponent, so the
    // percentage lives inside the icon and the slot is sized to hold both.
    slotSize: Math.ceil(glyphMetrics.width)
      + (root.barLabel !== "" ? Style.space(3) + Math.ceil(labelMetrics.width) : 0)
      + Style.space(2)
    active: backend.lowBattery
    // Rendered by a host component in AutoText mode; the device name comes
    // from the mouse's firmware, so it goes through Model.plain.
    tooltipText: backend.device ? Model.plain(backend.device.name, 60) : "Mouse CTRL"
    iconComponent: Component {
      Item {
        Row {
          anchors.centerIn: parent
          spacing: root.barLabel !== "" ? Style.space(3) : 0

          Text {
            textFormat: Text.PlainText
            anchors.verticalCenter: parent.verticalCenter
            text: "󰍽"
            color: root.barColor
            font.family: root.fontFamily
            font.pixelSize: Style.bar.iconFont
          }

          Text {
            textFormat: Text.PlainText
            anchors.verticalCenter: parent.verticalCenter
            visible: root.barLabel !== ""
            text: root.barLabel
            color: root.barColor
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
        }
      }
    }
    onPressed: function(b) {
      if (b === Qt.MiddleButton) backend.refresh()
      else root.toggle()
    }
  }

  // --- panel ---------------------------------------------------------------

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(380))
    // Fitted to the screen; anything taller scrolls.
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (root.wizardOpen) return
        if (!root.cursorActive) { root.cursorActive = true; return }
        if (dy !== 0) root.moveCursor(dy)
        else if (dx !== 0) root.adjust(dx)
      }
      onActivateRequested: {
        if (root.wizardOpen) wizardLoader.item.primary()
        else if (root.cursorActive) root.activate()
      }
      onCloseRequested: root.wizardOpen ? root.wizardOpen = false : root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) { if (!root.wizardOpen && (t === "r" || t === "R")) backend.refresh() }

      Flickable {
        id: flick
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
          width: flick.width
          spacing: Style.space(10)

          Loader {
            id: wizardLoader
            active: root.wizardOpen
            visible: active
            width: parent.width
            sourceComponent: Component {
              Wizard {
                width: column.width
                backend: root.mouseBackend
                startKey: root.wizardKey
                foreground: root.foreground
                accent: root.accent
                urgent: root.urgent
                hoverFill: root.hoverFill
                selectedFill: root.selectedFill
                fontFamily: root.fontFamily
                labelWidth: root.labelWidth
                onClosed: root.wizardOpen = false
              }
            }
          }

          Column {
            id: mainView
            visible: !root.wizardOpen
            width: parent.width
            spacing: Style.space(10)

            // ---------- hero: which mouse, and its battery ----------
            Column {
              visible: backend.device !== null
              width: parent.width
              spacing: Style.space(10)

              Item {
                width: parent.width
                implicitHeight: Math.max(names.implicitHeight, switcher.implicitHeight)

                Column {
                  id: names
                  anchors.left: parent.left
                  anchors.right: setupButton.left
                  anchors.rightMargin: Style.space(10)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(2)

                  Text {
                    textFormat: Text.PlainText
                    width: parent.width
                    text: backend.device ? (backend.device.profile ? backend.device.profile.name : backend.device.name) : ""
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.title
                    font.bold: true
                    elide: Text.ElideRight
                  }

                  Text {
                    textFormat: Text.PlainText
                    width: parent.width
                    text: backend.device ? (backend.device.family ? backend.device.family.title : backend.device.vidpid).toUpperCase() : ""
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                    font.letterSpacing: 1.2
                    elide: Text.ElideRight
                  }
                }

                PanelActionButton {
                  id: setupButton
                  anchors.right: switcher.visible ? switcher.left : parent.right
                  anchors.rightMargin: switcher.visible ? Style.space(6) : 0
                  anchors.verticalCenter: parent.verticalCenter
                  iconText: "󰐕"
                  tooltipText: "Set up a mouse"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  onClicked: root.openWizard(backend.selectedKey)
                }

                // Cycles through connected mice; only there when there is a choice.
                CursorSurface {
                  id: switcher
                  visible: backend.devices.length > 1
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  implicitWidth: switcherText.implicitWidth + Style.space(20)
                  implicitHeight: switcherText.implicitHeight + Style.space(12)
                  bordered: true
                  hasCursor: root.rowHasCursor("device", "")
                  foreground: root.foreground
                  fill: root.hoverFill

                  Text {
                    id: switcherText
                    textFormat: Text.PlainText
                    anchors.centerIn: parent
                    text: {
                      for (var i = 0; i < backend.devices.length; i++)
                        if (backend.devices[i].key === backend.selectedKey) return (i + 1) + "/" + backend.devices.length + "  󰅀"
                      return backend.devices.length + "  󰅀"
                    }
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                  }

                  MouseArea {
                    id: switcherMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onContainsMouseChanged: if (containsMouse) root.hoverRow("device", "")
                    onClicked: backend.cycleDevice()
                  }

                  PanelToolTip {
                    visible: switcherMouse.containsMouse
                    text: "Next mouse"
                    fontFamily: root.fontFamily
                  }
                }
              }

              Item {
                visible: backend.battery !== null
                width: parent.width
                implicitHeight: percent.implicitHeight

                Text {
                  id: percent
                  textFormat: Text.PlainText
                  anchors.left: parent.left
                  text: backend.battery ? backend.battery.percent + "%" : ""
                  color: backend.lowBattery ? root.urgent : root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.display
                  font.bold: true
                }

                Column {
                  anchors.left: percent.right
                  anchors.leftMargin: Style.space(14)
                  anchors.right: parent.right
                  anchors.verticalCenter: percent.verticalCenter
                  spacing: Style.space(6)

                  Rectangle {
                    width: parent.width
                    height: Style.space(6)
                    radius: Style.cornerRadius > 0 ? height / 2 : 0
                    color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.18)

                    Rectangle {
                      width: backend.battery ? parent.width * backend.battery.percent / 100 : 0
                      height: parent.height
                      radius: parent.radius
                      color: backend.lowBattery ? root.urgent
                           : (backend.battery && backend.battery.charging ? root.accent : root.foreground)
                    }
                  }

                  Text {
                    textFormat: Text.PlainText
                    text: backend.battery && backend.battery.charging ? "CHARGING" : "BATTERY"
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: true
                    font.letterSpacing: 1.2
                  }
                }
              }
            }

            // ---------- why there is nothing to show ----------
            Column {
              visible: !root.ready && !root.genericView
              width: parent.width
              spacing: Style.space(8)

              Text {
                textFormat: Text.PlainText
                width: parent.width
                wrapMode: Text.WordWrap
                text: backend.status === "starting" ? "Looking for mice…" : backend.statusMessage
                color: backend.status === "error" ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
              }

              Text {
                textFormat: Text.PlainText
                width: parent.width
                wrapMode: Text.WordWrap
                visible: text !== ""
                text: {
                  var s = backend.status
                  if (s === "asleep") return "Move the mouse to wake it; the panel retries on its own."
                  if (s === "noAccess") return "Setup installs the access rule; it asks for your password once."
                  if (s === "noProfile") return "Setup reads the mouse, lets you confirm what it found and saves a profile."
                  if (s === "unsupported") return "Setup keeps it in the panel with what can be read without a driver."
                  if (s === "noDevice") return "Plug in a mouse or its receiver."
                  return ""
                }
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }

              Button {
                visible: ["noProfile", "noAccess", "unsupported"].indexOf(backend.status) >= 0
                text: "Set up this mouse"
                iconText: "󰐕"
                bordered: true
                foreground: root.foreground
                accent: root.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                onClicked: root.openWizard(backend.selectedKey)
              }
            }

            // ---------- a mouse with no driver: what can be seen anyway ----------
            Column {
              visible: root.genericView
              width: parent.width
              spacing: 0

              PanelSeparator {
                foreground: root.foreground
              }

              PanelSectionHeader {
                topPadding: Style.space(6)
                bottomPadding: Style.space(4)
                text: "READ-ONLY"
                foreground: root.foreground
                fontFamily: root.fontFamily
              }

              Repeater {
                model: root.genericView ? [
                  { label: "Connection", text: backend.generic.connection },
                  { label: "Buttons", text: backend.generic.buttons + " declared" },
                  { label: "Battery", text: backend.battery ? backend.battery.percent + "%" : "not reported" }
                ] : []
                Item {
                  required property var modelData
                  width: parent.width
                  implicitHeight: Style.spacing.controlHeight + Style.space(2)
                  Text {
                    textFormat: Text.PlainText
                    x: Style.space(8)
                    anchors.verticalCenter: parent.verticalCenter
                    text: modelData.label
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                  }
                  Text {
                    textFormat: Text.PlainText
                    anchors.right: parent.right
                    anchors.rightMargin: Style.space(8)
                    anchors.verticalCenter: parent.verticalCenter
                    text: modelData.text
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                  }
                }
              }

              Item {
                width: parent.width
                implicitHeight: Style.spacing.controlHeight + Style.space(2)
                Text {
                  textFormat: Text.PlainText
                  x: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  text: "Polling rate"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }
                Row {
                  anchors.right: parent.right
                  anchors.rightMargin: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(10)
                  Text {
                    textFormat: Text.PlainText
                    anchors.verticalCenter: parent.verticalCenter
                    text: backend.measuring ? "move the mouse…" : backend.measuredHz > 0 ? "≈ " + backend.measuredHz + " Hz" : "—"
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                  }
                  Button {
                    text: "Measure"
                    bordered: true
                    foreground: root.foreground
                    accent: root.accent
                    fontFamily: root.fontFamily
                    fontSize: Style.font.bodySmall
                    onClicked: backend.measure()
                  }
                }
              }

              // ratbagd's own readings, read-only; settings belong to Piper.
              Column {
                visible: root.genericView && backend.generic.ratbag !== null
                width: parent.width
                spacing: 0

                PanelSectionHeader {
                  topPadding: Style.space(6)
                  bottomPadding: Style.space(4)
                  text: "LIBRATBAG"
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                }

                Repeater {
                  model: root.genericView && backend.generic.ratbag ? [
                    { label: "Profile", text: backend.generic.ratbag.profile + " of " + backend.generic.ratbag.profiles },
                    { label: "Polling rate", text: backend.generic.ratbag.pollingRate !== null ? backend.generic.ratbag.pollingRate + " Hz" : "—" },
                    { label: "DPI stages", text: Model.ratbagStagesText(backend.generic.ratbag) },
                    { label: "Firmware", text: backend.generic.ratbag.firmware }
                  ] : []
                  Item {
                    required property var modelData
                    width: parent.width
                    implicitHeight: Style.spacing.controlHeight + Style.space(2)
                    Text {
                      textFormat: Text.PlainText
                      x: Style.space(8)
                      anchors.verticalCenter: parent.verticalCenter
                      text: modelData.label
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                    }
                    Text {
                      textFormat: Text.PlainText
                      anchors.right: parent.right
                      anchors.rightMargin: Style.space(8)
                      anchors.verticalCenter: parent.verticalCenter
                      text: modelData.text
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                    }
                  }
                }

                Item {
                  width: parent.width
                  implicitHeight: Style.spacing.controlHeight + Style.space(8)
                  Text {
                    textFormat: Text.PlainText
                    x: Style.space(8)
                    width: root.labelWidth - Style.space(16)
                    anchors.verticalCenter: parent.verticalCenter
                    wrapMode: Text.WordWrap
                    text: backend.generic && backend.generic.piper ? "Change settings" : "Change settings with Piper (not installed)"
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                  }
                  Button {
                    visible: backend.generic !== null && backend.generic.piper
                    anchors.right: parent.right
                    anchors.rightMargin: Style.space(8)
                    anchors.verticalCenter: parent.verticalCenter
                    text: "Open Piper"
                    iconText: "󰏌"
                    bordered: true
                    foreground: root.foreground
                    accent: root.accent
                    fontFamily: root.fontFamily
                    fontSize: Style.font.bodySmall
                    onClicked: Quickshell.execDetached(["/usr/bin/piper"])
                  }
                }

                PanelSeparator {
                  foreground: root.foreground
                }
              }

              Item {
                width: parent.width
                implicitHeight: Style.spacing.controlHeight + Style.space(8)
                Text {
                  textFormat: Text.PlainText
                  x: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  text: "Report"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }
                Row {
                  id: reportButtons
                  property bool copied: false
                  anchors.right: parent.right
                  anchors.rightMargin: Style.space(8)
                  anchors.verticalCenter: parent.verticalCenter
                  spacing: Style.space(6)
                  Button {
                    text: reportButtons.copied ? "Copied" : "Copy report"
                    iconText: "󰆏"
                    bordered: true
                    foreground: root.foreground
                    accent: root.accent
                    fontFamily: root.fontFamily
                    fontSize: Style.font.bodySmall
                    onClicked: {
                      Quickshell.clipboardText = backend.generic.report
                      reportButtons.copied = true
                    }
                  }
                  Button {
                    text: "Add support"
                    iconText: "󰏌"
                    tooltipText: "How to add a driver for this mouse"
                    bordered: true
                    foreground: root.foreground
                    accent: root.accent
                    fontFamily: root.fontFamily
                    fontSize: Style.font.bodySmall
                    onClicked: Qt.openUrlExternally(root.contributeUrl)
                  }
                }
              }
            }

            // ---------- DPI ----------
            PanelSeparator {
              visible: stagesView.visible
              foreground: root.foreground
            }

            DpiStages {
              id: stagesView
              visible: root.ready && root.stagesCap !== null && backend.values[root.stagesCap.id] !== undefined
              width: parent.width
              cap: root.stagesCap || ({ writable: false, switchable: false, hasColors: false, step: 1, minimum: null, maximum: null })
              value: root.stagesCap ? (backend.values[root.stagesCap.id] || null) : null
              selectedIndex: root.selectedStage
              busy: root.stagesCap !== null && backend.pending[root.stagesCap.id] === true
              cursorRow: root.cursorRow && root.stagesCap && root.cursorRow.capId === root.stagesCap.id ? root.cursorRow.type : ""
              foreground: root.foreground
              accent: root.accent
              fill: root.hoverFill
              currentFill: root.selectedFill
              fontFamily: root.fontFamily
              labelWidth: root.labelWidth
              unitWidth: root.unitWidth
              onSelected: function(index) { root.selectStage(index) }
              onWriteRequested: function(value) { backend.write(root.stagesCap.id, value) }
              onRowHovered: function(row) { root.hoverRow(row, root.stagesCap.id) }
              onCursorRowChanged: if (cursorRow !== "") root.ensureVisible(stagesView)
            }

            // ---------- settings, one titled section each ----------
            Repeater {
              model: root.ready ? Model.settingSections(root.caps) : []

              Column {
                id: section
                required property var modelData
                width: column.width
                spacing: 0

                PanelSeparator {
                  foreground: root.foreground
                }

                PanelSectionHeader {
                  topPadding: Style.space(6)
                  bottomPadding: Style.space(4)
                  text: section.modelData.title
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                }

                Repeater {
                  model: section.modelData.caps

                  SettingRow {
                    required property var modelData
                    width: section.width
                    cap: modelData
                    value: backend.values[modelData.id]
                    busy: backend.pending[modelData.id] === true
                    hasCursor: root.rowHasCursor("setting", modelData.id)
                    foreground: root.foreground
                    accent: root.accent
                    fontFamily: root.fontFamily
                    labelWidth: root.labelWidth
                    unitWidth: root.unitWidth
                    onChangeRequested: function(value) { backend.write(modelData.id, value) }
                    onRowHovered: root.hoverRow("setting", modelData.id)
                    onHasCursorChanged: if (hasCursor) root.ensureVisible(this)
                  }
                }
              }
            }

            // ---------- footer ----------
            Text {
              readonly property string facts: root.ready ? Model.footerText(root.caps, backend.values) : ""
              textFormat: Text.PlainText
              visible: facts !== ""
              width: parent.width
              topPadding: Style.space(4)
              wrapMode: Text.WordWrap
              text: facts
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Text {
              textFormat: Text.PlainText
              visible: root.rows.length > 0
              width: parent.width
              wrapMode: Text.WordWrap
              text: "j/k move  ·  h/l adjust  ·  enter select  ·  r refresh"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Text {
              textFormat: Text.PlainText
              visible: backend.actionMessage !== ""
              width: parent.width
              wrapMode: Text.WordWrap
              text: backend.actionMessage
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Repeater {
              model: backend.profileProblems
              Text {
                required property var modelData
                textFormat: Text.PlainText
                width: column.width
                wrapMode: Text.WordWrap
                text: "Profile skipped: " + modelData
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }
        }
      }
    }
  }
}
