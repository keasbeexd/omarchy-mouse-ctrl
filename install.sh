#!/usr/bin/env bash
# Setup for the HSK Mouse plugin.
#
#   --udev       grant your user access to the mouse            (REQUIRED)
#   --link       put `hskctl` on your PATH for use in a terminal
#   --plugin     copy the plugin into place by hand, if you cloned it yourself
#   --dev        symlink this checkout into the plugins dir, for hacking on it
#   --uninstall  undo --link and --plugin
#
# With no arguments it does --udev and --link.
#
# The udev rule is not optional polish. Every exchange with the mouse is a HID
# feature report, and the hidraw ioctls that carry those need the node opened
# read-write -- so without the rule the plugin cannot even read the battery.
#
# This script is deliberately self-contained: the rule is a heredoc below
# rather than a separate file, because a plugin is distributed by cloning a
# repository and a file that must be present is a file that can go missing.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
# Read from the manifest rather than repeated here. The id was changed in
# manifest.json alone once, leaving this script installing to a directory the
# shell would never look in -- the same shape of break that got the first
# submission rejected.
PLUGIN_ID="$(
  python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['id'])" \
    "$REPO_DIR/manifest.json" 2>/dev/null \
  || sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$REPO_DIR/manifest.json" | head -1
)"
[[ -n "$PLUGIN_ID" ]] || die "could not read the plugin id out of manifest.json"
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/$PLUGIN_ID"
UDEV_RULE="/etc/udev/rules.d/60-gwolves-hsk.rules"
VENDOR_ID="33e4"

info() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m warn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

udev_rule_text() {
  cat <<RULE
# G-Wolves HSK (vendor id $VENDOR_ID) -- installed by the omarchy-hsk plugin.
#
# Configuring the mouse means sending HID *feature* reports, and the hidraw
# ioctls for those (HIDIOCSFEATURE / HIDIOCGFEATURE) require the device node to
# be opened read-write. The default mode on /dev/hidraw* is root-only, so
# without this rule hskctl and the Omarchy panel cannot talk to the mouse at
# all -- not even to read the battery.
#
# uaccess hands read-write to whoever is logged in at the seat, which is the
# same mechanism your sound card and webcam use. It is scoped to this vendor id.
KERNEL=="hidraw*", ATTRS{idVendor}=="$VENDOR_ID", MODE="0660", TAG+="uaccess"
RULE
}

install_udev() {
  info "Granting your user access to hidraw devices with vendor id $VENDOR_ID"
  echo
  udev_rule_text | sed 's/^/    /'
  echo
  read -r -p "Write this to $UDEV_RULE (needs sudo)? [y/N] " reply
  if [[ ! "$reply" =~ ^[Yy] ]]; then
    warn "Skipped. The plugin will not be able to reach the mouse until this"
    warn "rule exists -- rerun './install.sh --udev' when you are ready."
    return 0
  fi

  local tmp
  tmp="$(mktemp)"
  udev_rule_text >"$tmp"
  sudo install -m 0644 "$tmp" "$UDEV_RULE"
  rm -f "$tmp"
  sudo udevadm control --reload-rules
  sudo udevadm trigger
  info "Installed $UDEV_RULE"
  info "Now unplug and replug the mouse or its dongle -- the rule applies when"
  info "the device next appears, not to one that is already plugged in."
}

link_cli() {
  info "Linking hskctl into $BIN_DIR"
  mkdir -p "$BIN_DIR"
  ln -sf "$REPO_DIR/bin/hskctl" "$BIN_DIR/hskctl"
  command -v hskctl >/dev/null 2>&1 || warn "$BIN_DIR is not on your PATH"
}

copy_plugin() {
  info "Copying the plugin to $PLUGIN_DIR"
  mkdir -p "$PLUGIN_DIR"
  # Everything, because the QML runs the bundled CLI from its own directory.
  tar -C "$REPO_DIR" --exclude=.git -cf - . | tar -C "$PLUGIN_DIR" -xf -
  if command -v omarchy-shell >/dev/null 2>&1; then
    omarchy-shell shell rescanPlugins >/dev/null 2>&1 || warn "rescan failed -- is the shell running?"
  fi
  info "Now run: omarchy plugin enable $PLUGIN_ID"
}

link_plugin() {
  # Omarchy discovers third-party plugins with a glob -- `for sub in "$dir"/*/`
  # plus a `[[ -f "$sub/manifest.json" ]]` test -- and both follow symlinks, so
  # the plugin directory can be a link to a working checkout. (The first-party
  # scan uses `find` without -L and would not, but that path is not used here.)
  info "Linking $REPO_DIR into $PLUGIN_DIR"
  mkdir -p "$(dirname "$PLUGIN_DIR")"
  if [[ -e "$PLUGIN_DIR" && ! -L "$PLUGIN_DIR" ]]; then
    die "$PLUGIN_DIR already exists and is a real directory. Move it aside first."
  fi
  ln -sfn "$REPO_DIR" "$PLUGIN_DIR"
  if command -v omarchy-shell >/dev/null 2>&1; then
    omarchy-shell shell rescanPlugins >/dev/null 2>&1 || warn "rescan failed -- is the shell running?"
  fi
  info "Edits in $REPO_DIR are now live. Run:  omarchy plugin enable $PLUGIN_ID"
  warn "Auto-reload on save may not follow the symlink; use 'omarchy-shell shell rescanPlugins' after edits."
}

uninstall() {
  rm -f "$BIN_DIR/hskctl"
  rm -rf "$PLUGIN_DIR"
  info "Removed the symlink and $PLUGIN_DIR"
  info "Left in place: $UDEV_RULE (remove with sudo if you want)"
}

case "${1:-}" in
  --udev)      install_udev ;;
  --link)      link_cli ;;
  --plugin)    copy_plugin ;;
  --dev)       link_plugin ;;
  --uninstall) uninstall ;;
  -h|--help)   awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}" ;;
  "")
    command -v python3 >/dev/null || die "python3 is required"
    install_udev
    link_cli
    echo
    info "Done. Check it sees the mouse:"
    echo "     hskctl probe      # finds the config endpoint"
    echo "     hskctl status     # reads every setting"
    echo "     hskctl doctor     # if either of those looks wrong"
    ;;
  *) die "unknown option: $1 (try --help)" ;;
esac
