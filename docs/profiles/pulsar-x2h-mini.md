# Pulsar X2H mini — profile provenance

Notes for contributors on `profiles/pulsar-x2h-mini.json`. The profile
carries the bytes and their one-line caveats; the reasoning lives here.

## Confirmed on real hardware

A Pulsar X2H mini with its 4K wireless dongle. Battery, firmware version,
polling rate (up to 4000 Hz, confirming the dongle really is the 4K
variant), motion sync, angle snap, ripple control, turbo mode, lift-off
distance, debounce, sleep timer, and DPI stage 1's value and LED color
have all round-tripped real writes on that exact hardware — the LED color
write the owner visually confirmed actually changed the mouse's light.

Only the 4K dongle variant is verified. Pulsar sells the X2H mini with
either a 1K or a 4K dongle; if you have the 1K variant, the polling-rate
ceiling and possibly the USB ids could differ — report what `hskctl probe`
and a `hskctl set pollingRate 2000` attempt give.

## Source — three stages, cheapest first

1. **Community Linux tools** already speaking Pulsar's Nordic protocol —
   `packerlschupfer/pulsar-mouse-linux` (`drivers/nordic.py`, an
   address-mapped memory protocol for VID 0x3554) and
   `andrewrabert/python-pulsar-mouse-tool`, its credited source — gave the
   packet shape (17 bytes, opcode + address + length, `sum8_minus_55`
   checksum) and the command/address table, but not this exact model's USB
   ids nor (initially) the right transport.
2. **Static analysis of the real Pulsar Fusion Windows installer**
   (extracted with `innoextract`; `tools/analyze-driver.py`, recovered from
   repo history, pointed at `Pulsar Fusion Wireless Mice.exe` and its
   `Cfg.ini`) confirmed the real USB ids (`[OPT]`: VID=25A7,3554
   PID=fa7b,f507 PID2=fa7c,f508,f509) and this model's factory defaults
   (`[DEV_13]`: Sensor=0x3395/PAW3395, DPI=400,800,1600,3200, Debounce=3) —
   capability metadata, not wire protocol, since the app is native
   C++/MFC and its packet-building logic is machine code, not an
   extractable blob the way a .NET app's byte-array literals were for
   G-Wolves.
3. **Live testing against the real device** caught what 1 and 2 could
   not: the transport is a raw USB **Output** report (SET_REPORT) at
   report id `8`, not the Feature-report transport the HID descriptor's
   own feature-report id (`6`) first suggested — already implemented as
   `transport.kind: "output"` — and every scalar write needs a *second*
   checksum byte (`0x55 - value`) immediately after the value, separate
   from the whole-packet checksum. That became the engine's
   `valueChecksum` command option. DPI stages needed the `nordicDpi3`
   field encoding, cross-checked byte-for-byte against the real driver's
   `_raw_to_dpi`/`_dpi_to_raw` for several DPI values before it ever
   touched hardware.

Credit and thanks to both community projects for the original
reverse-engineering work.

## Identity, cable and dongle

Cable and dongle were seen as `3554:f507` in v3. On 2026-10-19 the 4K
receiver enumerated as its own identity, `3554:f509` ("Pulsar 4K Wireless
Receiver"), and **carried the full config protocol**: every read and write
in the v4 driver was verified through it.

The 1K receiver enumerates as `3554:f508` ("Pulsar 1K dongle update"),
with the same config interface (interface 1, output report 8, feature
report 6) but keyboard on interface 0 and mouse on interface 2. The mouse
is not paired to it, so nothing has been read through it yet; the driver
offers only 125–1000 Hz polling there, marked untested. Neither community
driver implements pairing.

Do **not** add `3710:1403` (X2H Wired Medium): a different, 64-byte,
register-addressed protocol that needs its own driver.

## DPI stages: values work, switching does not (v4 findings, 2026-10-19)

- **Stage values and colors write correctly in every slot, and persist.**
  Stage 2's DPI 800 → 850 → 800 and stage 1's color round-tripped through
  the v4 driver; v3 saw stage 1's DPI and color take effect live. With 850
  written, a power cycle of the mouse still read 850 on reconnect — and
  since the receiver refreshes from the mouse on connect, that means the
  write reached the mouse's own storage.
- **`0x04` reports the live stage; it is not a control.** A watcher saw it
  step 0 → 1 → 2 → 3 → 0 with each press of the mouse's DPI button (red →
  blue → magenta → white → red). Writing it stores and reads back but the
  mouse does not switch, and the mouse later overwrites it.
- **The DPI button works.** v3 recorded that it did nothing; it cycles the
  four stages.
- **Writing the stage count (`0x02`) is safe but has no live effect.** 4 → 4,
  4 → 3 → 4 left all eight slots byte-identical (v3's reported reset of the
  whole table did not reproduce). With 3 stored, the button still cycled
  four stages.
- **A reconnect does not apply them either; the mouse overwrites them.**
  With count 3 stored, the mouse was power-cycled: on reconnect `0x02` read
  4 again and the button cycled four stages. The receiver's memory mirrors
  the mouse and is refreshed from it on connect. Some writes are forwarded
  to the mouse (v3 saw stage 1's DPI change the cursor speed); count and
  active stage are not, so changing them needs whatever separate command
  Pulsar Fusion sends — a capture job.
- Both community drivers write `0x02`/`0x04` the same way with no commit
  step, and neither was confirmed on the X2H. pulsar-mouse-linux describes
  the receiver's memory as a window onto the active profile that
  `ACTIVE_PROFILE_SET` (`0x0F`) reloads. Untested here.
- Unsolicited `0x0A` event reports carry `0x01` for "DPI changed" and
  `0x04` for "profile changed" (pulsar-mouse-linux); a listener must read
  the value back, the event carries none.

## Wire details confirmed in v4

- Replies echo report id, opcode and address (bytes 0–4) but not always the
  length: battery and version requests send 0 at byte 5, replies carry 2.
- Replies carry the same `0x55 - sum` checksum at byte 16 as requests.
- The config interface also delivers pointer reports; the driver skips
  anything that does not echo its request.
- A sleeping mouse does not answer at all until it is moved (30 s sleep
  timer on this mouse).

## Low-risk known gaps

- Autosleep writes only 0xB7 and the mouse honours it; Pulsar Fusion also
  writes 0xAD with the same value (pulsar-mouse-linux, from an X2 CrazyLight
  capture).
- 8000 Hz is in `values` (register map confirmed on a Nordic-family
  sibling model) but withheld from `uiOptions` until an 8K dongle exists
  to test against.
