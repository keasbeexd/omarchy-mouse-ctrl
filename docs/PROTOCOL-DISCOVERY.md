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

## Step 2 — capture the vendor app, if step 1 was not enough

It was enough here: every command, offset and encoding in `profiles/` came out
of the binary, and no packet capture was ever needed. The helper scripts that
existed for this route were removed rather than shipped unused — they are in
git history if you want them. What follows is the method, for anyone profiling
a mouse whose driver is less readable.

Run the vendor software with the mouse attached and watch the bus. Under Wine,
HID passes through to the Linux kernel, so `usbmon` sees everything without a
second machine:

```bash
sudo modprobe usbmon
lsusb -d 33e4:                       # find the bus number
sudo tcpdump -i usbmon<BUS> -w capture.pcapng
```

On a real Windows machine, USBPcap plus Wireshark gives the same thing.

The discipline matters more than the tooling: **change exactly one setting at a
time and write down what you changed and when.** A capture of somebody clicking
around is nearly useless; a capture where you know that packet 41 was
"pollingRate 1000 → 4000" decodes itself.

## Step 3 — decode

Line the packets up against your notes. You are looking for:

- **A constant prefix.** Report id, length, opcode. Opcodes usually cluster —
  here every read opcode is its write opcode plus `0x80`.
- **The byte that changed** when you changed one setting. That is your value
  offset, and repeating the same change from different starting values tells
  you the encoding (`u8`, `u16be`, an index into a table).
- **A checksum**, or the absence of one. Try the obvious candidates — sum of
  the payload, XOR, sum negated — against several packets before concluding
  there is one. This mouse has none.
- **Packets you did not cause.** Battery polling and connection state show up
  unbidden and will confuse a naive diff.

Write what you find into a new `profiles/*.json` and let the engine interpret
it. Resist putting any of it in Python.

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
