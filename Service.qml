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
  property string model: "Mouse"
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
  // Legal values per field, e.g. which polling rates *this* mouse offers --
  // keyed by field name, from the profile via hskctl. Lets the panel build
  // its selectors from whatever mouse is actually plugged in instead of a
  // list baked in for one model.
  property var allowed: ({})

  // Optimistic overlay: a click should move the UI immediately rather than
  // waiting a full command round trip. Cleared once the re-read lands.
  property var pending: ({})

  // A full status refresh re-reads every field the profile knows -- DPI
  // stages, polling rate, every sensor toggle -- one exchange per distinct
  // command. Nothing but this plugin writes those, so there is little to
  // catch by polling them often; the panel also forces one on open. Battery
  // and charging are covered separately and far more often by batteryPollSec
  // below, which is the one thing that changes on its own.
  readonly property int refreshIntervalSec: intSetting("refreshIntervalSec", 30, 10, 3600)
  // Battery percent and charging share one HID exchange on every shipped
  // profile (`hskctl battery`), so polling just those is cheap enough to run
  // every few seconds without the "Writing to the mouse..." banner: this poll
  // never sets `busy`, only merges batteryPercent/charging into `values`.
  readonly property int batteryPollSec: intSetting("batteryPollSec", 3, 2, 60)
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

  // Which build is running, for the panel footer. It comes back in hskctl's
  // own JSON, and hskctl reads it from the manifest -- so it is the version
  // that was published, reported by the copy of the CLI sitting beside this
  // QML. That matters: the usual reason the footer is interesting at all is a
  // plugin directory and a checkout having drifted apart.
  //
  // This was an XMLHttpRequest against manifest.json. It failed silently in
  // the shell -- no error, no label, nothing to debug -- so it now rides the
  // one channel this plugin already depends on working.
  property string pluginVersion: ""

  readonly property bool busy: statusProcess.running || setProcess.running
  readonly property bool ready: state === "ready"
  readonly property bool lowBattery: Model.isLow(effectiveValues, lowBatteryPercent)

  // hskctl JSON output for one command is a few kilobytes at most. We cap at
  // 256 KiB anyway: the cap belongs at the producer -- before the bytes exist
  // in the shell -- and the reviewer's rule is that a whole-output collector
  // (StdioCollector, capture_output) is applied too late. If a runaway hskctl
  // ever exceeded this cap the pipe is closed and the process killed, rather
  // than the shell allocating without bound.
  readonly property int _maxProcessBytes: 262144
  // The battery poll's own payload is a few dozen bytes; a much smaller cap
  // is still generous and this one runs far more often than the others.
  readonly property int _maxBatteryBytes: 4096
  property string _statusStdout: ""
  property int _statusStdoutBytes: 0
  property string _statusStderr: ""
  property int _statusStderrBytes: 0
  property string _setStdout: ""
  property int _setStdoutBytes: 0
  property string _setStderr: ""
  property int _setStderrBytes: 0
  property string _batteryStdout: ""
  property int _batteryStdoutBytes: 0
  property bool _statusOverflow: false
  property bool _setOverflow: false
  property bool _batteryOverflow: false

  // In UTF-16 each JavaScript character is 1..2 code units; a byte cap read as
  // .length is conservative but safe as an upper bound. The producer side of
  // the cap is the process termination on overflow.
  function _appendChunk(current, chunk, budget) {
    var next = current + chunk
    if (next.length > budget) return next.substring(0, budget)
    return next
  }

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
    _statusStdout = ""; _statusStdoutBytes = 0
    _statusStderr = ""; _statusStderrBytes = 0
    _statusOverflow = false
    statusProcess.command = [hskctl, "--json", "status"]
    statusProcess.running = true
  }

  // The fast, quiet path: one exchange for batteryPercent + charging, merged
  // into `values` without touching `busy` or `pending`. Skips rather than
  // queues behind a real read or write -- this cycle is cheap to lose, and
  // the next one is batteryPollSec away.
  function pollBattery() {
    if (suspended || state !== "ready") return
    if (statusProcess.running || setProcess.running || batteryProcess.running) return
    if (_queue.length > 0 || Object.keys(_soon).length > 0) return
    _batteryStdout = ""; _batteryStdoutBytes = 0
    _batteryOverflow = false
    batteryProcess.command = [hskctl, "--json", "battery"]
    batteryProcess.running = true
  }

  function applyStatus(raw) {
    var parsed = Model.parseStatus(raw)
    state = parsed.state
    detected = parsed.detected
    if (parsed.model !== "") model = parsed.model
    devicePath = parsed.device
    values = parsed.settings || {}
    writable = parsed.writable || []
    allowed = parsed.allowed || {}
    if (parsed.version !== "") pluginVersion = parsed.version
    pending = ({})
    lastError = parsed.ok ? "" : parsed.error
    changed()
  }

  // Merges rather than replaces `values` -- a failed or stale poll must not
  // blank out DPI, polling rate and everything else read_all() last saw.
  // Silent on failure: this cycle just did not learn anything new, and the
  // next one is batteryPollSec away, so there is nothing useful to surface
  // as an error for what is background upkeep.
  function applyBattery(raw) {
    var parsed = Model.parseBattery(raw)
    if (!parsed.ok) return
    var merged = {}
    for (var key in values) merged[key] = values[key]
    for (var field in parsed.settings) merged[field] = parsed.settings[field]
    values = merged
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
    _setStdout = ""; _setStdoutBytes = 0
    _setStderr = ""; _setStderrBytes = 0
    _setOverflow = false
    setProcess.command = [hskctl, "--json", "set", String(job.field), String(job.value)]
    setProcess.running = true
  }

  property string _setField: ""

  function canWrite(field) {
    return Model.canWrite(writable, field)
  }

  function toggle(field) {
    if (!has(field) || !canWrite(field)) return
    set(field, value(field) ? "off" : "on")
  }

  // While the mouse has never been read successfully -- at shell startup,
  // before its dongle has finished enumerating, or after any failed read --
  // poll every few seconds instead of waiting the full interval. Without
  // this, one read losing a race with device enumeration at login left the
  // bar showing a stale battery reading for a full refreshIntervalSec (60s
  // by default), and the only way to see the real number sooner was to open
  // the panel, which calls refresh() itself on open. `ready` flips back to
  // true a few seconds later on its own now, with nothing to click.
  readonly property int fastRetryMs: 5000
  Timer {
    id: refreshTimer
    interval: (root.ready ? root.refreshIntervalSec * 1000 : root.fastRetryMs)
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // The fast path that actually answers "did the cable come out": only once
  // the mouse has been read successfully at least once -- before that,
  // refreshTimer's fastRetryMs loop already covers getting to `ready` quickly,
  // and there is no battery command to poll yet.
  Timer {
    id: batteryTimer
    interval: root.batteryPollSec * 1000
    repeat: true
    running: root.ready
    triggeredOnStart: true
    onTriggered: root.pollBattery()
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
      // signal(15) tears down the child rather than only setting `running`
      // to false, which just detaches the wrapper -- the shell would still
      // wait for the descendant to exit on its own. signal(9) follows if
      // it does not oblige.
      if (statusProcess.running) { statusProcess.signal(15); statusKillTimer.restart() }
      if (setProcess.running) { setProcess.signal(15); setKillTimer.restart() }
      root.pending = ({})
      root._queue = []
      root.lastError = "hskctl timed out"
      root.changed()
    }
  }

  Timer {
    // Same idea as `watchdog`, kept separate so a stuck battery poll tears
    // itself down quietly instead of setting `lastError` and disturbing
    // whatever the panel is showing -- this process was never allowed to
    // surface an error even when it exits cleanly (see applyBattery).
    id: batteryWatchdog
    interval: 15000
    repeat: false
    running: batteryProcess.running
    onTriggered: {
      batteryProcess.signal(15)
      batteryKillTimer.restart()
    }
  }

  // StdioCollector holds the whole stream before we can see it, so we cannot
  // bound the byte count from the shell side. SplitParser fires on each chunk,
  // and we count against a hard cap; on overflow the process is TERMed and
  // KILLed rather than allowed to keep allocating in the shell. hskctl is our
  // own trusted CLI, but the reviewer's rule is the same regardless of the
  // producer -- the cap has to be at the consumer boundary, before parsing.

  Timer {
    id: statusKillTimer
    interval: 1500
    repeat: false
    onTriggered: if (statusProcess.running) statusProcess.signal(9)
  }

  Timer {
    id: setKillTimer
    interval: 1500
    repeat: false
    onTriggered: if (setProcess.running) setProcess.signal(9)
  }

  Timer {
    id: batteryKillTimer
    interval: 1500
    repeat: false
    onTriggered: if (batteryProcess.running) batteryProcess.signal(9)
  }

  Process {
    id: statusProcess
    running: false
    command: []
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(chunk) {
        if (root._statusOverflow) return
        var s = String(chunk || "")
        root._statusStdoutBytes += s.length
        root._statusStdout = root._appendChunk(root._statusStdout, s, root._maxProcessBytes)
        if (root._statusStdoutBytes > root._maxProcessBytes) {
          root._statusOverflow = true
          statusProcess.signal(15)
          statusKillTimer.restart()
        }
      }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(chunk) {
        if (root._statusOverflow) return
        var s = String(chunk || "")
        // stderr is bounded to a smaller ceiling because it is only ever
        // rendered as a one-line error message. Truncation here is fine --
        // we already emit the exit code on failure.
        root._statusStderrBytes += s.length
        root._statusStderr = root._appendChunk(root._statusStderr, s, 4096)
      }
    }

    onExited: function(exitCode, exitStatus) {
      statusKillTimer.stop()
      root.refreshing = false
      if (root._statusOverflow) {
        root.state = "error"
        root.values = ({})
        root.writable = []
        root.pending = ({})
        root.lastError = "hskctl output exceeded " + root._maxProcessBytes + " bytes; killed"
        root.changed()
        return
      }
      var out = root._statusStdout
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
      var err = root._statusStderr.trim()
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
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(chunk) {
        if (root._setOverflow) return
        var s = String(chunk || "")
        root._setStdoutBytes += s.length
        root._setStdout = root._appendChunk(root._setStdout, s, root._maxProcessBytes)
        if (root._setStdoutBytes > root._maxProcessBytes) {
          root._setOverflow = true
          setProcess.signal(15)
          setKillTimer.restart()
        }
      }
    }
    stderr: SplitParser {
      splitMarker: ""
      onRead: function(chunk) {
        if (root._setOverflow) return
        var s = String(chunk || "")
        root._setStderrBytes += s.length
        root._setStderr = root._appendChunk(root._setStderr, s, 4096)
      }
    }

    onExited: function(exitCode, exitStatus) {
      setKillTimer.stop()
      if (root._setOverflow) {
        root.pending = ({})
        root.actionStatus = "hskctl output exceeded limit; killed"
        actionStatusTimer.restart()
        root._setField = ""
        if (root._queue.length > 0) {
          root._run(root._queue.shift())
        } else {
          drainTimer.stop()
          settleTimer.restart()
        }
        return
      }
      var parsed = Model.parseStatus(root._setStdout)
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

  Process {
    id: batteryProcess
    running: false
    command: []
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(chunk) {
        if (root._batteryOverflow) return
        var s = String(chunk || "")
        root._batteryStdoutBytes += s.length
        root._batteryStdout = root._appendChunk(root._batteryStdout, s, root._maxBatteryBytes)
        if (root._batteryStdoutBytes > root._maxBatteryBytes) {
          root._batteryOverflow = true
          batteryProcess.signal(15)
          batteryKillTimer.restart()
        }
      }
    }
    // No stderr capture here -- a failed poll is silent by design (see
    // applyBattery), so there is nothing to show a one-line error from.

    onExited: function(exitCode, exitStatus) {
      batteryKillTimer.stop()
      if (root._batteryOverflow) return
      root.applyBattery(root._batteryStdout)
    }
  }

  Component.onDestruction: {
    // Make sure any in-flight hskctl gets torn down when the panel is unloaded,
    // rather than surviving its own supervisor.
    if (statusProcess.running) statusProcess.signal(15)
    if (setProcess.running) setProcess.signal(15)
    if (batteryProcess.running) batteryProcess.signal(15)
  }
}
