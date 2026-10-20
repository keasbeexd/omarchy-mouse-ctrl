import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import "Model.js" as Model

// Drives hskctl. Nothing but this plugin can change a setting on the mouse,
// so there is nothing to gain by re-reading them on a timer: a full status
// read only ever runs once at startup, on an explicit refresh (the refresh
// button, middle-click, 'r'), and whenever the cable/dongle is plugged or
// unplugged. A write applies its own readback directly rather than
// triggering a separate re-read. Battery percent is the one thing that
// drifts on its own, so it still gets a slow timer; plug/unplug arrives as
// an event on the same stream.
//
// All of it runs over one long-lived `hskctl serve` process: JSON requests
// in ({id, cmd, ...}), one JSON response per request out (same id), and
// watch-link events as {event} lines with no id. A click is thus one
// exchange over pipes that are already open, not a Python interpreter
// launch, a profile detect and a link re-probe. The request layer here is
// deliberately thin -- the serve loop serializes device access in arrival
// order, and the cross-process device lock still covers CLI one-shots run
// alongside it.
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
  // Rendering metadata per field (label, description, unit, widget), from
  // the same profile. The panel is a template over this: no field name or
  // label lives in the QML.
  property var meta: ({})
  // The profile's own section layout -- the panel's order comes from here,
  // and a profile for a mouse we never heard of lays itself out.
  property var sections: []
  // Stage switching, if this profile declares it: the field name to gate on,
  // and one verdict per name. The engine reports the field; the live probe
  // (run through the same channel, once per firmware, verdict cached there
  // under ~/.cache) reports the verdict. Until that verdict is literally
  // `true`, the row stays hidden; `false` is cached engine-side, `null` is
  // treated as "not proven" for the rest of the session.
  property string stageSwitchField: ""
  property var stageCapable: ({})

  // The udev rule file this machine needs, from the engine's `udev-rule`
  // request: { ruleFile, rules: [...], text } or null until asked. Lives
  // beside the settings state because the panel shows it on the permission
  // card -- which only appears when the device is unreachable, so this must
  // be answerable with no device access at all (it reads /sys only).
  property var udevRules: null

  // The capability gate as a map, in the shape buildRows/fieldRowVisible take.
  // A field with no verdict is hidden, never half-shown: the switcher
  // appearing after the probe is better than appearing before it.
  function _stageGate() {
    if (stageSwitchField === "" || stageCapable[stageSwitchField] === true) return null
    var gate = {}
    gate[stageSwitchField] = false
    return gate
  }

  // The stage context buildRows/fieldRowVisible take: the profile's
  // stage-switch field and the stage it currently holds, from
  // `effectiveValues` so a click's optimistic overlay swaps which stage's
  // rows the section shows the instant the chip is pressed. Fields tagged
  // `showForStage` render only on their one stage; a null value (stage not
  // readable, switcher absent) keeps them hidden.
  function _stageContext() {
    if (stageSwitchField === "") return null
    var v = effectiveValues[stageSwitchField]
    return { field: stageSwitchField, value: typeof v === "number" ? v : null }
  }

  function rowVisible(field) {
    return Model.fieldRowVisible(field, writable, meta, _stageGate(), _stageContext())
  }

  // Optimistic overlay: a click should move the UI immediately rather than
  // waiting a full command round trip. Cleared once the readback lands.
  property var pending: ({})

  // Battery percent is the only reading that changes on its own (charging is
  // covered instantly by the link events instead), so this can run slowly --
  // default every 5 minutes. This poll never sets `busy`, only merges
  // batteryPercent into `values`.
  readonly property int batteryPollSec: intSetting("batteryPollSec", 300, 30, 3600)
  readonly property int lowBatteryPercent: intSetting("lowBatteryPercent", 15, 0, 50)
  readonly property bool showBatteryLabel: setting("showBatteryLabel", true) === true
  // Omarchy clones the plugin to ~/.config/omarchy/plugins/<id>/, and the CLI
  // ships inside it, so the widget works with nothing else installed. There
  // is deliberately no setting to point this elsewhere: whatever runs here is
  // handed the mouse's hidraw descriptor, so it is always the reviewed copy
  // beside this file.
  readonly property string hskctl: {
    var url = Qt.resolvedUrl("bin/hskctl").toString()
    return url.indexOf("file://") === 0 ? url.substring(7) : url
  }

  // Every hskctl child gets this environment and nothing inherited. The
  // launcher is a bash script, and a non-interactive bash still sources
  // $BASH_ENV; PYTHON*, LD_PRELOAD and the like would ride in the same way.
  // hskctl needs HOME (user profiles) and XDG_RUNTIME_DIR (its device lock),
  // and every executable it names is an absolute path, so PATH is fixed.
  readonly property var childEnvironment: {
    var env = { PATH: "/usr/bin:/bin", LANG: "C.UTF-8" }
    var names = ["HOME", "XDG_RUNTIME_DIR"]
    for (var i = 0; i < names.length; i++) {
      var v = Quickshell.env(names[i])
      if (v !== undefined && v !== null && String(v) !== "") env[names[i]] = String(v)
    }
    return env
  }

  // Which build is running, for the panel footer. It comes back in hskctl's
  // own JSON, and hskctl reads it from the manifest -- so it is the version
  // that was published, reported by the copy of the CLI sitting beside this
  // QML. That matters: the usual reason the footer is interesting at all is a
  // plugin directory and a checkout having drifted apart.
  property string pluginVersion: ""

  // `busy` means a request the panel is awaiting whose kind the user can
  // actually see: a status read or a write (the stage probe and the battery
  // poll are background upkeep, and never disable controls). Response
  // streams also arrive late if one request wedges the serve loop, which is
  // why the watchdog, not `busy`, is what eventually clears them.
  readonly property bool busy: {
    for (var id in _pending) {
      if (_pending[id].kind === "status" || _pending[id].kind === "set") return true
    }
    return false
  }
  readonly property bool ready: state === "ready"
  readonly property bool lowBattery: Model.isLow(effectiveValues, lowBatteryPercent)

  // ---- stream bounds --------------------------------------------------------
  //
  // hskctl's smallest response is one watch event (a few dozen bytes); its
  // largest is one status payload (measured ~6.4 KiB with 33 fields mapped).
  // The caps belong at the consumer -- before the bytes exist in the shell --
  // and they are enforced two ways: no single line longer than any real
  // response, and no unlimited stream. Exceeding either kills the process
  // and restarts it, rather than allocating without bound.
  readonly property int _maxRequestLineBytes: 4096
  readonly property int _maxResponseLineBytes: 65536
  readonly property int _maxStreamBytes: 1048576

  // ---- request layer --------------------------------------------------------
  //
  // Requests are answered in arrival order by the serve loop, so there is no
  // queue here and no drain timer: an in-flight read cannot reorder what a
  // click just sent. Outstanding requests are tracked with a monotonic id
  // until their response lands (or the watchdog fails them); a response
  // whose id nobody is waiting for is ignored -- it must be stale.
  property int _nextRequestId: 0
  property var _pending: ({})
  readonly property int _maxPendingRequests: 16

  function _send(kind, payload) {
    if (_destroyed || !serveProcess.running) return false
    if (Object.keys(_pending).length >= _maxPendingRequests) return false
    var line = JSON.stringify(payload)
    if (line === undefined || line === null || line.length > _maxRequestLineBytes) return false
    var id = ++root._nextRequestId
    payload.id = id
    line = JSON.stringify(payload)
    var map = {}
    for (var existing in _pending) map[existing] = _pending[existing]
    map[id] = { kind: kind, at: Date.now(), field: payload.field || "" }
    _pending = map
    serveProcess.write(line + "\n")
    return true
  }

  function _pendingHas(kind) {
    for (var id in _pending) if (_pending[id].kind === kind) return true
    return false
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
  // Cursor stops: the profile's own layout, writable fields only. Anything
  // the profile cannot write never becomes a stop, so the cursor can never
  // park on a control that would error.
  readonly property var rows: Model.buildRows(state, writable, sections, meta, _stageGate(), _stageContext())

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

  // Rendering metadata helpers. The panel renders rows it receives from the
  // rows list, but labels and units are read field-by-field while building
  // the tree, so these are the one place that looks them up.
  function metaFor(field) {
    return meta[field] || null
  }

  function labelFor(field) {
    var m = meta[field]
    return m && m.label ? m.label : field
  }

  function unitFor(field) {
    var m = meta[field]
    return m && m.unit ? m.unit : ""
  }

  // Anything asking for a refresh on purpose -- the user, IPC, a plug/unplug
  // event -- also gives the automatic retry its full budget back.
  function refresh() {
    _retryFailures = 0
    return _refresh()
  }

  function _refresh() {
    // A read must not overlap a write, and a second read must not pile up
    // behind the first. The serve loop serializes in arrival order anyway;
    // skipping here only avoids queueing redundant work behind ourselves.
    if (suspended) return false
    if (_pendingHas("status") || _pendingHas("set") || _pendingHas("probe")) return false
    // A refresh clears `pending`, so one landing between a click and its write
    // would snap the number back to the old value and then forward again.
    if (Object.keys(_soon).length > 0) return false
    refreshing = true
    if (!_send("status", { cmd: "status" })) {
      refreshing = false
      return false
    }
    return true
  }

  // The fast, quiet path: one exchange for batteryPercent + charging, merged
  // into `values` without touching `busy` or `pending`. Skips rather than
  // queues behind a real read or write -- this cycle is cheap to lose, and
  // the next one is batteryPollSec away.
  function pollBattery() {
    // Deliberately NOT gated on `ready`: a battery success while the panel
    // believes the mouse unreachable is the cheap proof that the link is
    // back (the RF link recovers on its own long before a wedged status
    // read does; see applyBattery) and is what pulls a full refresh.
    if (suspended) return
    if (_pendingHas("status") || _pendingHas("set") || _pendingHas("battery")) return
    if (Object.keys(_soon).length > 0) return
    _send("battery", { cmd: "battery" })
  }

  // The stage-switch probe: deliberately not gated on battery poll or user
  // activity -- it runs once per mouse+firmware, the engine caches the
  // verdict under ~/.cache, and until the answer exists the panel simply
  // shows one fewer row. Budgeted per session (two attempts), because an
  // inconclusive probe otherwise re-stages a write every refresh for a mouse
  // whose stage register is wedged.
  readonly property int _maxStageProbeAttempts: 2
  property int _stageProbeAttempts: 0

  function _armStageProbe() {
    if (_destroyed || suspended) return
    if (stageSwitchField === "") return
    if (stageCapable[stageSwitchField] === true || stageCapable[stageSwitchField] === false) return
    if (_stageProbeAttempts >= _maxStageProbeAttempts) return
    // Never overlaps anything else; skipping is fine because the next status
    // (refresh, plug event, battery-timer gate) re-arms it.
    if (_pendingHas("status") || _pendingHas("set") || _pendingHas("probe")) return
    if (Object.keys(_soon).length > 0) return
    _stageProbeAttempts += 1
    _send("probe", { cmd: "probe-stages" })
  }

  function applyStageProbe(obj) {
    var parsed = Model.parseStageProbe(obj)
    // ok:false or a null verdict is "not proven" -- nothing is stored, and
    // the session's attempt budget already limits how often it retries.
    if (!parsed.ok || (parsed.capable !== true && parsed.capable !== false)) return
    var key = parsed.field !== "" ? parsed.field : stageSwitchField
    if (key === "") return
    var map = {}
    for (var existing in stageCapable) map[existing] = stageCapable[existing]
    map[key] = parsed.capable
    stageCapable = map
    changed()
  }

  // Asked by the panel when the permission/setup card shows: the exact rule
  // text and the connected device's ids, without touching the mouse.
  function requestUdevRules() {
    if (_destroyed) return
    if (udevRules !== null || _pendingHas("udev")) return
    // A couple of bytes of sysfs text; if serve is not up the card's other
    // content still says what to run by hand.
    if (!_serveUp) return
    _send("udev", { cmd: "udev-rule" })
  }

  function applyUdevRules(obj) {
    var rules = Model.isList(obj && obj.rules) ? obj.rules : []
    var text = typeof (obj && obj.text) === "string" ? obj.text : ""
    if (rules.length === 0 && text === "") return
    var installed = Model.isPlainObject(obj && obj.installed)
      ? { present: obj.installed.present === true,
          upToDate: obj.installed.upToDate === true }
      : { present: false, upToDate: false }
    udevRules = {
      ruleFile: typeof obj.ruleFile === "string" ? obj.ruleFile : "",
      rules: rules,
      text: text,
      installed: installed
    }
    changed()
  }

  function applyStatus(obj) {
    var parsed = Model.parseStatus(obj)
    state = parsed.state
    detected = parsed.detected
    if (parsed.model !== "") model = parsed.model
    devicePath = parsed.device
    values = parsed.settings || {}
    writable = parsed.writable || []
    allowed = parsed.allowed || {}
    meta = parsed.meta || {}
    // A profile that carries no sections yet (or an hskctl from before they
    // existed) still gets a usable panel -- one flat SETTINGS section of
    // whatever is writable -- rather than an empty one.
    sections = Model.sectionsOrFallback(parsed.state, parsed.writable || [], meta, parsed.sections || [])
    stageSwitchField = parsed.state === "ready" ? parsed.stageSwitchField : ""
    if (parsed.version !== "") pluginVersion = parsed.version
    pending = ({})
    lastError = parsed.ok ? "" : parsed.error
    changed()
    // The probe only ever runs on a ready status: there is no field to gate
    // otherwise. Cheap no-op when the capability is already known for the
    // session, or this profile has no switcher at all.
    if (state === "ready") _armStageProbe()
  }

  // Merges rather than replaces `values` -- a failed or stale poll must not
  // blank out DPI, polling rate and everything else read_all() last saw.
  // Silent on failure: this cycle just did not learn anything new, and the
  // next one is batteryPollSec away, so there is nothing useful to surface
  // as an error for what is background upkeep.
  function applyBattery(obj) {
    var parsed = Model.parseBattery(obj)
    if (!parsed.ok) return
    var learned = false
    for (var f in parsed.settings) { learned = true; break }
    if (!learned) return
    var merged = {}
    for (var key in values) merged[key] = values[key]
    for (var field in parsed.settings) merged[field] = parsed.settings[field]
    values = merged
    changed()
    // The poll is the panel's cheap probe too: while a full status read is
    // still failing (e.g. a dongle whose config endpoint wedged across a
    // suspend), the one-exchange battery read can succeed long before the
    // whole status does -- the RF link is back. A battery success in a
    // non-ready state is therefore worth a full refresh right now, rather
    // than waiting batteryPollSec for the next hint. No ping-pong risk: in
    // ready state this branch never fires, and a refresh that fails again
    // does not consume anything the battery timer does not re-arm.
    if (root.state !== "ready") root.refresh()
  }

  // Writes used to be serialized here by starting one hskctl process at a
  // time; the serve loop does that in arrival order now, so this function is
  // only the overlay plus the request.
  function set(field, value) {
    // Field names come from the panel, but they cross into the CLI's parser
    // by way of a JSON line, so drop anything that is not a field name
    // before it becomes part of the wire.
    if (!Model.isFieldName(field)) return
    _run({ field: field, value: value })
  }

  // Repeated input on the same field -- clicking "+" ten times -- must not
  // become ten USB round trips. Each write is a read-modify-write of the whole
  // DPI block and takes the device lock, so ten of them queue up on the wire
  // and the panel spends two seconds visibly catching up. Only the last value
  // matters, so hold it briefly and write once.
  //
  // The optimistic value goes into `pending` immediately, so the number on
  // screen tracks every click even though the write is one.
  property var _soon: ({})
  readonly property bool writeQueued: Object.keys(_soon).length > 0
  readonly property bool working: busy || writeQueued

  function setSoon(field, value) {
    if (!Model.isFieldName(field)) return
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
    // `String()` here matches what the old CLI arguments carried: the engine
    // re-coerces on its side, so "1600", "true" and "#ffa500" arrive as the
    // exact spellings the one-shot `set` command always handled.
    var sent = _send("set", { cmd: "set", field: String(job.field), value: String(job.value) })
    if (!sent) {
      // The process is down (or restarting after a crash). The overlay for
      // this click would otherwise hang until the watchdog; show the failure
      // instead.
      var remaining = {}
      for (var key in pending) {
        if (key !== job.field) remaining[key] = pending[key]
      }
      pending = remaining
      actionStatus = "hskctl is not running"
      actionStatusTimer.restart()
    }
  }

  function canWrite(field) {
    return Model.canWrite(writable, field)
  }

  function toggle(field) {
    if (!has(field) || !canWrite(field)) return
    set(field, value(field) ? "off" : "on")
  }

  // The one and only unconditional startup: everything after this is either
  // an explicit user action (refresh button, middle-click, 'r') or a link
  // event. A dongle that has not finished enumerating yet at login is usually
  // covered by the same event the link watcher exists for: enumeration
  // finishing is exactly a hidraw node appearing (or its permissions
  // settling, IN_ATTRIB), which arrives as a `changed` event on this very
  // stream and calls refresh() itself.
  Component.onCompleted: root._startServe()

  // Covers the case the link events can't: a wireless dongle's hidraw node
  // never disappears across a system suspend/resume (only the RF link to the
  // mouse does), so no inotify event fires when the machine wakes, yet the
  // very first refresh after resume can still land before that RF link is
  // back up and come back as `error`. Without this, `pollBattery`/`batteryTimer`
  // are gated on `state === "ready"` (see below), so a single failed refresh
  // there means nothing else ever asks the mouse again until the panel is
  // opened or refreshed by hand.
  //
  // Bounded, though: exponential backoff with jitter from 5 s up to 5 min,
  // and a budget of eight attempts (about a quarter of an hour in all) before
  // it gives up. A missing udev rule or a mouse that is simply not there
  // would otherwise start a device read every few seconds for the rest of
  // the session. Any deliberate refresh -- including a link event -- restores
  // the budget.
  readonly property int _retryMs: 5000
  readonly property int _retryMaxMs: 300000
  readonly property int _retryBudget: 8
  property int _retryFailures: 0

  function _afterStatus() {
    if (state === "ready") {
      _retryFailures = 0
      retryTimer.stop()
      return
    }
    if (_destroyed || suspended || _retryFailures >= _retryBudget) return
    var base = Math.min(_retryMaxMs, _retryMs * Math.pow(2, _retryFailures))
    retryTimer.interval = base + Math.floor(Math.random() * base * 0.2)
    _retryFailures += 1
    retryTimer.restart()
  }

  Timer {
    id: retryTimer
    repeat: false
    // Skipped because a write or read was in flight: try again at the same
    // spacing without spending budget -- that attempt never happened. Once
    // the serve process itself is gone its restart machinery owns recovery
    // (and re-reads the device through onStarted), so a respin here would
    // only tick a dead loop.
    onTriggered: {
      if (root._destroyed || root.suspended) return
      if (!serveProcess.running) return
      if (!root._refresh()) retryTimer.restart()
    }
  }

  Timer {
    id: batteryTimer
    interval: root.batteryPollSec * 1000
    repeat: true
    // Runs whenever the serve process is alive, not only when `ready`: the
    // poll is also the panel's recovery probe after a failed status (see
    // pollBattery), and a dead loop must still tick to notice a wake.
    running: root._serveUp
    triggeredOnStart: true
    onTriggered: root.pollBattery()
  }

  Timer {
    id: actionStatusTimer
    interval: 2600
    repeat: false
    onTriggered: root.actionStatus = ""
  }

  // ---- serve process --------------------------------------------------------
  //
  // Long-lived: started once and left running for the life of the panel. Its
  // death is a restart, not a mourning period -- same backoff machinery the
  // old link watcher used (exponential 2 s up to 5 min with jitter, six
  // quick failures in a row then give up; a run of a minute or more was
  // healthy and resets the count). Everything the four per-command processes
  // used to carry -- four byte counters, four kill timers, three watchdogs --
  // collapses into one watchdog and one kill timer here, because a single
  // wedged USB exchange is answered by killing the process and retrying,
  // the same way it always was; the difference is that work already in the
  // queue behind it now rides the same stream instead of being lost.

  readonly property int _serveRestartMs: 2000
  readonly property int _serveRestartMaxMs: 300000
  readonly property int _serveRestartBudget: 6
  readonly property int _serveHealthyMs: 60000
  property int _serveFailures: 0
  property real _serveStartedAt: 0

  function _scheduleServeRestart() {
    if (Date.now() - _serveStartedAt >= _serveHealthyMs) _serveFailures = 0
    if (_serveFailures >= _serveRestartBudget) return
    var base = Math.min(_serveRestartMaxMs, _serveRestartMs * Math.pow(2, _serveFailures))
    serveRestartTimer.interval = base + Math.floor(Math.random() * base * 0.2)
    _serveFailures += 1
    serveRestartTimer.restart()
  }

  Timer {
    id: serveRestartTimer
    repeat: false
    onTriggered: if (!root._destroyed) root._startServe()
  }

  Timer {
    // signal(15) tears down the child rather than only setting `running` to
    // false, which just detaches the wrapper -- the shell would still wait
    // for the descendant to exit on its own. signal(9) follows if it does
    // not oblige.
    id: serveKillTimer
    interval: 1500
    repeat: false
    onTriggered: if (serveProcess.running) serveProcess.signal(9)
  }

  // Per-kind wall-clock budget. The probe stages a real write and folds in
  // seconds of settle time, so it gets its own; everything else is bounded
  // by the same 15 s the per-command watchdogs used.
  readonly property int _requestTimeoutMs: 15000
  readonly property int _probeTimeoutMs: 30000

  function _failAllPending(message) {
    var outstanding = _pending
    _pending = ({})
    for (var id in outstanding) {
      var kind = outstanding[id].kind
      if (kind === "set") {
        var field = outstanding[id].field
        var remaining = {}
        for (var key in pending) {
          if (key !== field) remaining[key] = pending[key]
        }
        pending = remaining
        actionStatus = message
        actionStatusTimer.restart()
      } else if (kind === "status") {
        state = "error"
        values = ({})
        writable = []
        lastError = message
      }
      // battery and probe fail silently: background upkeep does not raise
      // the panel's error banner (see applyBattery/applyStageProbe).
    }
    changed()
  }

  // `true` between the serve process's started/exited signals. A dedicated
  // flag rather than `serveProcess.running` in bindings: ids declared later
  // in the file read as undefined while bindings evaluate at construction,
  // which turns a harmless startup order issue into a seconds-long error
  // chorus; a property set by the process's own handlers is order-safe.
  property bool _serveUp: false

  Timer {
    // One watchdog for the whole process, ticked once a second while anything
    // is outstanding. Armed per request at send time (the `at` stamp), so an
    // idle panel with a healthy serve process costs nothing.
    id: serveWatchdog
    interval: 1000
    repeat: true
    running: Object.keys(root._pending).length > 0 && root._serveUp
    onTriggered: {
      var now = Date.now()
      var expired = false
      for (var id in root._pending) {
        var entry = root._pending[id]
        var budget = entry.kind === "probe" ? root._probeTimeoutMs : root._requestTimeoutMs
        if (now - entry.at > budget) { expired = true; break }
      }
      if (!expired) return
      serveProcess.signal(15)
      serveKillTimer.restart()
      root._failAllPending("hskctl timed out")
    }
  }

  // ---- inbound line handling -------------------------------------------------

  property int _streamBytes: 0
  property string _lineBuffer: ""

  function _handleLine(line) {
    var obj
    try {
      obj = JSON.parse(line)
    } catch (e) {
      return
    }
    if (!Model.isPlainObject(obj)) return
    // A response carries the id it was asked under; an event has none.
    // "initial" (cold start) and "changed" (a real plug/unplug) both mean
    // the same thing here: go find out what the connection and settings
    // actually are now.
    if (obj.id === undefined) {
      if (obj.ok === true && typeof obj.event === "string" && obj.event !== "") {
        // "set-applied": another hskctl process (the other bar's serve, or a
        // terminal `hskctl set`) changed a field and already holds the
        // authoritative readback -- merge it rather than re-reading the device.
        if (obj.event === "set-applied") {
          root.applyRemoteSet(obj)
          return
        }
        // "initial" (cold start), "changed" (a real plug/unplug), and
        // "refresh" (a multi-field write elsewhere: fix-dpi, apply,
        // probe-write) all mean the same thing: go find out what the
        // connection and settings actually are now.
        root.refresh()
        return
      }
      // The one other id-less line serve writes is its startup failure
      // notice (e.g. the profile cannot be loaded), emitted before any
      // request exists. Surface it rather than letting the generic exit
      // message mask the real error.
      if (obj.ok === false && typeof obj.error === "string" && obj.error !== ""
          && Object.keys(_pending).length === 0) {
        root.state = "error"
        root.lastError = Model.plain(obj.error, 400)
      }
      return
    }
    var entry = _pending[obj.id]
    if (entry === undefined) return
    var map = {}
    for (var key in _pending) {
      if (String(key) !== String(obj.id)) map[key] = _pending[key]
    }
    _pending = map
    if (entry.kind === "status") {
      root.refreshing = false
      root.applyStatus(obj)
      root._afterStatus()
    } else if (entry.kind === "set") {
      root._resolveSet(obj, entry.field)
    } else if (entry.kind === "battery") {
      root.applyBattery(obj)
    } else if (entry.kind === "udev") {
      root.applyUdevRules(obj)
    } else if (entry.kind === "probe") {
      root.applyStageProbe(obj)
    }
  }

  // A change another hskctl process made and published: the field's value is
  // the writer's own readback, so merging is the whole job. Merges rather
  // than replaces, like applyBattery -- a partial notification must not
  // blank out everything else. A field still under this panel's own
  // optimistic overlay keeps it: the in-flight write's own readback will
  // resolve it, which stays the more truthful of the two.
  function applyRemoteSet(obj) {
    var field = typeof obj.field === "string" ? obj.field : ""
    if (!Model.isFieldName(field)) return
    var remote = Model.cleanValue(obj.value)
    if (remote === undefined) return
    var merged = {}
    for (var key in values) merged[key] = values[key]
    merged[field] = remote
    values = merged
    changed()
  }

  function _resolveSet(obj, field) {
    // `set`'s own readback is the authoritative value -- the mouse just
    // confirmed it, so there is nothing a separate re-read would add.
    var parsed = Model.parseSet(obj)
    if (!parsed.ok) {
      root.actionStatus = parsed.error !== ""
        ? parsed.error
        : ("Could not set " + field)
      actionStatusTimer.restart()
    } else if (parsed.field !== "") {
      var merged = {}
      for (var key in root.values) merged[key] = root.values[key]
      merged[parsed.field] = parsed.value
      root.values = merged
    }
    // Either way the optimistic overlay for this field is resolved now --
    // on success it matches what was just merged into `values`, and on
    // failure showing the stale guess is worse than showing the truth.
    var remaining = {}
    for (var pendingKey in root.pending) {
      if (pendingKey !== field) remaining[pendingKey] = root.pending[pendingKey]
    }
    root.pending = remaining
    root.changed()
  }

  Process {
    id: serveProcess
    running: false
    command: [hskctl, "serve"]
    clearEnvironment: true
    environment: root.childEnvironment
    // Raw chunks, not a newline SplitParser: that one has to buffer until a
    // newline arrives before onRead can see the line, so it is no byte
    // ceiling at all. Lines are assembled here against hard caps -- one per
    // line, one for the whole window -- and a line longer than any real
    // hskctl response is dropped unread.
    stdout: SplitParser {
      splitMarker: ""
      onRead: function(chunk) {
        var buf = root._lineBuffer + String(chunk || "")
        root._streamBytes += buf.length
        if (root._streamBytes > root._maxStreamBytes) {
          // Unbounded accumulation is the thing this cap exists to prevent;
          // kill + restart, exactly like a wedged request.
          root._lineBuffer = ""
          root._streamBytes = 0
          serveProcess.signal(15)
          serveKillTimer.restart()
          root._failAllPending("hskctl output exceeded its byte budget; killed")
          return
        }
        var nl
        while ((nl = buf.indexOf("\n")) !== -1) {
          var line = buf.substring(0, nl)
          buf = buf.substring(nl + 1)
          if (line.length <= root._maxResponseLineBytes) root._handleLine(line)
        }
        if (buf.length > root._maxResponseLineBytes) {
          // A partial line that should already have been terminated was not;
          // discarding it is worth more than carrying a prefix that no
          // future JSON will complete from the middle.
          root._lineBuffer = ""
          return
        }
        root._lineBuffer = buf
      }
    }
    // No stderr capture: production hskctl is silent on stderr, and a
    // wedged process is answered by the watchdog, not by parsing messages.

    onStarted: {
      root._serveUp = true
      root._streamBytes = 0
      root._lineBuffer = ""
      root._serveStartedAt = Date.now()
      // Startup reads the device once, like the first refresh always did --
      // and deliberately through refresh(), since a serve (re)start is an
      // event on purpose, which restores the retry budget.
      root.refresh()
    }

    onExited: function(exitCode, exitStatus) {
      root._serveUp = false
      serveKillTimer.stop()
      root._lineBuffer = ""
      root._streamBytes = 0
      root.refreshing = false
      if (root._destroyed) return
      root._failAllPending("hskctl stopped running")
      root._scheduleServeRestart()
    }
  }

  property bool _destroyed: false

  function _startServe() {
    if (root._destroyed) return
    root._lineBuffer = ""
    root._streamBytes = 0
    serveProcess.running = true
  }

  Component.onDestruction: {
    // Make sure the serve process is torn down when the panel is unloaded,
    // rather than surviving its own supervisor. It owns no device lock at
    // that moment -- the lock is held per request -- so this is purely a
    // child-process cleanup.
    root._destroyed = true
    if (serveProcess.running) serveProcess.signal(15)
  }
}
