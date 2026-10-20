"""Binding a profile to a real hidraw node, and reading/writing settings."""

from __future__ import annotations

import atexit
import contextlib
import errno
import fcntl
import os
import stat
import time
from dataclasses import dataclass, field
from typing import Any

from .hidraw import HidrawDevice, HidrawInfo, enumerate_devices, sysfs_hid_devices
from .protocol import NotDiscovered, Profile, ProtocolError, list_profiles, load_profile

# Vendors ship several USB bridge chips across revisions, so name matching is
# the fallback when a profile has no VID/PID yet. Deliberately broad -- this
# only selects *candidates* to show the user, it never triggers a write.
NAME_HINTS = (
    "g-wolves", "gwolves", "hsk", "wireless dongle", "compx", "vgn", "pulsar",
)


def is_vendor_usage_page(page: int | None) -> bool:
    return page is not None and 0xFF00 <= page <= 0xFFFF


@dataclass
class Candidate:
    info: HidrawInfo
    score: int
    reasons: list[str]
    # Does this node match what the profile actually declares, rather than
    # merely looking plausible? Scoring answers "worth showing the user";
    # this answers "safe to send vendor commands to unprompted".
    identified: bool = False
    unmatched: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.info.as_dict()
        d["score"] = self.score
        d["reasons"] = self.reasons
        d["identified"] = self.identified
        d["unmatched"] = self.unmatched
        return d


def rank_candidates(profile: Profile | None = None) -> list[Candidate]:
    """Score every hidraw node by how likely it is to be the config endpoint.

    A vendor mouse typically shows up as three or four hidraw nodes: the boot
    mouse, a consumer-control node, and one vendor-defined node. Only the last
    speaks the config protocol, and it is the one with a vendor usage page and
    Feature reports.
    """
    match = profile.match if profile else {}
    want_vids = {v.lower() for v in match.get("vendorIds") or []}
    want_pids = {p.lower() for p in match.get("productIds") or []}
    want_iface = match.get("interface")
    want_page = match.get("usagePage")

    results: list[Candidate] = []
    for info in enumerate_devices():
        score = 0
        reasons: list[str] = []

        if want_vids and f"{info.vendor_id:04x}" in want_vids:
            score += 40
            reasons.append("vendor id matches profile")
        if want_pids and f"{info.product_id:04x}" in want_pids:
            score += 40
            reasons.append("product id matches profile")
        if want_iface is not None and info.interface == want_iface:
            score += 25
            reasons.append(f"on interface {want_iface}")
        if want_page is not None and info.usage_page == want_page:
            score += 25
            reasons.append("usage page matches profile")

        if is_vendor_usage_page(info.usage_page):
            score += 20
            reasons.append(f"vendor-defined usage page 0x{info.usage_page:04x}")
        if info.feature_report_ids:
            score += 15
            reasons.append(
                "has feature report id(s) "
                + ", ".join(str(r) for r in info.feature_report_ids)
            )
        if info.usage_page == 0x01 and info.usage == 0x02:
            score -= 30
            reasons.append("this is the pointer input node, not config")

        lowered = info.name.lower()
        if any(hint in lowered for hint in NAME_HINTS):
            score += 10
            reasons.append("device name looks like a known mouse vendor's node")

        # Identification is a conjunction of everything the profile declares,
        # not a score. A score is a ranking heuristic and will happily elect
        # the best of a bad field; sending vendor-specific feature reports to
        # the wrong device is not something to do on a heuristic.
        unmatched: list[str] = []
        if want_vids and f"{info.vendor_id:04x}" not in want_vids:
            unmatched.append(f"vendor id {info.vendor_id:04x} is not in the profile")
        if want_pids and f"{info.product_id:04x}" not in want_pids:
            unmatched.append(f"product id {info.product_id:04x} is not in the profile")
        if want_page is not None and info.usage_page != want_page:
            unmatched.append(
                f"usage page {info.usage_page if info.usage_page is None else f'0x{info.usage_page:04x}'}"
                f" is not the profile's 0x{want_page:04x}"
            )
        if match.get("requireFeatureReport") and not info.feature_report_ids:
            unmatched.append("no feature report, which the profile requires")
        if want_iface is not None and info.interface != want_iface:
            unmatched.append(f"interface {info.interface} is not {want_iface}")

        if score > 0:
            results.append(
                Candidate(info, score, reasons, identified=not unmatched,
                          unmatched=unmatched)
            )

    results.sort(key=lambda c: (-c.score, c.info.path))
    return results


def _bare_profile() -> Profile:
    """A placeholder standing for "no profile has been created yet".

    Shipping with zero bundled profiles is the normal state now: a profile
    is something the guided setup writes into the user's own config, so an
    empty profile list is a brand-new install, not a missing directory. The
    placeholder lets every command fall through to the same honest
    `undiscovered` shape the panel already renders, with no model name,
    because none is known until the user creates one.
    """
    return Profile({"fields": {}}, "<uncreated>")


