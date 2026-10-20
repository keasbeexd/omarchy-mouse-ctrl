# Adding support for a mouse

Mouse CTRL talks to a mouse through a **family driver**: one small Python
module that knows one vendor protocol. Many models share a protocol, so a
driver usually covers more than the mouse it was written for. This guide is
how to write one for your mouse.

You need the mouse. A protocol can be decoded without it, but every write has
to be proven on real hardware before anyone else trusts it, so the owner is
the right person for the job.

## Ground rules

- **Read before you write.** Reading a HID report is harmless. A malformed
  write can, in the worst case, corrupt the mouse's stored config.
- **Every byte comes from evidence:** the vendor app's code or a capture of it.
  Never send a packet you guessed.
- **Never write to the pointer interface** (the one carrying mouse movement).
  Settings live on a separate vendor interface.
- **Every write is followed by a read-back** that must match, and a test write
  restores the original value. `mousectrl test-write` does both.

## 1. Identify the mouse

Set the mouse up in the panel first (the **+** button). If no driver claims
it, the wizard saves it read-only and the panel's **Copy report** button
copies everything below. From a terminal:

```bash
mousectrl devices                    # vendor:product id, interfaces, access
mousectrl probe <vid:pid> --report   # every interface with its HID descriptor
```

In the report, the settings interface is usually the one with a vendor usage
page (`0xff00`–`0xffff`) and a feature or output report id.

## 2. Read the protocol out of the vendor app

Vendor configurators are usually C#/.NET or Electron, and in both the packet
layouts sit in the binary in readable form: C# stores `new byte[]{0x08, ...}`
as a blob the PE points at, and Electron ships its JavaScript. That gives you
the whole command table at once, with no Windows machine and no Wine.

```bash
scripts/analyze-driver.py VendorSoftware.zip --json report.json
```

It only parses the files, never runs them, and reports:

- USB vendor and product ids (your driver's `VENDOR_ID` / `PRODUCT_IDS`)
- which HID API the app calls: `HidD_SetFeature` means feature reports,
  `HidD_SetOutputReport` or a bare `WriteFile` means output reports
- the wrapper library (hidapi, HidSharp, node-hid…)
- candidate packet templates: byte arrays starting with a small constant
  that matches the report id
- setting-name strings, often in the same order as the bytes in a packet

`.zip` files are read directly; `.rar` and installer `.exe` files need `7z`
or `unrar`. The G-Wolves HSK protocol came out of its .NET binary this way
with no capture at all ([provenance](profiles/gwolves-hsk-pro-4k.md)).
Native C++ apps hide packet logic in machine code, but still give you ids and
factory defaults ([Pulsar](profiles/pulsar-x2h-mini.md) is that case).

## 3. Capture what the app sends, when step 2 was not enough

Run the vendor app with the mouse attached and record the bus. Under Wine,
HID passes through to the kernel, so `usbmon` sees it:

```bash
sudo modprobe usbmon
lsusb -d <vid>:                       # note the bus number
sudo tcpdump -i usbmon<bus> -w capture.pcapng
```

On Windows, USBPcap with Wireshark does the same. **Change exactly one
setting at a time and write down what you changed and when.** A capture
where packet 41 is known to be "polling 1000 → 4000" decodes itself; a
capture of someone clicking around does not.

## 4. Decode

Line the packets up against your notes. Look for:

- **A constant prefix:** report id, length, opcode. Read and write opcodes
  often differ by a fixed bit (the HSK's read = write + `0x80`).
- **The byte that moved** when one setting changed: its offset. Changing the
  same setting from several values reveals the encoding (u8, u16, a table).
- **A checksum,** or none: try sum, XOR and `0x55 - sum` over several packets.
  Pulsar checksums the whole packet *and* each written value.
- **Replies you did not cause:** pointer movement and status events arrive on
  the same interface. Match replies to requests by an echoed opcode.

## 5. Write the driver

Copy [contributor/driver_template.py](contributor/driver_template.py) to
`mousectrl/families/<vendor>_<protocol>.py`, fill in what it marks, and add
the class to `FAMILIES` in `mousectrl/families/__init__.py`.

- Declare each setting as a `Capability`; the kinds and value shapes are in
  `mousectrl/families/base.py`. Values reach `write()` already validated.
- Start every capability as `UNTESTED`.
- Raise `NoResponse` when the mouse is silent and `ProtocolFailure` when a
  reply cannot be trusted. Never return a value you did not check.
- Add a test module with a fake device that answers like the firmware
  (`tests/test_pulsar_nordic.py` replays a real capture). Run
  `python3 -I -m unittest discover -s tests -t tests`.

## 6. Prove it on the mouse

```bash
mousectrl read <vid:pid>                       # every value must match the vendor app
mousectrl test-write <vid:pid> <capability>    # write a nearby value, read back, restore
```

Then run the panel's setup wizard: confirm each group against the vendor
app, and run the write test for every untested setting. A capability becomes
`VERIFIED` in the driver only after it round-trips on hardware; record what
you did and saw in `docs/profiles/<model>.md`, the way the HSK and Pulsar
pages do.

## 7. Send it

Open a pull request with the driver, its test module and the provenance page.
Say which settings you verified, on which product id and firmware version,
and anything you saw but could not explain.
