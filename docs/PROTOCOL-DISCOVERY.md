# Decoding the HSK Pro 4K config protocol

The HSK Pro 4K has no libratbag support and no working web driver, so the only
description of its config protocol is inside G-Wolves' Windows application.
This document is the procedure for getting that description out.

The end product is a filled-in `profiles/gwolves-hsk-pro-4k.json`. Once that
file has real offsets, `hskctl` and the Omarchy panel start working — no code
changes needed.

**Safety, up front.** Reading from a HID device is harmless. Writing is not:
a malformed packet to the wrong endpoint can, in the worst case, corrupt the
mouse's stored config or its firmware. So:

- `hskctl probe` and `hskctl status` only ever *read*. They are safe to run.
- `hskctl set` refuses to run at all while the profile is `undiscovered`.
- Never write to the pointer-input node. `probe` scores that node negatively
  for exactly this reason.
- Prefer capturing what the vendor app already sends over guessing packets.
  Every byte in the profile should come from an observed transaction.

---

## Step 0 — find the endpoint

```bash
hskctl probe
```

A vendor mouse typically presents three or four hidraw nodes. You are looking
for the one with a **vendor-defined usage page** (`0xFF00`–`0xFFFF`) and at
least one **Feature report ID**. `probe` ranks them and explains its reasoning.

Record the winner in the profile:

```json
"match": {
  "vendorIds": ["1234"],
  "productIds": ["5678"],
  "interface": 1,
  "usagePage": 65280
}
```

Do this even before you have any field offsets — it makes every later step
target the right node.

---

## Step 1 — read the protocol out of the vendor software (try this first)

Before capturing anything, look inside the Windows driver. These apps are
usually C#/.NET or Electron, and in both cases the packet layouts are sitting
in the binary in readable form — C# stores `new byte[]{0x08, 0x04, ...}` as a
raw blob the PE points at, and Electron just ships the JavaScript. That gives
you the whole protocol at once instead of one setting per capture, and it needs
no Windows machine and no Wine.

