# Security policy

## Reporting a vulnerability

Please report privately, not in a public issue or pull request:

- Use GitHub's **Report a vulnerability** button on the Security tab of
  this repository (private vulnerability reporting):
  https://github.com/keasbeexd/omarchy-mouse-ctrl/security/advisories/new

Include what you found, how to reproduce it, and which commit you tested. A
proof of concept helps; please test only on your own machine.

You can expect an acknowledgement within a week. Fixes for confirmed issues
are released as soon as they are ready, with an advisory that credits you
unless you ask not to be named.

## What counts

Mouse CTRL is an Omarchy shell plugin that talks to your mouse over raw HID
and, with `install.sh --udev`, installs a udev rule as root. Examples of what
we want to hear about:

- a way for the plugin, `hskctl` or `install.sh` to run something as root
  that it did not show you and ask about first, or to change anything as
  root beyond its own udev rule (and any outdated udev rule it offers to
  remove);
- a udev rule that opens a device (or more devices) to more than the
  logged-in user;
- data from the mouse, a profile file or another program that reaches a
  shell, a command's arguments, the panel or a terminal as anything but plain
  text;
- a way to send a mouse a command that could brick it or change its
  firmware, rather than the settings the panel offers.

## Supported versions

Only the latest commit on the default branch is supported: Omarchy installs
plugins from it and `omarchy-plugin-update` moves to its newest commit.
