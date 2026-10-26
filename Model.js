// Pure helpers for Panel.qml and Backend.qml: validation of what `mousectrl
// serve` reports, formatting, and the keyboard cursor's row list. No QML
// objects in here, so every function can be exercised in plain JavaScript.

// Neutralise a string for host components that render with Qt.AutoText and
// cannot be pinned to PlainText from a plugin (BarIconButton tooltips). On
// AutoText, Qt enters rich-text mode for anything that looks like markup, and
// a rich-text <img src="..."> is a real fetch from the shell process. Device
// names come from the mouse's own firmware, so they are untrusted: strip the
// characters Qt keys on, C0/C1 controls and bidi overrides, and cap length.
function plain(text, limit) {
  var s = String(text == null ? "" : text)
  var cap = (typeof limit === "number" && limit > 0) ? limit : 200
  var out = ""
  for (var i = 0; i < s.length && out.length < cap; i++) {
    var c = s.charCodeAt(i)
    if (c < 0x20 || c === 0x7f || (c >= 0x80 && c <= 0x9f)) continue
    if ((c >= 0x202a && c <= 0x202e) || (c >= 0x2066 && c <= 0x2069)) continue
    if (c === 0x26 || c === 0x3c || c === 0x3e) continue
    out += s[i]
  }
  return out
}

// --- validating serve payloads ----------------------------------------------
//
// serve is our own process, but what it relays comes from mice and from
// profile files in the user's config. Anything off-shape is dropped, never
// coerced, and every collection is capped.

var KINDS = ["battery", "dpiStages", "enum", "toggle", "duration", "info"]
// Panel sections in display order; "device" is the footer.
var GROUPS = ["dpi", "performance", "sensor", "power", "device"]
var GROUP_TITLES = { dpi: "DPI", performance: "PERFORMANCE", sensor: "SENSOR", power: "POWER", device: "" }
var CAP_ID = /^[a-z][a-zA-Z0-9]{0,31}$/
var COLOR = /^#[0-9a-f]{6}$/
var MAX_DEVICES = 16
var MAX_CAPS = 64
var MAX_OPTIONS = 32
var MAX_STAGES = 16

function isObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v)
}

function isInt(v) {
  return typeof v === "number" && isFinite(v) && Math.floor(v) === v
}

function cleanDevices(payload) {
  if (!isObject(payload) || !Array.isArray(payload.devices)) return []
  var out = []
  for (var i = 0; i < payload.devices.length && out.length < MAX_DEVICES; i++) {
    var d = payload.devices[i]
    if (!isObject(d) || typeof d.key !== "string" || d.key.length > 200) continue
    out.push({
      key: d.key,
      name: plain(d.name, 80),
      vidpid: plain(d.vidpid, 9),
      accessible: d.accessible === true,
      family: isObject(d.family) ? { id: plain(d.family.id, 32), title: plain(d.family.title, 40) } : null,
      profile: isObject(d.profile) ? { id: plain(d.profile.id, 64), name: plain(d.profile.name, 64) } : null
    })
  }
  return out
}

function cleanCapability(c) {
  if (!isObject(c) || typeof c.id !== "string" || !CAP_ID.test(c.id)) return null
  if (KINDS.indexOf(c.kind) < 0 || GROUPS.indexOf(c.group) < 0) return null
  var options = []
  if (Array.isArray(c.options))
    for (var i = 0; i < c.options.length && options.length < MAX_OPTIONS; i++) {
      var o = c.options[i]
      if (isInt(o) || (typeof o === "string" && o.length <= 32)) options.push(o)
    }
  if (c.kind === "enum" && options.length === 0) return null
  return {
    id: c.id,
    kind: c.kind,
    label: plain(c.label, 40),
    unit: plain(c.unit, 8),
    options: options,
    minimum: isInt(c.minimum) ? c.minimum : null,
    maximum: isInt(c.maximum) ? c.maximum : null,
    step: isInt(c.step) && c.step > 0 ? c.step : 1,
    maxStages: isInt(c.maxStages) ? Math.min(c.maxStages, MAX_STAGES) : null,
    group: c.group,
    verification: c.verification === "verified" ? "verified" : "untested",
    readOnly: c.readOnly === true,
    hasColors: c.hasColors === true,
    switchable: c.switchable === true,
    writable: c.writable === true,
    visible: c.visible !== false,
    state: typeof c.state === "string" ? c.state : null
  }
}

