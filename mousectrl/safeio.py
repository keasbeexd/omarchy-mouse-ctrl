"""Reading a file mousectrl did not just create, the one way it does that.

`open(path)` follows a symlink planted at the name, blocks forever on a FIFO,
and reads to EOF before any size check can run. Everything here does one
open(O_NOFOLLOW | O_NONBLOCK), validates *that descriptor* -- regular file,
owner, size, no group/other write -- and reads at most `max_bytes + 1` from
it, so overflow is detected rather than silently truncated.
"""

from __future__ import annotations

import json
import os
import secrets
import stat


def read_bounded(
    path: str,
    max_bytes: int,
    *,
    allowed_owners: tuple[int, ...] | None = None,
    reject_shared_write: bool = False,
    single_link: bool = False,
    dir_fd: int | None = None,
) -> bytes:
    """Return the bytes of `path`, or raise OSError describing the refusal.

    `allowed_owners` defaults to just the current user. `single_link`
    refuses a hard link (another name for a file elsewhere). With `dir_fd`,
    `path` is a name inside that already-verified directory.
    FileNotFoundError is passed through as itself, so a caller can tell
    "absent" from "refused".
    """
    owners = allowed_owners if allowed_owners is not None else (os.geteuid(),)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path} is not a regular file")
        if st.st_uid not in owners:
            raise OSError(f"{path} is owned by uid {st.st_uid}; refusing to read it")
        if single_link and st.st_nlink != 1:
            raise OSError(f"{path} has {st.st_nlink} hard links; refusing it")
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


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def open_private_dir(path: str, *, owned: int = 2, create: bool = True) -> int:
    """A held descriptor on a directory the plugin owns, verified on the way.

    The last `owned` components of `path` belong to the plugin (for the
    profiles, `mousectrl/profiles`): each is opened relative to its parent's
    descriptor without following a symlink, created 0700 if missing, must
    be a directory owned by this user, and is narrowed to 0700. The rest of
    the path is the anchor (`~/.config`), opened as it is. The caller owns
    the returned descriptor; every later open, rename and unlink goes
    through it, so the directory checked is the directory used.
    FileNotFoundError when it is missing and `create` is false.
    """
    parts = [p for p in path.split(os.sep) if p]
    if len(parts) <= owned:
        raise OSError(f"{path} is too short to hold {owned} plugin-owned components")
    anchor = os.sep + os.path.join(*parts[:-owned]) if path.startswith(os.sep) else os.path.join(*parts[:-owned])
    fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in parts[-owned:]:
            try:
                child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(name, 0o700, dir_fd=fd)
                child = os.open(name, _DIR_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise OSError(f"{name} under {anchor} is not a plain directory: {exc.strerror}") from exc
            os.close(fd)
            fd = child
            st = os.fstat(fd)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
                raise OSError(f"{name} under {anchor} is not a directory owned by you")
            if st.st_mode & 0o077:
                os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_private_json(dir_fd: int, name: str, doc: dict) -> None:
    """Replace `name` in a verified directory atomically, readable by this user only.

    An unpredictable temporary is created exclusively at 0600 (O_EXCL and
    O_NOFOLLOW, so a planted name fails the open instead of being followed),
    written in full, fsynced, renamed over the target -- rename replaces a
    symlink rather than writing through it -- and the directory fsynced. A
    crash leaves the old file or the new one, never half of either.
    """
    data = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
    tmp = f".{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                 dir_fd=dir_fd)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(fd)
