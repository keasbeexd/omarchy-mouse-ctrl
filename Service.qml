import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "Model.js" as Model

// Drives hskctl. Every read is `hskctl --json status`; every write is
// `hskctl --json set <field> <value>` followed by a re-read, so the panel
// always shows what the mouse actually reports rather than what we asked for.
Item {
  id: root

  property var settings: ({})

  property string state: "loading"      // loading | ready | undiscovered | error
  property string model: "HSK Mouse"
  property string devicePath: ""
  property var values: ({})
  property string lastError: ""
  property string actionStatus: ""
  property bool refreshing: false
  property bool detected: false
  // Set by anything that must not be interrupted by a refresh. The coalescing
  // window guards itself (see refresh), so this is now only for callers that
  // want to hold a refresh off for longer.
  property bool suspended: false
  property var writable: []
  property var unverified: []

  // Optimistic overlay: a click should move the UI immediately rather than
  // waiting a full command round trip. Cleared once the re-read lands.
  property var pending: ({})

  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 60, 10, 3600)
  readonly property int lowBatteryPercent: intSetting("lowBatteryPercent", 15, 0, 50)
  readonly property bool showBatteryLabel: setting("showBatteryLabel", true) === true
  // Omarchy clones the plugin to ~/.config/omarchy/plugins/<id>/, and the CLI
  // ships inside it, so the widget works with nothing else installed. A
  // non-empty hskctlPath setting overrides this.
  readonly property string bundledHskctl: {
    var url = Qt.resolvedUrl("bin/hskctl").toString()
    return url.indexOf("file://") === 0 ? url.substring(7) : url
  }
  readonly property string hskctl: {
    var configured = String(setting("hskctlPath", "") || "").trim()
    return configured !== "" ? configured : bundledHskctl
  }

  readonly property bool busy: statusProcess.running || setProcess.running
  readonly property bool ready: state === "ready"
  readonly property bool lowBattery: Model.isLow(effectiveValues, lowBatteryPercent)

  // What the UI reads: device truth with any in-flight change laid over it.
  readonly property var effectiveValues: {
    var merged = {}
    for (var key in values) merged[key] = values[key]
    for (var pendingKey in pending) merged[pendingKey] = pending[pendingKey]
    return merged
  }

  readonly property string summary: Model.summaryLine(state, effectiveValues)
  readonly property string barText: Model.barLabel(effectiveValues, showBatteryLabel)
  readonly property var rows: Model.buildRows(state, effectiveValues, writable)

  signal changed()

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    return Math.max(min, Math.min(max, n))
  }

  function has(field) {
    return Model.has(effectiveValues, field)
  }

  function value(field) {
    return effectiveValues[field]
  }

  function refresh() {
    // A read must not overlap a write. Both are separate hskctl processes and
    // the device has one reply buffer, so interleaving them makes a write look
    // ignored and a read-back report the old value. hskctl also takes a file
    // lock, which covers the other bar instances and the CLI; this just avoids
    // queueing behind ourselves.
    if (suspended) return
    if (statusProcess.running || setProcess.running || _queue.length > 0) return
    // A refresh clears `pending`, so one landing between a click and its write
    // would snap the number back to the old value and then forward again.
    if (Object.keys(_soon).length > 0) return
    refreshing = true
    statusProcess.command = [hskctl, "--json", "status"]
    statusProcess.running = true
  }

  function applyStatus(raw) {
    var parsed = Model.parseStatus(raw)
    state = parsed.state
    detected = parsed.detected
    if (parsed.model !== "") model = parsed.model
    devicePath = parsed.device
    values = parsed.settings || {}
    writable = parsed.writable || []
    unverified = parsed.unverified || []
    pending = ({})
    lastError = parsed.ok ? "" : parsed.error
    changed()
  }

  // Writes are serialised: the mouse is a single shared resource and two
  // overlapping feature reports can interleave badly on the wire.
  property var _queue: []

  function set(field, value) {
    var job = { field: field, value: value }
    // Queue behind an in-flight write *or* an in-flight read, so a click during
    // a refresh is not thrown away.
    if (setProcess.running || statusProcess.running) {
      _queue.push(job)
      if (!drainTimer.running) drainTimer.start()
      return
    }
    _run(job)
  }

  // Repeated input on the same field -- clicking "+" ten times -- must not
  // become ten USB round trips. Each write is a read-modify-write of the whole
  // DPI block and takes the device lock, so ten of them queue up and the panel
  // spends two seconds visibly catching up. Only the last value matters, so
  // hold it briefly and write once.
  //
  // The optimistic value goes into `pending` immediately, so the number on
  // screen tracks every click even though the wire stays quiet.
  property var _soon: ({})
  readonly property bool writeQueued: Object.keys(_soon).length > 0 || _queue.length > 0
  readonly property bool working: busy || writeQueued

  function setSoon(field, value) {
    var overlay = {}
    for (var key in pending) overlay[key] = pending[key]
    overlay[field] = value
    pending = overlay

    var soon = {}
    for (var queued in _soon) soon[queued] = _soon[queued]
    soon[field] = value
    _soon = soon
    coalesceTimer.restart()
  }

  Timer {
    id: coalesceTimer
    interval: 240
    repeat: false
    onTriggered: {
      var soon = root._soon
      root._soon = ({})
      for (var field in soon) root.set(field, soon[field])
    }
  }

  function _run(job) {
    var overlay = {}
    for (var key in pending) overlay[key] = pending[key]
    overlay[job.field] = job.value
    pending = overlay

    actionStatus = ""
    _setField = job.field
    setProcess.command = [hskctl, "--json", "set", String(job.field), String(job.value)]
    setProcess.running = true
  }

  property string _setField: ""

  function setDpiStage(stage) {
    set("activeDpiStage", stage)
  }

  function cycleDpiStage() {
    var stages = Model.dpiStages(effectiveValues)
    if (stages.length === 0) return
    var current = effectiveValues.activeDpiStage
    var index = 0
    for (var i = 0; i < stages.length; i++) {
      if (stages[i].stage === current) { index = i; break }
    }
    setDpiStage(stages[(index + 1) % stages.length].stage)
  }

  function canWrite(field) {
    return Model.canWrite(writable, field)
  }

  function toggle(field) {
    if (!has(field) || !canWrite(field)) return
    set(field, value(field) ? "off" : "on")
  }

  Timer {
    id: refreshTimer
    interval: root.refreshIntervalSec * 1000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    // Waits for an in-flight read to finish, then releases the queued writes.
    id: drainTimer
    interval: 120
    repeat: true
    running: false
    onTriggered: {
      if (setProcess.running || statusProcess.running) return
      if (root._queue.length === 0) { drainTimer.stop(); return }
      root._run(root._queue.shift())
    }
  }

  Timer {
    id: settleTimer
    interval: 250
    repeat: false
    onTriggered: root.refresh()
  }

  Timer {
    id: actionStatusTimer
    interval: 2600
    repeat: false
    onTriggered: root.actionStatus = ""
  }

  Timer {
    // hskctl talks to hardware; a wedged USB stack can hang it. Without this a
    // single stuck call stops every later refresh, because each one is skipped
    // while the previous is still running.
    //
    // Armed while *either* process runs and only disarmed once both are idle --
    // stopping it on whichever finishes first would leave the other unwatched.
    id: watchdog
    interval: 15000
    repeat: false
    running: statusProcess.running || setProcess.running
    onTriggered: {
      if (statusProcess.running) statusProcess.running = false
      if (setProcess.running) setProcess.running = false
      root.pending = ({})
      root._queue = []
      root.lastError = "hskctl timed out"
      root.changed()
    }
  }

  Process {
    id: statusProcess
    running: false
    command: []
    stdout: StdioCollector { id: statusOut; waitForEnd: true }
    stderr: StdioCollector { id: statusErr; waitForEnd: true }

    onExited: function(exitCode) {
      root.refreshing = false
      var out = String(statusOut.text || "")
      if (out.trim() !== "") {
        root.applyStatus(out)
        return
      }
      // Empty stdout means hskctl never ran -- not installed, or not on the
      // shell's PATH, which is a different problem from "mouse not found".
      root.state = "error"
      root.values = ({})
      root.writable = []
      root.pending = ({})
      var err = String(statusErr.text || "").trim()
      root.lastError = err !== ""
        ? err.split("\n")[0]
        : "Could not run " + root.hskctl + " (exit " + exitCode + ")"
      root.changed()
    }
  }

  Process {
    id: setProcess
    running: false
    command: []
    stdout: StdioCollector { id: setOut; waitForEnd: true }
    stderr: StdioCollector { id: setErr; waitForEnd: true }

    onExited: function(exitCode) {
      var parsed = Model.parseStatus(setOut.text)
      if (exitCode !== 0 || !parsed.ok) {
        root.pending = ({})
        root.actionStatus = parsed.error !== ""
          ? parsed.error
          : ("Could not set " + root._setField)
        actionStatusTimer.restart()
      }
      root._setField = ""

      if (root._queue.length > 0) {
        var next = root._queue.shift()
        root._run(next)
      } else {
        drainTimer.stop()
        settleTimer.restart()
      }
    }
  }
}
