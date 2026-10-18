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

## IDENTITY, cable and dongle

Cable and the 2.4 GHz dongle present the **identical** `3554:f507`
identity on the identical hidraw node — the dongle tunnels the mouse's own
USB identity through transparently. There is no wired/wireless split to
show for this model. The RF receiver's separate identity (`3554:f509`) has
never been seen carrying the config protocol; it stays in
`match.productIds` for lack of positive evidence either way.

Do **not** add `3710:1403` (X2H Wired Medium) to this profile: that model
speaks a different, 64-byte, register-addressed protocol — it needs its
own profile.

## Stage switching does not work here (yet)

`activeDpiStage` writes round-trip (the byte stores and reads back) but
have no observed live effect on the mouse, and the physical DPI button
does not switch stages either. `dpiStageCount` was tried as the
explanation — Cfg.ini's factory default is 4 — and setting it reset the
whole DPI table to factory defaults (overwriting a custom stage 1 that had
been set moments earlier) without fixing switching, so it was reverted and
locked read-only. `activeDpiStage` is mapped **read-only** as well: that
flag is the profile's half of the panel's stage-switcher gate.

The real mechanism is most likely a dedicated command distinct from the
plain memory writes `nordic.py` assumes for other models (compare
`CMD_ACTIVE_PROFILE_SET`). Finding it needs a live USB capture
(usbmon/Wireshark) of Pulsar Fusion actually switching stages. Stages
2–4's storage addresses are known and were confirmed readable/writable
before stage 1 became the only mapped one:
`ADDR_DPI_BASE + 4*n` (0x0C + 4n) and `ADDR_LED_COLOR_BASE + 4*n`
(0x2C + 4n).

DPI records are 4 bytes: 3 data bytes (`nordicDpi3`) + 1 valueChecksum;
LED color is the same shape with `rgb` encoding. Byte 5 is 3 for reads and
4 for writes (3 value bytes + the checksum byte the device stores).

## Low-risk known gaps

- Autosleep here writes only 0xB7; the real driver mirrors the same value
  to 0xAD (`ADDR_AUTOSLEEP_ALT`).
- Unsolicited reports (e.g. opcode `0x0A` after a profile switch) can
  desync a naive read-once; the real driver retries until `resp[1]`
  matches the opcode sent, while `check_ack` here only matches a fixed
  expected byte.
- 8000 Hz is in `values` (register map confirmed on a Nordic-family
  sibling model) but withheld from `uiOptions` until an 8K dongle exists
  to test against.
- Sleep writes go to a single address where the driver uses two — needs a
  "write to two addresses" hook if the firmware requires both in sync.
