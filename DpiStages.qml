import QtQuick
import qs.Commons
import qs.Ui
import "Model.js" as Model

// The DPI section: stage chips, then the selected stage's DPI and LED
// color. Clicking a chip selects it for editing, and also makes it the live
// stage on mice whose driver allows switching (cap.switchable). The live
// stage is filled; the selected one is bordered.
Column {
  id: root

  required property var cap
  required property var value
  property bool busy: false
  property int selectedIndex: 0
  // Which of this section's rows holds the panel cursor:
  // "" | "stages" | "stageDpi" | "stageColor"
  property string cursorRow: ""
  // The settings grid, shared with SettingRow so these rows line up with it.
  property real labelWidth: width * 0.4
  property real unitWidth: Style.space(28)
  property color foreground
  property color accent
  property color fill
  property color currentFill
  property string fontFamily

  signal writeRequested(var value)
  signal selected(int index)
  signal rowHovered(string row)

  readonly property var stages: value ? value.stages : []
  readonly property int active: value ? value.active : -1
  readonly property var selectedStage: selectedIndex >= 0 && selectedIndex < stages.length ? stages[selectedIndex] : null
  readonly property color dim: Qt.darker(foreground, 1.4)
  // CursorSurface paints only the cursor, never raw hover, so the item under
  // the mouse is tracked here and given the cursor; -1 means none.
  property int hoveredChip: -1
  property int hoveredSwatch: -1

  spacing: Style.space(6)
  opacity: busy ? 0.7 : 1.0

  function pick(index) {
    selected(index)
    if (cap.writable && cap.switchable && index !== active) writeRequested(Model.withActive(value, index))
  }

  function setDpi(dpi) {
    if (!selectedStage) return
    var clean = Model.clampDpi(cap, dpi)
    if (clean !== selectedStage.dpi) writeRequested(Model.withStage(value, selectedIndex, { dpi: clean }))
  }

  function setColor(color) {
    if (selectedStage && cap.hasColors && color !== selectedStage.color)
      writeRequested(Model.withStage(value, selectedIndex, { color: color }))
  }

  function cycleColor(delta) {
    if (selectedStage) setColor(Model.cycleColor(selectedStage.color, delta))
  }

  Item {
    width: parent.width
    implicitHeight: header.implicitHeight

    PanelSectionHeader {
      id: header
      anchors.left: parent.left
      text: "DPI"
      foreground: root.foreground
      fontFamily: root.fontFamily
    }

    Text {
      textFormat: Text.PlainText
      anchors.right: parent.right
      anchors.verticalCenter: header.verticalCenter
      // Guarded: switching mice briefly pairs the new active index with the
      // old stage list.
      text: root.active >= 0 && root.active < root.stages.length
            ? "LIVE  " + root.stages[root.active].dpi : ""
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: true
      font.letterSpacing: 1.2
    }
  }

  Grid {
    id: chips
    width: parent.width
    columns: 4
    spacing: Style.space(6)

    Repeater {
      model: root.stages

      CursorSurface {
        id: chip
        required property var modelData
        required property int index
        readonly property bool isLive: index === root.active
        readonly property bool isSelected: index === root.selectedIndex

        width: (chips.width - chips.spacing * (chips.columns - 1)) / chips.columns
        implicitHeight: Style.space(28)
        current: isLive
        bordered: isSelected
        hasCursor: root.hoveredChip === index || (root.hoveredChip < 0 && root.cursorRow === "stages" && isSelected)
        foreground: root.foreground
        fill: root.fill
        currentFill: root.currentFill

        Row {
          anchors.centerIn: parent
          spacing: Style.space(6)

          Rectangle {
            visible: chip.modelData.color !== null
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(8)
            height: width
            radius: Style.cornerRadius > 0 ? width / 2 : 0
            color: chip.modelData.color || "transparent"
            border.color: Qt.darker(root.foreground, 1.4)
            border.width: 1
          }

          Text {
            textFormat: Text.PlainText
            anchors.verticalCenter: parent.verticalCenter
            text: chip.modelData.dpi
            color: chip.isLive || chip.isSelected ? root.foreground : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.bold: chip.isLive
          }
        }

        MouseArea {
          id: chipMouse
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onContainsMouseChanged: {
            if (containsMouse) {
              root.hoveredChip = chip.index
              root.rowHovered("stages")
            } else if (root.hoveredChip === chip.index) {
              root.hoveredChip = -1
            }
          }
          onClicked: root.pick(chip.index)
        }

        PanelToolTip {
          visible: chipMouse.containsMouse && !root.cap.switchable && root.cap.writable
          text: "Edit this stage. Switch stages with the mouse's DPI button."
          fontFamily: root.fontFamily
        }
      }
    }
  }

  // ---------- the selected stage, on the settings grid ----------
  Item {
    visible: root.cap.writable && root.selectedStage !== null
    width: parent.width
    implicitHeight: Style.spacing.controlHeight + Style.space(2)

    Text {
      textFormat: Text.PlainText
      x: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      text: "Stage " + (root.selectedIndex + 1)
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
    }

    NumberField {
      id: dpiField
      x: root.labelWidth
      anchors.verticalCenter: parent.verticalCenter
      fieldWidth: root.width - root.labelWidth - Style.space(8) - root.unitWidth
      value: root.selectedStage ? root.selectedStage.dpi : 0
      from: root.cap.minimum !== null ? root.cap.minimum : 0
      to: root.cap.maximum !== null ? root.cap.maximum : 100000
      stepSize: root.cap.step
      hasCursor: root.cursorRow === "stageDpi"
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      onHovered: function(on) { if (on) root.rowHovered("stageDpi") }
      onModified: function(v) { dpiDebounce.target = v; dpiDebounce.restart() }
    }

    Text {
      textFormat: Text.PlainText
      anchors.right: parent.right
      anchors.rightMargin: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      width: root.unitWidth
      horizontalAlignment: Text.AlignRight
      text: "DPI"
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }

  Item {
    visible: root.cap.writable && root.cap.hasColors && root.selectedStage !== null
    width: parent.width
    implicitHeight: Style.spacing.controlHeight + Style.space(2)

    Text {
      textFormat: Text.PlainText
      x: Style.space(8)
      anchors.verticalCenter: parent.verticalCenter
      text: "LED"
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.body
    }

    // Spread across the control column, so its edges match the rows below.
    Row {
      id: swatches
      x: root.labelWidth
      width: root.width - root.labelWidth - Style.space(8)
      height: parent.height
      spacing: Style.space(3)

      Repeater {
        model: Model.STAGE_COLORS

        CursorSurface {
          id: swatch
          required property var modelData
          required property int index
          readonly property bool isCurrent: root.selectedStage !== null && root.selectedStage.color === modelData

          width: (swatches.width - swatches.spacing * (Model.STAGE_COLORS.length - 1)) / Model.STAGE_COLORS.length
          height: parent.height
          bordered: isCurrent
          hasCursor: root.hoveredSwatch === index || (root.hoveredSwatch < 0 && root.cursorRow === "stageColor" && isCurrent)
          foreground: root.foreground
          fill: root.fill
          currentFill: root.currentFill

          Rectangle {
            anchors.fill: parent
            anchors.margins: Style.space(5)
            radius: Style.cornerRadius > 0 ? Style.space(3) : 0
            color: swatch.modelData
            border.color: Qt.darker(root.foreground, 1.4)
            border.width: 1
          }

          MouseArea {
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onContainsMouseChanged: {
              if (containsMouse) {
                root.hoveredSwatch = swatch.index
                root.rowHovered("stageColor")
              } else if (root.hoveredSwatch === swatch.index) {
                root.hoveredSwatch = -1
              }
            }
            onClicked: root.setColor(swatch.modelData)
          }
        }
      }
    }
  }

  Timer {
    id: dpiDebounce
    property int target: 0
    interval: 500
    onTriggered: root.setDpi(target)
  }
}