// Valid capabilities in section order, driver order within a section.
function cleanCapabilities(payload) {
  if (!isObject(payload) || !Array.isArray(payload.capabilities)) return []
  var out = []
  for (var i = 0; i < payload.capabilities.length && out.length < MAX_CAPS; i++) {
    var c = cleanCapability(payload.capabilities[i])
    if (c !== null && c.state !== "rejected") out.push(c)
  }
  for (var j = 0; j < out.length; j++) out[j].order = GROUPS.indexOf(out[j].group) * MAX_CAPS + j
  out.sort(function(a, b) { return a.order - b.order })
  return out
}

function cleanStages(v) {
  if (!isObject(v) || !Array.isArray(v.stages) || v.stages.length > MAX_STAGES) return null
  var stages = []
  for (var i = 0; i < v.stages.length; i++) {
    var s = v.stages[i]
    if (!isObject(s) || !isInt(s.dpi)) return null
    stages.push({ dpi: s.dpi, color: typeof s.color === "string" && COLOR.test(s.color) ? s.color : null })
  }
  if (!isInt(v.active) || v.active < 0 || v.active >= stages.length) return null
  return { active: v.active, stages: stages }
}

// A value is kept only if it has the shape its capability's kind defines.
function cleanValue(cap, v) {
  if (cap.kind === "battery") {
    if (!isObject(v) || !isInt(v.percent) || v.percent < 0 || v.percent > 100) return undefined
    return { percent: v.percent, charging: v.charging === true }
  }
  if (cap.kind === "dpiStages") {
    var stages = cleanStages(v)
    return stages === null ? undefined : stages
  }
  if (cap.kind === "toggle") return typeof v === "boolean" ? v : undefined
  if (cap.kind === "duration") return isInt(v) ? v : undefined
  if (cap.kind === "enum") return (isInt(v) || typeof v === "string") ? v : undefined
  if (cap.kind === "info") return typeof v === "string" ? plain(v, 60) : undefined
  return undefined
}

// --- lookups ----------------------------------------------------------------

function findCap(caps, id) {
  for (var i = 0; i < caps.length; i++) if (caps[i].id === id) return caps[i]
  return null
}

function firstOfKind(caps, kind) {
  for (var i = 0; i < caps.length; i++) if (caps[i].kind === kind && caps[i].visible) return caps[i]
  return null
}

// The device to show: the one asked for if still connected, else the first
// with a profile, else the first at all.
function pickDevice(devices, preferredKey) {
  for (var i = 0; i < devices.length; i++) if (devices[i].key === preferredKey) return devices[i]
  for (var j = 0; j < devices.length; j++) if (devices[j].profile) return devices[j]
  return devices.length > 0 ? devices[0] : null
}

function nextDeviceKey(devices, currentKey) {
  if (devices.length === 0) return ""
  for (var i = 0; i < devices.length; i++)
    if (devices[i].key === currentKey) return devices[(i + 1) % devices.length].key
  return devices[0].key
}

// The settings of one section, as drawn under its header: everything visible
// except the stage list and battery, which have their own places.
function sectionCaps(caps, group) {
  return caps.filter(function(c) {
    return c.group === group && c.visible && c.kind !== "dpiStages" && c.kind !== "battery"
  })
}

// Non-empty sections after DPI, for the settings area.
function settingSections(caps) {
  var out = []
  for (var i = 1; i < GROUPS.length - 1; i++) {
    var list = sectionCaps(caps, GROUPS[i])
    if (list.length > 0) out.push({ id: GROUPS[i], title: GROUP_TITLES[GROUPS[i]], caps: list })
  }
  return out
}

function footerText(caps, values, version) {
  var parts = []
  var device = sectionCaps(caps, "device")
  for (var i = 0; i < device.length; i++)
    if (values[device[i].id] !== undefined) parts.push(device[i].label + " " + valueText(device[i], values[device[i].id]))
  if (version) parts.push("Mouse CTRL v" + version)
  return parts.join("  ·  ")
}

