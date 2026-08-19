// Tests for the plugin's Model.js. Run with: node tests/test_model.js
// Model.js is plain JavaScript apart from the `.pragma library` line, which we
// strip so the same file the shell loads is the file under test.

const fs = require("fs");
const path = require("path");
const assert = require("assert");

const src = fs
  .readFileSync(
    path.join(__dirname, "..", "Model.js"),
    "utf8"
  )
  .replace(/^\s*\.pragma\s+library\s*$/m, "");

const Model = {};
new Function("exports", src + "\n;Object.assign(exports, {" +
  "POLLING_RATES, parseStatus, has, batteryGlyph, connectionGlyph, connectionLabel," +
  "summaryLine, barLabel, dpiStages, pollingOptions, isLow, buildRows, canWrite," +
  "DPI_MIN, DPI_MAX, DPI_STEP, clampDpi, nextStageColor, STAGE_COLORS," +
  "toggleLabel, toggleDescription, isPermissionError, manifestVersion});")(Model);

let passed = 0;
function test(name, fn) {
  try {
    fn();
    passed++;
    console.log(`  ok  ${name}`);
  } catch (err) {
    console.error(`  FAIL ${name}\n       ${err.message}`);
    process.exitCode = 1;
  }
}

console.log("parseStatus");
test("parses a ready payload", () => {
  const r = Model.parseStatus(
    JSON.stringify({
      ok: true,
      state: "ready",
      model: "G-Wolves HSK Pro 4K",
      device: "/dev/hidraw3",
      settings: { batteryPercent: 72, pollingRate: 4000 },
    })
  );
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.state, "ready");
  assert.strictEqual(r.settings.batteryPercent, 72);
});

test("parses the undiscovered payload without throwing", () => {
  const r = Model.parseStatus(
    JSON.stringify({
      ok: false,
      state: "undiscovered",
      model: "G-Wolves HSK Pro 4K",
      detected: true,
      settings: {},
      error: "Protocol not mapped yet for this device.",
    })
  );
  assert.strictEqual(r.state, "undiscovered");
  assert.strictEqual(r.detected, true);
  assert.match(r.error, /not mapped/);
});

test("empty output is an error, not a crash", () => {
  const r = Model.parseStatus("");
  assert.strictEqual(r.ok, false);
  assert.strictEqual(r.state, "error");
});

test("non-JSON output is an error, not a crash", () => {
  const r = Model.parseStatus("bash: hskctl: command not found");
  assert.strictEqual(r.ok, false);
  assert.strictEqual(r.state, "error");
  assert.match(r.error, /could not parse/);
});

test("missing settings key defaults to an object", () => {
  const r = Model.parseStatus(JSON.stringify({ ok: true, state: "ready" }));
  assert.deepStrictEqual(r.settings, {});
});

console.log("summaryLine");
test("undiscovered gets its own line", () => {
  assert.strictEqual(Model.summaryLine("undiscovered", {}), "Protocol not mapped yet");
});

test("assembles battery, link, dpi and rate", () => {
  const line = Model.summaryLine("ready", {
    batteryPercent: 64,
    connection: "dongle",
    activeDpiStage: 2,
    dpiStage2: 1600,
    pollingRate: 4000,
  });
  assert.strictEqual(line, "64% · 2.4 GHz · 1600 DPI · 4000 Hz");
});

test("charging is called out", () => {
  const line = Model.summaryLine("ready", { batteryPercent: 30, charging: true });
  assert.strictEqual(line, "30% charging");
});

test("a ready mouse with no mapped fields says so", () => {
  assert.strictEqual(Model.summaryLine("ready", {}), "No readings yet");
});

test("skips a dpi stage whose value is not mapped", () => {
  // activeDpiStage is known but the stage's DPI value is not -- must not
  // render 'undefined DPI'.
  const line = Model.summaryLine("ready", { batteryPercent: 50, activeDpiStage: 3 });
  assert.strictEqual(line, "50%");
});

console.log("dpiStages");
test("returns every stage that reported a value", () => {
  // There is no trustworthy stage-count byte -- rx[6] reads 1 on a mouse with
  // seven configured stages -- so show what the firmware actually returned
  // rather than hiding real stages behind a guess.
  const stages = Model.dpiStages({
    dpiStage1: 800,
    dpiStage2: 1600,
    dpiStage3: 3200,
    dpiStage4: 6400,
    activeDpiStage: 2,
  });
  assert.strictEqual(stages.length, 4);
  assert.deepStrictEqual(stages.map((s) => s.dpi), [800, 1600, 3200, 6400]);
  assert.strictEqual(stages[1].active, true);
  assert.strictEqual(stages[0].active, false);
});

test("no mapped stages yields an empty list", () => {
  assert.deepStrictEqual(Model.dpiStages({}), []);
});

