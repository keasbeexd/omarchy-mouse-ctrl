### Repository URL

https://github.com/keasbeexd/omarchy-hsk

### Category

Hardware

### Tags

Bar, Quickshell, System

### Maintainer notes

**What it is.** A bar widget and panel for configuring a G-Wolves HSK Pro 4K
mouse — battery, seven DPI stages with per-stage LED colours, polling rate up to
4000 Hz, motion sync, angle snapping and lift-off distance.

**Why it exists.** The HSK Pro 4K has no libratbag support, and G-Wolves' web
configurator does not cover this model — it ships a Windows-only application. So
there was no way to change DPI or polling rate from Linux at all. The protocol
was recovered by static analysis of the vendor's Windows binary (reading the CIL
of `hts_send_cmd` and the `HTS_*_CMD` byte arrays), then confirmed setting by
setting against real hardware.

**Dependencies.** Python 3.9+ and nothing else. No pip packages, no daemon, no
libratbag. The CLI the widget drives is bundled inside the plugin, so
`omarchy plugin add` is sufficient to install it.

**Permissions.** It talks to `/dev/hidraw*` directly. A udev rule
(`install.sh --udev`, shipped in the repo) grants the logged-in user access via
`uaccess` so nothing needs `sudo`. Without the rule the widget still reads and
displays, but writes fail — the panel reports that rather than failing silently.

**Safety.** Only the mouse's own configuration registers are written. The
firmware's factory-reset opcode is deliberately not bound to any control, and a
unit test enforces that it stays unreachable. DPI writes are read-modify-write,
so changing one stage cannot zero the others. Removing the plugin leaves the
mouse exactly as configured; settings live in the mouse's own storage.

**Configuration.** Four settings, all with defaults, exposed through the standard
`barWidget.schema`: refresh interval, low-battery threshold, whether to show the
battery percentage in the bar, and an optional override path for the CLI.
Existing configuration is preserved across updates.

**Other hardware.** The device protocol lives in `profiles/*.json` as data,
interpreted by a generic engine — there is no device-specific code. The vendor
application matches 14 product IDs across the HSK range, so other variants very
likely work; adding one is a profile, not a patch.

96 tests (58 Python, 38 JS) pin the decoded protocol and the view model.
