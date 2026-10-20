import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// Owns the long-lived `mousectrl serve` process and everything the panel
// shows: connected mice, the selected mouse's capabilities and values, and a
// status saying why there is nothing to show when there is not.
//
// Requests carry an id; replies are matched to the callback waiting for it.
// A reply nobody waits for is stale and dropped.
Item {
  id: root

  property var settings: ({})
  // Read more often while the panel is open: the active stage follows the
  // mouse's own DPI button, which no event announces.
  property bool panelOpen: false

  // "starting" | "noDevice" | "noAccess" | "unsupported" | "noProfile"
  // | "asleep" | "ready" | "generic" | "error"
  property string status: "starting"
  property string statusMessage: ""
  property var devices: []
  property string selectedKey: ""
  property var device: null
  property var capabilities: []
  property var values: ({})
  // capId -> true while a write is in flight.
  property var pending: ({})
  // Last write failure, shown under the settings for a few seconds.
  property string actionMessage: ""
  property var profileProblems: []
  // For a mouse with a generic profile: what `probe` and `measure` found.
  property var generic: null
  property int measuredHz: 0
  property bool measuring: false

  readonly property var battery: values.battery !== undefined ? values.battery : null
  readonly property int batteryPollMs: 1000 * Math.max(30, Math.min(3600, Number(setting("batteryPollSec", 300)) || 300))
  readonly property int lowBatteryPercent: Math.max(0, Math.min(50, Number(setting("lowBatteryPercent", 15)) || 0))
  readonly property bool lowBattery: battery !== null && !battery.charging && battery.percent <= lowBatteryPercent

  function setting(name, fallback) {
    var v = settings ? settings[name] : undefined
    return v === undefined || v === null ? fallback : v
  }

  // --- public actions ------------------------------------------------------

  function refresh() {
    request({ cmd: "devices" }, applyDevices)
  }

  function selectDevice(key) {
    selectedKey = key
    capabilities = []
    values = ({})
    generic = null
    measuredHz = 0
    applySelection()
  }

  function cycleDevice() {
    if (devices.length > 1) selectDevice(Model.nextDeviceKey(devices, selectedKey))
  }

  // Every write is logged to the journal (`journalctl --user -t omarchy-shell
  // | grep mousectrl`), so "what did the panel send?" has an answer.
  // Replies arrive after the user may have switched mice; one meant for a
  // mouse no longer shown must not touch the state of the one that is.
  function isCurrent(key) {
    return device !== null && device.key === key
  }

  function write(capId, value) {
    if (!device || pending[capId]) return
    var key = device.key
    var target = Model.plain(device.profile ? device.profile.name : device.name, 60)
    console.log("mousectrl: write " + capId + " = " + JSON.stringify(value) + " on " + target)
    setPending(capId, true)
    request({ cmd: "write", device: key, capability: capId, value: value }, function(reply) {
      setPending(capId, false)
      if (!isCurrent(key)) return
      if (!reply.ok) {
        console.warn("mousectrl: write " + capId + " on " + target + " failed: " + Model.plain(reply.error, 240))
        showAction(reply.error)
        if (reply.code === "noResponse") readValues(null)
        return
      }
      storeValue(capId, reply.value)
      // A count change reshapes the stage list; one cheap read keeps it true.
      var stages = Model.firstOfKind(capabilities, "dpiStages")
      if (stages && capId !== stages.id) readValues(stages.id)
    })
  }

  // --- setup wizard ----------------------------------------------------------

  // Raw access for the wizard's own requests (capabilities, read,
  // test-write, save-profile, udev-rule); the reply goes to `done`.
  function call(payload, done) {
    if (!serveUp) return done({ ok: false, code: "serve", error: "mousectrl is not running" })
    request(payload, done)
  }

  // Installs the udev rule as root through polkit. The text arrives on
  // stdin, so there is no file another process could swap between the check
  // below and root reading it; every line is re-validated here against the
  // same pattern install.sh enforces. Tools are absolute paths, and the
  // target is a constant, never taken from the reply.
  property bool installingRule: false
  property var ruleDone: null
  property string ruleText: ""

  readonly property string ruleScript: "set -eu; umask 022; "
    + "/usr/bin/install -o root -g root -m 0644 /dev/stdin \"$1\"; "
    + "/usr/bin/udevadm control --reload-rules; "
    + "/usr/bin/udevadm trigger --subsystem-match=hidraw --action=change"

  function installRule(text, done) {
    if (installingRule) return done({ ok: false, error: "an install is already running" })
    if (!Model.isValidRuleText(text)) return done({ ok: false, error: "the rule text did not validate; nothing was installed" })
    console.log("mousectrl: installing " + Model.RULE_FILE + " through pkexec")
    ruleText = text
    ruleDone = done
    installingRule = true
    ruleInstall.running = true
  }

  Process {
    id: ruleInstall
    command: ["/usr/bin/pkexec", "/usr/bin/bash", "-c", root.ruleScript, "mousectrl-rule", Model.RULE_FILE]
    clearEnvironment: true
    environment: root.childEnvironment
    stdinEnabled: true
    onStarted: {
      write(root.ruleText)
      // install(1) reads /dev/stdin to EOF; closing stdin is the EOF.
      stdinEnabled = false
    }
    onExited: function(exitCode, exitStatus) {
      var done = root.ruleDone
      root.installingRule = false
      root.ruleDone = null
      ruleInstall.stdinEnabled = true
      // pkexec: 126 = the password prompt was dismissed, 127 = not authorized.
      var message = exitCode === 0 ? "" : exitCode === 126 ? "Cancelled at the password prompt"
                  : exitCode === 127 ? "Not authorized to install the rule" : "Installing the rule failed (exit " + exitCode + ")"
      if (exitCode !== 0) console.warn("mousectrl: rule install: " + message)
      if (done) done({ ok: exitCode === 0, error: message })
      root.refresh()
    }
  }

  // --- replies -------------------------------------------------------------

  function applyDevices(reply) {
    if (!reply.ok) return fail(reply)
    devices = Model.cleanDevices(reply)
    profileProblems = Array.isArray(reply.profileProblems) ? reply.profileProblems.slice(0, 8).map(function(p) { return Model.plain(p, 200) }) : []
    var picked = Model.pickDevice(devices, selectedKey)
    var changed = !picked || picked.key !== selectedKey
    selectedKey = picked ? picked.key : ""
    if (changed) { capabilities = []; values = ({}) }
    applySelection()
  }

  function applySelection() {
    device = Model.pickDevice(devices, selectedKey)
    if (!device) return setStatus("noDevice", "No mouse connected")
    if (!device.family && device.profile) return loadGeneric()
    if (!device.family) return setStatus("unsupported", device.name + " is not in a supported family yet")
    if (!device.accessible) return setStatus("noAccess", "Mouse CTRL needs the udev rule to talk to " + device.name)
    if (!device.profile) return setStatus("noProfile", device.name + " is not set up yet")
    var key = device.key
    request({ cmd: "capabilities", device: key }, function(reply) {
      if (!isCurrent(key)) return
      if (!reply.ok) return fail(reply)
      capabilities = Model.cleanCapabilities(reply)
      readValues(null)
    })
  }

  function loadGeneric() {
    var key = device.key
    request({ cmd: "probe", device: key }, function(reply) {
      if (!isCurrent(key)) return
      if (!reply.ok) return fail(reply)
      var battery = Model.cleanValue({ kind: "battery" }, reply.battery)
      values = battery !== undefined ? { battery: battery } : ({})
      generic = {
        connection: Model.plain(reply.connection, 20),
        buttons: Model.isInt(reply.buttons) ? reply.buttons : 0,
        report: typeof reply.report === "string" ? reply.report.substring(0, 65536) : "",
        ratbag: Model.cleanRatbag(reply.ratbag),
        piper: reply.piper === "/usr/bin/piper"
      }
      setStatus("generic", "")
    })
  }

  function measure() {
    if (!device || measuring) return
    measuring = true
    var key = device.key
    request({ cmd: "measure", device: key }, function(reply) {
      measuring = false
      if (!isCurrent(key)) return
      if (!reply.ok) return showAction(reply.error)
      measuredHz = Model.isInt(reply.hz) ? reply.hz : 0
    })
  }

  // One capability, or all of them when capId is null.
  function readValues(capId) {
    if (!device) return
    var key = device.key
    var req = { cmd: "read", device: key }
    if (capId) req.capability = capId
    request(req, function(reply) {
      if (!isCurrent(key)) return
      if (!reply.ok) return fail(reply)
      var next = Object.assign({}, values)
      for (var i = 0; i < capabilities.length; i++) {
        var cap = capabilities[i]
        if (reply.values && reply.values.hasOwnProperty(cap.id)) {
          var v = Model.cleanValue(cap, reply.values[cap.id])
          if (v !== undefined) next[cap.id] = v
        }
      }
      values = next
      setStatus("ready", "")
    })
  }

  function storeValue(capId, raw) {
    var cap = Model.findCap(capabilities, capId)
    var v = cap ? Model.cleanValue(cap, raw) : undefined
    if (v === undefined) return
    var next = Object.assign({}, values)
    next[capId] = v
    values = next
  }

  function fail(reply) {
    if (reply.code === "noResponse") return setStatus("asleep", reply.error)
    if (reply.code === "noAccess") return setStatus("noAccess", reply.error)
    setStatus("error", reply.error)
  }

  function setStatus(s, message) {
    status = s
    statusMessage = Model.plain(message, 240)
  }

  function setPending(capId, on) {
    var next = Object.assign({}, pending)
    if (on) next[capId] = true
    else delete next[capId]
    pending = next
  }

  function showAction(message) {
    actionMessage = Model.plain(message, 240)
    actionTimer.restart()
  }

  Timer {
    id: actionTimer
    interval: 6000
    onTriggered: root.actionMessage = ""
  }

  // Background reads: battery for the bar, the stage list while open (it is
  // the one value the mouse changes on its own), and a retry while asleep.
  Timer {
    interval: root.batteryPollMs
    repeat: true
    running: root.serveUp && root.status === "ready" && Model.findCap(root.capabilities, "battery") !== null
    onTriggered: root.readValues("battery")
  }

  Timer {
    interval: 3000
    repeat: true
    running: root.serveUp && root.panelOpen && root.status === "ready"
             && Model.firstOfKind(root.capabilities, "dpiStages") !== null
    onTriggered: root.readValues(Model.firstOfKind(root.capabilities, "dpiStages").id)
  }

  Timer {
    interval: root.panelOpen ? 3000 : 60000
    repeat: true
    running: root.serveUp && root.status === "asleep"
    onTriggered: root.capabilities.length > 0 ? root.readValues(null) : root.applySelection()
  }

  // --- serve process -------------------------------------------------------
  //
  // Started once and kept for the life of the widget. Its death is a restart
  // with exponential backoff (2 s doubling to 5 min, with jitter); six quick
  // failures in a row give up, and a run of a minute resets the count.

  // Whatever runs here is handed the mouse's hidraw descriptor, so it is
  // always the reviewed copy beside this file; there is no setting to point
  // it elsewhere.
  readonly property string executable: {
    var url = Qt.resolvedUrl("bin/mousectrl").toString()
    return url.indexOf("file://") === 0 ? url.substring(7) : url
  }

  // The child gets this environment and nothing inherited: the launcher is a
  // bash script, and $BASH_ENV, LD_PRELOAD or PYTHON* would ride along
  // otherwise. mousectrl needs HOME (profiles) and XDG_RUNTIME_DIR (lock).
  readonly property var childEnvironment: {
    var env = { PATH: "/usr/bin:/bin", LANG: "C.UTF-8" }
    var names = ["HOME", "XDG_RUNTIME_DIR"]
    for (var i = 0; i < names.length; i++) {
      var v = Quickshell.env(names[i])
      if (v !== undefined && v !== null && String(v) !== "") env[names[i]] = String(v)
    }
    return env
  }

  readonly property int maxLineBytes: 262144
  readonly property int maxPending: 16
  readonly property int requestTimeoutMs: 20000

  property bool serveUp: false
  property bool destroyed: false
  property int nextId: 0
  // id -> { done: function, at: ms }
  property var waiting: ({})
  property string lineBuffer: ""
  property int failures: 0
  property real startedAt: 0

  function request(payload, done) {
    if (!serveUp) return
    if (Object.keys(waiting).length >= maxPending) return
    var id = ++nextId
    payload.id = id
    var next = Object.assign({}, waiting)
    next[id] = { done: done, at: Date.now() }
    waiting = next
    serve.write(JSON.stringify(payload) + "\n")
  }

  function handleLine(line) {
    var msg
    try { msg = JSON.parse(line) } catch (e) { return }
    if (!Model.isObject(msg)) return
    if (msg.event === "devices") return refresh()
    if (msg.id === undefined) return
    var entry = waiting[msg.id]
    if (!entry) return
    var next = Object.assign({}, waiting)
    delete next[msg.id]
    waiting = next
    entry.done(msg)
  }

  function failAllWaiting(message) {
    var outstanding = waiting
    waiting = ({})
    for (var id in outstanding)
      outstanding[id].done({ ok: false, code: "serve", error: message })
  }

  function scheduleRestart() {
    if (Date.now() - startedAt >= 60000) failures = 0
    if (failures >= 6) return setStatus("error", "mousectrl keeps exiting; check `mousectrl devices` in a terminal")
    var base = Math.min(300000, 2000 * Math.pow(2, failures))
    restartTimer.interval = base + Math.floor(Math.random() * base * 0.2)
    failures += 1
    restartTimer.restart()
  }

  Timer {
    id: restartTimer
    onTriggered: if (!root.destroyed) serve.running = true
  }

  // signal(15) ends the child; `running = false` alone only detaches it.
  Timer {
    id: killTimer
    interval: 1500
    onTriggered: if (serve.running) serve.signal(9)
  }

  Timer {
    interval: 1000
    repeat: true
    running: root.serveUp && Object.keys(root.waiting).length > 0
    onTriggered: {
      var now = Date.now()
      for (var id in root.waiting) {
        if (now - root.waiting[id].at <= root.requestTimeoutMs) continue
        serve.signal(15)
        killTimer.restart()
        root.failAllWaiting("mousectrl stopped answering")
        return
      }
    }
  }

  Process {
    id: serve
    command: [root.executable, "serve"]
    clearEnvironment: true
    environment: root.childEnvironment
    // Raw chunks rather than a newline splitter, so lines are assembled
    // here against a hard cap instead of buffering without bound.
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(chunk) {
        var buf = root.lineBuffer + String(chunk || "")
        var nl
        while ((nl = buf.indexOf("\n")) !== -1) {
          var line = buf.substring(0, nl)
          buf = buf.substring(nl + 1)
          if (line.length <= root.maxLineBytes) root.handleLine(line)
        }
        root.lineBuffer = buf.length > root.maxLineBytes ? "" : buf
      }
    }

    onStarted: {
      root.serveUp = true
      root.lineBuffer = ""
      root.startedAt = Date.now()
      root.refresh()
    }

    onExited: function(exitCode, exitStatus) {
      root.serveUp = false
      killTimer.stop()
      root.lineBuffer = ""
      if (root.destroyed) return
      root.failAllWaiting("mousectrl stopped running")
      root.scheduleRestart()
    }
  }

  Component.onCompleted: serve.running = true

  Component.onDestruction: {
    root.destroyed = true
    if (serve.running) serve.signal(15)
  }
}