def detect_profile(explicit: str | None = None) -> Profile:
    """Pick the profile that matches whatever mouse is actually plugged in.

    With several vendors' mice supported, `hskctl status` (no --profile) can no
    longer default to any one of them. Instead it asks every profile whether
    the hidraw nodes on this machine look like its `match` block, the same
    conjunction `open_session` uses before it will write to a device -- so
    "detected" here means the same thing it means everywhere else in hskctl.
    An explicit --profile always wins and skips this entirely.

    With no profiles at all there is nothing to ask: the bare placeholder
    profile is returned so callers report `undiscovered` rather than
    crashing on the absence of a default.
    """
    if explicit:
        return load_profile(explicit)

    names = list_profiles()
    if not names:
        return _bare_profile()

    fallback: Profile | None = None
    for name in names:
        try:
            candidate_profile = load_profile(name)
        except ProtocolError:
            continue
        if fallback is None:
            fallback = candidate_profile
        if any(c.identified for c in rank_candidates(candidate_profile)):
            return candidate_profile

    # Nothing plugged in matched anything. Report against whichever profile
    # sorts first, purely so `status`/`doctor` have a model name to talk
    # about -- the "not detected" message does not depend on which one.
    return fallback if fallback is not None else _bare_profile()


class DeviceNotFound(ProtocolError):
    pass


class DeviceBusy(ProtocolError):
    pass


class EmptyReply(ProtocolError):
    """The mouse ACKed but returned no payload -- "heard you, answered nothing".

    Its own subclass so callers that already tolerate failed reads
    (`read_some`/`read_all`) can skip the command exactly as they skip a
    dead one, while a caller that demands a real answer (`get`, the
    stage probe's verification read) sees the honest error instead of
    zeros it would have believed.
    """


_LOCK_HANDLE = None


class UnsafeLockPath(ProtocolError):
    pass