Download either build from the
[HSK Pro 4K product page](https://shop.g-wolves.com/pages/g-wolves-hsk-pro-4k-wireless-mouse):

| File | Version | Date |
|------|---------|------|
| `HSK_SW_FW20240320.zip` | v1.0.20.7 | 2024-03-20 |
| `HSK_Pro_4K_FWSW20230322.rar` | original | 2023-03-22 |

Then:

```bash
./tools/analyze-driver.py HSK_SW_FW20240320.zip --json driver-report.json
```

It never runs the vendor binary — it only parses it. What it reports:

- **USB vendor/product ids** scraped from every file, in both ASCII and the
  UTF-16 form .NET uses. These go straight into the profile's `match` block,
  and confirm what `hskctl probe` found.
- **Which HID API is used.** `HidD_SetFeature` means Feature reports
  (`transport.kind: "feature"`); `HidD_SetOutputReport` or a bare `WriteFile`
  means Output reports (`transport.kind: "output"`).
- **Which wrapper library.** HidLibrary, HidSharp, node-hid, and friends.
- **Candidate packet templates** — the byte-array blobs. A blob starting with
  a small constant that matches the report id is almost certainly a command
  template, and belongs in `commands.*.request`.
- **Setting-name strings** — `"Polling Rate"`, `"Lift Off Distance"`,
  `"MotionSync"` and so on, which tell you which settings the firmware
  actually exposes and in what order the UI presents them (often the same
  order as the bytes in the packet).

Grab the two archives if you can — diffing the 2023 and 2024 builds shows
which bytes the firmware update changed, which is a useful cross-check.

This usually gets you the transport, the report id, the packet length, and
most of the command templates. It rarely gets you every field offset, because
those are computed in code rather than stored as constants. For the remainder,
capture — which is now a much shorter job, because you already know what a
valid packet looks like.

## Step 2 — capture the vendor app

Two routes, depending on where you can run the Windows software.

### Route A — Wine, on this machine (preferred)

Wine passes HID through to the Linux kernel, so `usbmon` sees everything, and
you stay on one machine.

```bash
sudo ./tools/capture-usbmon.sh devices     # confirm the bus
sudo ./tools/capture-usbmon.sh start
```

Now launch the G-Wolves app under Wine and let it read the mouse once. Then,
for each setting, **change exactly one thing** and mark it:

```bash
./tools/capture-usbmon.sh mark pollingRate=1000
./tools/capture-usbmon.sh mark pollingRate=4000
./tools/capture-usbmon.sh mark dpiStage1=400
./tools/capture-usbmon.sh mark dpiStage1=1600
./tools/capture-usbmon.sh mark dpiStage1=3200
./tools/capture-usbmon.sh mark motionSync=1
./tools/capture-usbmon.sh mark motionSync=0
./tools/capture-usbmon.sh mark liftOffDistance=1
./tools/capture-usbmon.sh mark liftOffDistance=2
```

Use the field names from `hskctl fields` on the left of the `=`. That is what
lets the decoder emit a profile directly.

```bash
sudo ./tools/capture-usbmon.sh stop
```

### Route B — a real Windows machine

Install [Wireshark](https://www.wireshark.org/) with **USBPcap**, then:

1. Start Wireshark, pick the USBPcap interface the mouse is on.
2. In the filter bar: `usb.transfer_type == 0x02 || usbhid.data` — control
   transfers plus HID data. Widen it if you see nothing.
3. Change one setting in the G-Wolves app; note the frame number and what you
   changed.
4. Repeat for every setting and value.
5. `File → Export Packet Dissections → As JSON`.

Then build a labels file by hand — one line per change, `<epoch> <label>`:

```
1755400000.0 pollingRate=1000
1755400020.0 pollingRate=4000
```

Or skip timestamps entirely and paste the packet bytes into a labelled hexdump,
one packet per line, which the decoder also accepts:

```
pollingRate=1000: 08 05 00 00 01 03 20 00 ...
pollingRate=4000: 08 05 00 00 01 05 20 00 ...
```

---

## Step 3 — decode

```bash
./tools/decode-capture.py capture.json --labels capture.labels
```

The decoder does four things:

1. **Separates config packets from pointer motion.** It does this by
   repetition, not by frequency — motion reports are near-unique, config
   packets repeat. It prints the length groups and its scoring so you can
   override with `--length` if it picks wrong.
2. **Finds the checksum.** It tries `sum8`, `sum8_complement`, `xor8` and
   `sum8_minus_55` over every plausible range and reports what holds across
   every packet. Ranges too short to be meaningful are rejected.
3. **Attributes bytes to settings.** A byte that moves only when you changed
   `pollingRate` is the polling-rate byte. A byte that moves for *every*
   setting is the checksum or a sequence counter, and gets flagged rather than
   mapped.
4. **Infers the encoding.** If `dpiStage1=1600` shows a raw `32`, it proposes
   `"scale": 50`. If the values do not divide evenly it proposes an explicit
   enum instead.

When the output looks right:

```bash
./tools/decode-capture.py capture.json --labels capture.labels \
    --emit-profile ~/.config/hskctl/profiles/gwolves-hsk-pro-4k.json
```

Only high-confidence attributions are written. Merge them into the shipped
profile by hand, keeping the `from`, `min`, `max` and `values` metadata that
the panel uses for its controls.

---

## Step 4 — verify before trusting

Set `"status": "partial"` in the profile — that unlocks `hskctl`. Then:

```bash
hskctl status
```

Every value it prints should match what the vendor app shows. If polling rate
reads `250` when the app says `1000`, the offset or the enum is wrong. Fix it
before writing anything.

Only then try a write, starting with the most harmless field:

```bash
hskctl set pollingRate 1000
```

`hskctl set` always reads the field back and tells you if the mouse disagrees
with what you asked for. A mismatch means the mapping is wrong — stop and
re-check rather than trying more values.

Work up in this order, least to most risky:

1. `pollingRate`, `activeDpiStage` — reversible, obvious when wrong
2. `motionSync`, `angleSnap`, `rippleControl` — single bits
3. `liftOffDistance`, `debounceMs`
4. `dpiStage1`…`dpiStage6` — these rewrite stored config
5. `sleepMinutes`

When every field round-trips and survives unplugging the mouse, set
`"status": "verified"`.

---

## Notes on what to expect

**Read-modify-write.** `hskctl set` never synthesises a settings packet from
nothing. It reads the current block, changes one field, and sends it back, so
bytes you have not decoded keep whatever the mouse already had. If the
capture shows the vendor app doing something different — sending a
per-field command rather than a whole block — model that as a distinct
command in the profile instead.

**A commit packet.** Many vendor protocols need an explicit save/apply packet
after a write, or the change is silently lost on sleep. If the capture shows
one, put it in `commands.commit` and `hskctl` will send it automatically.

**Wireless vs wired.** The dongle and the cable often expose *different*
endpoints, and sometimes different packet formats. Capture both, and if they
differ, keep two profiles and select with `hskctl --profile`.

**Battery.** Battery is frequently on a separate command from the settings
block, which is why the profile has `readBattery` as its own command. It may
also only be readable over the dongle.