// Digits and dots only; anything else (the CLI's "0.0.0-unpackaged" fallback
// included) is not shown.
function cleanVersion(value) {
  return typeof value === "string" && /^[0-9]{1,4}(\.[0-9]{1,4}){0,3}$/.test(value) ? value : ""
}

// --- formatting -------------------------------------------------------------

// Chip labels stay short so a row of them fits: rates read 125 … 1K 2K 4K,
// other units are kept (1 mm).
function optionLabel(cap, option) {
  if (cap.unit === "Hz" && isInt(option)) return option >= 1000 ? (option / 1000) + "K" : String(option)
  return cap.unit && isInt(option) ? option + " " + cap.unit : String(option)
}

function valueText(cap, value) {
  if (value === undefined || value === null) return "—"
  if (cap.kind === "toggle") return value ? "On" : "Off"
  if (cap.kind === "enum") return cap.unit && isInt(value) ? value + " " + cap.unit : String(value)
  if (cap.kind === "duration") return value + (cap.unit ? " " + cap.unit : "")
  return String(value)
}

function batteryGlyph(battery) {
  if (!battery) return "󰍽"
  if (battery.charging) return "󰂄"
  var p = battery.percent
  if (p >= 90) return "󰁹"
  if (p >= 70) return "󰂀"
  if (p >= 50) return "󰁿"
  if (p >= 30) return "󰁽"
  if (p >= 10) return "󰁻"
  return "󰁺"
}

// --- DPI stages -------------------------------------------------------------

// Eight presets cycled with one click; the plugin's own set, not a palette
// read from any firmware.
var STAGE_COLORS = ["#ff0000", "#ffa500", "#ffff00", "#00ff00", "#00ffff", "#0000ff", "#800080", "#ffffff"]

function colorIndex(color) {
  return STAGE_COLORS.indexOf(String(color || ""))
}

function cycleColor(current, delta) {
  var i = STAGE_COLORS.indexOf(String(current || ""))
  var n = STAGE_COLORS.length
  return STAGE_COLORS[((i < 0 ? -1 : i) + delta + n) % n]
}

function withStage(value, index, change) {
  var stages = []
  for (var i = 0; i < value.stages.length; i++) {
    var s = value.stages[i]
    stages.push(i === index
      ? { dpi: change.dpi !== undefined ? change.dpi : s.dpi, color: change.color !== undefined ? change.color : s.color }
      : { dpi: s.dpi, color: s.color })
  }
  return { active: value.active, stages: stages }
}

function withActive(value, index) {
  return { active: index, stages: value.stages }
}

function clampDpi(cap, dpi) {
  var step = cap.step || 1
  var lo = cap.minimum !== null ? cap.minimum : step
  var hi = cap.maximum !== null ? cap.maximum : dpi
  var snapped = lo + Math.round((dpi - lo) / step) * step
  return Math.max(lo, Math.min(hi, snapped))
}

// --- keyboard cursor --------------------------------------------------------
//
// One flat list of focusable rows, top to bottom as drawn: the device
// switcher (only with more than one mouse), the stage chips and the selected
// stage's value and color, then every visible setting. Info and battery are
// not focusable.

function buildRows(caps, deviceCount) {
  var rows = []
  if (deviceCount > 1) rows.push({ type: "device", capId: "" })
  var stages = firstOfKind(caps, "dpiStages")
  if (stages) {
    rows.push({ type: "stages", capId: stages.id })
    if (stages.writable) {
      rows.push({ type: "stageDpi", capId: stages.id })
      if (stages.hasColors) rows.push({ type: "stageColor", capId: stages.id })
    }
  }
  for (var i = 0; i < caps.length; i++) {
    var c = caps[i]
    if (c.visible && c.writable && (c.kind === "enum" || c.kind === "toggle" || c.kind === "duration"))
      rows.push({ type: "setting", capId: c.id })
  }
  return rows
}

function rowIndex(rows, type, capId) {
  for (var i = 0; i < rows.length; i++)
    if (rows[i].type === type && (capId === undefined || rows[i].capId === capId)) return i
  return -1
}

