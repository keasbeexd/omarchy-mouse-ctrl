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
  moduleName: "io.github.keasbee.hsk"
  ipcTarget: "io.github.keasbee.hsk"
  manageIpc: false

  property int cursorIndex: 0
  property bool cursorActive: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color hoverFill: bar ? Style.hoverFillFor(bar.foreground, Color.accent) : "transparent"
  readonly property color selectedFill: bar ? Style.selectedFillFor(bar.foreground, Color.accent) : "transparent"

  readonly property var rows: mouse.rows
  readonly property var dpiStages: Model.dpiStages(mouse.effectiveValues)
  readonly property bool needsSetup: mouse.state === "undiscovered"
  readonly property bool hasError: mouse.state === "error"

  readonly property color barIconColor: mouse.lowBattery
    ? root.urgent
    : (mouse.ready ? barForeground : Qt.darker(barForeground, 1.55))

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Service {
    id: mouse
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
    if (row.kind === "pollingRate") {
      var options = Model.POLLING_RATES
      var current = mouse.value("pollingRate")
      var index = options.indexOf(current)
      if (index < 0) index = 0
      var next = Math.max(0, Math.min(options.length - 1, index + direction))
      if (options[next] !== current) mouse.set("pollingRate", options[next])
    } else if (row.kind === "liftOffDistance") {
      mouse.set("liftOffDistance", mouse.value("liftOffDistance") === "1mm" ? "2mm" : "1mm")
    } else if (row.kind === "toggle") {
      mouse.toggle(row.field)
    }
  }

  function activateCursor() {
    var row = currentRow()
    if (!row) return
    if (row.kind === "dpiStage") mouse.setDpiStage(row.stage)
    else if (row.kind === "toggle") mouse.toggle(row.field)
    else adjustCurrent(1)
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
    if (!row) return
    if (row.kind === "dpiStage" && stageColumn && row.stage - 1 < stageColumn.children.length) {
      scrollItemIntoView(stageColumn.children[row.stage - 1])
    }
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
    mouse.refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Connections {
    target: mouse
    function onChanged() { root.clampCursor() }
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { mouse.refresh(); return "ok" }
    function status(): string { return mouse.summary }
    function cycleDpi(): string { mouse.cycleDpiStage(); return "ok" }
    function setDpiStage(stage: string): string {
      mouse.setDpiStage(parseInt(stage, 10))
      return "ok"
    }
    function setPollingRate(rate: string): string {
      mouse.set("pollingRate", parseInt(rate, 10))
      return "ok"
    }
  }

  // --- bar item -----------------------------------------------------------

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: mouse.barText
    slotSize: mouse.barText !== "" ? Style.bar.statusSlot : Style.bar.iconSlot
    active: mouse.lowBattery
    tooltipText: mouse.model + " — " + mouse.summary
    iconComponent: Component {
      Item {
        Text {
          anchors.centerIn: parent
          text: mouse.ready
            ? Model.batteryGlyph(mouse.value("batteryPercent"), mouse.value("charging") === true)
            : "󰍽"
          color: root.barIconColor
          font.family: root.fontFamily
          font.pixelSize: Style.bar.iconFont
          opacity: mouse.ready ? 1.0 : 0.55
        }
      }
    }
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) mouse.cycleDpiStage()
      else if (buttonCode === Qt.MiddleButton) mouse.refresh()
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
        if (t === "r" || t === "R") mouse.refresh()
        else if (t === "d" || t === "D") mouse.cycleDpiStage()
        else if (t === "m" || t === "M") mouse.toggle("motionSync")
        else if (t >= "1" && t <= "6") mouse.setDpiStage(parseInt(t, 10))
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
            title: mouse.model
            meta: mouse.summary
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: mouse.ready ? 1.0 : 0.5
            iconComponent: Component {
              Text {
                text: mouse.ready
                  ? Model.batteryGlyph(mouse.value("batteryPercent"), mouse.value("charging") === true)
                  : "󰍽"
                color: mouse.lowBattery ? root.urgent : root.foreground
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
                onClicked: mouse.refresh()
              }
            }
          }

          Text {
            visible: mouse.actionStatus !== "" || (mouse.lastError !== "" && !root.needsSetup)
            width: parent.width
            text: mouse.actionStatus !== "" ? mouse.actionStatus : mouse.lastError
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
                  ? (mouse.detected ? "Mouse found, protocol not mapped" : "Mouse not detected")
                  : "hskctl unavailable"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
                wrapMode: Text.WordWrap
              }

              Text {
                width: parent.width
                text: {
                  if (root.hasError) return mouse.lastError
                  if (!mouse.detected) return "Plug in the mouse or its 2.4 GHz dongle, then refresh."
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
                visible: root.needsSetup
                text: "hskctl probe"
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
              spacing: Style.space(6)

              Repeater {
                model: root.dpiStages
                StageRow {
                  required property var modelData
                  width: stageColumn.width
                  stage: modelData.stage
                  dpi: modelData.dpi
                  isActive: modelData.active
                }
              }
            }
          }

          // --- performance ------------------------------------------------

          PanelSeparator {
            visible: mouse.canWrite("pollingRate") || mouse.canWrite("liftOffDistance")
            foreground: root.foreground
          }

          Column {
            visible: mouse.canWrite("pollingRate") || mouse.canWrite("liftOffDistance")
            width: parent.width
            spacing: Style.space(10)

            PanelSectionHeader {
              text: "PERFORMANCE"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Column {
              visible: mouse.canWrite("pollingRate")
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
                options: Model.pollingOptions(mouse.value("pollingRate"))
                value: String(mouse.value("pollingRate"))
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                cursorIndex: root.cursorActive && root.currentRow() && root.currentRow().kind === "pollingRate" ? 0 : -1
                onChanged: function(v) { mouse.set("pollingRate", parseInt(v, 10)) }
                onHovered: function(index, on) {
                  if (on) root.setCursor(root.rowIndexOf("pollingRate", null))
                }
              }
            }

            Column {
              visible: mouse.canWrite("liftOffDistance")
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
                value: String(mouse.value("liftOffDistance"))
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                onChanged: function(v) { mouse.set("liftOffDistance", v) }
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
              readonly property bool hasAny: mouse.canWrite("motionSync")
                || mouse.canWrite("angleSnap")
                || mouse.canWrite("rippleControl")

              Repeater {
                model: ["motionSync", "angleSnap", "rippleControl"]
                Toggle {
                  required property var modelData
                  visible: mouse.canWrite(modelData)
                  width: toggleColumn.width
                  label: Model.toggleLabel(modelData)
                  description: Model.toggleDescription(modelData)
                  checked: mouse.value(modelData) === true
                  foreground: root.foreground
                  accent: Color.accent
                  fontFamily: root.fontFamily
                  hasCursor: {
                    var row = root.currentRow()
                    return root.cursorActive && row && row.kind === "toggle" && row.field === modelData
                  }
                  onClicked: mouse.toggle(modelData)
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

  component StageRow: CursorSurface {
    id: stageRow
    property int stage: 0
    property int dpi: 0
    property bool isActive: false

    readonly property bool rowHasCursor: {
      var row = root.currentRow()
      return root.cursorActive && row && row.kind === "dpiStage" && row.stage === stageRow.stage
    }

    hasCursor: rowHasCursor
    current: isActive
    foreground: root.foreground
    fill: root.hoverFill
    currentFill: root.selectedFill
    implicitHeight: stageInner.implicitHeight + Style.spacing.xl

    Row {
      id: stageInner
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(6)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(8)

      Text {
        text: stageRow.isActive ? "󰄲" : "󰄱"
        color: stageRow.isActive ? root.foreground : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        width: Style.space(22)
        horizontalAlignment: Text.AlignHCenter
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        text: "Stage " + stageRow.stage
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: stageRow.isActive
        anchors.verticalCenter: parent.verticalCenter
        width: parent.width - Style.space(22) - Style.space(8) - dpiText.width - Style.space(8)
        elide: Text.ElideRight
      }

      Text {
        id: dpiText
        text: stageRow.dpi + " DPI"
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    MouseArea {
      id: stageMouse
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: root.setCursor(root.rowIndexOf("dpiStage", stageRow.stage))
      onClicked: mouse.setDpiStage(stageRow.stage)
    }

    PanelToolTip {
      visible: stageMouse.containsMouse && !stageRow.isActive
      text: "Switch to stage " + stageRow.stage
      fontFamily: root.fontFamily
    }
  }
}
