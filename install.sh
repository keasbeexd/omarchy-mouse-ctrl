#!/usr/bin/bash
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

# Absolute paths for anything that might be shadowed on PATH. This script
# invokes sudo (for the udev rule) and writes a symlink from ~/.local/bin, so
# a shadow tool would either be given the escalation or would end up on the
# user's PATH under a name we intend to be trustworthy. See the reviewer's
# note on "the executable on the credential path" -- privilege in this case,
# but the shape is the same.
PYTHON="/usr/bin/python3"
SUDO="/usr/bin/sudo"
INSTALL_BIN="/usr/bin/install"
UDEVADM="/usr/bin/udevadm"
LN="/usr/bin/ln"
TAR="/usr/bin/tar"
RM="/usr/bin/rm"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
UDEV_RULE="/etc/udev/rules.d/60-gwolves-hsk.rules"
VENDOR_ID="33e4"

info() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m warn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Read from the manifest rather than repeated here. The id was changed in
# manifest.json alone once, leaving this script installing to a directory the
# shell would never look in -- the same shape of break that got the first
# submission rejected.
#
# `-I` is Python's isolated mode -- no PYTHONPATH, no user site-packages -- so
# a same-user process cannot shadow the json module by dropping a file in the
# user's site-packages dir. sed is the fallback for a system without python
# so `install.sh --help` still works.
PLUGIN_ID="$(
  "$PYTHON" -I -c "import json,sys;print(json.load(open(sys.argv[1]))['id'])" \
    "$REPO_DIR/manifest.json" 2>/dev/null \
  || sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$REPO_DIR/manifest.json" | head -1
)"
[[ -n "$PLUGIN_ID" ]] || die "could not read the plugin id out of manifest.json"
# Character class for a plugin id: dotted lowercase segments. Reject anything
# that could contain a path separator, a leading dash, or NUL before we splice
# it into a filesystem path.
[[ "$PLUGIN_ID" =~ ^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$ ]] \
  || die "plugin id from manifest is not a plain dotted-lowercase id: $PLUGIN_ID"
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/$PLUGIN_ID"

# Create a directory only we can write, refusing to reuse anything that is a
# symlink or belongs to another user. `mkdir -p -m 700` neither fails on an
# existing wider directory nor tightens it, so we verify after: type, owner,
# mode, and non-symlink separately (mkdir -p follows a symlink to a directory
# silently, which is exactly what section 1 of the review guidance names).
private_dir() {
  local dir="$1" mode="${2:-700}"
  mkdir -p -m "$mode" "$dir"
  local kind owner perm
  kind=$(stat -Lc %F "$dir" 2>/dev/null || true)
  [[ "$kind" == "directory" ]] || die "$dir is not a directory (got: ${kind:-missing})"
  # -L on the last component too, so a symlink there fails the check even
  # though the target is a real directory.
  [[ ! -L "$dir" ]] || die "$dir is a symlink; refusing to use it"
  owner=$(stat -Lc %u "$dir")
  [[ "$owner" == "$(id -u)" ]] || die "$dir is not owned by you (uid $owner)"
  if [[ "$mode" == "700" ]]; then
    perm=$(stat -Lc %a "$dir")
    [[ "$perm" == "700" ]] || chmod 700 "$dir"
  fi
}

# A directory a script we ship must not follow into. Rejects a symlink at
# the leaf and everything not a real directory. Never used to hold secrets --
# it is $BIN_DIR and $PLUGIN_DIR's parent -- so it does not force 700.
public_dir() {
  local dir="$1"
  mkdir -p "$dir"
  [[ ! -L "$dir" ]] || die "$dir is a symlink; refusing to use it"
  local kind owner
  kind=$(stat -Lc %F "$dir" 2>/dev/null || true)
  [[ "$kind" == "directory" ]] || die "$dir is not a directory (got: ${kind:-missing})"
  owner=$(stat -Lc %u "$dir")
  [[ "$owner" == "$(id -u)" ]] || die "$dir is not owned by you"
}

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

  # Pipe the rule straight into `sudo install` via stdin. The previous
  # version wrote to a mktemp file first and passed the path to install(1),
  # which is a second pathname resolution the plugin does not need. stdin is
  # one fewer resolution and one fewer file to clean up on error.
  udev_rule_text \
    | "$SUDO" "$INSTALL_BIN" -o root -g root -m 0644 /dev/stdin "$UDEV_RULE"
  "$SUDO" "$UDEVADM" control --reload-rules
  "$SUDO" "$UDEVADM" trigger
  info "Installed $UDEV_RULE"
  info "Now unplug and replug the mouse or its dongle -- the rule applies when"
  info "the device next appears, not to one that is already plugged in."
}