test("a stage with no value is skipped, not shown as blank", () => {
  const stages = Model.dpiStages({ dpiStage1: 400, dpiStage3: 800 });
  assert.deepStrictEqual(stages.map((s) => s.stage), [1, 3]);
});

console.log("isLow");
test("flags a low battery", () => {
  assert.strictEqual(Model.isLow({ batteryPercent: 10 }, 15), true);
});

test("charging is never low", () => {
  assert.strictEqual(Model.isLow({ batteryPercent: 5, charging: true }, 15), false);
});

test("unmapped battery is never low", () => {
  assert.strictEqual(Model.isLow({}, 15), false);
});

console.log("buildRows");
test("no rows unless ready", () => {
  assert.deepStrictEqual(
    Model.buildRows("undiscovered", { pollingRate: 1000 }, ["pollingRate"]),
    []
  );
});

test("a readable-but-not-writable setting gets no row", () => {
  // DPI stages currently read fine but cannot be written, so the panel must
  // not offer a control that would come back as an error.
  const rows = Model.buildRows(
    "ready",
    { dpiStage1: 800, dpiStage2: 1600, activeDpiStage: 1, pollingRate: 1000 },
    ["pollingRate"]
  );
  assert.deepStrictEqual(rows.map((r) => r.kind), ["pollingRate"]);
});

test("missing writable list means nothing is interactive", () => {
  assert.deepStrictEqual(Model.buildRows("ready", { pollingRate: 1000 }), []);
});

test("every DPI stage gets a row, with colour and selectability", () => {
  const s = { activeDpiStage: 2 };
  for (let i = 1; i <= 7; i++) {
    s["dpiStage" + i] = i * 400;
    s["dpiStage" + i + "Color"] = "#aa0000";
  }
  const writable = ["activeDpiStage"].concat(
    [1, 2, 3, 4, 5, 6, 7].map((i) => "dpiStage" + i)
  );
  const rows = Model.buildRows("ready", s, writable);
  assert.strictEqual(rows.length, 7);
  assert.ok(rows.every((r) => r.kind === "dpiStage"));
  assert.ok(rows.every((r) => r.selectable === true));
  assert.strictEqual(rows[0].color, "#aa0000");
});

test("stage rows appear even when the active-stage selector is not writable", () => {
  // The slider and colour are useful on their own.
  const s = { dpiStage1: 400, dpiStage1Color: "#aa0000" };
  const rows = Model.buildRows("ready", s, ["dpiStage1"]);
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].selectable, false);
});

test("clampDpi snaps to the sensor's 50 DPI steps and range", () => {
  assert.strictEqual(Model.clampDpi(1637), 1650);
  assert.strictEqual(Model.clampDpi(-5), 50);
  assert.strictEqual(Model.clampDpi(99999), 26000);
});

test("colour cycling wraps and tolerates an unknown colour", () => {
  assert.strictEqual(Model.nextStageColor("#aa0000"), "#ffa500");
  assert.strictEqual(
    Model.nextStageColor(Model.STAGE_COLORS[Model.STAGE_COLORS.length - 1]),
    Model.STAGE_COLORS[0]
  );
  assert.strictEqual(Model.nextStageColor("nonsense"), Model.STAGE_COLORS[0]);
});

test("a stage whose axes disagree is flagged", () => {
  const s = { dpiStage1: 400, dpiStage1Y: 800 };
  assert.strictEqual(Model.dpiStages(s)[0].split, true);
  const t = { dpiStage1: 400, dpiStage1Y: 400 };
  assert.strictEqual(Model.dpiStages(t)[0].split, false);
});

test("builds one row per writable control", () => {
  const rows = Model.buildRows("ready", {
    dpiStage1: 800,
    dpiStage2: 1600,
    activeDpiStage: 1,
    pollingRate: 1000,
    motionSync: true,
  }, ["activeDpiStage", "dpiStage1", "dpiStage2", "pollingRate", "motionSync"]);
  assert.deepStrictEqual(rows.map((r) => r.kind), [
    "dpiStage",
    "dpiStage",
    "pollingRate",
    "toggle",
  ]);
  assert.strictEqual(rows[3].field, "motionSync");
});

test("omits controls the profile has not mapped", () => {
  const rows = Model.buildRows("ready", { pollingRate: 1000 }, ["pollingRate"]);
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].kind, "pollingRate");
});

console.log("barLabel");
test("hidden when the setting is off", () => {
  assert.strictEqual(Model.barLabel({ batteryPercent: 80 }, false), "");
});

test("hidden when battery is unmapped", () => {
  assert.strictEqual(Model.barLabel({}, true), "");
});

test("shows a percentage otherwise", () => {
  assert.strictEqual(Model.barLabel({ batteryPercent: 80 }, true), "80%");
});

console.log("batteryGlyph");
test("distinct glyphs across the range", () => {
  const glyphs = [0, 20, 40, 60, 80, 100].map((p) => Model.batteryGlyph(p, false));
  assert.strictEqual(new Set(glyphs).size, 6);
});

