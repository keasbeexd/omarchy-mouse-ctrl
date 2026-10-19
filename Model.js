// Pure functions shared by Service.qml and Panel.qml. Keeping the parsing and
// formatting out of QML makes the failure modes testable and keeps the panel
// free of string surgery.

// Neutralise a string for shared host components that render text with
// Qt.AutoText and cannot be pinned to PlainText from a plugin -- BarIconButton
// tooltips, PanelHero titles, section headers, ConfirmDialog messages. On
// AutoText, Qt sniffs the string and enters rich-text mode if it looks like
// markup; a rich-text `<img src="...">` in a device or setting name is then a
// real fetch from the shell process to whatever the string's author picks.
// So strip the three characters Qt keys on (& before < > because escaping <
// as &lt; would turn it back on when Qt sees &), plus C0 and C1 controls and
// bidi overrides, and cap the length so a runaway string cannot fill the bar.
//
// Every value from the device is a candidate: an HSK's product string is set
// by the mouse's own firmware, an error message can carry any bytes hskctl
// wants to include, and a model name in a future profile is free text.
function plain(text, limit) {
  var s = String(text == null ? "" : text)
  var out = ""
  var cap = (typeof limit === "number" && limit > 0) ? limit : 200
  for (var i = 0; i < s.length && out.length < cap; i++) {
    var c = s.charCodeAt(i)
    if (c < 0x20 || c === 0x7f) continue                        // C0 + DEL
    if (c >= 0x80 && c <= 0x9f) continue                        // C1
    if (c === 0x202a || c === 0x202b || c === 0x202c            // bidi
        || c === 0x202d || c === 0x202e
        || c === 0x2066 || c === 0x2067 || c === 0x2068 || c === 0x2069) continue
    if (c === 0x26 /* & */ || c === 0x3c /* < */ || c === 0x3e /* > */) continue
    out += s[i]
  }
  return out
}

// --- the shape of hskctl's JSON ---------------------------------------------
//
// hskctl is our own CLI, but what it reports comes from the mouse's firmware
// and from profiles, one of which can live in the user's ~/.config. So its
// output is validated like any other input before anything is installed as a
// model: field names are short identifiers, values are finite numbers,
// booleans or short strings, and every collection is capped. Anything else
// is dropped rather than coerced into shape.
//
// Nothing else in this file invents an option list for any field either: a
// control that appears comes from the profile's own allowed values, or it
// does not appear at all. Nothing from one mouse is carried into another by
// guesswork.

var MAX_FIELDS = 64
var MAX_ALLOWED_VALUES = 32
var MAX_VALUE_CHARS = 64
var FIELD_NAME = /^[A-Za-z][A-Za-z0-9]{0,39}$/
var STATES = ["loading", "ready", "undiscovered", "error"]

function isFieldName(key) {
  return typeof key === "string" && FIELD_NAME.test(key)
      && key !== "constructor" && key !== "prototype"
}

function isPlainObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v)
}

// A list in the widest sense that matters here. Values that cross a QML
// QObject property boundary (anything read back off a `property var`) come
// back as wrapped sequence objects: they index, iterate and slice like
// arrays -- `fields.length`, `fields[i]`, `Array.prototype.slice.call`
// all work -- but `Array.isArray()` returns false for them, and a check
// written that way silently hides every row that depends on it. The panel's
// vanished sections were exactly this. Whatever the data's route here, the
// shape test must stay the same, so every check that can receive such a
// value goes through this one.
function isList(v) {
  if (Array.isArray(v)) return true
  return v !== null && v !== undefined && typeof v === "object"
      && typeof v.length === "number"
      && typeof v.slice === "function"
}

// A single reading: a finite number, a boolean, or a short plain string.
// Returns undefined for anything else so callers can drop the field.
function cleanValue(v) {
  if (typeof v === "boolean") return v
  if (typeof v === "number") return isFinite(v) ? v : undefined
  if (typeof v === "string") return plain(v, MAX_VALUE_CHARS)
  if (v === null) return null
  return undefined
}

function cleanSettings(raw) {
  var out = {}
  if (!isPlainObject(raw)) return out
  var n = 0
  for (var key in raw) {
    if (n >= MAX_FIELDS) break
    if (!Object.prototype.hasOwnProperty.call(raw, key) || !isFieldName(key)) continue
    var v = cleanValue(raw[key])
    if (v === undefined) continue
    out[key] = v
    n++
  }
  return out
}

function cleanWritable(raw) {
  var out = []
  if (!isList(raw)) return out
  for (var i = 0; i < raw.length && out.length < MAX_FIELDS; i++) {
    if (isFieldName(raw[i]) && out.indexOf(raw[i]) === -1) out.push(raw[i])
  }
  return out
}

