"""Binding a profile to a real hidraw node, and reading/writing settings."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .hidraw import HidrawDevice, HidrawInfo, enumerate_devices
from .protocol import NotDiscovered, Profile, ProtocolError

# G-Wolves ships several USB bridge chips across revisions, so name matching is
# the fallback when the profile has no VID/PID yet. Deliberately broad -- this
# only selects *candidates* to show the user, it never triggers a write.
NAME_HINTS = ("g-wolves", "gwolves", "hsk", "wireless dongle", "compx", "vgn")


def is_vendor_usage_page(page: int | None) -> bool:
    return page is not None and 0xFF00 <= page <= 0xFFFF


@dataclass
class Candidate:
    info: HidrawInfo
    score: int
    reasons: list[str]

    def as_dict(self) -> dict:
        d = self.info.as_dict()
        d["score"] = self.score
        d["reasons"] = self.reasons
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
            reasons.append("device name looks like a G-Wolves node")

        if score > 0:
            results.append(Candidate(info, score, reasons))

    results.sort(key=lambda c: (-c.score, c.info.path))
    return results


class DeviceNotFound(ProtocolError):
    pass


@dataclass
class Session:
    profile: Profile
    info: HidrawInfo
    # Which link the mouse is on. Established by detect_link() before the first
    # exchange, because an ACK alone does not prove the flag was right.
    wireless: bool = False
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
        read_len = t.get("readLength") or t.get("packetLength")
        report_id = t.get("reportId") or 0
        settle = self._settle(command)
        with HidrawDevice(self.info.path) as dev:
            if t.get("kind") == "output":
                dev.write_output(packet)
                time.sleep(settle)
                reply = dev.read_input(read_len, timeout=1.5)
                return reply or b""
            dev.set_feature(packet)
            time.sleep(settle)
            return dev.get_feature(report_id, read_len)

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

    def detect_link(self) -> bool:
        """Ask the mouse which link it is on, and set the flag accordingly.

        This has to be explicit. The firmware acknowledges a packet carrying the
        *wrong* link flag -- it replies 0xA1 and echoes the header -- and then
        ignores it, answering with an all-zero payload. So an ACK is not proof
        the command was honoured, and retrying only on a missing ACK never
        corrects the flag: reads come back as zeros and writes vanish silently.

        `connection` is the one command that carries no link flag at all, so it
        answers the question regardless of which link we are on.
        """
        if self._link_detected:
            return self.wireless
        if not self.profile.has_command("connection"):
            self._link_detected = True
            return self.wireless

        spec = self.profile.data["commands"]["connection"]
        offset = spec.get("responseOffset", 5)
        packet = self.profile.build_request("connection", write=False)

        # Getting this wrong poisons everything after it: with the wrong flag
        # the firmware acknowledges and then ignores every command, so reads
        # come back as zeros and writes vanish. A mouse waking from sleep can
        # miss the first packet, so probe a few times before settling.
        for attempt in range(3):
            try:
                reply = self._exchange_checked("connection", packet)
            except (OSError, ProtocolError):
                time.sleep(0.08)
                continue
            if offset < len(reply) and any(reply[offset:]):
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
        out: dict[str, Any] = {}
        cache: dict[str, bytes] = {}
        fields = self.profile.data.get("fields", {})
        for name, spec in fields.items():
            if name.startswith("_") or not isinstance(spec, dict):
                continue
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

    def set_raw(self, name: str, raw: int) -> None:
        """Write a raw wire value, bypassing the friendly-value table.

        Needed to calibrate a mapping we do not know yet: you cannot ask for
        "4000 Hz" until you have established which byte means 4000 Hz, so the
        sweep has to address the register numerically.
        """
        if not self.profile.field_writable(name):
            raise ProtocolError(f"{name!r} is read-only on this device")
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

    def set(self, name: str, value: Any) -> None:
        """Write one setting.

        There is no read-modify-write here because there is nothing to merge:
        each setting is its own command carrying only its own value. The write
        template comes straight from the vendor firmware's own command table.
        """
        if not self.profile.field_writable(name):
            raise ProtocolError(f"{name!r} is read-only on this device")
        self.detect_link()
        command = self.profile.field_command(name)
        spec = self.profile.data["commands"][command]
        spec_field = self.profile.field(name)

        if spec.get("readModifyWrite"):
            # Commands that carry a whole block -- DPI carries seven stages and
            # their colours in one packet -- must not be synthesised from
            # nothing, or every field we have not decoded gets zeroed. Read the
            # mouse's own block, change one field in it, send it back.
            current = self._read(command)
            packet = bytearray(
                self.profile.build_request(command, write=True, wireless=self.wireless)
            )
            start, end = spec.get("payloadRange", [5, len(current)])
            end = min(end, len(current), len(packet))
            packet[start:end] = current[start:end]
            self.profile.encode_into(packet, name, value)
            # A linked field rides in the same packet. DPI has independent X and
            # Y axes, but the vendor app keeps them equal unless you explicitly
            # unlink them -- and a mouse whose axes disagree tracks wrong. One
            # write, both axes.
            linked = spec_field.get("linkedField")
            if linked and self.profile.has_field(linked):
                self.profile.encode_into(packet, linked, value)
            self.profile.checksum(packet)
            self._exchange_checked(command, bytes(packet))
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


def open_session(profile: Profile, path: str | None = None) -> Session:
    if path:
        from .hidraw import describe

        info = describe(path)
        if info is None:
            raise DeviceNotFound(f"{path} is not a readable hidraw node")
        return Session(profile, info)

    candidates = rank_candidates(profile)
    if not candidates:
        raise DeviceNotFound(
            "No candidate HID node found. Is the mouse plugged in (or its dongle)? "
            "Run `hskctl probe` to see every HID device on the system."
        )
    best = candidates[0]
    if not profile.discovered:
        raise DeviceNotFound(
            f"Best candidate is {best.info.path} ({best.info.vidpid}, {best.info.name!r}), "
            f"but the profile for {profile.model} has no protocol mapped yet, so "
            f"hskctl will not talk to it. Run `hskctl probe --json` and follow "
            f"docs/PROTOCOL-DISCOVERY.md."
        )
    return Session(profile, best.info)
