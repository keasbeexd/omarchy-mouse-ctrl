.pragma library

// Pure functions shared by Service.qml and Panel.qml. Keeping the parsing and
// formatting out of QML makes the failure modes testable and keeps the panel
// free of string surgery.

// Every one of these was measured by timing the mouse's own reports. The
// register is not a single formula -- 1/2/4 divide a 1000 Hz base while 32 and
// 64 are high-rate codes -- so the panel offers exactly the rates confirmed on
// hardware. 4K included.
var POLLING_RATES = [250, 500, 1000, 2000, 4000]

function parseStatus(raw) {
  var text = String(raw || "").trim()
  if (text === "") {
    return {
      ok: false,
      state: "error",
      model: "",
      settings: {},
      error: "hskctl produced no output"
    }
  }
  var parsed
  try {
    parsed = JSON.parse(text)
  } catch (e) {
    return {
      ok: false,
      state: "error",
      model: "",
      settings: {},
      // hskctl always emits JSON, so non-JSON here means the wrapper itself
      // failed -- a missing interpreter, a stale PATH, a partial install.
      error: "could not parse hskctl output: " + text.split("\n")[0]
    }
  }
  return {
    ok: parsed.ok === true,
    state: String(parsed.state || (parsed.ok ? "ready" : "error")),
    model: String(parsed.model || ""),
    device: String(parsed.device || ""),
    detected: parsed.detected === true,
    settings: parsed.settings || {},
    writable: parsed.writable || [],
    unverified: parsed.unverified || [],
    error: String(parsed.error || "")
  }
}

function has(settings, key) {
  return settings && settings[key] !== undefined && settings[key] !== null
}

function batteryGlyph(percent, charging) {
  if (charging) return "󰂄"
  if (percent === null || percent === undefined) return "󰍽"
  if (percent >= 90) return "󰁹"
  if (percent >= 70) return "󰂀"
  if (percent >= 50) return "󰁿"
  if (percent >= 30) return "󰁽"
  if (percent >= 10) return "󰁻"
  return "󰁺"
}

function connectionGlyph(connection) {
  if (connection === "wired") return "󰌘"
  if (connection === "bluetooth") return "󰂯"
  if (connection === "dongle") return "󰖩"
  return "󰍽"
}

function connectionLabel(connection) {
  if (connection === "wired") return "Wired"
  if (connection === "bluetooth") return "Bluetooth"
  if (connection === "dongle") return "2.4 GHz"
  return ""
}

// The hero's second line: battery and link, or an honest explanation of why
// there is nothing to show.
function summaryLine(status, settings) {
  if (status === "undiscovered") return "Protocol not mapped yet"
  if (status === "error") return "Not connected"
  var parts = []
  if (has(settings, "batteryPercent")) {
    var battery = settings.batteryPercent + "%"
    if (settings.charging) battery += " charging"
    parts.push(battery)
  }
  if (has(settings, "connection")) {
    var label = connectionLabel(settings.connection)
    if (label !== "") parts.push(label)
  }
  if (has(settings, "activeDpiStage") && has(settings, "dpiStage" + settings.activeDpiStage)) {
    parts.push(settings["dpiStage" + settings.activeDpiStage] + " DPI")
  }
  if (has(settings, "pollingRate")) parts.push(settings.pollingRate + " Hz")
  return parts.length > 0 ? parts.join(" · ") : "No readings yet"
}

function barLabel(settings, showBattery) {
  if (!showBattery || !has(settings, "batteryPercent")) return ""
  return settings.batteryPercent + "%"
}

// Which DPI stages does the mouse actually have configured? Falls back to
// whatever stages reported a value, so a partial mapping still renders.
function dpiStages(settings) {
  var stages = []
  for (var i = 1; i <= 7; i++) {
    var key = "dpiStage" + i
    if (!has(settings, key)) continue
    stages.push({
      stage: i,
      dpi: settings[key],
      y: has(settings, key + "Y") ? settings[key + "Y"] : settings[key],
      color: has(settings, key + "Color") ? settings[key + "Color"] : "",
      active: has(settings, "activeDpiStage") && settings.activeDpiStage === i,
      // The axes should track together; surface it when they do not so a
      // split stage is visible rather than silently odd.
      split: has(settings, key + "Y") && settings[key + "Y"] !== settings[key]
    })
  }
  return stages
}

