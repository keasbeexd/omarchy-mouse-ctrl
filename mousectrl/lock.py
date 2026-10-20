"""Cross-process fence around every conversation with a mouse.

A command is a send followed by a read of the device's single reply buffer,
so two processes interleaving turn both into nonsense: one gets the other's
answer, a write looks ignored, a read-back reports the old value. Omarchy
runs one bar per monitor, each with its own serve process, so this happens
by default on a multi-monitor setup.

The lock lives in $XDG_RUNTIME_DIR, verified private to this user. There is
deliberately no /tmp fallback: a predictable name in a world-writable
directory is unsafe however carefully it is checked afterwards.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import stat
import time

from .errors import DeviceBusy, UnsafePath

LOCK_NAME = "mousectrl.lock"


def _open_runtime_dir() -> int:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
    if not os.path.isabs(runtime):
        raise UnsafePath(
            "XDG_RUNTIME_DIR is not set, so there is no private place for the "
            "device lock. Run mousectrl from a normal login session."
        )
    try:
        fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise UnsafePath(f"XDG_RUNTIME_DIR={runtime} is unusable: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise UnsafePath(f"{runtime} is owned by uid {info.st_uid}, not you")
        if info.st_mode & 0o077:
            raise UnsafePath(
                f"{runtime} is accessible to other users (mode "
                f"{stat.S_IMODE(info.st_mode):04o}); refusing to use it"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_lock_file() -> tuple[str, int]:
    """Open the lock relative to the verified directory: checked is used."""
    dir_fd = _open_runtime_dir()
    path = os.path.join(os.environ["XDG_RUNTIME_DIR"], LOCK_NAME)
    try:
        fd = os.open(
            LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise UnsafePath(f"{path} is a symbolic link; delete it") from exc
        raise
    finally:
        os.close(dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafePath(f"{path} is not a regular file; refusing to use it")
        if info.st_uid != os.getuid():
            raise UnsafePath(f"{path} is owned by uid {info.st_uid}, not you")
    except BaseException:
        os.close(fd)
        raise
    return path, fd


@contextlib.contextmanager
def device_lock(timeout: float = 6.0):
    """Hold the fence for one request; released when the block ends.

    Scoped per request, never per process: a serve process lives as long as
    the panel, and holding the fence that long would lock out every CLI
    command run from a terminal.
    """
    path, fd = _open_lock_file()
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DeviceBusy(
                        f"another mousectrl process held the mouse for over "
                        f"{timeout:.0f}s (lock: {path})"
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        # Closing the descriptor releases the flock.
        os.close(fd)
