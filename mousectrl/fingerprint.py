"""Identify HID devices from sysfs alone.

/sys is world-readable, report descriptors included, so a mouse is identified
before any udev rule grants hidraw access. The wizard's Access step depends on
that: it has to name the mouse whose rule it is asking to install.

Telling mice from keyboards: QMK keyboards expose a mouse collection for
mouse keys, and Logitech receivers put a keyboard on interface 0, so neither
"has a mouse collection" nor "interface 0 is a mouse" works. USB boot protocol
does: only a real pointer interface declares bInterfaceProtocol 2.

A physical device exposes one hidraw node per HID interface. Nodes are grouped
into one Fingerprint by HID_PHYS with its "/inputN" suffix stripped, plus
HID_UNIQ, which keeps two identical Bluetooth mice on one adapter apart.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

SYSFS_HIDRAW = "/sys/class/hidraw"
DEV_DIR = "/dev"

BUS_USB = 0x03
BUS_BLUETOOTH = 0x05
USAGE_PAGE_GENERIC_DESKTOP = 0x01
USAGE_MOUSE = 0x02
BOOT_PROTOCOL_MOUSE = 2
VENDOR_PAGE_MIN = 0xFF00

# Bounds on what sysfs is trusted to hand back.
_MAX_NODES = 64
_MAX_UEVENT_BYTES = 4096
_MAX_DESCRIPTOR_BYTES = 4096
_MAX_TEXT = 128

_NODE_RE = re.compile(r"^hidraw\d{1,4}$")
_INPUT_SUFFIX_RE = re.compile(r"/input(\d{1,3})$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Collection:
    usage_page: int
    usage: int


@dataclass(frozen=True)
class Descriptor:
    collections: tuple[Collection, ...]
    feature_report_ids: tuple[int, ...]
    output_report_ids: tuple[int, ...]

    @property
    def is_vendor(self) -> bool:
        return any(c.usage_page >= VENDOR_PAGE_MIN for c in self.collections)

    @property
    def has_mouse(self) -> bool:
        return MOUSE in self.collections

    @property
    def is_only_mouse(self) -> bool:
        return self.collections == (MOUSE,)


MOUSE = Collection(USAGE_PAGE_GENERIC_DESKTOP, USAGE_MOUSE)


def parse_descriptor(desc: bytes) -> Descriptor:
    """Walk a HID report descriptor for what device matching needs.

    Records each top-level application collection's usage and which report
    ids carry Feature and Output items -- the ones a config protocol rides on.
    """
    page = 0
    report_id = 0
    local_usage: Collection | None = None
    depth = 0
    collections: list[Collection] = []
    feature: set[int] = set()
    output: set[int] = set()

    i, n = 0, min(len(desc), _MAX_DESCRIPTOR_BYTES)
    while i < n:
        prefix = desc[i]
        i += 1
        if prefix == 0xFE:  # long item: size byte, tag byte, data
            if i >= n:
                break
            i += 2 + desc[i]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        typ = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F
        value = int.from_bytes(desc[i:i + size], "little")
        i += size

        if typ == 1:  # Global
            if tag == 0x0:
                page = value
            elif tag == 0x8:
                report_id = value
        elif typ == 2 and tag == 0x0:  # Local Usage; 4 bytes = extended page:usage
            local_usage = (
                Collection(value >> 16, value & 0xFFFF) if size == 4 else Collection(page, value)
            )
        elif typ == 0:  # Main
            if tag == 0xA:
                if depth == 0 and local_usage is not None:
                    collections.append(local_usage)
                depth += 1
            elif tag == 0xC:
                depth = max(0, depth - 1)
            elif tag == 0xB:
                feature.add(report_id)
            elif tag == 0x9:
                output.add(report_id)
            local_usage = None

    return Descriptor(tuple(collections), tuple(sorted(feature)), tuple(sorted(output)))


@dataclass(frozen=True)
class Node:
    path: str
    interface: int | None
    boot_protocol: int | None
    descriptor: Descriptor
    raw_descriptor: bytes

    def accessible(self) -> bool:
        return os.access(self.path, os.R_OK | os.W_OK)


@dataclass(frozen=True)
class Fingerprint:
    bus: int
    vendor_id: int
    product_id: int
    name: str
    phys: str
    uniq: str
    nodes: tuple[Node, ...]

    @property
    def vidpid(self) -> str:
        return f"{self.vendor_id:04x}:{self.product_id:04x}"

    @property
    def key(self) -> str:
        suffix = f"#{self.uniq}" if self.bus == BUS_BLUETOOTH and self.uniq else ""
        return f"{self.vidpid}@{self.phys}{suffix}"

    @property
    def is_mouse(self) -> bool:
        if self.bus != BUS_USB:
            return any(n.descriptor.has_mouse for n in self.nodes)
        if any(n.boot_protocol == BOOT_PROTOCOL_MOUSE for n in self.nodes):
            return True
        # Some USB mice skip boot support entirely; a dedicated mouse-only
        # interface is the next best signal and still excludes composite
        # keyboard interfaces carrying mouse keys.
        if any(n.boot_protocol for n in self.nodes):
            return False
        return any(n.descriptor.is_only_mouse for n in self.nodes)

    def accessible(self) -> bool:
        return all(node.accessible() for node in self.nodes)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "vidpid": self.vidpid,
            "bus": self.bus,
            "accessible": self.accessible(),
            "nodes": [
                {
                    "path": n.path,
                    "interface": n.interface,
                    "vendor": n.descriptor.is_vendor,
                    "featureReportIds": list(n.descriptor.feature_report_ids),
                    "outputReportIds": list(n.descriptor.output_report_ids),
                    "descriptor": n.raw_descriptor.hex(),
                }
                for n in self.nodes
            ],
        }


def _clean(text: str) -> str:
    return _CONTROL_RE.sub("", text).strip()[:_MAX_TEXT]


def _read_uevent(path: str) -> dict[str, str]:
    with open(path, "rb") as fh:
        data = fh.read(_MAX_UEVENT_BYTES).decode("utf-8", "replace")
    fields = {}
    for line in data.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key] = value
    return fields


def _parse_hid_id(hid_id: str) -> tuple[int, int, int] | None:
    parts = hid_id.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        bus, vid, pid = (int(p, 16) for p in parts)
    except ValueError:
        return None
    if not (0 <= vid <= 0xFFFF and 0 <= pid <= 0xFFFF):
        return None
    return bus, vid, pid


def _read_boot_protocol(device: str) -> int | None:
    """bInterfaceProtocol of the USB interface above this HID device, if any."""
    try:
        with open(os.path.join(os.path.realpath(device), "..", "bInterfaceProtocol"), "rb") as fh:
            return int(fh.read(8).strip() or b"0", 16)
    except (OSError, ValueError):
        return None


def _read_node(sysfs_root: str, dev_dir: str, node: str):
    """(group key, identity, Node) for one hidraw node, or None if unreadable."""
    device = os.path.join(sysfs_root, node, "device")
    try:
        uevent = _read_uevent(os.path.join(device, "uevent"))
        with open(os.path.join(device, "report_descriptor"), "rb") as fh:
            raw = fh.read(_MAX_DESCRIPTOR_BYTES)
    except OSError:
        # A node vanishing mid-scan is an unplug racing us, not an error.
        return None
    ids = _parse_hid_id(uevent.get("HID_ID", ""))
    if ids is None:
        return None
    bus, vid, pid = ids
    phys = _clean(uevent.get("HID_PHYS", ""))
    match = _INPUT_SUFFIX_RE.search(phys)
    interface = int(match.group(1)) if match else None
    phys_root = phys[:match.start()] if match else phys
    uniq = _clean(uevent.get("HID_UNIQ", ""))
    identity = (bus, vid, pid, _clean(uevent.get("HID_NAME", "")), phys_root, uniq)
    return (
        (bus, vid, pid, phys_root, uniq),
        identity,
        Node(
            os.path.join(dev_dir, node),
            interface,
            _read_boot_protocol(device) if bus == BUS_USB else None,
            parse_descriptor(raw),
            raw,
        ),
    )


def scan(sysfs_root: str = SYSFS_HIDRAW, dev_dir: str = DEV_DIR) -> list[Fingerprint]:
    """Every HID device on the machine, mice or not, grouped by physical device."""
    try:
        names = sorted(n for n in os.listdir(sysfs_root) if _NODE_RE.match(n))
    except FileNotFoundError:
        return []
    groups: dict[tuple, tuple[tuple, list[Node]]] = {}
    for name in names[:_MAX_NODES]:
        read = _read_node(sysfs_root, dev_dir, name)
        if read is None:
            continue
        key, identity, node = read
        groups.setdefault(key, (identity, []))[1].append(node)
    return [
        Fingerprint(*identity, nodes=tuple(sorted(nodes, key=_node_order)))
        for identity, nodes in groups.values()
    ]


def _node_order(node: Node) -> tuple:
    return (node.interface is None, node.interface or 0, node.path)


def mice(sysfs_root: str = SYSFS_HIDRAW, dev_dir: str = DEV_DIR) -> list[Fingerprint]:
    return [fp for fp in scan(sysfs_root, dev_dir) if fp.is_mouse]
