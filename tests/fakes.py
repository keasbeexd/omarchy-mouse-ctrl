"""Test doubles: a fake sysfs/dev tree and a scriptable family driver."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mousectrl.families.base import UNTESTED, VERIFIED, Capability, Driver  # noqa: E402

# Generic Desktop / Mouse application collection with a Pointer inside.
MOUSE_DESC = bytes.fromhex("05010902a1010901a100c0c0")
# Mouse and keyboard application collections in one interface (QMK mouse keys).
COMPOSITE_DESC = bytes.fromhex("05010902a1018501c005010906a1018502c0")
KEYBOARD_DESC = bytes.fromhex("05010906a101c0")
# Vendor page 0xffff, Feature item on report id 0.
VENDOR_DESC = bytes.fromhex("06ffff0900a1010900b102c0")


class FakeSysfs:
    """Lays out /sys/class/hidraw and /dev the way the kernel does."""

    def __init__(self, root: str):
        self.root = root
        self.sysfs = os.path.join(root, "sys", "class", "hidraw")
        self.dev = os.path.join(root, "dev")
        os.makedirs(self.sysfs)
        os.makedirs(self.dev)
        self._count = 0

    def add(self, *, vid: int, pid: int, name: str, phys: str, desc: bytes,
            bus: int = 3, uniq: str = "", boot_protocol: int | None = None,
            hid_id: str | None = None) -> str:
        node = f"hidraw{self._count}"
        self._count += 1
        interface_dir = os.path.join(self.root, "devices", node + "-if")
        device_dir = os.path.join(interface_dir, f"{bus:04X}:{vid:04X}:{pid:04X}.{self._count:04X}")
        os.makedirs(device_dir)
        if boot_protocol is not None:
            with open(os.path.join(interface_dir, "bInterfaceProtocol"), "w") as fh:
                fh.write(f"{boot_protocol:02x}\n")
        with open(os.path.join(device_dir, "uevent"), "w") as fh:
            fh.write(
                f"HID_ID={hid_id or f'{bus:04X}:{vid:08X}:{pid:08X}'}\n"
                f"HID_NAME={name}\nHID_PHYS={phys}\nHID_UNIQ={uniq}\n"
            )
        with open(os.path.join(device_dir, "report_descriptor"), "wb") as fh:
            fh.write(desc)
        os.makedirs(os.path.join(self.sysfs, node))
        os.symlink(device_dir, os.path.join(self.sysfs, node, "device"))
        path = os.path.join(self.dev, node)
        with open(path, "wb"):
            pass
        return path

    def add_hsk(self) -> list[str]:
        """The HSK Pro 4K receiver's real interface layout."""
        common = dict(vid=0x33E4, pid=0x5807, name="G-Wolves HSK Pro 4K Receiver-N")
        return [
            self.add(**common, phys="usb-0000:07:00.4-1.3/input0", desc=MOUSE_DESC, boot_protocol=2),
            self.add(**common, phys="usb-0000:07:00.4-1.3/input1", desc=KEYBOARD_DESC, boot_protocol=1),
            self.add(**common, phys="usb-0000:07:00.4-1.3/input2", desc=VENDOR_DESC, boot_protocol=0),
        ]


class FakeDriver(Driver):
    """Claims vendor 0x33e4. Behaviour is scripted through class attributes."""

    family = "fake"
    title = "Fake family"
    vendor_ids = {0x33E4: "Fake vendor"}

    values: dict = {}
    readback_override: dict = {}
    fail_with: Exception | None = None
    closed: list = []

    @classmethod
    def matches(cls, fingerprint) -> bool:
        return fingerprint.vendor_id == 0x33E4

    def capabilities(self) -> list[Capability]:
        return [
            Capability("battery", "battery", "Battery", VERIFIED),
            Capability("pollingRate", "enum", "Polling rate", VERIFIED, options=(1000, 2000, 4000)),
            Capability("motionSync", "toggle", "Motion sync", UNTESTED),
            Capability("dpi", "dpiStages", "DPI", VERIFIED, minimum=100, maximum=26000,
                       step=50, max_stages=7, has_colors=True),
        ]

    def read(self, capability_id):
        if self.fail_with:
            raise self.fail_with
        return self.values[capability_id]

    def write(self, capability_id, value):
        if self.fail_with:
            raise self.fail_with
        self.values[capability_id] = value
        return self.readback_override.get(capability_id, value)

    def close(self) -> None:
        self.closed.append(self.fingerprint.key)
