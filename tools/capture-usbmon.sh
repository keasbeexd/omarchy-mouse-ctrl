#!/usr/bin/env bash
# Capture USB traffic to the mouse while the vendor app reconfigures it.
#
# Use this when you can run the G-Wolves Windows app under Wine on this
# machine: Wine passes HID through to the Linux kernel, so usbmon sees every
# config packet. If you are capturing on a real Windows box instead, use
# USBPcap + Wireshark and see docs/PROTOCOL-DISCOVERY.md.
#
#   sudo ./tools/capture-usbmon.sh start   # begins capture, prints the bus
#   ./tools/capture-usbmon.sh mark pollingRate=4000
#   ./tools/capture-usbmon.sh mark dpiStage1=800
#   sudo ./tools/capture-usbmon.sh stop
#   ./tools/decode-capture.py capture.json --labels capture.labels
#
# The mark command writes a timestamped label, and decode-capture.py uses those
# labels to work out which byte moved for which setting. Change exactly ONE
# setting between two marks or the diff cannot attribute the change.

set -euo pipefail

OUT_DIR="${HSK_CAPTURE_DIR:-$PWD}"
PCAP="$OUT_DIR/capture.pcapng"
JSON="$OUT_DIR/capture.json"
LABELS="$OUT_DIR/capture.labels"
PIDFILE="$OUT_DIR/.hsk-capture.pid"

die() { echo "error: $*" >&2; exit 1; }

need_root() {
  [[ $EUID -eq 0 ]] || die "this subcommand needs root (usbmon is privileged)"
}

ensure_usbmon() {
  if ! lsmod | grep -q '^usbmon'; then
    modprobe usbmon || die "could not load the usbmon module"
  fi
  [[ -d /sys/kernel/debug/usb/usbmon ]] || mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
  [[ -d /sys/kernel/debug/usb/usbmon ]] || die "usbmon is not available at /sys/kernel/debug/usb/usbmon"
}

find_mouse() {
  # Print "bus device vidpid description" for every plausible mouse or dongle.
  local line bus dev rest
  while read -r line; do
    [[ -z "$line" ]] && continue
    bus="${line:4:3}"
    dev="${line:15:3}"
    rest="${line#*: }"
    echo "$((10#$bus)) $((10#$dev)) $rest"
  done < <(lsusb | grep -iE 'g-wolves|gwolves|hsk|mouse|compx|receiver|dongle' || true)
}

cmd_devices() {
  echo "Candidate USB devices:"
  echo
  lsusb
  echo
  echo "Likely mouse/dongle:"
  find_mouse | while read -r bus dev rest; do
    printf "  bus %s device %s  %s\n" "$bus" "$dev" "$rest"
  done
  echo
  echo "hidraw nodes (this is what hskctl talks to):"
  python3 -m hskctl probe 2>/dev/null || echo "  (run from the repo root to use hskctl probe)"
}

cmd_start() {
  need_root
  ensure_usbmon
  command -v tshark >/dev/null || die "tshark not found -- install wireshark-cli"

  local bus="${1:-}"
  if [[ -z "$bus" ]]; then
    bus="$(find_mouse | head -1 | cut -d' ' -f1 || true)"
    [[ -n "$bus" ]] || die "could not guess the bus; run '$0 devices' and pass one: $0 start <bus>"
    echo "Guessed bus $bus. If that is wrong, stop and pass the right one."
  fi

  [[ -f "$PIDFILE" ]] && die "a capture is already running (pid $(cat "$PIDFILE")); run '$0 stop' first"

  : > "$LABELS"
  echo "Capturing bus $bus -> $PCAP"
  # -s 0 keeps whole packets; without it long config reports get truncated and
  # the trailing checksum byte -- the one we most need -- is lost.
  tshark -i "usbmon${bus}" -s 0 -w "$PCAP" >/dev/null 2>&1 &
  echo $! > "$PIDFILE"
  sleep 1
  kill -0 "$(cat "$PIDFILE")" 2>/dev/null || { rm -f "$PIDFILE"; die "tshark failed to start"; }

  cat <<EOF

Capture running (pid $(cat "$PIDFILE")).

Now, in the vendor app:
  1. Open it and let it read the mouse once.
  2. Change ONE setting.
  3. In another terminal, run:  $0 mark pollingRate=4000
  4. Repeat for each setting and each value you care about.

Use the exact field names from 'hskctl fields' on the left of the '=', so the
decoder can map them straight into the profile. When finished:

  sudo $0 stop

EOF
}

cmd_mark() {
  local label="${1:-}"
  [[ -n "$label" ]] || die "usage: $0 mark <field>=<value>"
  [[ -f "$PIDFILE" ]] || die "no capture is running"
  # Marks are written *after* you make the change in the app, so the label
  # applies backwards to the packets just captured. decode-capture.py applies
  # each label forward from its timestamp, so we record slightly in the past
  # to cover the packets the app just sent.
  local ts
  ts="$(python3 -c 'import time; print(f"{time.time() - 2.0:.6f}")')"
  echo "$ts $label" >> "$LABELS"
  echo "marked: $label"
}

cmd_stop() {
  need_root
  [[ -f "$PIDFILE" ]] || die "no capture is running"
  local pid
  pid="$(cat "$PIDFILE")"
  kill "$pid" 2>/dev/null || true
  sleep 1
  rm -f "$PIDFILE"

  echo "Converting to JSON..."
  tshark -r "$PCAP" -T json > "$JSON" 2>/dev/null
  chown "${SUDO_USER:-root}" "$PCAP" "$JSON" "$LABELS" 2>/dev/null || true

  echo
  echo "Wrote:"
  echo "  $PCAP"
  echo "  $JSON"
  echo "  $LABELS  ($(wc -l < "$LABELS") marks)"
  echo
  echo "Now analyse it:"
  echo "  ./tools/decode-capture.py '$JSON' --labels '$LABELS'"
  echo
  echo "And when it looks right, write a profile:"
  echo "  ./tools/decode-capture.py '$JSON' --labels '$LABELS' \\"
  echo "      --emit-profile profiles/gwolves-hsk-pro-4k.json"
}

case "${1:-}" in
  devices) shift; cmd_devices "$@" ;;
  start)   shift; cmd_start "$@" ;;
  mark)    shift; cmd_mark "$@" ;;
  stop)    shift; cmd_stop "$@" ;;
  *)
    cat <<EOF
usage: $0 <command>

  devices              list USB devices and hidraw nodes
  start [bus]          begin capturing (needs root)
  mark <field>=<value> label the moment you changed a setting
  stop                 end the capture and convert it for the decoder
EOF
    exit 1
    ;;
esac
