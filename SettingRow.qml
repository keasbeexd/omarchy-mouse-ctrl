import QtQuick
import qs.Commons
import qs.Ui
import "Model.js" as Model

// One setting: its label, and the control its capability kind calls for --
// a switch for toggles, a chip row for choices, a number field for
// durations. A setting that is not unlocked for writing shows its value with
// a lock instead of a control.
CursorSurface {
  id: root

  required property var cap
  property var value
  property bool busy: false
  property color foreground
  property color accent
  property string fontFamily

  signal changeRequested(var value)
  signal rowHovered()

  readonly property bool editable: cap.writable && !busy
  // A row of more than three choices does not fit beside its label, so it
  // drops onto its own line underneath.
  readonly property bool stacked: cap.writable && cap.kind === "enum" && cap.options.length > 3
  readonly property var chipOptions: cap.options.map(function(o) {
    return { value: String(o), label: Model.optionLabel(cap, o) }
  })

  implicitHeight: stacked
    ? label.implicitHeight + controls.implicitHeight + Style.space(18)
    : Math.max(Style.space(32), controls.implicitHeight + Style.space(6))
  foreground: root.foreground

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    acceptedButtons: Qt.NoButton
    onContainsMouseChanged: if (containsMouse && root.cap.writable) root.rowHovered()
  }

  Text {
    id: label
    textFormat: Text.PlainText
    anchors.left: parent.left
    anchors.leftMargin: Style.space(8)
    anchors.top: root.stacked ? parent.top : undefined
    anchors.topMargin: Style.space(6)
    anchors.verticalCenter: root.stacked ? undefined : parent.verticalCenter
    anchors.right: root.stacked ? parent.right : controls.left
    anchors.rightMargin: Style.space(8)
    text: root.cap.label
    color: root.cap.writable ? root.foreground : Qt.darker(root.foreground, 1.4)
    font.family: root.fontFamily
    font.pixelSize: Style.font.body
    elide: Text.ElideRight
  }

  // A Row lays out only its visible child, so exactly one control shows.
  Row {
    id: controls
    anchors.right: root.stacked ? undefined : parent.right
    anchors.rightMargin: Style.space(8)
    anchors.left: root.stacked ? parent.left : undefined
    anchors.leftMargin: Style.space(8)
    anchors.top: root.stacked ? label.bottom : undefined
    anchors.topMargin: Style.space(6)
    anchors.verticalCenter: root.stacked ? undefined : parent.verticalCenter
    opacity: root.busy ? 0.5 : 1.0

    ToggleSwitch {
      visible: root.cap.writable && root.cap.kind === "toggle"
      checked: root.value === true
      interactive: root.editable
      hasCursor: root.hasCursor
      foreground: root.foreground
      accent: root.accent
      onToggled: root.changeRequested(!(root.value === true))
      onHovered: function(on) { if (on) root.rowHovered() }
    }

    ButtonGroup {
      visible: root.cap.writable && root.cap.kind === "enum"
      options: root.chipOptions
      value: root.value === undefined ? "" : String(root.value)
      focusable: false
      cursorIndex: root.hasCursor ? selectedOptionIndex() : -1
      // The default is the theme's base background, which clashes with the
      // popup surface this panel sits on.
      background: "transparent"
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      fontSize: Style.font.bodySmall
      onChanged: function(v) {
        if (!root.editable) return
        for (var i = 0; i < root.cap.options.length; i++)
          if (String(root.cap.options[i]) === v) return root.changeRequested(root.cap.options[i])
      }
      onHovered: function(index, on) { if (on) root.rowHovered() }
    }

    NumberField {
      id: numberField
      visible: root.cap.writable && root.cap.kind === "duration"
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
      visible: numberField.visible && root.cap.unit !== ""
      anchors.verticalCenter: parent.verticalCenter
      leftPadding: Style.space(8)
      text: root.cap.unit
      color: Qt.darker(root.foreground, 1.4)
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Row {
      visible: !root.cap.writable
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
