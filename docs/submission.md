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
background service, no libratbag. The CLI the widget drives is bundled inside
the plugin, so `omarchy plugin add` plus the udev rule is the whole install.

**Permissions.** It talks to `/dev/hidraw*` directly, and this is a hard
requirement rather than a convenience: every exchange is a HID feature report,
and the hidraw ioctls that carry those need the node opened read-write, so
without access the plugin cannot even read the battery. `install.sh --udev`
installs a rule scoped to G-Wolves' vendor id that grants access via `uaccess`
— the same mechanism used for sound cards and webcams — and it prints the exact
rule text before asking for `sudo`. The rule is a heredoc inside the script,
not a separate file, so it cannot go missing from a clone. When access is
absent the panel says so explicitly and shows the command to fix it, rather
than reporting a raw EACCES.

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

107 tests (67 Python, 40 JS) pin the decoded protocol, the view model, and the
repository's own packaging — `install.sh` parses and handles every flag it
documents, nothing references a file the tree does not ship, and the README's
links and commands resolve. Those last ones were added after an earlier
submission was rejected for a truncated `install.sh` in the pushed tree.
