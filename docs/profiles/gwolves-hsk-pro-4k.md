# G-Wolves HSK Pro 4K — protocol provenance

Evidence behind `mousectrl/families/gwolves_hts.py` (v4) and every
verification flag in it. v3's `profiles/gwolves-hsk-pro-4k.json` carried the
same protocol as data; it is kept in git history and
`docs/contributor/families/gwolves-hsk-pro-4k.json` until v3 is removed.
For how the protocol was decoded, see docs/contributor/PROTOCOL-DISCOVERY.md.

## Source

Extracted from `G-Wolves_Software_V1.0.20.07` (2024-03-20), a C++/CLI
mixed-mode .NET binary using hidapi. The transport and command table are read
directly out of the shipped code — `hts_send_cmd`'s IL and the
`HTS_*_CMD` static byte arrays — so they are observed, not guessed.

## Wire format

65-byte HID Feature reports, report id 0, no checksum.

- byte 1: ACK (`0xA1`)
- byte 2: payload length
- byte 3: opcode — **read opcode = write opcode + 0x80**; opcodes already
  carrying the high bit are read-only
- byte 4: link flag (dongle/wired) — see "An ACK is not success" below
- byte 5 onward: value

`hts_send_cmd`: `Sleep(60); hid_send_feature_report(h, tx, 65);
Sleep(60); hid_get_feature_report(h, rx, 65); accept iff rx[1] == 0xA1`.

## An ACK is not success

The firmware acknowledges a packet carrying the wrong link flag and then
ignores it, replying with an all-zero payload — identical to a command it
never received. And that link flag describes *the endpoint you opened*, not
where the mouse is: plug the cable in while the dongle is still in and the
two disagree, at which point every setting reads back 0. `hskctl`
establishes it by probing both flags on every candidate node and keeping
the one that answers with real data (the `linkProbe` block). `dpi` is the
probe command because a working mouse cannot have an all-zero DPI block,
whereas battery, motion sync and angle snap can all legitimately read 0.

## The DPI block has a header that changes how it is parsed

`rx[5]` is the active stage and `rx[6]` is how many stages the firmware
will take out of a write (`dpiStageCount`). A mouse reporting 0 there
silently discards every DPI value and every color, and a naive
read-modify-write copies that 0 straight back, so it never recovers. The
`repairOnWrite` fix in the profile substitutes 7 — confirmed on hardware
2026-08-18: setting the count to 7 made value and color writes land
immediately in both.

The layout read back and decoded 2026-10-18 (dongle link, no writes):
stage entries start at `rx[7]` and stride 7 bytes each — X (u16be), Y
(u16be), RGB — so stage N lives at `rx[7 + (N-1)*7]` through
`rx[6 + N*7]`. That one read showed the vendor ramp
400/1600/1600/3200/4500/5000/6400 with per-stage colors, and it is why
the profile can map all seven stages as ordinary offset fields in the
same block rather than staging a write to read one.

The DPI write also needs a longer `settleMs` (140 vs 60): 51 bytes of
payload against one for the scalar commands, and at the vendor's 60 ms the
reply is sometimes not ready — an all-zero payload that looks exactly like
a command the mouse ignored, which is how stage selection failed
intermittently while smaller commands were fine.

## Why `readOnly` / `_needsVerification` are load-bearing

- `debounceMs`: read returns byte 0 of a 4-byte tuple (observed
  `03 0a 23 14`, row 1 of the driver's 6-row table) while the write takes
  a row index — the two are not the same number, so writing stays disabled
  until the table is modelled.
- `dpiStageCount`: writing it is untested on hardware. Read it, change the
  count, and check the mouse cycles that many stages before trusting the
  write.
- `_needsVerification` closes the write path outright (see
  `hskctl/protocol.py` `field_writable`). Verify on hardware and delete
  the marker; that is the only route.

## Battery branch by product id

`get_Battery_empty` branches on product id: the 5407/5408, 5707/5708,
5807/5808 and 5907/5908 family (this model among them) only rounds the
byte down to a multiple of 5 before display. The other family
(5403/5404, 5703/5704, 5803/5804) runs it through a four-segment piecewise
curve instead: raw/2 up to 40, then `(raw-40)*25/30+20`,
`(raw-70)*30/20+45` and `(raw-90)*25/10+76`. "The byte is a percentage" is
a fact about this model, not about the protocol — do not carry it to
another profile without checking which branch that product id takes.
hskctl reports the byte unrounded, which is why it can read 97 where the
vendor app shows 95.

Battery discharge was confirmed on hardware 2026-08-19: 100 → 81 across a
night on the dongle, about 2.4 points/hour, implying roughly 40 hours from
full. Charging flips within one sample of the cable going in or out;
`rx[4]` stayed 1 (dongle) throughout, so the mouse keeps its RF link while
charging.

## Polling rates

Every value in `pollingRate`'s `values` map was measured by timing the
mouse's own input reports — none inferred. Raw 1–6 divide a 1000 Hz base,
while 32 and 64 are separate high-rate codes for 2000 and 4000 Hz:
non-monotonic, so an explicit map is the only honest model. Raw 8
(predicted 125 Hz) and raw 16 are untested. The vendor slider emits
`1 << position`, so positions 0..6 give raw 1,2,4,8,16,32,64.

`uiOptions` is what the panel renders; `values` is the full set
`hskctl set pollingRate <n>` accepts. 200/333/167 Hz are real, confirmed,
settable values — just not rates G-Wolves' own app offers as a selection.

## Sleep timer

SECONDS, confirmed on hardware 2026-08-19 by leaving the mouse still and
timing it: a stored 240 slept after four minutes. (It was called
`sleepMinutes` until then, on no evidence whatsoever.) The 30 s step comes
from `Sleep_update_ui`, which positions its slider at `(value / 30) - 1`;
the vendor only ever emits multiples of 30, and a value under 30 selects a
different label, presumably "off". The slider's Maximum is not in
`Sleep_update_ui`, so the profile's max is deliberately generous — a value
the firmware rejects fails `set`'s read-back and is reported, rather than
landing somewhere unexpected.

## v4 driver findings (2026-10-19, dongle, firmware 1.0.7)

- **Reply byte 1 = `0xA0` means the receiver answered without the mouse.**
  With the mouse asleep, a read on the dongle flag returned `a0` and an
  all-zero payload (the cable flag returned `a1`, all-zero); moving the
  mouse turned it into `a1` with data. The driver reports this as "the
  mouse is not linked" instead of a generic failure.
- **The DPI read's length byte is `0x33` (51), not the `0x39` sent.** The
  driver checks the ACK and the echoed opcode, never the length.
- **Stage count writes round-trip.** 7 → 6 → 7 read back each step; while at
  6 the block listed six stages, and stage 7's value and color were intact
  after returning to 7 — the firmware keeps a dropped stage in memory.
- **Every writable capability round-tripped through `mousectrl write`:**
  polling 1000 → 2000 → 1000, motion sync on → off → on, stage 1 color
  red → white → red (the whole block identical afterwards).

## Factory reset

`HTS_RESET_CMD` (opcode `09`) restores factory defaults and is deliberately
not wired to any field — nothing in the panel should be one keystroke from
wiping the mouse's configuration.

## On the shape of this file

The profile once carried its provenance prose inside `_comment`-style
keys. Those essays now live on in this document and in repo history; the
JSON keeps the machine-readable flags (`readOnly`, `_needsVerification`,
`_verified`, `repairOnWrite`, `values`, `ui`) because code acts on those.
