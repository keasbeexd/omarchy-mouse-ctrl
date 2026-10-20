"""What can be learned about any mouse without knowing its protocol.

The read-only view and the report for mice no family driver claims: the
button count its descriptor declares, a battery the kernel already exposes,
a polling rate measured from its own input reports, and a plain-text report
for whoever writes its driver.
"""

from __future__ import annotations

import os
import time

from .errors import NoAccess, NoResponse
from .fingerprint import BUS_BLUETOOTH, BUS_USB, SYSFS_HIDRAW, Fingerprint
from .hidraw import HidrawDevice

MEASURE_SECONDS = 2.5
_MIN_REPORTS = 20
_MAX_READ = 64


def connection(fp: Fingerprint) -> str:
    return {BUS_USB: "usb", BUS_BLUETOOTH: "bluetooth"}.get(fp.bus, f"bus {fp.bus:#06x}")


def _read_small(path: str, limit: int = 64) -> str:
    with open(path, "rb") as fh:
        return fh.read(limit).decode("ascii", "replace").strip()


def kernel_battery(fp: Fingerprint, sysfs_root: str = SYSFS_HIDRAW) -> dict | None:
    """A battery the kernel's HID driver already publishes, if any.

    Found under the HID device in sysfs (power_supply/<name>/capacity), so
    it needs no access to the mouse and no protocol.
    """
    for node in fp.nodes:
        base = os.path.join(sysfs_root, os.path.basename(node.path), "device", "power_supply")
        try:
            supplies = sorted(os.listdir(base))
        except OSError:
            continue
        for name in supplies:
            try:
                capacity = int(_read_small(os.path.join(base, name, "capacity")))
                status = _read_small(os.path.join(base, name, "status"))
            except (OSError, ValueError):
                continue
            if 0 <= capacity <= 100:
                return {"percent": capacity, "charging": status == "Charging"}
    return None


def measure_polling(fp: Fingerprint, seconds: float = MEASURE_SECONDS) -> dict:
    """Count the mouse's own input reports while the user moves it.

    A mouse reports at its polling rate while moving and not at all while
    still, so the rate is taken over the span from the first report to the
    last, and too few reports means the mouse was not moved.
    """
    node = fp.pointer_node
    if node is None:
        raise NoResponse(f"{fp.name} has no pointer interface to measure")
    if not node.accessible():
        raise NoAccess(f"no access to {fp.name}'s pointer interface; set the mouse up first")
    stamps = []
    deadline = time.monotonic() + seconds
    with HidrawDevice(node.path) as dev:
        while (left := deadline - time.monotonic()) > 0:
            if dev.read_input(_MAX_READ, timeout=left) is not None:
                stamps.append(time.monotonic())
    if len(stamps) < _MIN_REPORTS or stamps[-1] <= stamps[0]:
        raise NoResponse(f"only {len(stamps)} reports arrived; move the mouse continuously while measuring")
    rate = (len(stamps) - 1) / (stamps[-1] - stamps[0])
    return {"hz": round(rate), "reports": len(stamps), "seconds": round(stamps[-1] - stamps[0], 2)}


def report(fp: Fingerprint) -> str:
    """Everything a driver author needs to start, as plain text."""
    lines = [
        f"mouse      {fp.name}",
        f"id         {fp.vidpid}  ({connection(fp)})",
        f"buttons    {fp.buttons} declared",
    ]
    for node in fp.nodes:
        d = node.descriptor
        usages = ", ".join(f"{c.usage_page:#06x}:{c.usage:#04x}" for c in d.collections) or "none"
        lines.append(
            f"interface  {node.interface if node.interface is not None else '?'}"
            f"  boot={node.boot_protocol if node.boot_protocol is not None else '-'}"
            f"  collections={usages}  feature={list(d.feature_report_ids)}"
            f"  output={list(d.output_report_ids)}"
        )
        lines.append(f"descriptor {node.raw_descriptor.hex()}")
    return "\n".join(lines) + "\n"
