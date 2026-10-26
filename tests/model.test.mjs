// Tests for Model.js, the panel's pure helpers. Model.js is a QML JavaScript
// resource (no exports), so it is evaluated in a sandbox and its top-level
// functions read off the context. Run: node --test tests/
import { test } from "node:test"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import vm from "node:vm"

const source = readFileSync(fileURLToPath(new URL("../Model.js", import.meta.url)), "utf8")
const M = vm.createContext({})
vm.runInContext(source, M)

const DPI = { id: "dpiStages", kind: "dpiStages", group: "dpi", label: "DPI", minimum: 50, maximum: 26000, step: 50,
              maxStages: 7, hasColors: true, switchable: true, writable: true, visible: true, state: "verified",
              options: [], unit: "" }

test("plain strips markup characters, controls and bidi overrides", () => {
  assert.equal(M.plain('<img src="x">&Mouse‮\u0007'), 'img src="x"Mouse')
  assert.equal(M.plain("a".repeat(500), 10).length, 10)
  assert.equal(M.plain(null), "")
})

test("cleanDevices drops malformed entries and sanitises names", () => {
  const out = M.cleanDevices({ devices: [
    { key: "33e4:5807@usb-1", name: "<b>HSK</b>", vidpid: "33e4:5807", accessible: true,
      family: { id: "gwolves_hts", title: "G-Wolves HTS" }, profile: { id: "hsk", name: "HSK" } },
    { name: "no key" },
    "garbage",
  ] })
  assert.equal(out.length, 1)
  assert.equal(out[0].name, "bHSK/b")
  assert.equal(out[0].profile.id, "hsk")
})

test("cleanCapabilities rejects unknown kinds, bad ids and rejected caps", () => {
  const caps = M.cleanCapabilities({ capabilities: [
    { id: "pollingRate", kind: "enum", group: "performance", options: [1000, 2000, true], writable: true },
    { id: "evil", kind: "script", group: "sensor" },
    { id: "Bad-Id", kind: "toggle", group: "sensor" },
    { id: "noGroup", kind: "toggle" },
    { id: "angleSnap", kind: "toggle", group: "sensor", state: "rejected" },
    { id: "emptyEnum", kind: "enum", group: "performance", options: [] },
  ] })
  assert.deepEqual(Array.from(caps, (c) => c.id), ["pollingRate"])
  assert.deepEqual([...caps[0].options], [1000, 2000])
})

test("cleanValue keeps only the shape the kind defines", () => {
  assert.deepEqual({ ...M.cleanValue({ kind: "battery" }, { percent: 82, charging: 0 }) }, { percent: 82, charging: false })
  assert.equal(M.cleanValue({ kind: "battery" }, { percent: 120 }), undefined)
  assert.equal(M.cleanValue({ kind: "toggle" }, 1), undefined)
  assert.equal(M.cleanValue(DPI, { active: 3, stages: [{ dpi: 400, color: "#ff0000" }] }), undefined)
  const stages = M.cleanValue(DPI, { active: 0, stages: [{ dpi: 400, color: "javascript:" }] })
  assert.equal(stages.stages[0].color, null)
})

test("pickDevice prefers the asked-for key, then a profiled mouse", () => {
  const devices = [{ key: "a", profile: null }, { key: "b", profile: { id: "x" } }]
  assert.equal(M.pickDevice(devices, "a").key, "a")
  assert.equal(M.pickDevice(devices, "gone").key, "b")
  assert.equal(M.pickDevice([], "a"), null)
  assert.equal(M.nextDeviceKey(devices, "b"), "a")
})

test("formatting", () => {
  const rate = { kind: "enum", unit: "Hz" }
  assert.equal(M.optionLabel(rate, 4000), "4K")
  assert.equal(M.optionLabel(rate, 500), "500")
  assert.equal(M.optionLabel({ kind: "enum", unit: "mm" }, 1), "1 mm")
  assert.equal(M.valueText(rate, 500), "500 Hz")
  assert.equal(M.valueText({ kind: "duration", unit: "s" }, 120), "120 s")
  assert.equal(M.valueText({ kind: "toggle" }, false), "Off")
  assert.equal(M.batteryGlyph({ percent: 5, charging: true }), "󰂄")
})

test("stage helpers never mutate their input", () => {
  const value = { active: 0, stages: [{ dpi: 400, color: "#ff0000" }, { dpi: 800, color: "#0000ff" }] }
  const next = M.withStage(value, 1, { dpi: 1600 })
  assert.equal(next.stages[1].dpi, 1600)
  assert.equal(next.stages[1].color, "#0000ff")
  assert.equal(value.stages[1].dpi, 800)
  assert.equal(M.withActive(value, 1).active, 1)
  assert.equal(M.cycleColor("#ffffff", 1), "#ff0000")
  assert.equal(M.cycleColor("#123456", 1), "#ff0000")
  assert.equal(M.clampDpi(DPI, 425), 450)
  assert.equal(M.clampDpi(DPI, 99999), 26000)
})

test("buildRows orders focusable rows as drawn and skips read-only ones", () => {
  const caps = [
    DPI,
    { id: "pollingRate", kind: "enum", options: [1000], writable: true, visible: true },
    { id: "debounce", kind: "info", writable: false, visible: true },
    { id: "liftOff", kind: "enum", options: [1, 2], writable: false, visible: true },
    { id: "hidden", kind: "toggle", writable: true, visible: false },
  ]
  const rows = M.buildRows(caps, 2)
  assert.deepEqual(Array.from(rows, (r) => r.type + ":" + r.capId),
    ["device:", "stages:dpiStages", "stageDpi:dpiStages", "stageColor:dpiStages", "setting:pollingRate"])
  assert.equal(M.rowIndex(rows, "setting", "pollingRate"), 4)
})