function cleanAllowed(raw) {
  var out = {}
  if (!isPlainObject(raw)) return out
  var n = 0
  for (var key in raw) {
    if (n >= MAX_FIELDS) break
    if (!Object.prototype.hasOwnProperty.call(raw, key) || !isFieldName(key)) continue
    var list = raw[key]
    if (!isList(list)) continue
    var clean = []
    for (var i = 0; i < list.length && clean.length < MAX_ALLOWED_VALUES; i++) {
      var v = cleanValue(list[i])
      if (v !== undefined && v !== null) clean.push(v)
    }
    out[key] = clean
    n++
  }
  return out
}

// How a field asks to be rendered. hskctl derives the widget from the
// field's byte shape and passes the profile's own label through, but both
// sides of that pipe are JSON we parse, so everything here is re-validated
// with the same rules the other collections get before it reaches QML.
var WIDGETS = ["toggle", "chips", "slider", "color", "info"]

function cleanMeta(raw) {
  var out = {}
  if (!isPlainObject(raw)) return out
  var n = 0
  for (var key in raw) {
    if (n >= MAX_FIELDS) break
    if (!Object.prototype.hasOwnProperty.call(raw, key) || !isFieldName(key)) continue
    var meta = raw[key]
    if (!isPlainObject(meta)) continue
    var clean = {}
    var widget = meta.widget
    if (WIDGETS.indexOf(widget) !== -1) clean.widget = widget
    if (typeof meta.label === "string" && meta.label) clean.label = plain(meta.label, 40)
    if (typeof meta.description === "string" && meta.description) clean.description = plain(meta.description, 120)
    if (typeof meta.unit === "string" && meta.unit) clean.unit = plain(meta.unit, 8)
    if (typeof meta.step === "number" && isFinite(meta.step) && meta.step > 0) clean.step = meta.step
    // A field with no recognizable widget is not rendered at all rather than
    // guessed at -- the fallback here is the same info row the engine means
    // for read-only diagnostics, and writable rows must never degrade that far.
    if (!WIDGETS.includes(clean.widget)) continue
    out[key] = clean
    n++
  }
  return out
}

// The profile's own section layout, capped the way every other collection
// is. A section survives only with a title and known, distinct field names;
// `hskctl` already filtered to mapped fields, so what survives here decides
// the panel's whole order -- which is exactly why it gets a hard cap.
var MAX_SECTIONS = 16
var MAX_SECTION_FIELDS = 16

function cleanSections(raw) {
  var out = []
  if (!isList(raw)) return out
  for (var i = 0; i < raw.length && out.length < MAX_SECTIONS; i++) {
    var section = raw[i]
    if (!isPlainObject(section)) continue
    var title = typeof section.title === "string" ? plain(section.title, 24) : ""
    if (!title) continue
    var fields = section.fields
    if (!isList(fields)) continue
    var clean = []
    for (var j = 0; j < fields.length && clean.length < MAX_SECTION_FIELDS; j++) {
      var f = fields[j]
      if (isFieldName(f) && clean.indexOf(f) === -1) clean.push(f)
    }
    if (clean.length > 0) out.push({ title: title, fields: clean })
  }
  return out
}

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
      error: "could not parse hskctl output: " + plain(text.split("\n")[0], 200)
    }
  }
  if (!isPlainObject(parsed)) {
    return { ok: false, state: "error", model: "", settings: {}, error: "hskctl output is not an object" }
  }
  var state = typeof parsed.state === "string" ? parsed.state : (parsed.ok === true ? "ready" : "error")
  if (STATES.indexOf(state) === -1) state = "error"
  return {
    ok: parsed.ok === true,
    state: state,
    model: typeof parsed.model === "string" ? plain(parsed.model, 120) : "",
    device: typeof parsed.device === "string" ? plain(parsed.device, 120) : "",
    detected: parsed.detected === true,
    settings: cleanSettings(parsed.settings),
    writable: cleanWritable(parsed.writable),
    allowed: cleanAllowed(parsed.allowed),
    meta: cleanMeta(parsed.ui),
    sections: cleanSections(parsed.sections),
    // The one field that carries an extra runtime gate (a live probe the
    // service runs once per firmware). A field name or nothing -- validated
    // like every name that reaches QML.
    stageSwitchField: isFieldName(parsed.stageSwitchField) ? parsed.stageSwitchField : "",
    version: typeof parsed.version === "string" ? plain(parsed.version, 32) : "",
    error: typeof parsed.error === "string" ? plain(parsed.error, 400) : ""
  }
}