test("charging overrides level", () => {
  assert.strictEqual(Model.batteryGlyph(5, true), Model.batteryGlyph(95, true));
});

test("unknown level falls back to the mouse glyph", () => {
  assert.strictEqual(Model.batteryGlyph(undefined, false), "󰍽");
});

console.log("pollingOptions");
test("polling options are exactly the rates measured on hardware", () => {
  assert.deepStrictEqual(Model.POLLING_RATES, [250, 500, 1000, 2000, 4000]);
});

test("parseStatus carries writable and unverified through", () => {
  const r = Model.parseStatus(
    JSON.stringify({ ok: true, state: "ready", settings: { pollingRate: 1000 },
                     writable: ["pollingRate"], unverified: ["pollingRate"] })
  );
  assert.deepStrictEqual(r.writable, ["pollingRate"]);
  assert.deepStrictEqual(r.unverified, ["pollingRate"]);
});

test("parseStatus defaults writable to empty when absent", () => {
  const r = Model.parseStatus(JSON.stringify({ ok: true, state: "ready" }));
  assert.deepStrictEqual(r.writable, []);
});

test("dpiStages handles all seven stages", () => {
  const s = {};
  for (let i = 1; i <= 7; i++) s["dpiStage" + i] = i * 400;
  assert.strictEqual(Model.dpiStages(s).length, 7);
});

test("every option has a value and a label", () => {
  const options = Model.pollingOptions(1000);
  assert.strictEqual(options.length, Model.POLLING_RATES.length);
  options.forEach((o) => {
    assert.ok(typeof o.value === "string" && o.value.length > 0);
    assert.ok(typeof o.label === "string" && o.label.length > 0);
  });
  assert.strictEqual(options.find((o) => o.value === "1000").label, "1K");
  assert.strictEqual(options.find((o) => o.value === "4000").label, "4K");
  assert.strictEqual(options.find((o) => o.value === "500").label, "500");
});

test("option values match what ButtonGroup compares against", () => {
  // ButtonGroup does String(o.value) === value, and the panel passes
  // String(mouse.value("pollingRate")). These must line up exactly.
  const options = Model.pollingOptions(1000);
  assert.ok(options.some((o) => String(o.value) === String(1000)));
});


// A missing udev rule is the most common first-run failure, and the panel
// shows a completely different screen for it -- with the command to fix it --
// so the classifier has to be right about both directions.
test("permission failures are recognised however they surface", () => {
  const messages = [
    "[Errno 13] Permission denied: '/dev/hidraw6'",
    "could not open /dev/hidraw6: Permission denied",
    "EACCES opening the config endpoint",
    "operation not permitted",
  ];
  for (const m of messages) {
    assert.strictEqual(Model.isPermissionError(m), true, m);
  }
});

test("a missing mouse is not mistaken for a missing udev rule", () => {
  const messages = [
    "no candidate HID node found",
    "the mouse did not acknowledge 'dpi' (byte 1 = 0x00, expected 0xa1)",
    "another hskctl is talking to the mouse",
    "",
  ];
  for (const m of messages) {
    assert.strictEqual(Model.isPermissionError(m), false, m);
  }
});


// The panel reads its version out of the bundled manifest. Anything it cannot
// read has to come back as "" so the label hides rather than lying.
test("the version is read out of the manifest", () => {
  assert.strictEqual(
    Model.manifestVersion('{"id":"x","version":"1.0.1"}'), "1.0.1");
});

test("an unreadable manifest hides the label rather than guessing", () => {
  for (const raw of ["", "   ", "{not json", "[]", '{"id":"x"}', '{"version":3}',
                     null, undefined]) {
    assert.strictEqual(Model.manifestVersion(raw), "", JSON.stringify(raw));
  }
});


// The footer version rides in hskctl's JSON, because fetching manifest.json
// from QML failed silently in the shell. It has to survive the error payload
// too -- knowing which build is misbehaving matters most when it is failing.
test("the version comes through a successful status", () => {
  const parsed = Model.parseStatus(JSON.stringify(
    { ok: true, state: "ready", version: "1.1.0", settings: {} }));
  assert.strictEqual(parsed.version, "1.1.0");
});

test("the version comes through an error status", () => {
  const parsed = Model.parseStatus(JSON.stringify(
    { ok: false, state: "error", version: "1.1.0", error: "no mouse" }));
  assert.strictEqual(parsed.version, "1.1.0");
  assert.strictEqual(parsed.error, "no mouse");
});

test("an older hskctl without a version does not break the panel", () => {
  const parsed = Model.parseStatus(JSON.stringify(
    { ok: true, state: "ready", settings: {} }));
  assert.strictEqual(parsed.version, "");
});

console.log(`\n${passed} passed`);