// The value an h/l press moves a setting to, or undefined at either end.
function steppedValue(cap, value, delta) {
  if (cap.kind === "toggle") return !value
  if (cap.kind === "enum") {
    var i = cap.options.indexOf(value)
    var j = i < 0 ? 0 : i + delta
    return j >= 0 && j < cap.options.length ? cap.options[j] : undefined
  }
  if (cap.kind === "duration") {
    var next = (isInt(value) ? value : cap.minimum) + delta * cap.step
    if (cap.minimum !== null && next < cap.minimum) return undefined
    if (cap.maximum !== null && next > cap.maximum) return undefined
    return next
  }
  return undefined
}

// --- setup wizard -------------------------------------------------------------

// The only shape a udev rule line may take before it is handed to root: the
// same pattern install.sh enforces. A rule grants device access, so text
// that does not match is refused whole rather than edited into shape.
var RULE_LINE = /^KERNEL=="hidraw\*", ATTRS\{idVendor\}=="[0-9a-f]{4}"(, ATTRS\{idProduct\}=="[0-9a-f]{4}")?, MODE="0660", TAG\+="uaccess"$/
var RULE_FILE = "/etc/udev/rules.d/60-mousectrl.rules"

function isValidRuleText(text) {
  if (typeof text !== "string" || text.length === 0 || text.length > 65536) return false
  var lines = text.split("\n")
  var rules = 0
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i]
    if (line === "" || line.charAt(0) === "#") continue
    if (!RULE_LINE.test(line)) return false
    rules++
  }
  return rules > 0
}

// What the confirm step asks about, one group at a time: the readings of
// each section, with the battery and device facts first.
function confirmGroups(caps, values) {
  var order = ["device"].concat(GROUPS.slice(0, GROUPS.length - 1))
  var out = []
  for (var g = 0; g < order.length; g++) {
    var rows = []
    for (var i = 0; i < caps.length; i++) {
      var c = caps[i]
      if (c.group !== order[g] || c.kind === "dpiStages") continue
      var v = values[c.id]
      rows.push({ label: c.label, text: c.kind === "battery" && v ? v.percent + "%" + (v.charging ? " charging" : "") : valueText(c, v) })
    }
    var stages = order[g] === "dpi" ? firstOfKind(caps, "dpiStages") : null
    if (rows.length === 0 && !stages) continue
    out.push({ id: order[g], title: order[g] === "device" ? "BASICS" : GROUP_TITLES[order[g]], rows: rows, stagesId: stages ? stages.id : "" })
  }
  return out
}

// The state each capability is saved with, from the wizard's answers:
// a group the user said was wrong is rejected; a read-only reading is
// verified when the driver vouches for it; a writable setting is verified
// (driver-proven), tested (passed its test here) or left read-only.
function finalStates(caps, rejectedGroups, testResults, hiddenGroups) {
  var out = {}
  for (var i = 0; i < caps.length; i++) {
    var c = caps[i]
    var state
    if (rejectedGroups[c.group] === true) state = "rejected"
    else if (c.readOnly || c.verification === "verified") state = c.verification === "verified" ? "verified" : "readOnly"
    else state = testResults[c.id] === "passed" ? "tested" : "readOnly"
    out[c.id] = { state: state, visible: hiddenGroups[c.group] !== true }
  }
  return out
}

// --- libratbag ----------------------------------------------------------------

// ratbagd's readings for a mouse it manages, as probe relays them; shown
// read-only, its settings belong to Piper.
function cleanRatbag(r) {
  if (!isObject(r)) return null
  var stages = []
  if (Array.isArray(r.stages))
    for (var i = 0; i < r.stages.length && stages.length < MAX_STAGES; i++)
      if (isObject(r.stages[i]) && isInt(r.stages[i].dpi))
        stages.push({ dpi: r.stages[i].dpi, active: r.stages[i].active === true })
  return {
    name: plain(r.name, 80),
    firmware: plain(r.firmware, 40),
    profile: isInt(r.profile) ? r.profile : 0,
    profiles: isInt(r.profiles) ? r.profiles : 0,
    pollingRate: isInt(r.pollingRate) ? r.pollingRate : null,
    stages: stages
  }
}

function ratbagStagesText(r) {
  return r.stages.map(function(s) { return s.active ? "[" + s.dpi + "]" : String(s.dpi) }).join("  ")
}