// The verdict of the stage-switch probe: capable true/false, or null when
// inconclusive. `reason` is device-adjacent text and gets the same
// sanitisation as any error string. The name of the field probed comes
// straight back so the capability is stored under the name it was asked for.
function parseStageProbe(raw) {
  var text = String(raw || "").trim()
  if (text === "") return { ok: false, capable: null, field: "", cached: false, reason: "" }
  var parsed
  try {
    parsed = JSON.parse(text)
  } catch (e) {
    return { ok: false, capable: null, field: "", cached: false, reason: plain(text.split("\n")[0], 200) }
  }
  if (!isPlainObject(parsed)) return { ok: false, capable: null, field: "", cached: false, reason: "" }
  var out = {
    ok: parsed.ok === true,
    field: isFieldName(parsed.field) ? parsed.field : "",
    capable: typeof parsed.capable === "boolean" ? parsed.capable : null,
    cached: parsed.cached === true,
    reason: typeof parsed.reason === "string" ? plain(parsed.reason, 200) : ""
  }
  return out
}

// Same shape of parsing as parseStatus, for `hskctl battery`'s much smaller
// payload -- just ok/settings, no model/writable/allowed to carry.
function parseBattery(raw) {
  var text = String(raw || "").trim()
  if (text === "") return { ok: false, settings: {} }
  try {
    var parsed = JSON.parse(text)
    if (!isPlainObject(parsed)) return { ok: false, settings: {} }
    return { ok: parsed.ok === true, settings: cleanSettings(parsed.settings) }
  } catch (e) {
    return { ok: false, settings: {} }
  }
}

// hskctl's `set` payload: {ok, field, requested, value, link, error}. `value`
// is the mouse's own readback of what it just wrote -- the authoritative
// number to show, no separate re-read needed.
function parseSet(raw) {
  var text = String(raw || "").trim()
  if (text === "") return { ok: false, field: "", value: undefined, error: "hskctl produced no output" }
  try {
    var parsed = JSON.parse(text)
    if (!isPlainObject(parsed)) return { ok: false, field: "", value: undefined, error: "hskctl output is not an object" }
    var field = isFieldName(parsed.field) ? parsed.field : ""
    var value = cleanValue(parsed.value)
    return {
      // A readback the panel cannot represent is not a success.
      ok: parsed.ok === true && field !== "" && value !== undefined,
      field: field,
      value: value,
      error: typeof parsed.error === "string" ? plain(parsed.error, 400) : ""
    }
  } catch (e) {
    return { ok: false, field: "", value: undefined, error: "could not parse hskctl output: " + plain(text.split("\n")[0], 200) }
  }
}

