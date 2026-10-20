import QtQuick
import qs.Commons
import qs.Ui
import "Model.js" as Model

// The in-panel setup wizard: pick a mouse, grant access, confirm what the
// driver reads, unlock writes, save a profile. Unknown mice skip the two
// middle steps and save a read-only "generic" profile. A re-run starts from
// the mouse's existing profile.
Column {
  id: root

  required property var backend
  property string startKey: ""
  property color foreground
  property color accent
  property color urgent
  property color hoverFill
  property color selectedFill
  property string fontFamily
  property real labelWidth: width * 0.4

  signal closed()

  readonly property color dim: Qt.darker(foreground, 1.4)

  property string step: "detect"
  property string deviceKey: ""
  readonly property var device: {
    var list = backend.devices
    for (var i = 0; i < list.length; i++) if (list[i].key === deviceKey) return list[i]
    return null
  }
  readonly property bool supported: device !== null && device.family !== null
  readonly property var steps: supported ? ["detect", "access", "confirm", "writes", "save"] : ["detect", "access", "save"]
  readonly property var stepTitles: ({ detect: "Detect", access: "Access", confirm: "Confirm", writes: "Writes", save: "Save" })
  readonly property int stepIndex: steps.indexOf(step)

  property var caps: []
  property var values: ({})
  property var existing: null
  // group id -> true (matches) | false (wrong)
  property var answers: ({})
  // capId -> "running" | "passed" | "failed"
  property var tests: ({})
  // group id -> true when hidden from the panel
  property var hidden: ({})
  property string profileName: ""
  property string ruleText: ""
  property bool busy: false
  property string message: ""

  readonly property var groups: Model.confirmGroups(caps, values)
  readonly property var rejected: {
    var out = {}
    for (var g in answers) if (answers[g] === false) out[g] = true
    return out
  }
  readonly property var writableCaps: caps.filter(function(c) {
    return !c.readOnly && c.kind !== "info" && c.kind !== "battery" && rejected[c.group] !== true
  })
  readonly property var shownSections: Model.GROUPS.slice(0, Model.GROUPS.length - 1).filter(function(g) {
    return rejected[g] !== true && caps.some(function(c) { return c.group === g })
  })
  readonly property bool allAnswered: groups.every(function(g) { return answers[g.id] === true || answers[g.id] === false })

  spacing: Style.space(12)

  function setPart(name, key, value) {
    var next = Object.assign({}, root[name])
    next[key] = value
    root[name] = next
  }

  function fail(reply) {
    busy = false
    message = Model.plain(reply.error || "Something went wrong", 240)
  }

  function go(next) {
    message = ""
    step = next
    if (next === "access") enterAccess()
    else if (next === "confirm") loadReadings()
    else if (next === "save" && profileName === "")
      profileName = existing ? existing.name : device.profile ? device.profile.name : Model.plain(device.name, 40)
  }

  function forward() { if (stepIndex < steps.length - 1) go(steps[stepIndex + 1]) }
  function back() { if (stepIndex > 0) go(steps[stepIndex - 1]) }

  function pickDevice(key) {
    if (key === deviceKey) return
    deviceKey = key
    caps = []; values = ({}); existing = null; answers = ({}); tests = ({}); hidden = ({}); profileName = ""
  }

  // ---------- access ----------
  function enterAccess() {
    if (device && device.accessible) return forward()
    busy = true
    var req = { cmd: "udev-rule" }
    if (!supported) req.extra = [device.vidpid]
    backend.call(req, function(reply) {
      busy = false
      if (!reply.ok) return fail(reply)
      ruleText = typeof reply.text === "string" ? reply.text : ""
    })
  }

  function installRule() {
    busy = true
    message = ""
    backend.installRule(ruleText, function(result) {
      busy = false
      message = result.ok ? "Rule installed. If the mouse stays locked, unplug it and plug it back in." : Model.plain(result.error, 200)
    })
  }

  // Access is granted by the kernel a moment after the rule lands (or on
  // replug); the backend's device list announces it.
  Connections {
    target: root.backend
    function onDevicesChanged() {
      if (root.step === "access" && root.device && root.device.accessible) root.forward()
    }
  }

  // ---------- confirm ----------
  function loadReadings() {
    if (caps.length > 0) return
    busy = true
    backend.call({ cmd: "capabilities", device: deviceKey }, function(reply) {
      if (!reply.ok) return fail(reply)
      caps = Model.cleanCapabilities(reply).concat(rejectedCaps(reply))
      prefill(reply.profile)
      backend.call({ cmd: "read", device: deviceKey }, function(read) {
        busy = false
        if (!read.ok) return fail(read)
        var next = {}
        for (var i = 0; i < caps.length; i++) {
          var v = read.values ? Model.cleanValue(caps[i], read.values[caps[i].id]) : undefined
          if (v !== undefined) next[caps[i].id] = v
        }
        values = next
      })
    })
  }

  // cleanCapabilities drops what a profile rejected; the wizard has to see
  // them again to re-ask.
  function rejectedCaps(reply) {
    var out = []
    var list = Array.isArray(reply.capabilities) ? reply.capabilities : []
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].state === "rejected") {
        var c = Model.cleanCapability(Object.assign({}, list[i], { state: null }))
        if (c) out.push(c)
      }
    }
    return out
  }

  function prefill(profile) {
    if (!Model.isObject(profile) || !Model.isObject(profile.capabilities)) return
    existing = { name: Model.plain(profile.name, 64) }
    var a = {}, t = {}, h = {}
    for (var i = 0; i < caps.length; i++) {
      var entry = profile.capabilities[caps[i].id]
      if (!Model.isObject(entry)) continue
      a[caps[i].group] = a[caps[i].group] === false ? false : entry.state !== "rejected"
      if (entry.state === "tested") t[caps[i].id] = "passed"
      if (entry.visible === false) h[caps[i].group] = true
    }
    answers = a; tests = t; hidden = h
  }

  // ---------- writes ----------
  function runTest(capId) {
    setPart("tests", capId, "running")
    backend.call({ cmd: "test-write", device: deviceKey, capability: capId }, function(reply) {
      if (!reply.ok) {
        setPart("tests", capId, "failed")
        return fail(reply)
      }
      setPart("tests", capId, reply.passed === true ? "passed" : "failed")
    })
  }

  // ---------- save ----------
  function save() {
    busy = true
    message = ""
    var states = supported ? Model.finalStates(caps, rejected, tests, hidden) : {}
    backend.call({ cmd: "save-profile", device: deviceKey, name: profileName.trim(), capabilities: states }, function(reply) {
      busy = false
      if (!reply.ok) return fail(reply)
      console.log("mousectrl: saved profile " + Model.plain(profileName, 64))
      backend.selectDevice(deviceKey)
      backend.refresh()
      root.closed()
    })
  }

  readonly property bool canContinue: {
    if (busy) return false
    if (step === "detect") return device !== null
    if (step === "access") return device !== null && device.accessible
    if (step === "confirm") return caps.length > 0 && allAnswered
    if (step === "save") return profileName.trim().length > 0
    return true
  }

  function primary() {
    if (!canContinue) return
    if (step === "save") save()
    else forward()
  }

  Component.onCompleted: {
    var start = Model.pickDevice(backend.devices, startKey)
    if (start && startKey !== "") pickDevice(start.key)
  }

  // ---------- header ----------
  Column {
    width: parent.width
    spacing: Style.space(8)

    Item {
      width: parent.width
      implicitHeight: Math.max(title.implicitHeight, closeButton.implicitHeight)

      Column {
        id: title
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        spacing: Style.space(2)

        Text {
          textFormat: Text.PlainText
          text: "Set up a mouse"
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.title
          font.bold: true
        }

        Text {
          textFormat: Text.PlainText
          text: ((root.stepIndex + 1) + " / " + root.steps.length + "  ·  " + root.stepTitles[root.step]).toUpperCase()
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          font.bold: true
          font.letterSpacing: 1.2
        }
      }

      PanelActionButton {
        id: closeButton
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        iconText: "󰅖"
        tooltipText: "Cancel setup"
        foreground: root.foreground
        fontFamily: root.fontFamily
        onClicked: root.closed()
      }
    }

    Row {
      width: parent.width
      spacing: Style.space(4)
      Repeater {
        model: root.steps.length
        Rectangle {
          required property int index
          width: (parent.width - parent.spacing * (root.steps.length - 1)) / root.steps.length
          height: Style.space(3)
          color: index <= root.stepIndex ? root.accent : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.18)
        }
      }
    }
  }

  // ---------- 1. detect ----------
  Column {
    visible: root.step === "detect"
    width: parent.width
    spacing: Style.space(6)

    Text {
      textFormat: Text.PlainText
      width: parent.width
      wrapMode: Text.WordWrap
      text: root.backend.devices.length > 0 ? "Pick the mouse to set up. Keyboards and other devices are not listed."
                                            : "No mouse found. Plug in a mouse or its receiver; the list updates on its own."
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Repeater {
      model: root.backend.devices

      CursorSurface {
        id: deviceRow
        required property var modelData
        width: parent.width
        implicitHeight: deviceText.implicitHeight + Style.space(14)
        current: modelData.key === root.deviceKey
        hasCursor: deviceMouse.containsMouse
        foreground: root.foreground
        fill: root.hoverFill
        currentFill: root.selectedFill

        Column {
          id: deviceText
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.leftMargin: Style.space(10)
          anchors.rightMargin: Style.space(10)
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(2)

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: deviceRow.modelData.name
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            font.bold: true
            elide: Text.ElideRight
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: deviceRow.modelData.vidpid + "  ·  "
                  + (deviceRow.modelData.profile ? "set up as " + deviceRow.modelData.profile.name + ", run again to change"
                     : deviceRow.modelData.family ? "supported: " + deviceRow.modelData.family.title
                     : "no supported family: read-only")
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }

        MouseArea {
          id: deviceMouse
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: root.pickDevice(deviceRow.modelData.key)
        }
      }
    }
  }

  // ---------- 2. access ----------
  Column {
    visible: root.step === "access"
    width: parent.width
    spacing: Style.space(8)

    Text {
      textFormat: Text.PlainText
      width: parent.width
      wrapMode: Text.WordWrap
      text: root.device && root.device.accessible
            ? "Mouse CTRL can talk to this mouse."
            : "Linux only lets root talk to a mouse's settings interface. This udev rule grants it to whoever is logged in at this seat, scoped to the vendors and devices listed."
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Rectangle {
      visible: root.ruleText !== "" && !(root.device && root.device.accessible)
      width: parent.width
      implicitHeight: ruleView.implicitHeight + Style.space(16)
      color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.08)
      radius: Style.cornerRadius

      Text {
        id: ruleView
        textFormat: Text.PlainText
        anchors.fill: parent
        anchors.margins: Style.space(8)
        wrapMode: Text.WrapAnywhere
        text: root.ruleText.split("\n").filter(function(l) { return l !== "" && l.charAt(0) !== "#" }).join("\n")
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }

    Text {
      textFormat: Text.PlainText
      visible: root.ruleText !== "" && !(root.device && root.device.accessible)
      text: "→ " + Model.RULE_FILE
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Button {
      visible: root.ruleText !== "" && !(root.device && root.device.accessible)
      text: root.backend.installingRule ? "Waiting for the password prompt…" : "Install rule (asks for your password)"
      iconText: "󰌾"
      bordered: true
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      fontSize: Style.font.bodySmall
      onClicked: if (!root.backend.installingRule && !root.busy) root.installRule()
    }

    Text {
      textFormat: Text.PlainText
      visible: root.ruleText !== "" && !(root.device && root.device.accessible)
      width: parent.width
      wrapMode: Text.WordWrap
      text: "Prefer a terminal? install.sh --udev in the plugin folder writes the same file."
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }

  // ---------- 3. confirm ----------
  Column {
    visible: root.step === "confirm"
    width: parent.width
    spacing: Style.space(10)

    Text {
      textFormat: Text.PlainText
      width: parent.width
      wrapMode: Text.WordWrap
      text: root.busy && root.caps.length === 0 ? "Reading the mouse…"
            : "Check each group against the mouse or its vendor app. A group marked wrong is hidden and never written, because a wrong reading means the writes would be wrong too."
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Repeater {
      model: root.groups

      Column {
        id: group
        required property var modelData
        width: parent.width
        spacing: 0

        PanelSectionHeader {
          bottomPadding: Style.space(4)
          text: group.modelData.title
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Flow {
          visible: group.modelData.stagesId !== ""
          width: parent.width
          spacing: Style.space(10)
          bottomPadding: Style.space(6)
          leftPadding: Style.space(8)

          Repeater {
            model: {
              var v = root.values[group.modelData.stagesId]
              return v ? v.stages : []
            }
            Row {
              required property var modelData
              spacing: Style.space(4)
              Rectangle {
                visible: modelData.color !== null
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(8)
                height: width
                color: modelData.color || "transparent"
                border.color: root.dim
                border.width: 1
              }
              Text {
                textFormat: Text.PlainText
                text: modelData.dpi
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
            }
          }
        }

        Repeater {
          model: group.modelData.rows
          Item {
            required property var modelData
            width: group.width
            implicitHeight: Style.space(24)
            Text {
              textFormat: Text.PlainText
              x: Style.space(8)
              width: root.labelWidth - Style.space(16)
              anchors.verticalCenter: parent.verticalCenter
              text: modelData.label
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              elide: Text.ElideRight
            }
            Text {
              textFormat: Text.PlainText
              x: root.labelWidth
              anchors.verticalCenter: parent.verticalCenter
              text: modelData.text
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }
          }
        }

        // Matches | Wrong, on the grid's control column like every choice.
        Item {
          width: group.width
          implicitHeight: Style.spacing.controlHeight + Style.space(6)

          Text {
            textFormat: Text.PlainText
            x: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            text: "Is this right?"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Row {
            id: verdict
            x: root.labelWidth
            width: group.width - root.labelWidth - Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(3)

            Repeater {
              model: [{ value: true, label: "Matches" }, { value: false, label: "Wrong" }]
              Button {
                required property var modelData
                width: (verdict.width - verdict.spacing) / 2
                height: Style.spacing.controlHeight
                text: modelData.label
                selected: root.answers[group.modelData.id] === modelData.value
                bordered: true
                foreground: root.foreground
                accent: root.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                onClicked: root.setPart("answers", group.modelData.id, modelData.value)
              }
            }
          }
        }
      }
    }
  }

  // ---------- 4. writes ----------
  Column {
    visible: root.step === "writes"
    width: parent.width
    spacing: 0

    Text {
      textFormat: Text.PlainText
      width: parent.width
      bottomPadding: Style.space(8)
      wrapMode: Text.WordWrap
      text: "Settings proven on this family can be changed right away. Untested ones stay read-only unless their test passes: write a nearby value, read it back, restore the original."
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }

    Repeater {
      model: root.writableCaps

      Item {
        id: writeRow
        required property var modelData
        readonly property string result: root.tests[modelData.id] || ""
        width: parent.width
        implicitHeight: Style.spacing.controlHeight + Style.space(2)

        Text {
          textFormat: Text.PlainText
          x: Style.space(8)
          width: root.labelWidth - Style.space(16)
          anchors.verticalCenter: parent.verticalCenter
          text: writeRow.modelData.label
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          visible: writeRow.modelData.verification === "verified" || writeRow.result === "passed" || writeRow.result === "failed"
          anchors.right: parent.right
          anchors.rightMargin: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          text: writeRow.modelData.verification === "verified" ? "verified · writable"
                : writeRow.result === "passed" ? "test passed · writable" : "test failed · read-only"
          color: writeRow.result === "failed" ? root.urgent : root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Button {
          visible: writeRow.modelData.verification !== "verified" && writeRow.result !== "passed" && writeRow.result !== "failed"
          anchors.right: parent.right
          anchors.rightMargin: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          text: writeRow.result === "running" ? "Testing…" : "Run test"
          bordered: true
          foreground: root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          fontSize: Style.font.bodySmall
          onClicked: if (writeRow.result !== "running") root.runTest(writeRow.modelData.id)
        }
      }
    }
  }

  // ---------- 5. save ----------
  Column {
    visible: root.step === "save"
    width: parent.width
    spacing: 0

    Item {
      width: parent.width
      implicitHeight: Style.spacing.controlHeight + Style.space(8)

      Text {
        textFormat: Text.PlainText
        x: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
        text: "Name"
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }

      TextField {
        x: root.labelWidth
        width: parent.width - root.labelWidth - Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
        text: root.profileName
        maximumLength: 64
        foreground: root.foreground
        accent: root.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        onTextEdited: root.profileName = text
        onAccepted: root.primary()
      }
    }

    Repeater {
      model: root.supported ? root.shownSections : []

      Item {
        id: sectionRow
        required property var modelData
        width: parent.width
        implicitHeight: Style.spacing.controlHeight + Style.space(2)

        Text {
          textFormat: Text.PlainText
          x: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          text: "Show " + Model.GROUP_TITLES[sectionRow.modelData].toLowerCase()
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        ToggleSwitch {
          anchors.right: parent.right
          anchors.rightMargin: Style.space(8)
          anchors.verticalCenter: parent.verticalCenter
          checked: root.hidden[sectionRow.modelData] !== true
          foreground: root.foreground
          accent: root.accent
          onToggled: root.setPart("hidden", sectionRow.modelData, root.hidden[sectionRow.modelData] !== true)
        }
      }
    }

    Text {
      textFormat: Text.PlainText
      width: parent.width
      topPadding: Style.space(8)
      wrapMode: Text.WordWrap
      text: {
        if (!root.supported) return "No supported family yet, so this mouse is saved read-only: it is listed in the panel with what can be seen without its protocol."
        var states = Model.finalStates(root.caps, root.rejected, root.tests, root.hidden)
        var n = { writable: 0, readOnly: 0, rejected: 0 }
        for (var id in states) {
          var c = Model.findCap(root.caps, id)
          if (states[id].state === "rejected") n.rejected++
          else if ((states[id].state === "verified" || states[id].state === "tested") && c && !c.readOnly && c.kind !== "info" && c.kind !== "battery") n.writable++
          else if (c && c.kind !== "info" && c.kind !== "battery") n.readOnly++
        }
        return (root.device && root.device.family ? root.device.family.title + "  ·  " : "")
          + n.writable + " writable, " + n.readOnly + " read-only" + (n.rejected ? ", " + n.rejected + " hidden as wrong" : "")
          + (root.existing ? "  ·  replaces the existing profile" : "")
      }
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
    }
  }

  // ---------- message and navigation ----------
  Text {
    textFormat: Text.PlainText
    visible: root.message !== ""
    width: parent.width
    wrapMode: Text.WordWrap
    text: root.message
    color: root.urgent
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  PanelSeparator {
    foreground: root.foreground
  }

  Item {
    width: parent.width
    implicitHeight: primaryButton.implicitHeight

    Button {
      visible: root.stepIndex > 0
      anchors.left: parent.left
      text: "Back"
      bordered: true
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      fontSize: Style.font.bodySmall
      onClicked: root.back()
    }

    Button {
      id: primaryButton
      anchors.right: parent.right
      text: root.step === "save" ? "Save profile" : "Continue"
      selected: root.canContinue
      bordered: true
      opacity: root.canContinue ? 1.0 : 0.5
      foreground: root.foreground
      accent: root.accent
      fontFamily: root.fontFamily
      fontSize: Style.font.bodySmall
      onClicked: root.primary()
    }
  }
}
