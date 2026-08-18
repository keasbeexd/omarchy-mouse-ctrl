#!/usr/bin/env bash
# Optional extras for the HSK Mouse plugin.
#
# You do NOT need this to use the plugin. `omarchy plugin add` clones the
# whole repo -- CLI included -- into ~/.config/omarchy/plugins/, and the widget
# runs the copy of hskctl bundled beside it.
#
# What this adds:
#   --udev       let your user write to the mouse without sudo   (recommended)
#   --autoapply  re-apply saved settings whenever the mouse reconnects
#   --link       put `hskctl` on your PATH for use in a terminal
#   --plugin     copy the plugin into place by hand, if you cloned it yourself
#   --uninstall  undo --link and --plugin
#
# With no arguments it does --udev and --link.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
PLUGIN_ID="io.github.keasbeexd.hsk"
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/$PLUGIN_ID"
UDEV_RULE="/etc/udev/rules.d/60-gwolves-hsk.rules"
VENDOR_ID="33e4"

info() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m warn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

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

install_udev() {
  # The vendor id is known from the driver, so this needs no device present.
  info "Granting your user access to hidraw devices with vendor id $VENDOR_ID"
  local tmp
  tmp="$(mktemp)"
  cp "$REPO_DIR/install/60-gwolves-hsk.rules" "$tmp"
  echo; cat "$tmp"; echo
  read -r -p "Write this to $UDEV_RULE (needs sudo)? [y/N] " reply
  if [[ "$reply" =~ ^[Yy] ]]; then
    sudo install -m 0644 "$tmp" "$UDEV_RULE"
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    info "Installed. Unplug and replug the mouse or its dongle."
  else
    info "Skipped -- hskctl will need sudo to change settings."
  fi
  rm -f "$tmp"
}

install_autoapply() {
  # The mouse has no save-to-flash command. The vendor's Windows app works
  # around this by storing settings in files and pushing them back when the
  # mouse reconnects; this is the same idea as a systemd user unit.
  local unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
  info "Installing the re-apply service to $unit_dir"
  mkdir -p "$unit_dir"
  install -m 0644 "$REPO_DIR/install/hskctl-apply.service" "$unit_dir/hskctl-apply.service"
  systemctl --user daemon-reload 2>/dev/null || warn "could not reload the user daemon"

  info "Saving your current settings as the ones to restore"
  "$REPO_DIR/bin/hskctl" save || warn "could not read the mouse -- run 'hskctl save' once it is connected"

  echo
  info "Re-apply is armed. Whenever the mouse reappears, systemd runs 'hskctl apply'."
  echo "     Change a setting, then run 'hskctl save' to make it the new baseline."
  echo "     Test it now with:  systemctl --user start hskctl-apply.service"
}

uninstall() {
  rm -f "$BIN_DIR/hskctl"
  rm -rf "$PLUGIN_DIR"
  rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/hskctl-apply.service"
  systemctl --user daemon-reload 2>/dev/null || true
  info "Removed the symlink and $PLUGIN_DIR"
  info "Left in place: $UDEV_RULE (remove with sudo if you want)"
}

case "${1:-}" in
  --udev)      install_udev ;;
  --autoapply) install_autoapply ;;
  --link)      link_cli ;;
  --plugin)    copy_plugin ;;
  --uninstall) uninstall ;;
  "")
    command -v python3 >/dev/null || die "python3 is required"
    install_udev
    link_cli
    install_autoapply
    echo
    info "Done. Check it sees the mouse:"
    echo "     hskctl probe"
    echo "     hskctl status     # compare against the Windows app before writing"
    ;;
  *) die "unknown option: $1" ;;
esac
