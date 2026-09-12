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
| G-Wolves | HSK Pro 4K | Confirmed on hardware — battery, DPI (7 stages + colour), polling rate, sensor settings, sleep timer |
| Pulsar | X2H mini | Reads and writes confirmed working, cabled — battery, firmware, polling rate, sensor settings, DPI stage index (not DPI values yet). Dongle-only operation unconfirmed. See [Pulsar X2H mini](#pulsar-x2h-mini) below |

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

This is the newest addition, and its profile (`profiles/pulsar-x2h-mini.json`)
has **reads and writes both confirmed working, cabled**, on a real X2H mini.
It started as a transcription of two open-source Linux tools that speak to
Pulsar's Nordic wireless dongle —
[packerlschupfer/pulsar-mouse-linux](https://github.com/packerlschupfer/pulsar-mouse-linux)
and [andrewrabert/python-pulsar-mouse-tool](https://github.com/andrewrabert/python-pulsar-mouse-tool)
— and real-hardware testing plus cloning that source directly (rather than
relying on a summary of it) corrected several wrong guesses along the way.
Current state:

- **Detection, reads and writes are all confirmed working, cabled.** The
  mouse enumerates as `3554:f507`; its config endpoint answers real data
  over a raw USB **Output** report at report id `8`, read back over the
  interrupt endpoint — not the Feature-report transport first guessed from
  the HID descriptor's feature report id (`6`), which turned out to belong
  to a different report entirely. `hskctl status` returns real values,
  including `debounceMs: 3` — an exact match to the real Pulsar Fusion
  installer's own factory-default config for this model — and
  owner-confirmed `dpiStageCount: 1` and `sleepSeconds: 30`.
- **Writes needed one more fix beyond the transport**: this protocol
  checksums every scalar value on its own, separately from the usual
  whole-packet checksum — the value byte, then `0x55 - value` immediately
  after it. That's now a generic `valueChecksum` option in `protocol.py`
  (not Pulsar-specific code), and it round-tripped correctly on real
  hardware (`hskctl set motionSync off` → reads back `false`, `on` → reads
  back `true`). Every scalar field is writable now except `dpiStageCount`,
  kept read-only on purpose — writing DPI stage count wrong is exactly the
  class of bug that broke DPI entirely on the G-Wolves profile before it
  was repaired.
- The RF receiver enumerates separately as `3554:f509` ("Pulsar 4K
  Wireless Receiver"); its own config endpoint isn't positively identified
  yet, so **everything above is confirmed cabled only — dongle-only
  operation is still unverified.**
- DPI stage values and LED colour are not mapped at all yet: they're
  3-4 byte records with their own per-record checksum, which
  `valueChecksum` doesn't generalize to yet (it only handles a single
  value byte so far). Cfg.ini (extracted from the real installer) confirms
  this model ships 4 DPI stages by default (400/800/1600/3200), not 7 like
  the G-Wolves profile.

If you own an X2H mini, `hskctl probe --json` with the dongle connected is
the most useful thing left to check — see the `_followUp` list at the bottom
of the profile's JSON for the concrete next steps, and open an issue with
what it reports.

## Using it

**Bar widget:** left click opens the panel, right click cycles DPI stage,
middle click refreshes.

**In the panel**, each DPI stage is a row: a selector for the active stage,
`−`/`+` buttons that step by 50 DPI (hold to repeat), and a swatch that cycles
the stage's LED colour.

Rapid input is coalesced — hold `+` from 400 to 3200 and the mouse gets one
write, when you stop — and the panel says *Writing to the mouse…* whenever an
exchange is in flight, because a write is a real USB round trip and silence
reads as a dead click.

The version of the plugin you are running is in the bottom-right of the panel;
hover it for the firmware version and which hidraw node is in use. `hskctl
--version` reports the same number.

**Keyboard:** `↑`/`↓` between rows, `←`/`→` to adjust the row under the cursor,
`Enter` to select a stage, `c` to cycle its colour, `1`–`7` to switch straight
to a stage, `d` to cycle DPI, `m` for motion sync, `r` to refresh.

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
omarchy-shell keasbeexd.mousectrl cycleDpi
omarchy-shell keasbeexd.mousectrl setPollingRate 1000
```

## Settings

| Setting | Key | Default | |
|---|---|---|---|
| Refresh interval | `refreshIntervalSec` | 30 | how often the bar re-reads the mouse |
| Low battery warning | `lowBatteryPercent` | 15 | when the icon turns urgent |
| Show battery percentage | `showBatteryLabel` | on | the `94%` text beside the icon |
| Path to `hskctl` | `hskctlPath` | *(bundled)* | override only if you installed it yourself |

There is no settings UI. Omarchy keeps every widget's options **inline on that
widget's entry** in `~/.config/omarchy/shell.json`, under the `bar` key, in
whichever of the three layout arrays the widget sits in:

```json
{
  "bar": {
    "right": [
      { "id": "keasbeexd.mousectrl", "showBatteryLabel": true, "refreshIntervalSec": 30 }
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

While the mouse has not been read successfully yet — right after login,
before its dongle has finished enumerating — the bar retries every few
seconds rather than waiting a full `refreshIntervalSec`, so a slow-to-wake
dongle catches up on its own within a handful of seconds instead of showing a
stale reading until you open the panel (which also forces an immediate
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
`refreshIntervalSec`, `lowBatteryPercent`, `showBatteryLabel`, `hskctlPath` —
onto the new entry; they were not renamed, only the `id` they hang off of.

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
change, not a code change. Issues and PRs welcome — for Pulsar mice
especially, since [Pulsar X2H mini](#pulsar-x2h-mini) above still needs its
write path finished and dongle-only operation confirmed.

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

The Pulsar profile above was not recovered the same way — it has no capture
or decompile behind it yet, only a transcription of existing open-source
tools. Credit and thanks to
[packerlschupfer/pulsar-mouse-linux](https://github.com/packerlschupfer/pulsar-mouse-linux)
and [andrewrabert/python-pulsar-mouse-tool](https://github.com/andrewrabert/python-pulsar-mouse-tool)
for doing that original reverse-engineering work.

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
