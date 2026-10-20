import QtQuick
import qs.Commons
import qs.Ui
import "Model.js" as Model

// One setting on the panel's two-column grid: the label in the left column
// (labelWidth), the control in the right. Every control ends at the same
// right edge; choices and number fields fill the control column, and a
// number field leaves a unitWidth slot for its unit so fields line up.
// A setting not unlocked for writing shows its value with a lock instead.
// Only the control reacts to the mouse and shows the panel cursor; the row
// and its label never highlight.
Item {
  id: root

  required property var cap
  property bool hasCursor: false
  property var value
  property bool busy: false
  property real labelWidth: width * 0.4
  property real unitWidth: Style.space(28)
  property color foreground
  property color accent
  property string fontFamily

  signal changeRequested(var value)
  signal rowHovered()

  readonly property bool editable: cap.writable && !busy
  readonly property real pad: Style.space(8)
  readonly property real controlWidth: width - labelWidth - pad

  implicitHeight: Style.spacing.controlHeight + Style.space(2)

  Text {
    textFormat: Text.PlainText
    x: root.pad
    width: root.labelWidth - root.pad * 2
    anchors.verticalCenter: parent.verticalCenter
    text: root.cap.label
    color: root.cap.writable ? root.foreground : Qt.darker(root.foreground, 1.4)
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    elide: Text.ElideRight
  }

  Item {
    id: controlColumn
    x: root.labelWidth
    width: root.controlWidth
    height: parent.height
    opacity: root.busy ? 0.5 : 1.0

    ToggleSwitch {
      visible: root.cap.writable && root.cap.kind === "toggle"
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      checked: root.value === true
      interactive: root.editable
      hasCursor: root.hasCursor
      foreground: root.foreground
      accent: root.accent
      onToggled: root.changeRequested(!(root.value === true))
      onHovered: function(on) { if (on) root.rowHovered() }
    }

    // A segmented control spanning the whole column, one equal segment per
    // option, however many the capability declares.
    Row {
      id: segments
      visible: root.cap.writable && root.cap.kind === "enum"
      anchors.fill: parent
      anchors.topMargin: Style.space(1)
      anchors.bottomMargin: Style.space(1)
      spacing: Style.space(3)

      Repeater {
        model: root.cap.kind === "enum" ? root.cap.options : []

        Button {
          required property var modelData
          width: (segments.width - segments.spacing * (root.cap.options.length - 1)) / root.cap.options.length
          height: segments.height
          horizontalPadding: Style.space(2)
          text: Model.optionLabel(root.cap, modelData)
          selected: modelData === root.value
          hasCursor: root.hasCursor && modelData === root.value
          bordered: true
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          onClicked: if (root.editable && modelData !== root.value) root.changeRequested(modelData)
          onHovered: function(on) { if (on) root.rowHovered() }
        }
      }
    }

    NumberField {
      id: numberField
      visible: root.cap.writable && root.cap.kind === "duration"
      anchors.left: parent.left
      anchors.verticalCenter: parent.verticalCenter
      fieldWidth: parent.width - root.unitWidth
      value: typeof root.value === "number" ? root.value : 0
      from: root.cap.minimum !== null ? root.cap.minimum : 0
      to: root.cap.maximum !== null ? root.cap.maximum : 100000
      stepSize: root.cap.step
      hasCursor: root.hasCursor
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      onHovered: function(on) { if (on) root.rowHovered() }
      onModified: function(v) { debounce.target = v; debounce.restart() }
    }

    Text {
      textFormat: Text.PlainText
      visible: numberField.visible
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      width: root.unitWidth
      horizontalAlignment: Text.AlignRight
      text: root.cap.unit
      color: Qt.darker(root.foreground, 1.4)
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Row {
      visible: !root.cap.writable
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      spacing: Style.space(6)

      Text {
        textFormat: Text.PlainText
        visible: root.cap.kind !== "info"
        anchors.verticalCenter: parent.verticalCenter
        text: "󰌾"
        color: Qt.darker(root.foreground, 1.4)
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      Text {
        textFormat: Text.PlainText
        anchors.verticalCenter: parent.verticalCenter
        text: Model.valueText(root.cap, root.value)
        color: Qt.darker(root.foreground, 1.2)
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }
    }
  }

  Timer {
    id: debounce
    property int target: 0
    interval: 500
    onTriggered: if (root.editable && target !== root.value) root.changeRequested(target)
  }
}
