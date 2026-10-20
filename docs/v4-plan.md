# Mouse CTRL v4 — rewrite plan

Status: draft from the 2026-10-19 planning session. Nothing here is built yet.

## Goal

A guided, in-panel wizard identifies the connected mouse, works out what it
can do, and saves a profile. The bar widget and panel then show and control
exactly the capabilities in that profile.

## The constraint that shapes everything

Vendor HID config protocols are not self-describing. Nothing on the wire says
"DPI stages live at offset 7", and probing an unknown mouse with guessed
writes can corrupt its stored config. So the wizard does **not** discover
protocols. It:

1. fingerprints the device (VID/PID, report descriptor, hidraw layout),
2. matches it to a **known protocol family** (a Python driver),
3. reads everything the driver knows and asks the user to confirm the values,
4. unlocks writes per field (see "Write gate").

Decoding a new family is a contributor job (vendor binary analysis or a USB
capture), done once per family, supported by tools in `scripts/`. The v3
attempt to do in-wizard black-box discovery (diff rounds, ambient rounds,
length oracles) is dropped.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Coverage | Curated families + libratbag as an optional backend | Families cover vendor-only mice; libratbag already covers Logitech/SteelSeries/etc. |
| Unknown mice | Generic read-only panel + copyable report + link to the contributor guide | Only the owner has the hardware every write test needs; issue requests would hand that work to a maintainer without the mouse |
| Family format | One Python driver module per family | Quirks (link-flag probing, DPI-count repair, settle times) are code, not JSON keys a generic engine must grow to interpret — the main cause of v3's sprawl |
| Profile | One per physical mouse model (dongle + cable = one mouse); user data only | Settings live on the mouse; the profile records identity, family, verified capabilities, display choices |
| Write gate | Read-verify, then per-field guarded test | Driver-verified fields are writable at once; untested fields need a write/read-back test in the wizard |
| Multiple mice | Show the connected one; switcher when several are | Covers laptop + desktop cheaply |
| UI | In-panel QML wizard, HTML mockups first | v3's floating-terminal wizard felt bolted on |
| Backend | Python 3 stdlib only, `/usr/bin/python3` | Zero install burden; hidraw/udev code ports directly |
| Rewrite | Fresh tree on a new branch, port selectively | Old design would leak back in through incremental refactoring |

## Architecture

```
Panel.qml / Wizard*.qml        UI only; no protocol knowledge
        │  JSON lines over stdio (long-lived `mousectrl serve`)
mousectrl/ (Python)
  cli.py          thin argument parsing → commands
  server.py       serve loop: requests in, events out (plug/unplug, values)
  registry.py     fingerprint → backend/driver match
  fingerprint.py  VID/PID, descriptor, nodes (from v3 hidraw.py)
  hidraw.py       ioctls, enumeration, inotify (ported)
  safeio.py       bounded reads (ported)
  udev.py         rule generation + pkexec install (ported from cli.py)
  profiles.py     load/save/validate user profiles
  generic.py      read-only probes for any mouse
  backends/
    ratbag.py     libratbag via `busctl --json` to ratbagd, if present
  families/
    base.py       Driver interface + Capability types
    gwolves_hts.py   HSK Pro 4K family (33e4)
    pulsar_nordic.py Pulsar family (3554/25a7)
scripts/          contributor tools, not installed (analyze-driver.py, capture helper)
```

### Driver interface (sketch)

```python
class Driver:
    family: str
    def matches(fp: Fingerprint) -> bool
    def open(fp) -> Session                # picks the node, probes link flag etc.
    def capabilities(session) -> list[Capability]
    def read(session, cap_id) -> Value
    def write(session, cap_id, value) -> Value   # always reads back
```

`Capability` is a small closed set of kinds the panel knows how to render:
`battery`, `dpiStages` (values, colors, active, count), `enum` (polling,
LOD, debounce), `toggle` (motion sync, angle snap, ripple), `duration`
(sleep timer), `info` (firmware, connection). Each capability carries a
`verification` state from the driver: `verified` or `untested`.

Adding a capability kind needs a QML renderer; adding a family does not.

### Profile (user data, `~/.config/mousectrl/profiles/<id>.json`)

```json
{
  "id": "gwolves-hsk-pro-4k",
  "name": "HSK",
  "backend": "family:gwolves_hts",
  "match": { "vendorId": "33e4", "productIds": ["5807", "5808"] },
  "capabilities": {
    "dpiStages": { "state": "verified", "visible": true },
    "debounce":  { "state": "readOnly", "visible": true }
  },
  "createdAt": "2026-10-19"
}
```

No opcodes or offsets live in profiles. Those belong to the driver.

## Generic read-only probes (any mouse, no protocol)

