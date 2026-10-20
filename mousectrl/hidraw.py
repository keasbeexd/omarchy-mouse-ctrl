"""Dependency-free Linux hidraw transport.

Talks to /dev/hidrawN directly with the kernel's ioctls, so mousectrl needs
nothing from pip. Transport only: identifying devices is fingerprint.py's job
(from sysfs, no open needed) and protocol knowledge lives in families/.
"""

from __future__ import annotations

import array
import ctypes
import fcntl
import os
import select
import stat
import struct

# --- ioctl encoding (asm-generic, correct on x86_64 and aarch64) -------------

_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14

_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS

_IOC_WRITE = 1
_IOC_READ = 2


def _IOC(direction: int, typ: int, nr: int, size: int) -> int:
    return (
        (direction << _IOC_DIRSHIFT)
        | (typ << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def HIDIOCSFEATURE(length: int) -> int:
    return _IOC(_IOC_READ | _IOC_WRITE, ord("H"), 0x06, length)


def HIDIOCGFEATURE(length: int) -> int:
    return _IOC(_IOC_READ | _IOC_WRITE, ord("H"), 0x07, length)


class HidrawError(OSError):
    """Raised for hidraw-specific failures with a human-readable hint."""


def _require_char_device(fd: int, path: str) -> None:
    """Refuse anything but a character device on the descriptor just opened.

    A FIFO or regular file planted at a node path would otherwise be handed
    ioctls and writes meant for a mouse. Checked on the descriptor, so the
    node checked is the node used.
    """
    if not stat.S_ISCHR(os.fstat(fd).st_mode):
        raise HidrawError(f"{path} is not a character device; refusing it")


class HidrawDevice:
    """An open hidraw session. Use as a context manager."""

    def __init__(self, path: str):
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> "HidrawDevice":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> None:
        if self._fd is not None:
            return
        try:
            # O_NONBLOCK so a FIFO at the path cannot hang the open before the
            # type check runs; cleared again once it is known to be a device.
            fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except PermissionError as exc:
            raise HidrawError(
                f"No read-write access to {self.path}. Install the udev rule "
                f"(`mousectrl udev-rule`) and replug the mouse."
            ) from exc
        except FileNotFoundError as exc:
            raise HidrawError(f"{self.path} disappeared -- is the mouse plugged in?") from exc
        try:
            _require_char_device(fd, self.path)
            os.set_blocking(fd, True)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @property
    def fd(self) -> int:
        if self._fd is None:
            raise HidrawError(f"{self.path} is not open")
        return self._fd

    def get_feature(self, report_id: int, length: int) -> bytes:
        """`length` includes the report-ID byte."""
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
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return None
        return os.read(self.fd, length)


# --- inotify (dependency-free hotplug watching) ------------------------------
#
# A hidraw node appearing, disappearing or changing permissions under /dev is
# the kernel's own signal that a mouse was plugged, unplugged, or just granted
# access by a fresh udev rule. There is no inotify in the standard library, so
# this binds the syscalls directly via ctypes, the same tier as the ioctls.

_libc = ctypes.CDLL("libc.so.6", use_errno=True)

IN_ATTRIB = 0x00000004
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200

_INOTIFY_EVENT_FMT = "iIII"
_INOTIFY_EVENT_SIZE = struct.calcsize(_INOTIFY_EVENT_FMT)


def inotify_init() -> int:
    fd = _libc.inotify_init1(os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1 failed")
    return fd


def inotify_add_watch(fd: int, path: str, mask: int) -> int:
    wd = _libc.inotify_add_watch(fd, path.encode(), mask)
    if wd < 0:
        raise OSError(ctypes.get_errno(), f"inotify_add_watch failed for {path}")
    return wd


def read_inotify_names(fd: int) -> list[str]:
    """One read's worth of events, as the filenames they name."""
    buf = os.read(fd, 4096)
    names = []
    pos = 0
    while pos + _INOTIFY_EVENT_SIZE <= len(buf):
        _wd, _mask, _cookie, name_len = struct.unpack_from(_INOTIFY_EVENT_FMT, buf, pos)
        pos += _INOTIFY_EVENT_SIZE
        raw_name = buf[pos:pos + name_len]
        pos += name_len
        names.append(raw_name.split(b"\0", 1)[0].decode(errors="replace"))
    return names