def _open_lock_dir() -> int:
    """Hold a descriptor on $XDG_RUNTIME_DIR, verified private to this user.

    systemd creates it 0700 and owned by the user, which is exactly the
    property the lock needs. There is deliberately no fallback: the old one
    was a per-uid directory in /tmp, which is a predictable name in a
    world-writable directory however carefully it is checked afterwards.
    Without a runtime directory hskctl refuses to run rather than guessing.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR") or ""
    if not os.path.isabs(runtime):
        raise UnsafeLockPath(
            "XDG_RUNTIME_DIR is not set, so there is no private place for "
            "hskctl's device lock. Run it from a normal login session."
        )
    try:
        fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise UnsafeLockPath(f"XDG_RUNTIME_DIR={runtime} is unusable: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise UnsafeLockPath(f"{runtime} is owned by uid {info.st_uid}, not you")
        if info.st_mode & 0o077:
            raise UnsafeLockPath(
                f"{runtime} is accessible to other users (mode "
                f"{stat.S_IMODE(info.st_mode):04o}); refusing to use it"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


_LOCK_NAME = "hskctl.lock"


def _open_lock_file():
    """Open the lock file without following a symlink and without truncating.

    Opened relative to the verified runtime directory descriptor, so the
    directory checked is the directory used. O_NOFOLLOW makes the open fail
    outright if the name is a symlink; O_NONBLOCK keeps a FIFO planted there
    from hanging the open before the type check runs.
    """
    dir_fd = _open_lock_dir()
    path = os.path.join(os.environ["XDG_RUNTIME_DIR"], _LOCK_NAME)
    try:
        fd = os.open(
            _LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            # O_NOFOLLOW reports a symlink as ELOOP, which reads like a
            # filesystem fault rather than what it is.
            raise UnsafeLockPath(
                f"{path} is a symbolic link. hskctl will not open a lock through "
                f"it. Delete it."
            ) from exc
        raise
    finally:
        os.close(dir_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeLockPath(f"{path} is not a regular file; refusing to use it")
        if info.st_uid != os.getuid():
            raise UnsafeLockPath(
                f"{path} is owned by uid {info.st_uid}, not you; refusing to use it"
            )
        os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return path, os.fdopen(fd, "r+b")


def _acquire_device_lock_handle(timeout: float = 6.0):
    """Take the cross-process flock, waiting up to `timeout`.

    A command is a send followed by a read of the *device's* single reply
    buffer, so two processes interleaving turns both of them into nonsense:
    one gets the other's answer, a write appears to be ignored, and a
    read-back reports the old value.

    This is not hypothetical. Omarchy runs one bar per monitor, each with its
    own refresh timer, so a two-monitor setup fires concurrent `status` reads
    by default -- and a click lands a `set` right in the middle of one.

    Returns the (path, handle) pair on success; the caller owns releasing it,
    either through atexit (one-shot processes) or a scope exit (`device_lock`).
    """
    handle = None
    try:
        path, handle = _open_lock_file()
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise DeviceBusy(
                        f"another hskctl is talking to the mouse and did not finish "
                        f"within {timeout:.0f}s (lock: {path}). The lock is released "
                        f"when that process exits, so wait for it or stop it."
                    )
                time.sleep(0.05)
        return path, handle
    except BaseException:
        if handle is not None:
            handle.close()
        raise


def acquire_device_lock(timeout: float = 6.0) -> None:
    """Hold the device lock for the rest of this process.

    One-shot commands run at most one request, so taking the lock once and
    releasing it at exit is both simplest and safest -- the release cannot be
    forgotten. A long-lived `serve` process cannot do this: its lock would
    fence every CLI one-shot out for the life of the panel, so it scopes the
    lock per request with `device_lock` instead.
    """
    global _LOCK_HANDLE
    if _LOCK_HANDLE is not None:
        return
    _LOCK_HANDLE = _acquire_device_lock_handle(timeout)[1]
    atexit.register(_release_device_lock)


@contextlib.contextmanager
def device_lock(timeout: float = 6.0):
    """Hold the cross-process device lock for a bounded scope.

    Same fence `acquire_device_lock` builds, but the release happens when the
    `with` block ends instead of at process exit -- the shape `serve` needs,
    since it must leave the lock free between requests.
    """
    path, handle = _acquire_device_lock_handle(timeout)
    try:
        yield path
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _release_device_lock() -> None:
    global _LOCK_HANDLE
    if _LOCK_HANDLE is None:
        return
    try:
        fcntl.flock(_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
        _LOCK_HANDLE.close()
    except OSError:
        pass
    _LOCK_HANDLE = None


@dataclass
class Session:
    profile: Profile
    info: HidrawInfo
    # Which link the mouse is on. Established by detect_link() before the first
    # exchange, because an ACK alone does not prove the flag was right.
    wireless: bool = False
    # False when the node was named explicitly and does not match the profile.
    # Reads are allowed -- that is how you profile a new device -- but writes
    # are not, because a vendor feature report aimed at the wrong hardware is
    # exactly the kind of blind write that bricks things.
    identified: bool = True
    allow_unidentified_writes: bool = False
    _link_detected: bool = False

    # -- low level -----------------------------------------------------------

    def _settle(self, command: str | None) -> float:
        """Seconds to wait between sending and reading back.

        The vendor uses 60ms for everything, but its commands carry one byte of
        payload. DPI carries 51, and at 60ms the reply is sometimes not ready --
        which shows up as an all-zero payload, indistinguishable from a mouse
        that ignored the packet. Commands can raise their own settle.
        """
        spec = (self.profile.data.get("commands") or {}).get(command or "") or {}
        ms = spec.get("settleMs", self.profile.transport.get("settleMs", 60))
        return ms / 1000.0

    def _exchange(self, packet: bytes, command: str | None = None) -> bytes:
        t = self.profile.transport
        read_len = self.profile.read_length
        report_id = t.get("reportId") or 0
        settle = self._settle(command)
        # Tracing lives here rather than in the callers so that *every* wire
        # transaction is captured -- including the link-flag retry, the
        # wake-up resend and the verification read-back. A trace that only
        # shows the packet you meant to send hides exactly the cases where
        # something else went out instead.
        self._trace(f"-> {command or '?'}", packet)
        with HidrawDevice(self.info.path) as dev:
            if t.get("kind") == "output":
                dev.write_output(packet)
                time.sleep(settle)
                reply = dev.read_input(read_len, timeout=1.5) or b""
            else:
                dev.set_feature(packet)
                time.sleep(settle)
                reply = dev.get_feature(report_id, read_len)
        self._trace(f"<- {command or '?'}", reply)
        return reply

    def _describe_reply(self, reply: bytes) -> str:
        if not reply:
            return "no reply at all"
        if len(reply) < 2:
            return f"a {len(reply)}-byte reply"
        return f"byte 1 = {reply[1]:#04x}, expected 0xa1"

    def _exchange_checked(self, command: str, packet: bytes) -> bytes:
        """Send a request, retrying on the other link flag if it goes unheard.

        Byte 4 tells the firmware whether it is being addressed over the cable
        or the dongle. A command that carries no link flag -- `connection`,
        `sleep` -- must be left alone: flipping byte 4 on those corrupts the
        packet, and for `sleep` that byte is part of the sub-command.
        """
        reply = self._exchange(packet, command)
        if self.profile.check_ack(reply):
            return reply

        flag = self.profile.transport.get("wirelessFlagOffset")
        carries_flag = (self.profile.data["commands"].get(command) or {}).get(
            "linkFlag", True
        )
        if flag is None or not carries_flag:
            # Nothing to flip. A silent mouse here is usually one that is
            # asleep, and the packet we just sent is what wakes it, so try the
            # identical packet again before giving up.
            time.sleep(self._settle(command))
            reply = self._exchange(packet, command)
            if self.profile.check_ack(reply):
                return reply
            raise ProtocolError(
                f"the mouse did not acknowledge {command!r} "
                f"({self._describe_reply(reply)})"
            )

        self.wireless = not self.wireless
        # Reuse the original packet and flip only the link flag, so a write
        # keeps its payload.
        flipped = bytearray(packet)
        flipped[flag] = 1 if self.wireless else 0
        self.profile.checksum(flipped)
        reply = self._exchange(bytes(flipped), command)
        if self.profile.check_ack(reply):
            return reply
        self.wireless = not self.wireless  # neither worked; leave the guess as it was
        raise ProtocolError(
            f"the mouse did not acknowledge {command!r} on either link "
            f"({self._describe_reply(reply)}). Is this the config endpoint? "
            f"Try `hskctl probe`."
        )

    def probe_link(self, wireless: bool) -> bool:
        """Does this endpoint answer with real data when addressed this way?

        Byte 4 tells the firmware which transport the packet is addressed
        over, and it is a property of *the endpoint we opened*, not of where
        the mouse happens to be. Get it wrong and the firmware acknowledges the
        packet and then ignores it, answering with an all-zero payload -- which
        is indistinguishable from a mouse that is simply switched off.

        So do not deduce it. Send a read on each flag and keep whichever one
        comes back with a payload that is not all zeros.
        """
        spec = self.profile.transport.get("linkProbe") or {}
        command = spec.get("command")
        if not command or not self.profile.has_command(command):
            return False
        offset = spec.get("payloadFrom", 5)
        packet = self.profile.build_request(command, write=False, wireless=wireless)
        try:
            reply = self._exchange(packet, command)
        except (OSError, ProtocolError):
            return False
        return bool(reply) and self.profile.check_ack(reply) and any(reply[offset:])

    def detect_link(self) -> bool:
        """Work out which link flag this endpoint answers on.

        This used to ask the mouse over the `connection` command, which was
        wrong in a way that took a while to see: `connection` reports whether
        the mouse is currently linked over RF or sitting on a cable, which is a
        different question from which flag *this endpoint* wants. Plug the
        cable in while the dongle is still in and the two disagree -- the reply
        says "wired", every packet then goes to the dongle carrying flag 0, the
        firmware ACKs and discards all of it, and every setting reads back 0
        with nothing to say why.
        """
        if self._link_detected:
            return self.wireless

        if self.profile.transport.get("linkProbe"):
            # Two rounds, because a sleeping mouse misses the first packet --
            # and the packet itself is what wakes it.
            for round_index in range(2):
                for flag in (self.wireless, not self.wireless):
                    if self.probe_link(flag):
                        self.wireless = flag
                        self._link_detected = True
                        return self.wireless
                if round_index == 0:
                    time.sleep(0.12)
            raise ProtocolError(
                f"{self.info.path} acknowledges but answers with empty data on "
                f"both link flags. Either the mouse is asleep -- move it and try "
                f"again -- or this is the dongle while the mouse is on the "
                f"cable, in which case the mouse is a different node. "
                f"`hskctl doctor` lists every candidate and which one is alive."
            )

        # Older profiles with no probe defined fall back to asking.
        if not self.profile.has_command("connection"):
            self._link_detected = True
            return self.wireless

        spec = self.profile.data["commands"]["connection"]
        offset = spec.get("responseOffset", 5)
        packet = self.profile.build_request("connection", write=False)
        opcode = packet[3] if len(packet) > 3 else None

        # What makes a reply real is the echoed opcode, NOT a non-zero payload:
        # 0 is precisely what a mouse on the cable reports, so requiring
        # non-zero here made a wired mouse undetectable.
        for _ in range(3):
            try:
                reply = self._exchange_checked("connection", packet)
            except (OSError, ProtocolError):
                time.sleep(0.08)
                continue
            if len(reply) > offset and (opcode is None or reply[3] == opcode):
                self.wireless = reply[offset] != 0
                self._link_detected = True
                return self.wireless
            time.sleep(0.08)

        raise ProtocolError(
            "could not work out whether the mouse is on the cable or the dongle. "
            "It is probably asleep -- move it and try again. (Guessing here would "
            "make every later read return zeros.)"
        )

    def _read(self, command: str) -> bytes:
        self.detect_link()
        packet = self.profile.build_request(command, write=False, wireless=self.wireless)
        reply = self._exchange_checked(command, packet)
        # A mouse that just woke answers the first packet with an empty payload.
        # Reads are idempotent, so one more costs 60ms and never hurts.
        if reply and not any(reply[5:]):
            time.sleep(self._settle(command))
            retry = self._exchange_checked(command, packet)
            if retry and any(retry[5:]):
                return retry
            # Still empty after the retry. This is not data: no real payload
            # is all zeros (the DPI block, for one, always carries a nonzero
            # stage count -- the profile's own repairOnWrite insists on it),
            # and decoding it anyway turns "no answer" into lying 0% readings
            # that a post-suspend panel then trusts for everything. Surface
            # the truth: the mouse heard us and answered nothing. Recoverable
            # by moving the mouse (its sleep) -- after a system resume the
            # dongle's config endpoint can stay wedged until the cable is
            # cycled, which is exactly what the message should say.
            raise EmptyReply(
                "the mouse acknowledges commands but answers with an empty "
                "payload (asleep, or the dongle wedged after a suspend -- "
                "move the mouse or replug its cable)"
            )
        return reply

    def trial_read(self, command: str, wireless: bool) -> dict:
        """One read attempt, reporting exactly what went over the wire.

        Deliberately does not raise and does not retry: `doctor` needs the raw
        result of each individual attempt, including the failures, rather than
        the tidied-up answer `_read` produces.
        """
        try:
            packet = self.profile.build_request(command, write=False, wireless=wireless)
        except ProtocolError as exc:
            return {"command": command, "wireless": wireless, "error": str(exc)}
        out = {
            "command": command,
            "wireless": wireless,
            "request": packet.hex(" "),
        }
        try:
            reply = self._exchange(packet, command)
        except (OSError, ProtocolError) as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
            return out
        out["reply"] = reply.hex(" ") if reply else ""
        out["replyLength"] = len(reply)
        out["ack"] = self.profile.check_ack(reply)
        out["ackByte"] = reply[1] if len(reply) > 1 else None
        out["allZero"] = bool(reply) and not any(reply)
        return out

    # -- high level ----------------------------------------------------------

    def read_all(self) -> dict:
        """Everything the profile knows how to read.

        Each command is issued once and shared by every field that reads from
        it, so a status refresh costs one exchange per command rather than one
        per field. A command that fails is skipped rather than aborting the
        whole read -- a mouse on the cable legitimately has no battery reading.
        """
        fields = self.profile.data.get("fields", {})
        names = [
            name for name, spec in fields.items()
            if not name.startswith("_") and isinstance(spec, dict)
        ]
        return self.read_some(names)

    def read_some(self, names: list[str]) -> dict:
        """Like `read_all`, restricted to the given fields.

        Still shares one exchange per distinct command among the fields
        asked for, same as `read_all` -- so asking for `batteryPercent` and
        `charging` together (they share the `battery` command on every
        shipped profile) still costs one exchange, not two. Exists for the
        panel's fast battery poll, which should not pay for reading DPI
        stages, polling rate and every sensor toggle just to notice a
        charging-cable flip.
        """
        out: dict[str, Any] = {}
        cache: dict[str, bytes] = {}
        for name in names:
            if not self.profile.has_field(name):
                continue
            command = self.profile.field_command(name)
            if command not in cache:
                try:
                    cache[command] = self._read(command)
                except (OSError, ProtocolError):
                    cache[command] = b""
            buf = cache[command]
            if not buf:
                continue
            try:
                out[name] = self.profile.decode(name, buf)
            except (ProtocolError, IndexError):
                continue
        return out

    def get(self, name: str) -> Any:
        command = self.profile.field_command(name)
        buf = self._read(command)
        return self.profile.decode(name, buf)

    def _mirror_block(self, mirror: dict) -> None:
        """Write the same settings again in the firmware's legacy layout.

        The vendor app sends the DPI block twice -- once in the current format
        and once in an older, more compact one -- and it is the second write
        that survives a power cycle. Writing only the first lands in RAM and is
        lost when the mouse is switched off.

        The two layouts differ, so this restates the values rather than copying
        bytes: source stages are 7 bytes (X u16be, Y u16be, RGB), mirror stages
        are 5 (DPI u16be, RGB).
        """
        source_cmd = mirror["from"]
        target_cmd = mirror["command"]
        source = self._read(source_cmd)
        target = bytearray(self._read(target_cmd))
        if not source or not target:
            raise ProtocolError(f"could not read {target_cmd!r} to mirror into")

        packet = bytearray(
            self.profile.build_request(target_cmd, write=True, wireless=self.wireless)
        )
        start, end = mirror.get("payloadRange", [5, 65])
        packet[start:end] = target[start:end]

        s_first, s_stride = mirror["sourceFirst"], mirror["sourceStride"]
        t_first, t_stride = mirror["targetFirst"], mirror["targetStride"]
        for n in range(mirror["stages"]):
            so, to = s_first + n * s_stride, t_first + n * t_stride
            if so + 7 > len(source) or to + 5 > len(packet):
                break
            packet[to : to + 2] = source[so : so + 2]      # X DPI -> DPI
            packet[to + 2 : to + 5] = source[so + 4 : so + 7]  # RGB
        self.profile.checksum(packet)
        self._exchange_checked(target_cmd, bytes(packet))

    def _guard_write(self) -> None:
        if self.identified or self.allow_unidentified_writes:
            return
        raise ProtocolError(
            f"{self.info.path} does not match the {self.profile.model} profile "
            f"({self.info.vidpid}), so hskctl will not write to it. Reads are "
            f"allowed so you can profile it; if you are certain, pass "
            f"--force-unmatched. Sending vendor feature reports to the wrong "
            f"device can leave it unusable."
        )

    def set_raw(self, name: str, raw: int) -> None:
        """Write a raw wire value, bypassing the friendly-value table.

        Needed to calibrate a mapping we do not know yet: you cannot ask for
        "4000 Hz" until you have established which byte means 4000 Hz, so the
        sweep has to address the register numerically.
        """
        if not self.profile.field_writable(name):
            raise ProtocolError(f"{name!r} is read-only on this device")
        self._guard_write()
        self.detect_link()
        command = self.profile.field_command(name)
        spec = self.profile.field(name)
        enc = spec.get("encoding", "u8")
        width = {"u8": 1, "u16le": 2, "u16be": 2}.get(enc, 1)
        order = "little" if enc == "u16le" else "big"
        payload = int(raw).to_bytes(width, order)
        packet = self.profile.build_request(
            command, write=True, value_bytes=payload, wireless=self.wireless
        )
        self._exchange_checked(command, packet)

    def get_raw(self, name: str) -> int:
        command = self.profile.field_command(name)
        reply = self._read(command)
        spec = self.profile.field(name)
        offset = self.profile.response_offset(command, spec)
        enc = spec.get("encoding", "u8")
        if enc == "u16le":
            return int.from_bytes(reply[offset : offset + 2], "little")
        if enc == "u16be":
            return int.from_bytes(reply[offset : offset + 2], "big")
        return reply[offset]

    # Filled in when tracing is on, so `set --verbose` can show exactly what
    # went over the wire rather than only the outcome.
    trace: list = None

    def _trace(self, label: str, data) -> None:
        if self.trace is None:
            return
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).hex(" ")
        self.trace.append({"step": label, "data": data})

    def _repair_header(self, packet: bytearray, spec: dict) -> None:
        """Refuse to echo back a header byte the mouse cannot have meant.

        Read-modify-write is the right shape for a block command, but it has a
        failure mode: a header byte that controls how the firmware *interprets*
        the block gets copied back verbatim, so once it is wrong it stays
        wrong, and it takes every subsequent write down with it.

        That is not hypothetical. This mouse reported a DPI stage count of 0.
        The firmware writes that many stages out of the packet, so it wrote
        none -- while the active-stage byte beside it, outside the array, kept
        working. Every DPI value and every stage color was silently discarded,
        and each write faithfully restored the 0 that caused it. Setting the
        count to 7 fixed both in one go, confirmed on hardware.

        Which bytes are load-bearing, which values are impossible and what to
        substitute are all in the profile -- see `repairOnWrite`.
        """
        for fix in spec.get("repairOnWrite") or []:
            offset = fix.get("offset")
            if offset is None or offset >= len(packet):
                continue
            if packet[offset] in fix.get("invalid", []):
                self._trace(
                    f"repaired byte {offset}: {packet[offset]} -> {fix['value']}"
                    f" ({fix.get('why', 'impossible value')})",
                    bytes([packet[offset], fix["value"]]),
                )
                packet[offset] = fix["value"]

    def set(self, name: str, value: Any) -> None:
        """Write one setting.

        There is no read-modify-write here because there is nothing to merge:
        each setting is its own command carrying only its own value. The write
        template comes straight from the vendor firmware's own command table.
        """
        if not self.profile.field_writable(name):
            raise ProtocolError(f"{name!r} is read-only on this device")
        self._guard_write()
        self.detect_link()
        command = self.profile.field_command(name)
        spec = self.profile.data["commands"][command]
        spec_field = self.profile.field(name)

        if spec.get("readModifyWrite"):
            # Commands that carry a whole block -- DPI carries seven stages and
            # their colors in one packet -- must not be synthesised from
            # nothing, or every field we have not decoded gets zeroed. Read the
            # mouse's own block, change one field in it, send it back.
            current = self._read(command)
            self._trace("read before write", current)
            packet = bytearray(
                self.profile.build_request(command, write=True, wireless=self.wireless)
            )
            self._trace("write template", packet)
            start, end = spec.get("payloadRange", [5, len(current)])
            end = min(end, len(current), len(packet))
            packet[start:end] = current[start:end]
            self._trace("after copying the current block", packet)
            self._repair_header(packet, spec)
            self.profile.encode_into(packet, name, value)
            # A linked field rides in the same packet. DPI has independent X and
            # Y axes, but the vendor app keeps them equal unless you explicitly
            # unlink them -- and a mouse whose axes disagree tracks wrong. One
            # write, both axes.
            linked = spec_field.get("linkedField")
            if linked and self.profile.has_field(linked):
                self.profile.encode_into(packet, linked, value)
            self.profile.checksum(packet)
            self._trace("packet sent", packet)
            reply = self._exchange_checked(command, bytes(packet))
            self._trace("reply to the write", reply)
        else:
            payload = self.profile.encode_value(name, value)
            packet = self.profile.build_request(
                command, write=True, value_bytes=payload, wireless=self.wireless
            )
            self._exchange_checked(command, packet)
        mirror = spec.get("mirrorTo")
        if mirror:
            self._mirror_block(mirror)

        if self.profile.has_command("commit"):
            time.sleep(self.profile.transport.get("settleMs", 60) / 1000.0)
            self._exchange(
                self.profile.build_request("commit", write=True, wireless=self.wireless)
            )

    def probe_stage_switch(self, field: str) -> dict:
        """Prove the stage-switch mechanism live, as far as reads can prove it.

        The profile declaring the stage field writable is the author's claim;
        this probe is the runtime half of the gate (the profile is the other
        half -- a profile may refuse, and the Pulsar one does): switch to a
        different stage, verify the readback moved, verify nothing else in the
        block changed, then switch back. It runs once per mouse+firmware and
        the CLI cached its verdict under ~/.cache (both verdicts, positive and
        negative), so the mouse's LED-staged write only ever happens on first
        contact.

        Known limit, and the reason the probe alone is not trusted: a readback
        check cannot see the cursor. A firmware that stores the byte and
        ignores it reads back identically to one that applies it. What the
        probe catches is the cruder failure modes -- firmware that refuses the
        write outright, or that damages the stage table on the way past -- and
        its `reason` says exactly what was checked.

        The revert runs in a `finally`: whatever the probe concludes about the
        mechanism, the mouse is left on the stage its user chose, or the
        result says it could not be.
        """
        bounds = self.profile.stage_range(field)
        if bounds is None:
            return {
                "capable": False,
                "reason": "the profile declares no usable stage range",
                "reverted": True,
                "stages": 0,
            }
        lo, hi = bounds
        current = self.get(field)
        if not isinstance(current, int) or not lo <= current <= hi:
            return {
                "capable": None,
                "reason": f"active stage reads {current!r}, "
                          f"outside the profile's {lo}..{hi} range",
                "reverted": True,
                "stages": hi - lo + 1,
            }

        command = self.profile.field_command(field)
        spec = self.profile.field(field)
        stage_offset = self.profile.response_offset(command, spec)
        # The switch invariant is "the block did not change beyond the stage
        # byte". Stating it against one baseline read is too strict, though:
        # this firmware carries live telemetry inside the block's tail
        # (observed on hardware: byte 56 flips 00/ff with no exchange in
        # between, twice in a row). So read the block twice *before* touching
        # anything, and any byte that already disagrees with itself is
        # telemetry by observation -- excluded from the invariant rather than
        # compared against a value it can never satisfy.
        before = self._read(command)
        baseline = self._read(command)
        unstable = {
            i
            for i in range(min(len(before), len(baseline)))
            if before[i] != baseline[i]
        } | {stage_offset}

        target = current + 1 if current < hi else lo
        result = {"capable": True, "reason": "", "reverted": False, "stages": hi - lo + 1}
        try:
            try:
                self.set(field, target)
            except (OSError, ProtocolError) as exc:
                # An exchange that failed outright is *transient* evidence --
                # a sleeping mouse or a mid-probe cable pull. Verdict null:
                # nothing is cached, the next open asks again.
                result["capable"] = None
                result["reason"] = f"the stage write did not land: {exc}"
                return result
            stage_after = self.get(field)
            if stage_after != target:
                result["capable"] = False
                result["reason"] = (
                    f"write of stage {target} read back as {stage_after!r}; "
                    "the firmware does not keep the stage it was given"
                )
                return result
            after = self._read(command)
            mutated = [
                i
                for i in range(min(len(before), len(after)))
                if i not in unstable and before[i] != after[i]
            ]
            if mutated:
                # A stage switch that rewrites neighbours may even put them
                # back -- but a mechanism that mutates the table it lives in
                # is not proven trustworthy, so the switcher stays hidden.
                result["capable"] = False
                result["reason"] = (
                    "switching stages also changed block bytes "
                    + ", ".join(str(i) for i in mutated)
                    + "; refusing a switcher built on that"
                )
                return result
        finally:
            try:
                self.set(field, current)
                back = self.get(field)
                result["reverted"] = back == current
            except (OSError, ProtocolError) as exc:
                result["revertError"] = str(exc)
        if not result["reverted"]:
            result["reason"] = result["reason"] or "the original stage could not be restored"
        return result


def _new_session(profile: Profile, info: HidrawInfo, identified: bool = True) -> Session:
    return Session(
        profile, info,
        wireless=bool(profile.transport.get("defaultWireless")),
        identified=identified,
    )


def _no_node_error(profile: Profile) -> str:
    """The message for an empty candidate list.

    With a missing (or stale) udev rule every /dev/hidraw node opens with
    EACCES, describe() returns None for all of them, and the candidate list
    comes back empty -- indistinguishable from an unplugged dongle without a
    second look. The second look is sysfs: if a node whose ids match the
    profile is visible there, the dongle is present and the real failure is
    access -- so the message says what is plugged in and names the rule,
    which is what the panel's permission card keys off (Model.js
    isPermissionError matches "udev"+"hidraw" in the text).
    """
    match = profile.match if profile else {}
    want_vids = {str(v).lower() for v in match.get("vendorIds") or []}
    for device in sysfs_hid_devices():
        if want_vids and device["vid"] in want_vids:
            return (
                "The mouse looks plugged in -- sysfs shows a HID node "
                f"{device['vid']}:{device['pid']} at {device['node']} -- but the node "
                "cannot be opened read-write, almost always the missing udev rule. "
                "Run the plugin's `install.sh --udev` (or use the panel's fix button); "
                "then unplug and replug the mouse or its dongle."
            )
    return (
        "No candidate HID node found. Is the mouse plugged in (or its dongle)? "
        "Run `hskctl probe` to see every HID device on the system."
    )


def open_session(profile: Profile, path: str | None = None, lock: bool = True) -> Session:
    # Every caller that reaches the device goes through here, so this is the
    # one place the lock has to be taken. `lock=False` is for a serve process:
    # it scopes the lock around each request with `device_lock`, including the
    # ones that build a session, rather than holding the fence for the life
    # of the panel.
    if lock:
        acquire_device_lock()
    if path:
        from .hidraw import describe

        info = describe(path)
        if info is None:
            raise DeviceNotFound(f"{path} is not a readable hidraw node")
        # An explicit path is an instruction, not a suggestion -- do not go
        # looking elsewhere when someone has named a node. It is not a licence
        # to write to it, though: if it does not match the profile, reads work
        # and writes need --force-unmatched.
        matched = [c for c in rank_candidates(profile) if c.info.path == path and c.identified]
        return _new_session(profile, info, identified=bool(matched))

    candidates = rank_candidates(profile)
    if not candidates:
        raise DeviceNotFound(_no_node_error(profile))
    best = candidates[0]
    if not profile.discovered:
        raise DeviceNotFound(
            f"Best candidate is {best.info.path} ({best.info.vidpid}, {best.info.name!r}), "
            f"but the profile for {profile.model} has no protocol mapped yet, so "
            f"hskctl will not talk to it. Run `hskctl probe --json` and follow "
            f"docs/contributor/PROTOCOL-DISCOVERY.md."
        )

    # Only nodes that match what the profile declares are eligible for
    # automatic selection. Ranking is a heuristic -- a device with a vendor
    # usage page and a feature report scores 35 here without matching a single
    # declared id -- and "best of whatever is plugged in" is not a basis for
    # sending vendor commands to somebody's hardware.
    eligible = [c for c in candidates if c.identified]
    if not eligible:
        listing = "\n".join(
            f"  {c.info.path}  {c.info.vidpid}  {c.info.name!r}\n"
            f"      {'; '.join(c.unmatched)}"
            for c in candidates[:5]
        )
        raise DeviceNotFound(
            f"No HID node matches the {profile.model} profile. The closest "
            f"were:\n{listing}\n\n"
            f"hskctl will not pick one of these on its own -- a vendor feature "
            f"report sent to the wrong device can leave it unusable. If you "
            f"know which node it is, name it with --device; `hskctl probe` "
            f"lists every candidate and why each one was rejected."
        )
    candidates = eligible
    best = candidates[0]

    if not profile.transport.get("linkProbe"):
        return _new_session(profile, best.info)

    # Ranking is static: it reads descriptors, so it cannot tell a dongle whose
    # mouse is awake from one whose mouse has just moved onto the cable and is
    # now a different node entirely. Both look identical, and the second
    # answers every command with zeros. So take the highest-scoring endpoint
    # that actually *replies with data*, not simply the highest-scoring one.
    failures: list[str] = []
    for candidate in candidates:
        session = _new_session(profile, candidate.info)
        try:
            session.detect_link()
            return session
        except (OSError, ProtocolError) as exc:
            failures.append(f"  {candidate.info.path} ({candidate.info.vidpid}): {exc}")

    raise DeviceNotFound(
        "Found candidate nodes, but none of them answered with any data:\n"
        + "\n".join(failures)
        + "\n\nThe mouse may be asleep -- move it and try again. If it is on the "
        "cable, check the cable carries data rather than only power. "
        "`hskctl doctor` shows the raw exchange with every candidate."
    )
