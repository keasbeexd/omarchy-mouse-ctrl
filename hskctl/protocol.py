"""Declarative protocol engine.

The whole point of this module: the *shape* of the G-Wolves config protocol is
not yet known, but the machinery for speaking a vendor HID protocol is always
the same -- build a packet, checksum it, send it as a Feature or Output report,
read a reply, slice fields out of it.

So all the device-specific knowledge lives in a JSON profile under profiles/,
and this module is a generic interpreter for that JSON. When the capture work
in docs/PROTOCOL-DISCOVERY.md yields real byte layouts, you edit the profile,
not this file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

PROFILE_SEARCH_PATHS = [
    os.path.expanduser("~/.config/hskctl/profiles"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profiles"),
    "/usr/share/hskctl/profiles",
]


class ProtocolError(Exception):
    pass


class NotDiscovered(ProtocolError):
    """Raised when a profile declares a field it does not yet know how to encode."""

    def __init__(self, field: str):
        self.field = field
        super().__init__(
            f"'{field}' is not mapped yet in this device profile. "
            f"Run a capture (see docs/PROTOCOL-DISCOVERY.md) and fill in "
            f"profiles/*.json, then re-run."
        )


# --- checksums --------------------------------------------------------------
# Vendor mice overwhelmingly use one of these four. The profile names which.


def _sum8(data: bytes) -> int:
    return sum(data) & 0xFF


def _sum8_complement(data: bytes) -> int:
    return (0x100 - (sum(data) & 0xFF)) & 0xFF


def _xor8(data: bytes) -> int:
    acc = 0
    for b in data:
        acc ^= b
    return acc


def _sum8_minus(data: bytes) -> int:
    # Seen on several Chinese vendor protocols: 0x55 - sum, truncated.
    return (0x55 - (sum(data) & 0xFF)) & 0xFF


CHECKSUMS = {
    "sum8": _sum8,
    "sum8_complement": _sum8_complement,
    "xor8": _xor8,
    "sum8_minus_55": _sum8_minus,
    "none": None,
}


# --- field codecs -----------------------------------------------------------


def _decode_scalar(buf: bytes, spec: dict) -> Any:
    enc = spec.get("encoding", "u8")
    off = spec["offset"]
    if enc == "u8":
        raw = buf[off]
    elif enc == "u16le":
        raw = int.from_bytes(buf[off : off + 2], "little")
    elif enc == "u16be":
        raw = int.from_bytes(buf[off : off + 2], "big")
    elif enc == "bit":
        raw = (buf[off] >> spec.get("bit", 0)) & 0x01
    elif enc == "nibble_low":
        raw = buf[off] & 0x0F
    elif enc == "nibble_high":
        raw = (buf[off] >> 4) & 0x0F
    elif enc == "rgb":
        return "#%02x%02x%02x" % (buf[off], buf[off + 1], buf[off + 2])
    elif enc == "version3":
        # Three bytes of major.minor.patch, as the firmware reports it.
        return "%d.%d.%d" % (buf[off], buf[off + 1], buf[off + 2])
    else:
        raise ProtocolError(f"unknown encoding {enc!r}")

    # A divider register: the firmware stores a divisor of a fixed base clock,
    # so the friendly value is base/raw rather than a table lookup. Raw 0 is
    # treated as 1 -- the hardware clamps it to full rate.
    base = spec.get("dividerBase")
    if base:
        return int(round(base / max(raw, 1)))

    scale = spec.get("scale")
    if scale:
        raw = raw * scale
    offset_add = spec.get("add")
    if offset_add:
        raw = raw + offset_add

    values = spec.get("values")
    if values:
        # values maps wire value (as string key) -> friendly value
        return values.get(str(raw), raw)
    if spec.get("type") == "bool":
        return bool(raw)
    return raw


def _encode_scalar(buf: bytearray, spec: dict, value: Any) -> None:
    values = spec.get("values")
    if values:
        inverse = {str(v): int(k) for k, v in values.items()}
        key = str(value)
        if key not in inverse:
            raise ProtocolError(
                f"{value!r} is not one of the allowed values: "
                f"{', '.join(sorted(inverse, key=str))}"
            )
        raw = inverse[key]
    elif spec.get("type") == "bool":
        # Boolean fields carry no enum, so accept whatever the caller has:
        # a real bool, 0/1, or the on/off words the CLI and panel use.
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("on", "true", "yes", "1", "enabled"):
                raw = 1
            elif lowered in ("off", "false", "no", "0", "disabled"):
                raw = 0
            else:
                raise ProtocolError(f"{value!r} is not a valid on/off value")
        else:
            raw = 1 if value else 0
    elif spec.get("encoding") == "rgb":
        text = str(value).lstrip("#")
        if len(text) != 6:
            raise ProtocolError(f"{value!r} is not a #rrggbb colour")
        try:
            raw = bytes.fromhex(text)
        except ValueError as exc:
            raise ProtocolError(f"{value!r} is not a #rrggbb colour") from exc
    elif spec.get("dividerBase"):
        base = spec["dividerBase"]
        wanted = int(value)
        if wanted <= 0 or base % wanted:
            allowed = sorted({base // d for d in range(1, 17)}, reverse=True)
            raise ProtocolError(
                f"{value} is not reachable: this register divides a {base} Hz base, "
                f"so only exact divisors work ({', '.join(str(a) for a in allowed[:8])}, ...)"
            )
        raw = base // wanted
    else:
        raw = int(value)
        offset_add = spec.get("add")
        if offset_add:
            raw = raw - offset_add
        scale = spec.get("scale")
        if scale:
            if raw % scale:
                raise ProtocolError(f"{value} is not a multiple of {scale}")
            raw = raw // scale
        lo = spec.get("min")
        hi = spec.get("max")
        if lo is not None and int(value) < lo:
            raise ProtocolError(f"{value} is below the minimum of {lo}")
        if hi is not None and int(value) > hi:
            raise ProtocolError(f"{value} is above the maximum of {hi}")

    enc = spec.get("encoding", "u8")
    off = spec["offset"]
    if enc == "u8":
        buf[off] = raw & 0xFF
    elif enc == "u16le":
        buf[off : off + 2] = (raw & 0xFFFF).to_bytes(2, "little")
    elif enc == "u16be":
        buf[off : off + 2] = (raw & 0xFFFF).to_bytes(2, "big")
    elif enc == "bit":
        bit = spec.get("bit", 0)
        if raw:
            buf[off] |= 1 << bit
        else:
            buf[off] &= ~(1 << bit) & 0xFF
    elif enc == "nibble_low":
        buf[off] = (buf[off] & 0xF0) | (raw & 0x0F)
    elif enc == "nibble_high":
        buf[off] = (buf[off] & 0x0F) | ((raw & 0x0F) << 4)
    elif enc == "rgb":
        buf[off : off + 3] = bytes(raw)
    else:
        raise ProtocolError(f"unknown encoding {enc!r}")


def parse_template(template: str, length: int) -> bytearray:
    """'08 04 00 ..' -> a zero-padded packet. '..' means 'leave as zero'."""
    buf = bytearray(length)
    tokens = template.split()
    for i, tok in enumerate(tokens):
        if i >= length:
            break
        if tok in ("..", "__", "xx"):
            continue
        buf[i] = int(tok, 16)
    return buf


@dataclass
class Profile:
    data: dict
    path: str

    @property
    def model(self) -> str:
        return self.data.get("model", "unknown device")

    @property
    def status(self) -> str:
        return self.data.get("status", "undiscovered")

    @property
    def discovered(self) -> bool:
        return self.status in ("partial", "verified")

    @property
    def transport(self) -> dict:
        return self.data.get("transport", {})

    @property
    def match(self) -> dict:
        return self.data.get("match", {})

    def field(self, name: str) -> dict:
        fields = self.data.get("fields", {})
        spec = fields.get(name)
        if not isinstance(spec, dict):
            raise NotDiscovered(name)
        # A field is mapped once the command carrying it is known. The byte
        # offset is usually inherited from the command (the firmware always
        # answers at the same place), so an explicit offset is optional.
        command = spec.get("command") or spec.get("from")
        if not command or not self.has_command(command):
            raise NotDiscovered(name)
        return spec

    def has_field(self, name: str) -> bool:
        try:
            self.field(name)
            return True
        except NotDiscovered:
            return False

    def field_command(self, name: str) -> str:
        spec = self.field(name)
        return spec.get("command") or spec["from"]

    def field_writable(self, name: str) -> bool:
        """Can this field be written -- and should it be?

        `_needsVerification` marks a mapping nobody has confirmed against real
        hardware. Writing through one is exactly the class of blind write that
        corrupted a mouse earlier in this project, so the marker now closes the
        write path rather than merely printing a warning nobody reads. The
        profile shipped `dpiStageCount` and the sleep timer as writable while
        declaring their mappings unverified, which is a contradiction the code
        should not have allowed to exist.

        Verify it on hardware and delete the marker. That is the only route.
        """
        if not self.has_field(name):
            return False
        spec = self.data["fields"][name]
        if spec.get("readOnly"):
            return False
        if spec.get("_needsVerification"):
            return False
        return self.can_write(self.field_command(name))

    def command(self, name: str) -> dict:
        cmds = self.data.get("commands", {})
        spec = cmds.get(name)
        if not spec or not (spec.get("get") or spec.get("set") or spec.get("request")):
            raise NotDiscovered(f"command:{name}")
        return spec

    def has_command(self, name: str) -> bool:
        spec = self.data.get("commands", {}).get(name)
        if not isinstance(spec, dict):
            return False
        return bool(spec.get("get") or spec.get("set") or spec.get("request"))

    def can_write(self, name: str) -> bool:
        spec = self.data.get("commands", {}).get(name) or {}
        return bool(spec.get("set"))

    # -- packet construction for the command-per-setting protocol -------------
    #
    # The HSK firmware does not expose one big settings block. Each setting is
    # its own command: a 65-byte Feature report whose byte 3 is the opcode,
    # with the read opcode being the write opcode + 0x80. The value rides in
    # one field of the request and comes back in one field of the reply.

    def build_request(
        self,
        command_name: str,
        write: bool,
        value_bytes: bytes = b"",
        wireless: bool = False,
    ) -> bytes:
        cmd = self.command(command_name)
        length = self.transport.get("packetLength", 65)
        template = cmd.get("set" if write else "get") or cmd.get("request")
        if not template:
            raise NotDiscovered(f"command:{command_name}:{'set' if write else 'get'}")
        buf = parse_template(template, length)

        # Byte 4 flags a wireless link -- but only for the commands whose vendor
        # function actually sets it. `hts_get_connect_state` and
        # `hts_get_set_sleep` have no is_wireless block at all, and for sleep
        # byte 4 carries part of the sub-command, so writing a link flag there
        # corrupts the packet. Commands opt out with "linkFlag": false.
        flag_offset = self.transport.get("wirelessFlagOffset")
        if flag_offset is not None and wireless and cmd.get("linkFlag", True):
            buf[flag_offset] = 1

        if write and value_bytes:
            offset = cmd.get("valueOffset", 5)
            buf[offset : offset + len(value_bytes)] = value_bytes

        return bytes(self.checksum(buf))

    def check_ack(self, reply: bytes) -> bool:
        """The firmware answers every accepted command with 0xA1 at byte 1."""
        ack = self.transport.get("ack")
        if not ack or not reply:
            return bool(reply)
        offset = ack.get("offset", 1)
        if offset >= len(reply):
            return False
        return reply[offset] == ack.get("value", 0xA1)

    def response_offset(self, command_name: str, field_spec: dict) -> int:
        if field_spec.get("offset") is not None:
            return field_spec["offset"]
        cmd = self.data.get("commands", {}).get(command_name) or {}
        return cmd.get("responseOffset", 5)

    def checksum(self, buf: bytearray) -> bytearray:
        spec = self.transport.get("checksum") or {}
        kind = spec.get("kind", "none")
        fn = CHECKSUMS.get(kind)
        if fn is None:
            return buf
        start, end = spec.get("range", [1, len(buf) - 1])
        buf[spec["offset"]] = fn(bytes(buf[start:end]))
        return buf

    def build(self, command_name: str, values: dict | None = None) -> bytes:
        cmd = self.command(command_name)
        length = self.transport.get("packetLength", 65)
        buf = parse_template(cmd["request"], length)
        for key, value in (values or {}).items():
            spec = cmd.get("params", {}).get(key)
            if spec is None:
                raise ProtocolError(f"command {command_name!r} takes no parameter {key!r}")
            _encode_scalar(buf, spec, value)
        return bytes(self.checksum(buf))

    def decode(self, name: str, buf: bytes) -> Any:
        spec = dict(self.field(name))
        spec["offset"] = self.response_offset(self.field_command(name), spec)
        return _decode_scalar(buf, spec)

    def encode_into(self, buf: bytearray, name: str, value: Any) -> bytearray:
        _encode_scalar(buf, self.field(name), value)
        return buf

    def encode_value(self, name: str, value: Any) -> bytes:
        """Encode one field's value as the bytes that ride in a set request."""
        spec = dict(self.field(name))
        width = {"u8": 1, "u16le": 2, "u16be": 2}.get(spec.get("encoding", "u8"), 1)
        scratch = bytearray(width)
        spec["offset"] = 0
        _encode_scalar(scratch, spec, value)
        return bytes(scratch)

    def allowed(self, name: str) -> list | None:
        """The set of legal values for a field, for CLI help and the panel UI."""
        spec = self.data.get("fields", {}).get(name) or {}
        values = spec.get("values")
        if values:
            return list(values.values())
        base = spec.get("dividerBase")
        if base:
            return sorted({base // d for d in range(1, 9)}, reverse=True)
        if spec.get("min") is not None and spec.get("max") is not None:
            return [spec["min"], spec["max"]]
        return None


def load_profile(name: str | None = None) -> Profile:
    filename = f"{name}.json" if name else "gwolves-hsk-pro-4k.json"
    for directory in PROFILE_SEARCH_PATHS:
        candidate = os.path.join(directory, filename)
        if os.path.exists(candidate):
            with open(candidate, "r", encoding="utf-8") as fh:
                return Profile(json.load(fh), candidate)
    raise ProtocolError(
        f"no profile named {filename!r} found in: {', '.join(PROFILE_SEARCH_PATHS)}"
    )


def list_profiles() -> list[str]:
    seen = []
    for directory in PROFILE_SEARCH_PATHS:
        if not os.path.isdir(directory):
            continue
        for entry in sorted(os.listdir(directory)):
            if entry.endswith(".json") and entry[:-5] not in seen:
                seen.append(entry[:-5])
    return seen
