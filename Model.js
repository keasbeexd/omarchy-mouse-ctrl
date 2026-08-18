.pragma library

// Pure functions shared by Service.qml and Panel.qml. Keeping the parsing and
// formatting out of QML makes the failure modes testable and keeps the panel
// free of string surgery.

// Measured on hardware: the polling register divides a 1000 Hz base clock, so
// the reachable rates are 1000/n. These are the divisors that land on round
// numbers; the ceiling is the base, not the "4K" on the box.
var POLLING_RATES = [1000, 500, 250, 125]

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
  // With no stage count reported, show every stage that has a value rather
  // than guessing a number and hiding real ones.
  var count = has(settings, "dpiStageCount") ? settings.dpiStageCount : 7
  for (var i = 1; i <= 7; i++) {
    var key = "dpiStage" + i
    if (!has(settings, key)) continue
    if (i > count) continue
    stages.push({
      stage: i,
      dpi: settings[key],
      active: has(settings, "activeDpiStage") && settings.activeDpiStage === i
    })
  }
  return stages
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
  if (canWrite(writable, "activeDpiStage")) {
    var stages = dpiStages(settings)
    for (var i = 0; i < stages.length; i++) {
      rows.push({ kind: "dpiStage", stage: stages[i].stage, dpi: stages[i].dpi })
    }
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