test("steppedValue walks options and bounds", () => {
  const rate = { kind: "enum", options: [1000, 2000, 4000] }
  assert.equal(M.steppedValue(rate, 2000, 1), 4000)
  assert.equal(M.steppedValue(rate, 4000, 1), undefined)
  const sleep = { kind: "duration", minimum: 30, maximum: 60, step: 30 }
  assert.equal(M.steppedValue(sleep, 30, 1), 60)
  assert.equal(M.steppedValue(sleep, 60, 1), undefined)
  assert.equal(M.steppedValue({ kind: "toggle" }, true, 1), false)
})

test("capabilities sort into sections; settings and footer split out", () => {
  const caps = M.cleanCapabilities({ capabilities: [
    { id: "firmware", kind: "info", group: "device", label: "Firmware" },
    { id: "motionSync", kind: "toggle", group: "sensor", writable: true },
    { id: "pollingRate", kind: "enum", group: "performance", options: [1000], writable: true },
    { id: "dpiStages", kind: "dpiStages", group: "dpi", writable: true },
  ] })
  assert.deepEqual(Array.from(caps, (c) => c.id), ["dpiStages", "pollingRate", "motionSync", "firmware"])
  assert.deepEqual(Array.from(M.settingSections(caps), (s) => s.title), ["PERFORMANCE", "SENSOR"])
  assert.equal(M.footerText(caps, { firmware: "1.0.7" }), "Firmware 1.0.7")
  assert.equal(M.footerText(caps, { firmware: "1.0.7" }, "4.0.1"), "Firmware 1.0.7  ·  Mouse CTRL v4.0.1")
})

test("cleanVersion keeps only a plain dotted version", () => {
  assert.equal(M.cleanVersion("4.0.1"), "4.0.1")
  assert.equal(M.cleanVersion("0.0.0-unpackaged"), "")
  assert.equal(M.cleanVersion("<b>4</b>"), "")
  assert.equal(M.cleanVersion(4), "")
})

test("isValidRuleText accepts only the narrow rule shape", () => {
  const good = '# header\nKERNEL=="hidraw*", ATTRS{idVendor}=="33e4", MODE="0660", TAG+="uaccess"\n'
  assert.ok(M.isValidRuleText(good))
  assert.ok(M.isValidRuleText('KERNEL=="hidraw*", ATTRS{idVendor}=="1234", ATTRS{idProduct}=="abcd", MODE="0660", TAG+="uaccess"'))
  assert.ok(!M.isValidRuleText("# only comments\n"))
  assert.ok(!M.isValidRuleText(good + 'KERNEL=="*", MODE="0666"\n'))
  assert.ok(!M.isValidRuleText(good + 'RUN+="/bin/sh -c evil"\n'))
  assert.ok(!M.isValidRuleText(good.replace("33e4", "33E4")))
  assert.ok(!M.isValidRuleText(null))
})

test("confirmGroups puts basics first and skips empty sections", () => {
  const caps = M.cleanCapabilities({ capabilities: [
    { id: "battery", kind: "battery", group: "device", label: "Battery" },
    { id: "dpiStages", kind: "dpiStages", group: "dpi", label: "DPI" },
    { id: "pollingRate", kind: "enum", group: "performance", label: "Polling rate", options: [1000], unit: "Hz" },
  ] })
  const groups = M.confirmGroups(caps, { battery: { percent: 82, charging: true }, pollingRate: 1000 })
  assert.deepEqual(Array.from(groups, (g) => g.title), ["BASICS", "DPI", "PERFORMANCE"])
  assert.equal(groups[0].rows[0].text, "82% charging")
  assert.equal(groups[1].stagesId, "dpiStages")
  assert.equal(groups[2].rows[0].text, "1000 Hz")
})

test("finalStates turns answers into honest profile states", () => {
  const caps = M.cleanCapabilities({ capabilities: [
    { id: "battery", kind: "battery", group: "device", verification: "verified", readOnly: true },
    { id: "curved", kind: "battery", group: "device", verification: "untested", readOnly: true },
    { id: "pollingRate", kind: "enum", group: "performance", options: [1000], verification: "verified" },
    { id: "motionSync", kind: "toggle", group: "sensor", verification: "untested" },
    { id: "angleSnap", kind: "toggle", group: "sensor", verification: "untested" },
    { id: "sleepSeconds", kind: "duration", group: "power", verification: "verified" },
  ] })
  const out = M.finalStates(caps, { power: true }, { motionSync: "passed", angleSnap: "failed" }, { performance: true })
  assert.equal(out.battery.state, "verified")
  assert.equal(out.curved.state, "readOnly")
  assert.deepEqual({ ...out.pollingRate }, { state: "verified", visible: false })
  assert.equal(out.motionSync.state, "tested")
  assert.equal(out.angleSnap.state, "readOnly")
  assert.equal(out.sleepSeconds.state, "rejected")
})

test("cleanRatbag validates ratbagd readings and marks the live stage", () => {
  const r = M.cleanRatbag({ name: "<b>G Pro</b>", firmware: "12.1", profile: 2, profiles: 5, pollingRate: 1000,
                            stages: [{ dpi: 800, active: false }, { dpi: 1600, active: true }, { dpi: "x" }] })
  assert.equal(r.name, "bG Pro/b")
  assert.equal(r.stages.length, 2)
  assert.equal(M.ratbagStagesText(r), "800  [1600]")
  assert.equal(M.cleanRatbag(null), null)
})
