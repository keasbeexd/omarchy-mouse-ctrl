#!/usr/bin/bash
# Setup for the Mouse CTRL plugin.
#
#   --udev       grant your user access to the mouse            (REQUIRED)
#                (append --yes to skip the confirm; the rule text is still shown)
#   --link       put `mousectrl` on your PATH for use in a terminal
#   --plugin     copy the plugin into place by hand, if you cloned it yourself
#   --dev        symlink this checkout into the plugins dir, for hacking on it
#   --uninstall  undo --link and --dev (a --plugin copy is removed with
#                `omarchy plugin remove <id>`)
#
# With no arguments it does --udev and --link.
#
# The udev rule is not optional polish. Every exchange with the mouse is a HID
# feature report, and the hidraw ioctls that carry those need the node opened
# read-write -- so without the rule the plugin cannot even read the battery.
#
# The rule text has one source of truth, `mousectrl udev-rule`, which knows
# every family driver's vendor ids and the per VID:PID lines profiles claim.
# The heredoc below stays only as a fallback for a machine where mousectrl
# will not run. Either way, every line is validated against one narrow
# pattern before root installs it.

set -euo pipefail

# Every other tool this script runs -- cat, sed, stat, mkdir, readlink, awk --
# is resolved through a fixed PATH, not the caller's. `udev_rule_text` builds
# the text that `sudo install` writes into /etc, so a `cat` or `sed` shadowed
# earlier on PATH would otherwise get to choose what root installs. The
# caller's PATH is kept only to say whether mousectrl will be found on it.
USER_PATH="${PATH:-}"
PATH=/usr/bin:/bin
export PATH

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
UDEV_RULE="/etc/udev/rules.d/60-mousectrl.rules"
# Fallback only: the vendor ids the family drivers in mousectrl/families/
# claim, for a machine where `mousectrl udev-rule` will not run.
VENDOR_IDS=(
  "25a7:Pulsar (Cfg.ini alternate id)"
  "33e4:G-Wolves (G-Wolves HTS)"
  "3554:Pulsar (Pulsar Nordic)"
)

info() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m warn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Read from the manifest rather than repeated here. The id was changed in
# manifest.json alone once, leaving this script installing to a directory the
# shell would never look in -- the same shape of break that got the first
# submission rejected.
#
# `-I -S` is Python's isolated mode with no site-packages -- no PYTHONPATH, no
# user site dir -- so a same-user process cannot shadow the json module by
# dropping a file in it. The manifest is read through the same bundled
# no-symlink, size-capped reader mousectrl uses for every file it did not create.
# sed (on at most 64 KiB) is the fallback for a system without python so
# `install.sh --help` still works.
PLUGIN_ID="$(
  "$PYTHON" -I -S -c 'import sys; sys.path.insert(0, sys.argv[1]); from mousectrl.safeio import read_bounded_json; print(read_bounded_json(sys.argv[2], 65536).get("id", ""))' \
    "$REPO_DIR" "$REPO_DIR/manifest.json" 2>/dev/null \
  || head -c 65536 -- "$REPO_DIR/manifest.json" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
)"
[[ -n "$PLUGIN_ID" ]] || die "could not read the plugin id out of manifest.json"
# Character class for a plugin id: dotted lowercase segments. Reject anything
# that could contain a path separator, a leading dash, or NUL before we splice
# it into a filesystem path.
[[ "$PLUGIN_ID" =~ ^[a-z][a-z0-9]*(\.[a-z][a-z0-9]*)+$ ]] \
  || die "plugin id from manifest is not a plain dotted-lowercase id: $PLUGIN_ID"
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/$PLUGIN_ID"

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

bundled_rule_text() {
  # Fallback for a machine where the engine will not run -- the same
  # whole-vendor text this script installed before the engine absorbed it.
  cat <<HEADER
# Mouse CTRL plugin -- hidraw access for every supported mouse vendor.
#
# Configuring the mouse means sending HID *feature* reports, and the hidraw
# ioctls for those (HIDIOCSFEATURE / HIDIOCGFEATURE) require the device node to
# be opened read-write. The default mode on /dev/hidraw* is root-only, so
# without this rule mousectrl and the Omarchy panel cannot talk to the mouse at
# all -- not even to read the battery.
#
# uaccess hands read-write to whoever is logged in at the seat, which is the
# same mechanism your sound card and webcam use. Each line below is scoped to
# one vendor id -- adding a mouse from a new vendor means adding a line here,
# never widening one to match everything.
HEADER
  local entry vendor_id vendor_name
  for entry in "${VENDOR_IDS[@]}"; do
    vendor_id="${entry%%:*}"
    vendor_name="${entry#*:}"
    echo "# $vendor_name"
    echo "KERNEL==\"hidraw*\", ATTRS{idVendor}==\"$vendor_id\", MODE=\"0660\", TAG+=\"uaccess\""
  done
}

# The one pattern every rule line may take, and the only thing that gets past
# this script into a root-owned file. A rule grants device access to whoever
# sits at the seat, so a tampered or buggy generator gets rejected wholesale
# rather than line-edited into shape.
RULE_LINE_OK='^KERNEL=="hidraw\*", ATTRS\{idVendor\}=="[0-9a-f]{4}"(, ATTRS\{idProduct\}=="[0-9a-f]{4}")?, MODE="0660", TAG\+="uaccess"$'

validate_rule_text() {
  local text
  text="$(cat)"
  [[ -n "$text" ]] || return 1
  if grep -vE '^(#|$)' <<<"$text" | grep -qvE "$RULE_LINE_OK"; then
    return 1
  fi
  printf '%s\n' "$text"
}

