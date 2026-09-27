"""Reading a file hskctl did not just create, the one way it does that.

`open(path)` follows a symlink planted at the name, blocks forever on a FIFO,
and reads to EOF before any size check can run. Everything here does one
open(O_NOFOLLOW | O_NONBLOCK), validates *that descriptor* -- regular file,
owner, size, no group/other write -- and reads at most `max_bytes + 1` from
it, so overflow is detected rather than silently truncated.
"""

from __future__ import annotations

import json
import os
import stat


def read_bounded(
    path: str,
    max_bytes: int,
    *,
    allowed_owners: tuple[int, ...] | None = None,
    reject_shared_write: bool = False,
) -> bytes:
    """Return the bytes of `path`, or raise OSError describing the refusal.

    `allowed_owners` defaults to just the current user. FileNotFoundError is
    passed through as itself, so a caller can tell "absent" from "refused".
    """
    owners = allowed_owners if allowed_owners is not None else (os.geteuid(),)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path} is not a regular file")
        if st.st_uid not in owners:
            raise OSError(f"{path} is owned by uid {st.st_uid}; refusing to read it")
        if reject_shared_write and st.st_mode & 0o022:
            raise OSError(
                f"{path} is writable by other users (mode "
                f"{stat.S_IMODE(st.st_mode):04o}); refusing it -- chmod go-w it"
            )
        if st.st_size > max_bytes:
            raise OSError(f"{path} is larger than {max_bytes} bytes; refusing")
        os.set_blocking(fd, True)
        data = b""
        while len(data) <= max_bytes:
            chunk = os.read(fd, min(65536, max_bytes + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        if len(data) > max_bytes:
            raise OSError(f"{path} grew past the {max_bytes} byte cap during read")
        return data
    finally:
        os.close(fd)


def read_bounded_json(path: str, max_bytes: int, **kwargs) -> dict:
    """read_bounded, then parse; anything but a JSON object is a ValueError."""
    doc = json.loads(read_bounded(path, max_bytes, **kwargs).decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return doc