- Name, VID/PID, connection type (dongle/wired) from descriptors
- Battery when the kernel exposes a `power_supply` for the device
- Polling rate measured from evdev event timestamps while the user moves the mouse
- Optional DPI measurement: drag a known distance, compare counts
- Button count from the report descriptor

The HSK does not expose a kernel `power_supply`, which is why the family driver matters
for battery.

## Wizard flow

Mockups: https://claude.ai/artifact/68W3oYVjEeZkMqqur8oihD (private canvas).

1. **Detect & identify** — list connected mice (keyboards and other HID
   devices hidden), each already labelled with its registry match: family
   driver, libratbag, already set up, or unknown. Pick one.
2. **Access** — if hidraw is not readable, show the udev rule and a pkexec
   install button; wait for replug and continue automatically.
3. **Read & confirm** — show every value read; user confirms one group at
   a time ("press the DPI button — do these stages and colors match?").
   A "no" marks that capability hidden.
4. **Unlock writes** — verified fields are on. For untested fields, offer a
   guarded test: write a nearby value, read back, restore. Fail → field
   stays read-only.
5. **Save** — name, which sections show in the panel, bar options; profile
   written and the panel switches to it.

Unknown mice branch at step 1: run the generic probes, offer the read-only
panel, and produce the report (fingerprint, descriptor hex, node list) with
a copy button and an "Add support for this mouse" link to
`docs/contributing-a-family.md`. No issue button and no issue bot: a
protocol can be decoded without the mouse, but every write test needs it,
so the owner is the right person to do the work.

## Panel (v1 for HSK and Pulsar)

- Bar: battery percent + charging, low-battery warning
- Panel: battery, DPI stages (values, colors, active stage switch, edit),
  polling rate, lift-off, motion sync, angle snap, ripple, debounce, sleep
  timer, firmware/connection info
- Mouse switcher when more than one profiled mouse is connected
- All colors from the active Omarchy theme

## What ports from v3

- `hidraw.py` (ioctls, descriptor parsing, enumeration, inotify)
- `safeio.py`
- udev rule generation and pkexec flow (`cmd_udev_rule`)
- Device lock (`device.py` lock helpers)
- Protocol knowledge from `docs/profiles/*.md` and
  `docs/contributor/families/gwolves-hsk-pro-4k.json`, re-expressed as driver code
- `analyze-driver.py` from git history → `scripts/`

Everything else (cli.py, protocol.py, wizard.py, sweep.py, Panel/Service/Model)
is rewritten.

## Phases

1. **Mockups** — HTML mockups of wizard steps and panel; agree on the UI.
2. **Core** — new branch, package skeleton, ported hidraw/safeio/udev,
   fingerprint, registry, driver interface, `serve` protocol.
3. **Families** — `gwolves_hts` and `pulsar_nordic` drivers, verified on
   your hardware field by field.
4. **Panel** — capability renderers, mouse switcher, bar widget.
5. **Wizard** — steps 1–7 plus the unknown-mouse branch and generic probes.
6. **libratbag backend** — optional, detected at runtime.
7. **Contributor tooling + docs** — scripts/, `docs/contributing-a-family.md`
   (vendor app → analyzer → capture fallback → driver → wizard test), a
   fill-in-the-blanks driver template (opcodes, offsets, report size; quirks
   as optional overrides), README rewrite with a "PRs welcome" line,
   marketplace security review.

## Progress

**Phase 2 (core) — done.** `mousectrl/` lives beside the v3 tree on the
`v4` branch: the installed plugin is a symlink to this working tree, so v3's
`hskctl/` and QML stay until the phase 4 panel replaces them. Tests:
`python3 -I -m unittest discover -s tests -t tests`.

What phase 2 established:

- **Identification needs no permission.** `/sys/class/hidraw/*/device/report_descriptor`
  is world-readable, so the wizard names the mouse before the udev rule exists.
- **Mouse vs keyboard is decided by USB boot protocol.** QMK keyboards (the
  Preonic) expose a mouse collection for mouse keys, and Logitech receivers
  put a keyboard on interface 0, so collection- or interface-based tests both
  fail. `bInterfaceProtocol == 2` on any interface is the rule, with a
  mouse-only-collection fallback for boot-less USB mice and the mouse
  collection for Bluetooth.
- **Polling-rate measurement for unknown mice (phase 5) must read hidraw
  input reports, not evdev:** `/dev/input/event*` is `root:input` and the
  user is not in `input`. The udev rule already grants the hidraw node.
- `serve` commands: `devices`, `capabilities`, `read`, `write`, `udev-rule`;
  events: `devices` (hotplug or permission change), `watcherFailed`.

## Settled after the session

- Plugin id stays `keasbeexd.mousectrl`; v4 replaces v3 in place.
- No v3 profile migration: the author is the only v3 user and reruns the wizard.
- Onboard-memory profile slots are deferred past v1.