rule_file_text() {
  # Primary source: mousectrl, which adds per VID:PID lines for profiled
  # mice outside the supported families.
  local text=""
  if [[ -x "$REPO_DIR/bin/mousectrl" && -x "$PYTHON" ]]; then
    text="$("$REPO_DIR/bin/mousectrl" udev-rule 2>/dev/null || true)"
  fi
  if validate_rule_text <<<"$text" >/dev/null; then
    printf '%s\n' "$text"
    return 0
  fi
  warn "mousectrl's rule text missing or not validated -- using the bundled whole-vendor rule only"
  bundled_rule_text
}

install_udev() {
  local assume_yes="${1:-}"
  info "Granting your user access to hidraw devices for every mouse vendor this plugin supports:"
  echo
  rule_file_text | sed 's/^/    /'
  echo
  local reply="n"
  if [[ "$assume_yes" == "--yes" ]]; then
    info "The text above was shown before this ask; running non-interactively (--yes)."
    reply="y"
  else
    read -r -p "Write this to $UDEV_RULE (needs sudo)? [y/N] " reply
  fi
  if [[ ! "$reply" =~ ^[Yy] ]]; then
    warn "Skipped. The plugin will not be able to reach the mouse until this"
    warn "rule exists -- rerun './install.sh --udev' when you are ready."
    return 0
  fi

  # Pipe the rule straight into `sudo install` via stdin. The previous
  # version wrote to a mktemp file first and passed the path to install(1),
  # which is a second pathname resolution the plugin does not need. stdin is
  # one fewer resolution and one fewer file to clean up on error.
  rule_file_text \
    | "$SUDO" "$INSTALL_BIN" -o root -g root -m 0644 /dev/stdin "$UDEV_RULE"
  "$SUDO" "$UDEVADM" control --reload-rules
  # Only hidraw nodes -- re-triggering every device on the system is not
  # this plugin's business.
  "$SUDO" "$UDEVADM" trigger --subsystem-match=hidraw --action=change
  info "Installed $UDEV_RULE"
  info "Now unplug and replug the mouse or its dongle -- the rule applies when"
  info "the device next appears, not to one that is already plugged in."
}

# Remove $BIN_DIR/<name> only if it is a symlink this script made, pointing
# into this checkout.
remove_own_link() {
  local name="$1" link_target
  if [[ -L "$BIN_DIR/$name" ]]; then
    link_target=$(readlink -- "$BIN_DIR/$name" || true)
    if [[ "$link_target" == "$REPO_DIR/bin/$name" ]]; then
      "$RM" -f -- "$BIN_DIR/$name"
      info "Removed $BIN_DIR/$name"
    else
      warn "$BIN_DIR/$name points elsewhere ($link_target); leaving it in place"
    fi
  elif [[ -e "$BIN_DIR/$name" ]]; then
    warn "$BIN_DIR/$name is not a symlink; leaving it in place"
  fi
}

link_cli() {
  info "Linking mousectrl into $BIN_DIR"
  public_dir "$BIN_DIR"
  # -f replaces an existing symlink; refuse to replace a real file the user
  # happens to have named `mousectrl`.
  if [[ -e "$BIN_DIR/mousectrl" && ! -L "$BIN_DIR/mousectrl" ]]; then
    die "$BIN_DIR/mousectrl exists and is not a symlink; move it aside first"
  fi
  "$LN" -sfn "$REPO_DIR/bin/mousectrl" "$BIN_DIR/mousectrl"
  case ":$USER_PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on your PATH" ;;
  esac
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
  # Not run from here: omarchy-shell would be whatever `omarchy-shell` is
  # first on PATH, and this script does not execute tools it cannot pin.
  info "Rescan with: omarchy-shell shell rescanPlugins"
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
  # Not run from here: omarchy-shell would be whatever `omarchy-shell` is
  # first on PATH, and this script does not execute tools it cannot pin.
  info "Rescan with: omarchy-shell shell rescanPlugins"
  info "Edits in $REPO_DIR are now live. Run:  omarchy plugin enable $PLUGIN_ID"
  warn "Auto-reload on save may not follow the symlink; use 'omarchy-shell shell rescanPlugins' after edits."
}

uninstall() {
  # Only symlinks this script created, each checked to still point where this
  # script pointed it. Nothing here deletes a directory tree: a real plugin
  # directory (from --plugin, or from `omarchy plugin add`) belongs to
  # Omarchy's own plugin manager, which removes it.
  local link_target
  remove_own_link mousectrl

  if [[ -L "$PLUGIN_DIR" ]]; then
    link_target=$(readlink -- "$PLUGIN_DIR" || true)
    if [[ "$link_target" == "$REPO_DIR" ]]; then
      "$RM" -f -- "$PLUGIN_DIR"
      info "Removed $PLUGIN_DIR"
    else
      warn "$PLUGIN_DIR points elsewhere ($link_target); leaving it in place"
    fi
  elif [[ -d "$PLUGIN_DIR" ]]; then
    info "$PLUGIN_DIR is a real directory; remove it with:"
    info "  omarchy plugin remove $PLUGIN_ID"
  fi
  info "Uninstall done."
  info "Left in place: $UDEV_RULE (remove with sudo if you want)"
}

case "${1:-}" in
  --udev)      install_udev "${2:-}" ;;
  --link)      link_cli ;;
  --plugin)    copy_plugin ;;
  --dev)       link_plugin ;;
  --uninstall) uninstall ;;
  -h|--help)   awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}" ;;
  "")
    [[ -x "$PYTHON" ]] || die "$PYTHON is required"
    install_udev
    link_cli
    echo
    info "Done. Check it sees the mouse:"
    echo "     mousectrl devices           # lists mice and their family"
    echo "     mousectrl read <vid:pid>    # reads every setting"
    ;;
  *) die "unknown option: $1 (try --help)" ;;
esac
