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
    # Which link we currently believe the mouse is on. Corrected automatically
    # on the first exchange that gets no acknowledgement.
    wireless: bool = False

    # -- low level -----------------------------------------------------------

    def _exchange(self, packet: bytes) -> bytes:
        t = self.profile.transport
        read_len = t.get("readLength") or t.get("packetLength")
        report_id = t.get("reportId") or 0
        with HidrawDevice(self.info.path) as dev:
            if t.get("kind") == "output":
                dev.write_output(packet)
                time.sleep(t.get("settleMs", 30) / 1000.0)
                reply = dev.read_input(read_len, timeout=1.5)
                return reply or b""
            dev.set_feature(packet)
            time.sleep(t.get("settleMs", 30) / 1000.0)
            return dev.get_feature(report_id, read_len)

    def _describe_reply(self, reply: bytes) -> str:
        if not reply:
            return "no reply at all"
        if len(reply) < 2:
            return f"a {len(reply)}-byte reply"
        return f"byte 1 = {reply[1]:#04x}, expected 0xa1"

    def _exchange_checked(self, command: str, packet: bytes) -> bytes:
        """Send a request, and retry once on the other link flag.

        Byte 4 tells the firmware whether it is being addressed over the cable
        or the dongle, and it stays silent when that is wrong. Rather than make
        the user declare which link they are on, we try the current guess and
        flip it once -- then remember which one worked.
        """
        reply = self._exchange(packet)
        if self.profile.check_ack(reply):
            return reply
        if self.profile.transport.get("wirelessFlagOffset") is None:
            raise ProtocolError(
                f"the mouse did not acknowledge {command!r} ({self._describe_reply(reply)})"
            )
        self.wireless = not self.wireless
        # Reuse the original packet and flip only the link flag, so a write
        # keeps its payload.
        flag = self.profile.transport["wirelessFlagOffset"]
        flipped = bytearray(packet)
        flipped[flag] = 1 if self.wireless else 0
        self.profile.checksum(flipped)
        reply = self._exchange(bytes(flipped))
        if self.profile.check_ack(reply):
            return reply
        self.wireless = not self.wireless  # neither worked; leave the guess as it was
        raise ProtocolError(
            f"the mouse did not acknowledge {command!r} on either link "
            f"({self._describe_reply(reply)}). Is this the config endpoint? "
            f"Try `hskctl probe`."
        )

    def _read(self, command: str) -> bytes:
        packet = self.profile.build_request(command, write=False, wireless=self.wireless)
        return self._exchange_checked(command, packet)

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

    def set(self, name: str, value: Any) -> None:
        """Write one setting.

        There is no read-modify-write here because there is nothing to merge:
        each setting is its own command carrying only its own value. The write
        template comes straight from the vendor firmware's own command table.
        """
        if not self.profile.field_writable(name):
            raise ProtocolError(f"{name!r} is read-only on this device")
        command = self.profile.field_command(name)
        payload = self.profile.encode_value(name, value)
        packet = self.profile.build_request(
            command, write=True, value_bytes=payload, wireless=self.wireless
        )
        self._exchange_checked(command, packet)
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
