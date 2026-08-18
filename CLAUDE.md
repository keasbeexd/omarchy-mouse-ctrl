# omarchy-hsk — working notes for Claude

Read this before changing anything. It records what is proven, what is guessed,
and which of the two you are allowed to act on.

## What this is

An Omarchy Quattro plugin plus `hskctl`, a Linux CLI that configures a
**G-Wolves HSK Pro 4K** mouse over raw HID. The mouse has no libratbag support
and its vendor web driver does not support this model, so the protocol was
recovered by static analysis of the Windows driver.

The repo root *is* the plugin: `manifest.json`, `Panel.qml`, `Service.qml` and
`Model.js` sit at the top level so `omarchy plugin add <url>` works, and
`hskctl` ships inside so the widget runs `bin/hskctl` from its own directory.

## The protocol, and where it came from

Extracted from `G-Wolves_Software_V1.0.20.07` (2024-03-20) — a C++/CLI
mixed-mode .NET binary over hidapi. The `hts_*` functions compile to managed
CIL, so `hts_send_cmd` and the `HTS_*_CMD` static byte arrays were read
directly. This is observed behaviour, not inference from other vendors' mice.

```
hts_send_cmd(tx, rx):
    Sleep(60)
    hid_send_feature_report(handle, tx, 65)
    Sleep(60)
    hid_get_feature_report(handle, rx, 65)
    accept iff rx[1] == 0xA1
```

65-byte HID Feature reports, report id 0, **no checksum**.

| Byte | Meaning |
|------|---------|
| 0 | hidapi report-id byte, always `0x00` |
| 1 | `0xA1` in replies — the acknowledgement |
| 2 | payload length |
| 3 | opcode — **read opcode = write opcode + 0x80** |
| 4 | link flag: `1` on the dongle, `0` on the cable |
| 5+ | value |

VID `33e4`, 14 product ids. The config endpoint is identified by a vendor usage
page plus a feature report, **not** by interface number — the dongle enumerates
at interface 7 despite the driver's `mi_02` string.

The reply echoes the request: `rx[2]` response length, `rx[3]` opcode, `rx[4]`
link flag, payload from `rx[5]`. **An ACK is not proof of success** — the
firmware acknowledges a packet carrying the wrong link flag and then ignores
it, replying with an all-zero payload. Sessions call `detect_link()` first.

DPI is one block: `rx[5]` active stage, `rx[6]` an unidentified flag, then seven
stages of seven bytes from `rx[7]` — X `u16be`, Y `u16be`, R, G, B.
`sleep` is the one exception to the byte-3 rule: it is a sub-command of opcode
`0x02` carrying its selector at byte 6.

## The line you must not cross

The command table is proven. The **value maps are not** — which raw byte means
4000 Hz lives in the vendor's WinForms UI code, not the command table. Fields
in that state carry `_needsVerification` in the profile, and `hskctl fields`
prints them.

Rules:

1. **Never fill in or "correct" a `_needsVerification` mapping from reasoning
   alone.** It gets confirmed against real hardware or it stays flagged.
2. **Never bind anything to `HTS_RESET_CMD` (opcode `09`).** It is factory
   reset. It is deliberately absent from `commands` and a test enforces that.
3. **DPI writes go read-modify-write.** The packet carries all seven stages
   and their colours at once, so `set` reads the mouse's own block, changes one
   field, and echoes the rest back untouched. Never synthesise a DPI packet
   from an empty buffer — the undecoded colour bytes would be zeroed.
4. Blind writes to a HID device can brick it. Prefer reading. If you need a new
   command, get it from the vendor binary or a capture — not from a guess.

## Verification needs hardware you probably do not have

A cloud session (Claude Code on the web, or a Cowork sandbox) has **no mouse**.
`hskctl probe` will find nothing, and `hskctl status` will report `state:
error`. That is expected, not a bug — do not "fix" it.

Work that needs the physical mouse:
- confirming the `_needsVerification` value maps
- enabling DPI writes
- anything that ends in `hskctl set`

Work that does not:
- QML, docs, tests, refactors, packaging
- anything driven by `tools/analyze-driver.py` against the vendor binary

If you are in a cloud session, stay in the second list and leave a note about
what needs checking on hardware.

## Design rule

**No device knowledge belongs in code.** Commands, opcodes, offsets and
encodings are data in `profiles/*.json`, interpreted by a generic engine in
`hskctl/protocol.py`. Correcting a mapping means editing JSON. Adding another
HSK variant means adding a profile, not a branch.

If you find yourself writing `if model == ...` in Python, stop — it belongs in
the profile.

## Diagnosing a misbehaving mouse

`hskctl doctor` sends every read opcode on both link flags and prints the raw
request and reply for each. It writes nothing. Use it before theorising: it
distinguishes "the mouse answered 0" from "the mouse never answered", which
look identical in the panel.

## Tests

```bash
python3 -m unittest discover -s tests    # 30 tests
node tests/test_model.js                 # 33 tests
```

The Python suite deliberately pins the decoded protocol: the +0x80 rule across
every command, wired and wireless packets differing in exactly one byte,
big-endian DPI, read-only fields refusing writes, and factory reset being
unreachable. If a change breaks one of those, the change is almost certainly
wrong — do not relax the test to make it pass.

`hskctl` must always emit parseable JSON under `--json`, including on failure.
`Service.qml` parses that output, and a panel that has to guess why stdout was
empty shows the wrong thing.

## Layout

```
manifest.json Panel.qml Service.qml Model.js   the Omarchy plugin
bin/hskctl                                     launcher for the bundled CLI
hskctl/       hidraw.py protocol.py device.py cli.py
profiles/     the decoded protocol, as data
tools/        analyze-driver.py, capture-usbmon.sh, decode-capture.py
tests/        protocol + view-model tests
docs/         how the protocol was decoded and how to verify it
```

## Open work

1. `pollingRate` is the last unverified map. `hskctl measure-polling` times the
   mouse's own input reports; compare with `hskctl get pollingRate` and correct
   the enum from the pair.
2. Confirm a DPI write survives a replug. The block layout is confirmed against
   hardware and the arithmetic checks out (2 + 7*7 = 51 = the reported payload
   length), but no write has been made yet.
3. `charging` reads byte 5 of the battery reply; observed 0 while discharging.
   Confirm it reads 1 on the cable.
4. `rx[6]` of the DPI reply is an unidentified flag (observed 1). Probably the
   stage count or an independent-XY flag; both would read 1 here.
5. `debounce` read and write are asymmetric -- read returns byte 0 of a 4-byte
   tuple, write takes a row index into the driver's 6-row table. Model the
   table to re-enable writing.
4. `tools/analyze-driver.py` never got run against the older 2023 build
   (`HSK_Pro_4K_FWSW20230322.rar`). Diffing the two would cross-check the
   command table. Does not need hardware, only the archive.