// hskctl's `watch-link` payload: one line per event, {ok, event, connected}.
function parseLinkEvent(raw) {
  var text = String(raw || "").trim()
  if (text === "") return null
  try {
    var parsed = JSON.parse(text)
    if (!isPlainObject(parsed) || parsed.ok !== true) return null
    return { event: String(parsed.event || ""), connected: parsed.connected === true }
  } catch (e) {
    return null
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
  // Only stage 1 is ever mapped -- see the profiles' own notes on why DPI
  // stage switching is not exposed.
  if (has(settings, "dpiStage1")) {
    parts.push(settings.dpiStage1 + " DPI")
  }
  if (has(settings, "pollingRate")) parts.push(settings.pollingRate + " Hz")
  return parts.length > 0 ? parts.join(" · ") : "No readings yet"
}

function barLabel(settings, showBattery) {
  if (!showBattery || !has(settings, "batteryPercent")) return ""
  return settings.batteryPercent + "%"
}

// Eight preset colors the panel cycles through with one click, so a color can
// be changed without opening a full picker. This is the plugin's own set, not
// a palette read back from the firmware; the hex field below accepts any
// #rrggbb color the LED supports.
var STAGE_COLORS = [
  "#aa0000", "#ffa500", "#ffff00", "#00ff00",
  "#00ffff", "#0000ff", "#800080", "#ffffff"
]

function nextStageColor(current) {
  var i = STAGE_COLORS.indexOf(String(current || "").toLowerCase())
  return STAGE_COLORS[(i + 1) % STAGE_COLORS.length]
}

// One-click presets shown beside the current-color swatch. Three, not the
// full STAGE_COLORS cycle -- the panel already has a "click to cycle" swatch
// for reaching the rest of the palette; these are just the fastest path to
// the colors people actually reach for.
var DPI_PRESET_COLORS = ["#ff0000", "#00ff00", "#0000ff"]

function isValidHexColor(text) {
  return /^#[0-9a-fA-F]{6}$/.test(String(text || ""))
}


function isLow(settings, threshold) {
  if (!has(settings, "batteryPercent")) return false
  if (settings.charging) return false
  return settings.batteryPercent <= threshold
}

// Fallback for an hskctl whose JSON predates sections (a mixed --dev
// checkout, most likely): one unsorted section of whatever is writable, so
// the panel degrades to a flat list instead of rendering nothing. Never a
// path a matching shipped pair takes.
function sectionsOrFallback(state, writable, meta, sections) {
  if (state === "ready" && isList(sections) && sections.length > 0) return sections
  var out = []
  if (state !== "ready" || !writable) return out
  var fields = []
  for (var i = 0; i < writable.length; i++) {
    var field = writable[i]
    var m = meta[field]
    if (m && isRowWidget(m.widget)) fields.push(field)
  }
  if (fields.length > 0) out.push({ title: "SETTINGS", fields: fields })
  return out
}

function canWrite(writable, field) {
  if (!writable) return false
  return writable.indexOf(field) !== -1
}

// The one predicate every render decision shares: does this field become a
// control? buildRows uses it for cursor stops, the panel's section delegates
// use it for visibility, so a drawn row and a cursor stop cannot disagree.
function isRowWidget(widget) {
  return widget === "toggle" || widget === "chips" || widget === "slider" || widget === "color"
}

// A field renders only if it is writable, has renderable metadata, and is not
// hidden by a capability gate (true = shown). `gate` maps names as the engine
// reported them; nothing about the gate is hardwired to any field here.
function fieldRowVisible(field, writable, meta, gate) {
  if (gate && Object.prototype.hasOwnProperty.call(gate, field) && gate[field] !== true) return false
  if (!canWrite(writable, field)) return false
  var m = meta[field]
  if (!m) return false
  return isRowWidget(m.widget)
}

// The flat list of keyboard-navigable rows, straight from the profile:
// the profile's sections in order, their fields in order, writable fields
// only. No field name lives in this function -- that is the point of it.
// Keeping this as data rather than QML structure means the cursor logic is
// one index into one array, instead of a section/row state machine.
//
// Rows are capped by cleanSections/cleanMeta upstream (16 sections x 16
// fields), but a profile that lists one field in several sections would
// still have produced one cursor stop per mention, so a field renders at
// most once panel-wide.
//
// `gate` hides fields whose capability is not yet proven (`true` = shown);
// the service fills it for the one feature with an extra runtime check --
// stage switching, whose probe reports back after the panel's first status.
// It is keyed by field name exactly as the engine reported it, so no QML
// side knowledge is involved in what it hides.
function buildRows(state, writable, sections, meta, gate) {
  var rows = []
  if (state !== "ready") return rows
  var seen = []
  for (var s = 0; s < sections.length; s++) {
    var fields = sections[s].fields
    for (var i = 0; i < fields.length; i++) {
      var field = fields[i]
      if (seen.indexOf(field) !== -1) continue
      if (!fieldRowVisible(field, writable, meta, gate)) continue
      seen.push(field)
      rows.push({ field: field, widget: meta[field].widget })
    }
  }
  return rows
}

// The choices a chips row renders: the profile's own allowed values, in the
// order hskctl sorted them, each with a label. String values print as they
// are ("1mm"); numbers get the field's unit -- including the Hz shorthand
// ("4K" for 4000), which is exactly the formatting the polling-rate chips
// have always used, now driven by the unit the profile declares instead of
// a rate-shaped special case in the QML.
function chipsOptions(field, allowed, unit) {
  var values = allowed && allowed[field]
  if (!isList(values)) return []
  var options = []
  for (var i = 0; i < values.length && options.length < MAX_ALLOWED_VALUES; i++) {
    var v = values[i]
    var label
    if (typeof v === "number" && isFinite(v)) {
      label = (unit === "Hz" && v >= 1000 && v % 1000 === 0) ? (v / 1000) + "K" : String(v)
    } else {
      label = String(v)
    }
    options.push({ value: Array.isArray(v) ? "" : String(v), label: label })
  }
  return options
}

// The [min, max] a slider row drags between, from the profile's allowed
// list (hskctl sends the endpoints of bounded ranges). The step is the
// field's own if the profile says one, else 1 -- a slider with no reason
// to skip steps does not invent a reason.
var SLIDER_STEP_DEFAULT = 1

function sliderBounds(field, allowed, meta) {
  var pair = allowed ? allowed[field] : undefined
  if (isList(pair) && pair.length === 2) {
    var lo = pair[0], hi = pair[1]
    if (typeof lo === "number" && typeof hi === "number"
        && isFinite(lo) && isFinite(hi)
        && lo < hi && hi - lo <= 1000000) {
      var step = meta && typeof meta.step === "number" && meta.step > 0 ? meta.step : SLIDER_STEP_DEFAULT
      return { min: lo, max: hi, step: step }
    }
  }
  return null
}

function clampStep(value, bounds) {
  if (!bounds) return value
  var v = Math.round(value / bounds.step) * bounds.step
  return Math.max(bounds.min, Math.min(bounds.max, v))
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
