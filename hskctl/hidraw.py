"""Dependency-free Linux hidraw transport.

Talks to /dev/hidrawN directly with the kernel's ioctls, so hskctl needs
nothing from pip. Everything here is transport only -- no knowledge of any
particular mouse protocol lives in this module.
"""

from __future__ import annotations

import array
import ctypes
import fcntl
import glob
import os
import struct
from dataclasses import dataclass, field
from typing import Iterator

# --- ioctl encoding (asm-generic, correct on x86_64 and aarch64) -------------

_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14

_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS

_IOC_NONE = 0
_IOC_WRITE = 1
_IOC_READ = 2

HID_MAX_DESCRIPTOR_SIZE = 4096


def _IOC(direction: int, typ: int, nr: int, size: int) -> int:
    return (
        (direction << _IOC_DIRSHIFT)
        | (typ << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def _IOR(typ: str, nr: int, size: int) -> int:
    return _IOC(_IOC_READ, ord(typ), nr, size)


def _IOWR_LEN(typ: str, nr: int, size: int) -> int:
    return _IOC(_IOC_READ | _IOC_WRITE, ord(typ), nr, size)


HIDIOCGRDESCSIZE = _IOR("H", 0x01, 4)
HIDIOCGRDESC = _IOR("H", 0x02, 4 + HID_MAX_DESCRIPTOR_SIZE)
HIDIOCGRAWINFO = _IOR("H", 0x03, 4 + 2 + 2)


def HIDIOCGRAWNAME(length: int) -> int:
    return _IOC(_IOC_READ, ord("H"), 0x04, length)


def HIDIOCGRAWPHYS(length: int) -> int:
    return _IOC(_IOC_READ, ord("H"), 0x05, length)


def HIDIOCSFEATURE(length: int) -> int:
    return _IOWR_LEN("H", 0x06, length)


def HIDIOCGFEATURE(length: int) -> int:
    return _IOWR_LEN("H", 0x07, length)


class HidrawError(OSError):
    """Raised for hidraw-specific failures with a human-readable hint."""


@dataclass
class HidrawInfo:
    """Everything we can learn about a hidraw node without opening a session."""

    path: str
    bustype: int
    vendor_id: int
    product_id: int
    name: str
    phys: str
    # Parsed out of the report descriptor.
    usage_page: int | None = None
    usage: int | None = None
    interface: int | None = None
    report_descriptor: bytes = b""
    feature_report_ids: list[int] = field(default_factory=list)
    output_report_ids: list[int] = field(default_factory=list)

    @property
    def vidpid(self) -> str:
        return f"{self.vendor_id:04x}:{self.product_id:04x}"

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "bustype": self.bustype,
            "vendorId": f"{self.vendor_id:04x}",
            "productId": f"{self.product_id:04x}",
            "vidpid": self.vidpid,
            "name": self.name,
            "phys": self.phys,
            "interface": self.interface,
            "usagePage": self.usage_page,
            "usage": self.usage,
            "featureReportIds": self.feature_report_ids,
            "outputReportIds": self.output_report_ids,
            "reportDescriptor": self.report_descriptor.hex(),
        }


def _ioctl_string(fd: int, request_builder, length: int = 256) -> str:
    buf = ctypes.create_string_buffer(length)
    try:
        fcntl.ioctl(fd, request_builder(length), buf, True)
    except OSError:
        return ""
    return buf.value.decode("utf-8", errors="replace")


def _interface_number(path: str) -> int | None:
    """Walk sysfs to find which USB interface this hidraw node belongs to.

    Vendor mice almost always expose config on a separate interface from the
    one carrying pointer input, so this is how we tell them apart.
    """
    node = os.path.basename(path)
    sysfs = f"/sys/class/hidraw/{node}/device"
    try:
        real = os.path.realpath(sysfs)
    except OSError:
        return None
    # .../usbN/x-y/x-y:1.2/0003:VVVV:PPPP.NNNN -> we want the ":1.2" component.
    for part in real.split(os.sep)[::-1]:
        if ":" in part and "." in part:
            head = part.split(":")[-1]
            if "." in head:
                try:
                    return int(head.split(".")[1])
                except (ValueError, IndexError):
                    continue
    return None


def parse_report_descriptor(desc: bytes) -> dict:
    """Minimal HID report-descriptor walk.

    We only need three things: the top-level usage page/usage (to spot the
    vendor-defined collection), and which report IDs carry Feature and Output
    items (those are the ones a config protocol rides on).
    """
    usage_page = None
    usage = None
    current_page = None
    current_report_id = None
    feature_ids: list[int] = []
    output_ids: list[int] = []
    depth = 0

    i = 0
    n = len(desc)
    while i < n:
        prefix = desc[i]
        i += 1
        if prefix == 0xFE:  # long item
            if i >= n:
                break
            size = desc[i]
            i += 2 + size
            continue
        size = prefix & 0x03
        if size == 3:
            size = 4
        typ = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F
        data = desc[i : i + size]
        i += size
        value = int.from_bytes(data, "little") if data else 0

        if typ == 1:  # Global
            if tag == 0x0:  # Usage Page
                current_page = value
            elif tag == 0x8:  # Report ID
                current_report_id = value
        elif typ == 2:  # Local
            if tag == 0x0 and depth == 0:  # Usage, before first collection
                usage = value
                usage_page = current_page
        elif typ == 0:  # Main
            # HID main item tags: Input 0x8, Output 0x9, Collection 0xA,
            # Feature 0xB, End Collection 0xC.
            if tag == 0xA:
                depth += 1
            elif tag == 0xC:
                depth = max(0, depth - 1)
            elif tag == 0xB:  # Feature
                rid = current_report_id if current_report_id is not None else 0
                if rid not in feature_ids:
                    feature_ids.append(rid)
            elif tag == 0x9:  # Output
                rid = current_report_id if current_report_id is not None else 0
                if rid not in output_ids:
                    output_ids.append(rid)

    return {
        "usage_page": usage_page,
        "usage": usage,
        "feature_report_ids": sorted(feature_ids),
        "output_report_ids": sorted(output_ids),
    }


def describe(path: str) -> HidrawInfo | None:
    """Read everything about one hidraw node. Returns None if unreadable."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    except OSError:
        # Fall back to read-only: enough for enumeration, not for feature reports.
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return None
    try:
        buf = array.array("B", [0] * 8)
        try:
            fcntl.ioctl(fd, HIDIOCGRAWINFO, buf, True)
            bustype, vendor, product = struct.unpack("ihh", bytes(buf))
        except OSError:
            return None
        name = _ioctl_string(fd, HIDIOCGRAWNAME, 256)
        phys = _ioctl_string(fd, HIDIOCGRAWPHYS, 256)

        desc = b""
        try:
            size_buf = array.array("i", [0])
            fcntl.ioctl(fd, HIDIOCGRDESCSIZE, size_buf, True)
            desc_size = size_buf[0]
            if 0 < desc_size <= HID_MAX_DESCRIPTOR_SIZE:
                raw = array.array("B", [0] * (4 + HID_MAX_DESCRIPTOR_SIZE))
                struct.pack_into("I", raw, 0, desc_size)
                fcntl.ioctl(fd, HIDIOCGRDESC, raw, True)
                desc = bytes(raw[4 : 4 + desc_size])
        except OSError:
            desc = b""

        parsed = parse_report_descriptor(desc) if desc else {}
        return HidrawInfo(
            path=path,
            bustype=bustype,
            vendor_id=vendor & 0xFFFF,
            product_id=product & 0xFFFF,
            name=name,
            phys=phys,
            interface=_interface_number(path),
            report_descriptor=desc,
            usage_page=parsed.get("usage_page"),
            usage=parsed.get("usage"),
            feature_report_ids=parsed.get("feature_report_ids", []),
            output_report_ids=parsed.get("output_report_ids", []),
        )
    finally:
        os.close(fd)


def enumerate_devices() -> Iterator[HidrawInfo]:
    for path in sorted(glob.glob("/dev/hidraw*")):
        info = describe(path)
        if info is not None:
            yield info


class HidrawDevice:
    """An open hidraw session. Use as a context manager."""

    def __init__(self, path: str):
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> "HidrawDevice":
        try:
            self._fd = os.open(self.path, os.O_RDWR)
        except PermissionError as exc:
            raise HidrawError(
                f"No write access to {self.path}. Install the udev rule "
                f"(see install/60-gwolves-hsk.rules) and replug the mouse, "
                f"or re-run with sudo."
            ) from exc
        except FileNotFoundError as exc:
            raise HidrawError(f"{self.path} disappeared -- is the mouse plugged in?") from exc
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @property
    def fd(self) -> int:
        if self._fd is None:
            raise HidrawError("device is not open -- use `with HidrawDevice(path) as dev:`")
        return self._fd

    def get_feature(self, report_id: int, length: int) -> bytes:
        """SET_REPORT-free read. `length` includes the report-ID byte."""
        buf = array.array("B", [0] * length)
        buf[0] = report_id
        fcntl.ioctl(self.fd, HIDIOCGFEATURE(length), buf, True)
        return bytes(buf)

    def set_feature(self, payload: bytes) -> int:
        """payload[0] must be the report ID (0 for devices with no report IDs)."""
        buf = array.array("B", payload)
        return fcntl.ioctl(self.fd, HIDIOCSFEATURE(len(buf)), buf, True)

    def write_output(self, payload: bytes) -> int:
        return os.write(self.fd, payload)

    def read_input(self, length: int = 64, timeout: float = 1.0) -> bytes | None:
        import select

        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return None
        return os.read(self.fd, length)