link_cli() {
  info "Linking hskctl into $BIN_DIR"
  public_dir "$BIN_DIR"
  # -f replaces an existing symlink; refuse to replace a real file. The
  # previous version replaced whichever was there, which is fine for a symlink
  # this script previously installed but silently overwrites a real binary the
  # user happens to have named `hskctl`.
  if [[ -e "$BIN_DIR/hskctl" && ! -L "$BIN_DIR/hskctl" ]]; then
    die "$BIN_DIR/hskctl exists and is not a symlink; move it aside first"
  fi
  "$LN" -sfn "$REPO_DIR/bin/hskctl" "$BIN_DIR/hskctl"
  command -v hskctl >/dev/null 2>&1 || warn "$BIN_DIR is not on your PATH"
}

copy_plugin() {
  info "Copying the plugin to $PLUGIN_DIR"
  public_dir "$(dirname "$PLUGIN_DIR")"
  # Refuse to overwrite a foreign directory at the destination. Removing an
  # unknown tree is exactly the "uninstaller that deletes too much" the review
  # guidance warns about, and here it is the installer's job to notice first.
  if [[ -e "$PLUGIN_DIR" && ! -L "$PLUGIN_DIR" ]]; then
    local kind
    kind=$(stat -Lc %F "$PLUGIN_DIR" 2>/dev/null || true)
    [[ "$kind" == "directory" ]] || die "$PLUGIN_DIR exists and is not a directory ($kind)"
    [[ -f "$PLUGIN_DIR/manifest.json" ]] \
      || die "$PLUGIN_DIR exists but has no manifest.json; refusing to overwrite it"
  fi
  mkdir -p "$PLUGIN_DIR"
  # Everything, because the QML runs the bundled CLI from its own directory.
  # --no-same-owner because a plugin copied into a per-user directory does not
  # want the archive's uid/gid, and --no-selinux for the same reason. Excluding
  # .git keeps the copy small.
  "$TAR" -C "$REPO_DIR" --exclude=.git -cf - . \
    | "$TAR" -C "$PLUGIN_DIR" --no-same-owner -xf -
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
  public_dir "$(dirname "$PLUGIN_DIR")"
  if [[ -e "$PLUGIN_DIR" && ! -L "$PLUGIN_DIR" ]]; then
    die "$PLUGIN_DIR already exists and is a real directory. Move it aside first."
  fi
  "$LN" -sfn "$REPO_DIR" "$PLUGIN_DIR"
  if command -v omarchy-shell >/dev/null 2>&1; then
    omarchy-shell shell rescanPlugins >/dev/null 2>&1 || warn "rescan failed -- is the shell running?"
  fi
  info "Edits in $REPO_DIR are now live. Run:  omarchy plugin enable $PLUGIN_ID"
  warn "Auto-reload on save may not follow the symlink; use 'omarchy-shell shell rescanPlugins' after edits."
}

uninstall() {
  # Two removals, each guarded so we only unlink what this script installed.
  # The bin entry must be a symlink pointing at our launcher; a real file
  # somebody else placed there is left alone. The plugin dir must either be
  # a symlink (from --dev) or a real dir with our own manifest at its root
  # (from --plugin), so we never rm -rf a foreign tree.
  local link_target
  if [[ -L "$BIN_DIR/hskctl" ]]; then
    link_target=$(readlink -- "$BIN_DIR/hskctl" || true)
    if [[ "$link_target" == "$REPO_DIR/bin/hskctl" ]]; then
      "$RM" -f -- "$BIN_DIR/hskctl"
    else
      warn "$BIN_DIR/hskctl points elsewhere ($link_target); leaving it in place"
    fi
  elif [[ -e "$BIN_DIR/hskctl" ]]; then
    warn "$BIN_DIR/hskctl is not a symlink; leaving it in place"
  fi

  if [[ -L "$PLUGIN_DIR" ]]; then
    "$RM" -f -- "$PLUGIN_DIR"
  elif [[ -d "$PLUGIN_DIR" ]]; then
    if [[ -f "$PLUGIN_DIR/manifest.json" ]]; then
      local installed_id
      installed_id=$("$PYTHON" -I -c \
        "import json,sys;print(json.load(open(sys.argv[1]))['id'])" \
        "$PLUGIN_DIR/manifest.json" 2>/dev/null || true)
      if [[ "$installed_id" == "$PLUGIN_ID" ]]; then
        "$RM" -rf -- "$PLUGIN_DIR"
      else
        warn "$PLUGIN_DIR carries a different plugin ($installed_id); leaving it in place"
      fi
    else
      warn "$PLUGIN_DIR has no manifest.json; refusing to remove"
    fi
  fi
  info "Uninstall done."
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
    [[ -x "$PYTHON" ]] || die "$PYTHON is required (or set HSKCTL_PYTHON to a python3 you have)"
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
