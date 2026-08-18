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
| Battery, connection, firmware | confirmed on hardware |
| DPI — 7 stages, X/Y linked, LED colours | read and write confirmed on hardware |
| Polling rate, 250–4000 Hz | measured on hardware, not inferred |
| Motion sync, angle snap, lift-off | confirmed on hardware |
| Debounce | read-only — read and write are asymmetric |
| Sleep timer | readable; unit not established |
| Persistence across power-cycle | the mouse has no flash — see below |

`hskctl fields` marks anything still unverified, and `hskctl doctor` dumps the
raw bytes of every read command when something looks wrong.

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

Device: **VID `33e4`**, 14 product ids. The config endpoint is found by vendor
usage page plus a feature report, *not* interface number — the dongle enumerates
at interface 7 despite the driver's `mi_02` string.

The reply echoes the request: `rx[2]` length, `rx[3]` opcode, `rx[4]` link flag,
payload from `rx[5]`. **An ACK does not mean success** — the firmware
acknowledges a wrong link flag and then ignores the command, replying with an
all-zero payload.

DPI is one block: active stage at `rx[5]`, then seven stages of seven bytes from
`rx[7]` — X `u16be`, Y `u16be`, R, G, B. Sleep is a sub-command of opcode `0x02`
with its selector at byte 6.

Factory reset (`09`) is deliberately not bound to any field — nothing in the
panel should be one keystroke from wiping the mouse's config.

## Measured behaviour

Everything below was measured by timing the mouse's own input reports, not
inferred from the binary:

| raw | Hz | | raw | Hz |
|-----|------|---|-----|------|
| 1 | 1000 | | 5 | 200 |
| 2 | 500  | | 6 | 167 |
| 3 | 333  | | 32 | 2000 |
| 4 | 250  | | 64 | 4000 |

Note it is not one formula. Raw 1..6 divide a 1000 Hz base; 32 and 64 are
high-rate codes. `hskctl calibrate-polling` reproduces this in about 20 seconds.

## Settings do not survive a power cycle

The mouse has no save-to-flash command, and the vendor's Windows software does
not rely on one: `in_Queue_Close` / `in_Queue_Open` read and write *local
files*, and the app pushes settings back when the mouse reconnects. That is
what its tray icon and process watcher are for.

This does the same thing with systemd instead:

```bash
hskctl save                 # record the current settings as your baseline
./install.sh --autoapply    # re-apply them whenever the mouse reappears
```

Change something, run `hskctl save` again, and that becomes the new baseline.

## Install

```bash
omarchy plugin add https://github.com/keasbeexd/omarchy-hsk.git
omarchy plugin enable io.github.keasbeexd.hsk
```

That is the whole install. The repo *is* the plugin — `manifest.json` sits at
the root — and `hskctl` ships inside it, so the widget runs the copy bundled
beside itself with nothing else to set up.

One optional extra: a udev rule, so changing settings doesn't need sudo.

```bash
cd ~/.config/omarchy/plugins/io.github.keasbeexd.hsk
./install.sh --udev     # --link also puts hskctl on your PATH
```

Python 3.9+, no pip packages — it talks to `/dev/hidraw*` directly.

## Verify before trusting a write

```bash
hskctl probe      # rank the candidate endpoints
hskctl status     # read everything
hskctl doctor     # raw bytes of every read command; writes nothing
```

`set` always reads the field back and tells you if the mouse disagrees with what
you asked for, so a mapping error surfaces immediately rather than silently
landing somewhere else.

```bash
hskctl set pollingRate 4000
hskctl set dpiStage1 800
```

Full detail in [docs/PROTOCOL-DISCOVERY.md](docs/PROTOCOL-DISCOVERY.md).

## Using it

Bar widget: **left click** opens the panel, **right click** cycles DPI stage,
**middle click** refreshes.

In the panel: `↑`/`↓` moves, `←`/`→` changes the value under the cursor, `1`–`7`
jumps to a DPI stage, `d` cycles DPI, `m` toggles Motion Sync, `r` refreshes.

The panel only renders controls for settings the profile can actually write, so
it never offers an action that comes back as an error.

```bash
omarchy-shell io.github.keasbeexd.hsk cycleDpi
omarchy-shell io.github.keasbeexd.hsk setPollingRate 1000
hskctl status --json
```

## Layout

```
manifest.json    the Omarchy plugin manifest -- at the root, so
Panel.qml        `omarchy plugin add <url>` works directly
Service.qml
Model.js
bin/hskctl       launcher for the bundled CLI
hskctl/          CLI + hidraw transport + the profile interpreter
  hidraw.py        dependency-free ioctl wrapper, report-descriptor parser
  protocol.py      declarative profile engine (commands, codecs, ack)
  device.py        endpoint ranking, link-flag auto-detection
  cli.py           always-JSON-parseable command line
profiles/        the decoded protocol -- data, not code
tools/           analyze-driver.py, capture-usbmon.sh, decode-capture.py
tests/           42 Python tests, 33 JS tests
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

The Python suite pins both the decoded protocol and the hardware measurements:
every read opcode is its write opcode + `0x80`, wired and wireless packets
differ in exactly one byte, the DPI block decodes the captured reply, the
polling map is exactly what was measured, read-only fields refuse writes, and
factory reset stays unreachable.
