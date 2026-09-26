# Mouse Control — an Omarchy plugin

Battery, DPI, polling rate and sensor settings for supported **G-Wolves** and
**Pulsar** mice, in the Omarchy bar.

![The Mouse Control bar widget and panel](preview.png)

> **Renamed and moved from HSK Mouse.** This plugin used to be G-Wolves-only,
> called `keasbeexd.hskmouse` / "HSK Mouse", and lived in the
> [`omarchy-hsk`](https://github.com/keasbeexd/omarchy-hsk) repo. Now that it
> also speaks Pulsar's protocol, it's `keasbeexd.mousectrl` / "Mouse Control"
> instead, developed here in this repo (`mouse-ctrl`) going forward.
> `omarchy-hsk` still exists with the G-Wolves-only version, unchanged, for
> anyone not ready to move. If you have the old version installed, see
> [Upgrading from HSK Mouse](#upgrading-from-hsk-mouse) below — it is neither
> an automatic update nor an in-place one.

Most of these mice have no libratbag support, and several vendors' own Linux
story is "there isn't one" — DPI and polling rate configuration ships as a
Windows-only app, if it ships at all. This plugin talks to the mouse directly
over raw HID, using protocols recovered from those Windows apps (for G-Wolves)
or from existing open-source Linux tooling (for Pulsar).

## Supported mice

| Vendor | Model | Status |
|---|---|---|
| G-Wolves | HSK Pro 4K | Confirmed on hardware — battery, DPI (stage 1 + colour), polling rate, sensor settings, sleep timer |
| Pulsar | X2H mini (4K dongle) | Confirmed on hardware — battery, DPI (stage 1 + colour), polling rate up to 4K, sensor settings, sleep timer. 1K dongle variant unconfirmed. See [Pulsar X2H mini](#pulsar-x2h-mini) below |

The panel only ever shows the settings the connected mouse's profile actually
reports and can write — see [How multiple mice work](#how-multiple-mice-work).
Adding another mouse is a new file under `profiles/`, not a code change; see
[Other models](#other-models).

## Install

Two steps. **Both are required** — the second is not optional polish.

```bash
omarchy plugin add https://github.com/keasbeexd/mouse-ctrl.git
omarchy plugin enable keasbeexd.mousectrl
```

```bash
~/.config/omarchy/plugins/keasbeexd.mousectrl/install.sh --udev
```

Then **unplug and replug** the mouse or its dongle, and add the **Mouse
Control** widget to your bar.

Why the second step matters: configuring the mouse means sending HID *feature*
reports, and the hidraw ioctls that carry those need the device node opened
read-write. `/dev/hidraw*` is root-only by default, so without the rule the
plugin cannot reach the mouse at all — not even to read the battery. The rule
grants access to whoever is logged in at the seat (the same `uaccess` mechanism
your sound card uses), scoped to one line per supported vendor's id and
nothing else — see the script's `VENDOR_IDS` list. The script shows you
exactly what it will write before asking for `sudo`.

If something looks wrong, `hskctl doctor` says in the first few lines whether
permissions are the problem.

Requires Python 3.9+. Nothing else — no pip packages, no daemon, no background
service, no libratbag. The CLI the widget drives ships inside the plugin.

## What it does

On the G-Wolves HSK Pro 4K, confirmed on real hardware:

| | |
|---|---|
| **Battery** | percentage in the bar, low-battery warning, charging state |
| **DPI** | seven stages, each with its own value and LED colour |
| **Polling rate** | 250, 500, 1000, 2000 and 4000 Hz |
| **Sensor** | motion sync, angle snapping, lift-off distance |
| **Firmware** | version and link (dongle or cable) |

A different mouse shows only the rows above that its own profile actually
maps and can write — see [How multiple mice work](#how-multiple-mice-work).
Nothing here is a guess carried from one mouse's profile into another's.

## How multiple mice work

Every setting the panel can show or write comes from `profiles/*.json`, one
file per mouse, interpreted by a generic protocol engine — there is no
per-mouse code anywhere in this plugin. Plug a mouse in and `hskctl` picks the
profile whose `match` block (vendor id, product id, HID usage page) fits a
device actually connected, the same way it decides whether it is safe to
*write* to that device. Nothing is shown or shared across mice by guesswork:

- A row only appears if the connected mouse's profile reports that field in
  `hskctl status` **and** the profile can write it. A mouse with no lift-off
  distance setting has no lift-off row; one whose polling-rate mapping is
  still unverified has a rate reading but no selector to change it.
- Polling rate options, DPI ranges and similar per-field choices come from
  the matched profile too (its `values`/`min`/`max`), not from a list baked
  in for one mouse — so a mouse that goes to 8000 Hz offers 8000 Hz, and one
  that tops out at 1000 Hz does not show rates it cannot reach.
- `hskctl status` (no `--profile`) auto-detects: it asks every profile in
  `profiles/` whether its `match` block fits a connected device, and uses the
  first one that does. `hskctl status --profile <name>` or `--device <path>`
  overrides detection when you are working on a profile that is not
  matching yet.

## Pulsar X2H mini

Its profile (`profiles/pulsar-x2h-mini.json`) is **fully confirmed on real
hardware — a Pulsar X2H mini with its 4K wireless dongle.** Every setting
this plugin exposes for it, reads and writes, round-tripped on that exact
mouse: battery, firmware version, polling rate (up to 4000 Hz), motion sync,
angle snap, ripple control, turbo mode, lift-off distance, debounce, sleep
timer, and DPI stage 1's value and LED colour — the colour confirmed by
watching the mouse's actual LED change on command.

**Only DPI stage 1 is mapped, deliberately.** The mouse's firmware has four
DPI stages, and the obvious next step was to expose all four with a switcher.
That turned out not to work: writes to `activeDpiStage` round-trip correctly
(the byte you write reads back), but have no observed effect on the mouse's
actual cursor speed or LED, and the mouse's own physical DPI button does
nothing either. Rather than ship a stage switcher that looks like it works
but doesn't, this plugin only ever reads and writes stage 1 — which is also
all this plugin's own author uses on their G-Wolves mouse, so it costs
nothing in practice. `dpiStageCount` and `activeDpiStage` stay mapped in the
profile as read-only diagnostics for anyone chasing the real switch mechanism
later (see the profile's own `_followUp` notes), but the UI never surfaces
them.

**If you have the 1K dongle variant instead of the 4K one, this is
unconfirmed for you** — Pulsar sells the X2H mini with either, and only the
4K dongle has been tested. The polling-rate ceiling in particular may differ;
please open an issue with what `hskctl probe` and a `hskctl set pollingRate
2000` attempt report on a 1K dongle.

It started as a transcription of two open-source Linux tools that speak to
Pulsar's Nordic wireless dongle —
[packerlschupfer/pulsar-mouse-linux](https://github.com/packerlschupfer/pulsar-mouse-linux)
and [andrewrabert/python-pulsar-mouse-tool](https://github.com/andrewrabert/python-pulsar-mouse-tool)
— which got the packet shape and command/address table right but not this
exact model's USB ids or (initially) its transport details. Getting the rest
right took two more steps, in order of how cheap they were:

- **Static analysis of the real Pulsar Fusion Windows installer**
  (`tools/analyze-driver.py`, a vendor-neutral script recovered from this
  repo's own git history — see [Local development](#local-development))
  found real vendor data with no hardware needed: extracting the installer
  (it's an Inno Setup package; `innoextract` unpacks it) and pointing the
  script at `Pulsar Fusion Wireless Mice.exe` and the `Cfg.ini` it ships
  confirmed the real USB ids (`VID=25A7,3554 PID=fa7b,f507
  PID2=fa7c,f508,f509`) and this exact model's factory defaults
  (`Sensor=0x3395` — a PixArt PAW3395 — `DPI=400,800,1600,3200`,
  `Debounce=3`). The app turned out to be native C++/MFC, not .NET or
  Electron, so its actual packet-building logic is machine code rather than
  an extractable blob the way it was for G-Wolves' .NET app.
- **Live testing against the real device** caught what static analysis
  couldn't: the transport is a raw USB **Output** report at report id `8`,
  not the Feature-report transport the HID descriptor's own feature-report
  id (`6`) first suggested, and every scalar write needs a *second*
  checksum byte (`0x55 - value`) immediately after the value, separate from
  the usual whole-packet checksum. Both are now generic engine features —
  `transport.kind: "output"` and a `valueChecksum` command option — not
  Pulsar-specific code. DPI stages needed a new field encoding
  (`nordicDpi3`) for their bit-packed 3-byte format, cross-checked
  byte-for-byte against the real driver's own math for several DPI values
  before it ever touched hardware.

One more real finding along the way: cable and the 2.4GHz dongle present the
**identical** `3554:f507` identity on the identical hidraw node — there is no
separate wired/wireless split for this model the way there might be for
others, and the RF receiver's own separate USB identity (`3554:f509`) has
never actually been seen carrying the config protocol at all.

See the `_followUp` list at the bottom of the profile's JSON for the
remaining narrower gaps (the 1K dongle, a couple of low-risk protocol-engine
edge cases, multiple onboard profiles on chipsets that have them), and open
an issue with what you find on hardware this hasn't been tested against.

## Using it

**Bar widget:** left click opens the panel, middle click refreshes.

**In the panel**, DPI is one row: a number field you can type an exact value
into, a draggable slider (50 DPI steps) below it, then three one-click
preset colours, a swatch showing the current colour (click it to cycle the
firmware's full palette), and a hex field for typing an exact colour. There
is deliberately no stage picker — see [Pulsar X2H mini](#pulsar-x2h-mini)
above for why, and note this applies to the G-Wolves profile too even though
its firmware's stage switching does actually work: this plugin only ever
uses stage 1.

Rapid input is coalesced — drag the slider from 400 to 3200 and the mouse
gets one write, when you release — and the panel says *Writing to the
mouse…* whenever an exchange is in flight, because a write is a real USB
round trip and silence reads as a dead click.

The version of the plugin you are running is in the bottom-right of the panel;
hover it for the firmware version and which hidraw node is in use. `hskctl
--version` reports the same number.

**Keyboard:** `↑`/`↓` between rows, `←`/`→` to adjust the row under the cursor
(DPI in 50-unit steps), `r` to refresh.

The panel only renders controls for settings that can actually be written, so it
never offers an action that comes back as an error.

## From the command line, and from scripts

The plugin is a front end for `hskctl`, which is useful on its own:

```bash
hskctl status                    # everything the mouse reports
hskctl set pollingRate 4000
hskctl set dpiStage1 1600
hskctl doctor                    # raw bytes of every read; writes nothing
hskctl watch-battery             # sample the battery over time; writes nothing
```

On the battery reading: `hskctl` reports the byte the mouse sends, which for
this model is a percentage. The Windows app rounds it down to a multiple of 5
before displaying it, so hskctl can read 97 where the app shows 95 — the same
value, shown with one fewer step of rounding. `watch-battery` prints both.

`--json` on any command gives parseable output, including on failure.

Omarchy IPC works too:

```bash
omarchy-shell keasbeexd.mousectrl setDpi 1600
omarchy-shell keasbeexd.mousectrl setPollingRate 1000
```

## Settings

| Setting | Key | Default | |
|---|---|---|---|
| Battery poll | `batteryPollSec` | 300 | how often the bar re-reads the battery percentage, in seconds (30–3600) |
| Low battery warning | `lowBatteryPercent` | 15 | when the icon turns urgent |
| Show battery percentage | `showBatteryLabel` | on | the `94%` text beside the icon |
| Path to `hskctl` | `hskctlPath` | *(bundled)* | override only if you installed it yourself |

The battery percentage is the only reading that changes on its own, so it is
the only thing re-read on a timer (`batteryPollSec`). Plugging or unplugging the
cable or dongle does not wait for that timer: a watcher notices it straight
away and triggers a full refresh. Everything else only this plugin writes, so
it is read once at startup, on a manual refresh, and back from each write. The
battery poll is also silent: it never shows *Writing to the mouse…*, which is
reserved for an actual write or a full refresh.

There is no settings UI. Omarchy keeps every widget's options **inline on that
widget's entry** in `~/.config/omarchy/shell.json`, under the `bar` key, in
whichever of the three layout arrays the widget sits in:

```json
{
  "bar": {
    "right": [
      { "id": "keasbeexd.mousectrl", "showBatteryLabel": true, "batteryPollSec": 300 }
    ]
  }
}
```

Every key is optional — leave one out and the default above applies. Restart the
shell after editing:

```bash
omarchy-restart-shell
```

If `showBatteryLabel` is on but no number appears, the label is deliberately
blank in two cases: whenever the battery cannot be read, so the bar shows the
icon alone rather than a stale or invented figure — `hskctl status` will say
why the read is failing — and in a vertical bar, which is one icon wide and has
nowhere to put it.

While the mouse has not been read successfully — right after login before its
dongle has finished enumerating, or right after the computer wakes from
suspend before a wireless dongle's RF link to the mouse has reconnected — the
bar retries every few seconds rather than waiting on the next scheduled poll,
so it catches up on its own within a handful of seconds instead of showing a
stale reading indefinitely (opening the panel also forces an immediate
refresh, if you want one sooner).

## Removing

```bash
omarchy plugin remove keasbeexd.mousectrl
```

That takes the plugin out of the shell. What survives, and how to remove it:

- The **udev rule** at `/etc/udev/rules.d/60-mousectrl.rules` stays in place
  (it needs `sudo` to have got there in the first place). Remove it with
  `sudo rm /etc/udev/rules.d/60-mousectrl.rules && sudo udevadm control --reload-rules`
  if you no longer want your user to have hidraw access to any of these mice.
- If you ran `./install.sh --link`, a symlink at `~/.local/bin/hskctl` points
  at the plugin. `./install.sh --uninstall` removes it (and only if it still
  points at this checkout — a shadow binary someone else put there is left
  alone).
- Any `hskctl save` snapshot lives at `~/.config/hskctl/settings.json`; delete
  it if you kept one. It only ever holds settings the mouse itself reports
  (DPI stages, polling rate, motion sync), never anything private.

Nothing else persists: the plugin writes no cache, no log, no state file on
its own, and it never runs anything in the background. Mouse configuration
lives on the mouse itself and follows the mouse, not this plugin.

## Upgrading from HSK Mouse

Versions before 2.0 were `keasbeexd.hskmouse` / "HSK Mouse", G-Wolves only,
and lived in a **different repository**,
[`keasbeexd/omarchy-hsk`](https://github.com/keasbeexd/omarchy-hsk) — this
plugin is not a new version published from that repo, it is a new repo
(`keasbeexd/mouse-ctrl`) that continues the same project under a new name.
`omarchy-hsk` has not been touched and still installs the old, G-Wolves-only
plugin if you never move. Since both the plugin id and the source repo
changed, this is neither an automatic update nor an in-place one:

```bash
omarchy plugin remove keasbeexd.hskmouse
omarchy plugin add https://github.com/keasbeexd/mouse-ctrl.git
omarchy plugin enable keasbeexd.mousectrl
~/.config/omarchy/plugins/keasbeexd.mousectrl/install.sh --udev
```

Re-add the widget under its new name in `shell.json` (or the bar settings UI)
and carry over any of the settings you had customized —
`refreshIntervalSec`, `batteryPollSec`, `lowBatteryPercent`, `showBatteryLabel`,
`hskctlPath` — onto the new entry; they were not renamed (`batteryPollSec` is
new in 2.0), only the `id` they hang off of.

`install.sh --udev` notices the old `/etc/udev/rules.d/60-gwolves-hsk.rules`
from a previous install and offers to remove it once the new rule is in place
(it is otherwise harmless left behind — the new rule covers the same vendor
id). Your G-Wolves mouse's own settings are unaffected either way: they live
on the mouse, not in the plugin.

For anything about G-Wolves, Pulsar, or multi-mouse support going forward,
file it here rather than on `omarchy-hsk`.

## Other models

The protocol for each vendor lives in `profiles/*.json` as data, interpreted
by a generic engine — there is no per-mouse code. G-Wolves' vendor app
matches 14 product IDs across the HSK range, so variants other than the Pro
4K very likely speak the same protocol already described in
`profiles/gwolves-hsk-pro-4k.json`. If you have one, `hskctl probe` and
`hskctl doctor` will tell you whether it's recognized, and adding a new
product id (or a whole new profile, for a different protocol) is a data
change, not a code change. Issues and PRs welcome — for other Pulsar models
and mice especially, since only the X2H mini's 4K dongle variant is confirmed
so far; see [Pulsar X2H mini](#pulsar-x2h-mini) above.

## How the protocol was recovered

Statically, from `G-Wolves_Software_V1.0.20.07` — a C++/CLI mixed-mode .NET
binary. The `hts_*` functions compile to managed CIL, so `hts_send_cmd` and the
`HTS_*_CMD` byte arrays were read directly out of the shipped binary rather than
inferred from USB captures.

```
hts_send_cmd(tx, rx):
    Sleep(60); hid_send_feature_report(h, tx, 65)
    Sleep(60); hid_get_feature_report(h, rx, 65)
    accept iff rx[1] == 0xA1
```

65-byte HID Feature reports, report id 0, no checksum. Byte 2 is the payload
length, byte 3 the opcode (**read opcode = write opcode + 0x80**), byte 4 the
link flag, byte 5 onwards the value.

Two things that are easy to get wrong, and cost real time here:

**An ACK is not success.** The firmware acknowledges a packet carrying the wrong
link flag and then ignores it, replying with an all-zero payload — identical to
a command it never received. And that link flag describes *the endpoint you
opened*, not where the mouse is: plug the cable in while the dongle is still
in and the two disagree, at which point every setting reads back 0. `hskctl`
establishes it by probing both flags on every candidate node and keeping the
one that answers with real data.

**The DPI block has a header that changes how it is parsed.** `rx[5]` is the
active stage and `rx[6]` is how many stages the firmware will take out of a
write. A mouse reporting 0 there silently discards every DPI value and every
colour, and a naive read-modify-write copies that 0 straight back, so it never
recovers. This plugin repairs it.

Polling rates were **measured**, by timing the mouse's own input reports, not
read off a plausible-looking table — and the answer is not a formula: raw 1–6
divide a 1000 Hz base, while 32 and 64 are separate high-rate codes for 2000 and
4000 Hz.

Full detail in [docs/PROTOCOL-DISCOVERY.md](docs/PROTOCOL-DISCOVERY.md).

The Pulsar profile above was recovered differently — starting from a
transcription of existing open-source tools rather than a capture of its
own, then corrected against a real installer's static analysis and finally
against the real device itself (see [Pulsar X2H mini](#pulsar-x2h-mini)
above for the full path). Credit and thanks to
[packerlschupfer/pulsar-mouse-linux](https://github.com/packerlschupfer/pulsar-mouse-linux)
and [andrewrabert/python-pulsar-mouse-tool](https://github.com/andrewrabert/python-pulsar-mouse-tool)
for the original reverse-engineering work that made the rest possible.

## Safety

Factory reset (opcode `09`) is deliberately not bound to any field — nothing
in the panel should be one keystroke from wiping your mouse's configuration.

Writes go read-modify-write, so changing one DPI stage cannot zero the others.
Settings live on the mouse itself and follow it between machines.

## Local development

```bash
git clone https://github.com/keasbeexd/mouse-ctrl.git
cd mouse-ctrl
./install.sh --udev      # permissions; replug afterwards
./install.sh --dev       # symlink into ~/.config/omarchy/plugins
omarchy plugin enable keasbeexd.mousectrl
```

The tree that ships to the marketplace holds only what a user needs at
runtime. Contributor tooling — the test suite, the vendor-binary decoder,
and the internal development notes — lives in this repo's git history
(inherited from `omarchy-hsk`, where it was stripped from the shipped tree
by the "Slim the shipped tree to what the marketplace needs" commit) rather
than in the installed tree, so `omarchy plugin add` does not copy 300+ KiB of
developer-only files onto every user's machine.

```
manifest.json  Panel.qml  Service.qml  Model.js   the plugin
install.sh                                        udev rule, self-contained
bin/hskctl                                        launcher for the bundled CLI
hskctl/          hidraw, protocol engine, device, CLI
profiles/        one JSON file per supported mouse -- data, not code
docs/            how the G-Wolves protocol was decoded, for anyone profiling a variant
```

## Contributing

Bug reports and profiles for other mouse models are both welcome -- G-Wolves
variants and other Pulsar models alike. Open an issue at
[keasbeexd/mouse-ctrl](https://github.com/keasbeexd/mouse-ctrl); for Pulsar
mice, a `hskctl probe --json` and `hskctl doctor` capture from real hardware
is the single most useful thing you can attach to it.

## Licence

MIT. See [LICENSE](LICENSE).

Not affiliated with, sponsored by, or endorsed by G-Wolves or Pulsar.