// The sensor steps in 50 DPI increments.
var DPI_MIN = 50
var DPI_MAX = 26000
var DPI_STEP = 50

function clampDpi(value) {
  var v = Math.round(value / DPI_STEP) * DPI_STEP
  return Math.max(DPI_MIN, Math.min(DPI_MAX, v))
}

// Stage colours the firmware ships with, offered as a cycle so a colour can be
// changed from the panel without a full picker.
var STAGE_COLORS = [
  "#aa0000", "#ffa500", "#ffff00", "#00ff00",
  "#00ffff", "#0000ff", "#800080", "#ffffff"
]

function nextStageColor(current) {
  var i = STAGE_COLORS.indexOf(String(current || "").toLowerCase())
  return STAGE_COLORS[(i + 1) % STAGE_COLORS.length]
}

function pollingOptions(current) {
  var options = []
  for (var i = 0; i < POLLING_RATES.length; i++) {
    var rate = POLLING_RATES[i]
    options.push({
      value: String(rate),
      label: rate >= 1000 ? (rate / 1000) + "K" : String(rate)
    })
  }
  return options
}

function isLow(settings, threshold) {
  if (!has(settings, "batteryPercent")) return false
  if (settings.charging) return false
  return settings.batteryPercent <= threshold
}

// Build the flat list of keyboard-navigable rows for the panel. Keeping this
// as data rather than QML structure means the cursor logic is one index into
// one array, instead of a section/row state machine.
function canWrite(writable, field) {
  if (!writable) return false
  return writable.indexOf(field) !== -1
}

function buildRows(state, settings, writable) {
  var rows = []
  if (state !== "ready") return rows

  // Only writable settings become cursor stops. A DPI stage the profile can
  // read but not yet write is still shown -- it just is not selectable, so
  // the panel never offers an action that would come back as an error.
  // One row per stage, whether or not the active-stage selector is writable --
  // the slider and colour are useful on their own.
  var stages = dpiStages(settings)
  for (var i = 0; i < stages.length; i++) {
    if (!canWrite(writable, "dpiStage" + stages[i].stage)) continue
    rows.push({
      kind: "dpiStage",
      stage: stages[i].stage,
      dpi: stages[i].dpi,
      color: stages[i].color,
      selectable: canWrite(writable, "activeDpiStage")
    })
  }
  if (canWrite(writable, "pollingRate")) rows.push({ kind: "pollingRate" })
  if (canWrite(writable, "liftOffDistance")) rows.push({ kind: "liftOffDistance" })
  if (canWrite(writable, "motionSync")) rows.push({ kind: "toggle", field: "motionSync" })
  if (canWrite(writable, "angleSnap")) rows.push({ kind: "toggle", field: "angleSnap" })
  if (canWrite(writable, "rippleControl")) rows.push({ kind: "toggle", field: "rippleControl" })
  return rows
}

function toggleLabel(field) {
  if (field === "motionSync") return "Motion Sync"
  if (field === "angleSnap") return "Angle snapping"
  if (field === "rippleControl") return "Ripple control"
  return field
}

function toggleDescription(field) {
  if (field === "motionSync") return "Align sensor reads to the polling clock"
  if (field === "angleSnap") return "Straighten near-horizontal movement"
  if (field === "rippleControl") return "Smooth jitter at high DPI"
  return ""
}

// Does this hskctl error mean "you lack permission" rather than "no mouse"?
// Matched on the message because the failure surfaces from several layers --
// open(2) returning EACCES, an ioctl refusing, or hskctl's own doctor advice --
// and they do not share an exit code.
function isPermissionError(message) {
  var text = String(message || "").toLowerCase()
  if (text === "") return false
  return text.indexOf("permission denied") >= 0
      || text.indexOf("errno 13") >= 0
      || text.indexOf("eacces") >= 0
      || text.indexOf("not permitted") >= 0
      || (text.indexOf("udev") >= 0 && text.indexOf("hidraw") >= 0)
}
