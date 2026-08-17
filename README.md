# omarchy-hsk

Configure a **G-Wolves HSK Pro 4K** from Omarchy — battery, DPI, polling rate,
and sensor settings, in the bar.

## Status

The protocol is **decoded**. It was extracted statically from G-Wolves'
own Windows driver (`G-Wolves_Software_V1.0.20.07`, a C++/CLI mixed-mode .NET
binary using hidapi) by reading `hts_send_cmd`'s CIL and the `HTS_*_CMD` static
byte arrays. Nothing in the profile is a guess carried over from another
vendor's mouse.

| Part | State |
|------|-------|
| Omarchy Quattro plugin (bar widget + panel) | done |
| `hskctl` CLI and HID transport | done |
| Transport + command table | decoded from the vendor binary |
| Value maps (which byte means 4000 Hz) | **needs one check against the real mouse** |
| DPI writes | read-only until verified |

`hskctl fields` marks anything still unverified. Those fields read correctly;
what is unconfirmed is the raw↔friendly mapping, because that lives in the
vendor's WinForms UI code rather than in the command table.

## The protocol

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

Command table, straight from the firmware:

| Setting | write | read |
|---------|-------|------|
| polling rate | `02` | `82` |
| DPI + colours | `03` | `83` |
| key map | `04` | `84` |
| debounce | `05` | `85` |
| lift-off distance | `06` | `86` |
| angle snap | `07` | `87` |
| LED | `08` | `88` |
| factory reset | `09` | — |
| motion sync | `11` | `91` |
| wheel debounce | `12` | `92` |
| battery | — | `8f` |
| connection state | — | `90` |
| firmware version | — | `81` |

Device: **VID `33e4`**, 14 product ids, **interface 2**. DPI stages are
big-endian `u16` (seven of them), sleep is a sub-command of opcode `0x02` with
its selector at byte 6.

Factory reset (`09`) is deliberately not bound to any field — nothing in the
panel should be one keystroke from wiping the mouse's config.

## Install

```bash
tar xzf omarchy-hsk.tar.gz && cd omarchy-hsk
./install.sh
omarchy plugin enable io.github.keasbee.hsk
```

`install.sh` puts `hskctl` in `~/.local/bin`, the profile in
`~/.config/hskctl/profiles/`, the plugin in `~/.config/omarchy/plugins/`, and
offers to write a udev rule (VID `33e4`) so writes don't need sudo.

Python 3.9+, no pip packages — it talks to `/dev/hidraw*` directly.

## Verify before trusting a write

```bash
hskctl probe      # confirm it picks the interface-2 node
hskctl status     # every value should match the Windows app
hskctl fields     # shows what is writable and what is unverified
```

Check `status` against the vendor app first. If polling rate reads `250` when
the app says `1000`, the enum in the profile is wrong — fix
`fields.pollingRate.values` before writing anything. Then:

```bash
hskctl set pollingRate 1000
```

`set` always reads the field back and tells you if the mouse disagrees with
what you asked for. A mismatch means the mapping is wrong — stop rather than
trying more values. When every field round-trips and survives a replug, set
`"status": "verified"` in the profile and drop the `_needsVerification` notes.

Full detail in [docs/PROTOCOL-DISCOVERY.md](docs/PROTOCOL-DISCOVERY.md).

## Using it

Bar widget: **left click** opens the panel, **right click** cycles DPI stage,
**middle click** refreshes.

In the panel: `↑`/`↓` moves, `←`/`→` changes the value under the cursor, `1`–`7`
jumps to a DPI stage, `d` cycles DPI, `m` toggles Motion Sync, `r` refreshes.

The panel only renders controls for settings the profile can actually write, so
it never offers an action that comes back as an error.

```bash
omarchy-shell io.github.keasbee.hsk cycleDpi
omarchy-shell io.github.keasbee.hsk setPollingRate 1000
hskctl status --json
```

## Layout

```
hskctl/          CLI + hidraw transport + the profile interpreter
  hidraw.py        dependency-free ioctl wrapper, report-descriptor parser
  protocol.py      declarative profile engine (commands, codecs, ack)
  device.py        endpoint ranking, link-flag auto-detection
  cli.py           always-JSON-parseable command line
profiles/        the decoded protocol -- data, not code
plugin/          the Omarchy Quattro plugin (manifest, Panel, Service, Model)
tools/           analyze-driver.py, capture-usbmon.sh, decode-capture.py
tests/           30 Python tests, 33 JS tests
docs/            how the protocol was decoded, and how to verify it
```

**No device knowledge lives in code.** Commands, opcodes, offsets and encodings
are all data in `profiles/*.json`. The same engine handles the other HSK
variants if you profile them.

## Tests

```bash
python3 -m unittest discover -s tests
node tests/test_model.js
```

The Python suite pins the decoded protocol: that every read opcode is its write
opcode + `0x80`, that wired and wireless packets differ in exactly one byte,
that DPI decodes big-endian, that read-only fields refuse writes, and that
factory reset is unreachable.
