"""Declarative protocol engine.

The whole point of this module: no two vendors' config protocols look alike,
but the machinery for speaking a vendor HID protocol is always the same --
build a packet, checksum it, send it as a Feature or Output report, read a
reply, slice fields out of it.

So all the device-specific knowledge lives in a JSON profile under profiles/,
and this module is a generic interpreter for that JSON. When the capture work
in docs/contributor/PROTOCOL-DISCOVERY.md yields real byte layouts, you edit the profile,
not this file.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from .safeio import read_bounded_json

# Bundled profiles first, so the protocol this plugin shipped with (and was
# reviewed with) is the one that drives writes to the mouse. A same-named file
# under ~/.config can no longer shadow it; the user directory only adds new
# profile names, for someone mapping a mouse this plugin does not ship.
PROFILE_SEARCH_PATHS = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profiles"),
    "/usr/share/hskctl/profiles",
    os.path.expanduser("~/.config/hskctl/profiles"),
]

# The largest shipped profile is ~21 KiB. A profile decides which bytes are
# written to the mouse, so it is read like any other untrusted file: one
# O_NOFOLLOW descriptor, validated, capped.
_MAX_PROFILE_BYTES = 262144
# Bounds `list_profiles`, which runs on every `status` without --profile.
_MAX_PROFILES_PER_DIR = 64
_PROFILE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class ProtocolError(Exception):
    pass


class NotDiscovered(ProtocolError):
    """Raised when a profile declares a field it does not yet know how to encode."""

    def __init__(self, field: str):
        self.field = field
        super().__init__(
            f"'{field}' is not mapped yet in this device profile. "
            f"Run a capture (see docs/contributor/PROTOCOL-DISCOVERY.md) and fill in "
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


def _rot2(nibble: int) -> int:
    """Rotate a 4-bit value left by 2. Self-inverse.

    Pulsar's Nordic DPI record packs a 2-bit mode and a 2-bit page into one
    nibble, rotated so the common case (mode 0, page 0) round-trips through
    a value that looks like noise rather than 0 -- this exists only to match
    the firmware's own bit layout, confirmed against
    packerlschupfer/pulsar-mouse-linux's drivers/nordic.py.
    """
    return ((nibble << 2) | (nibble >> 2)) & 0x0F


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
    elif enc == "version2":
        # Two bytes of major.minor, as some firmwares report it.
        return "%d.%d" % (buf[off], buf[off + 1])
    elif enc == "nordicDpi3":
        # Pulsar's Nordic DPI record: [lo, lo, high] where `high`'s low
        # nibble rotates a 2-bit mode and 2-bit page. Mode selects a
        # (multiplier, base, ceiling) row from the field's `dpiModes` list
        # (single-mode devices, the only ones mapped so far, have exactly
        # one row); page extends `lo` past 8 bits. Confirmed against
        # packerlschupfer/pulsar-mouse-linux's drivers/nordic.py
        # _raw_to_dpi/_dpi_to_raw.
        step = spec.get("step", 50)
        modes = spec.get("dpiModes") or [[0, 1, 1, None]]
        high = _rot2(buf[off + 2] & 0x0F)
        mode, page = high >> 2, high & 0x03
        for m, mult, mbase, _limit in modes:
            if m == mode:
                break
        else:
            # Unknown mode: fall back to the first row rather than invent a
            # number -- this only happens against a device we have not
            # actually mapped that mode for yet.
            _m, mult, mbase, _limit = modes[0]
        return (buf[off] + mbase + 256 * page) * step * mult
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
            raise ProtocolError(f"{value!r} is not a #rrggbb color")
        try:
            raw = bytes.fromhex(text)
        except ValueError as exc:
            raise ProtocolError(f"{value!r} is not a #rrggbb color") from exc
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
    elif spec.get("encoding") == "nordicDpi3":
        # Inverse of the nordicDpi3 decode above -- see its comment there for
        # the bit layout. `modes` picks the coarsest row whose ceiling still
        # covers this DPI (None = "everything above"), the same rule
        # _dpi_to_raw uses, so a value above the finest mode's range doesn't
        # silently wrap.
        step = spec.get("step", 50)
        modes = spec.get("dpiModes") or [[0, 1, 1, None]]
        dpi = int(value)
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and dpi < lo:
            raise ProtocolError(f"{value} is below the minimum of {lo}")
        if hi is not None and dpi > hi:
            raise ProtocolError(f"{value} is above the maximum of {hi}")
        for mode, mult, mbase, limit in modes:
            if limit is None or dpi <= limit:
                break
        unit = step * mult
        index = round(dpi / unit) - mbase
        if not 0 <= index <= 1023:
            raise ProtocolError(f"{dpi} DPI is out of range for this device")
        page, low = divmod(index, 256)
        nib = _rot2((mode << 2) | page)
        raw = bytes([low, low, (nib << 4) | nib])
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
    elif enc == "nordicDpi3":
        buf[off : off + 3] = raw
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


def _bounded_length(transport: dict, key: str, default: int) -> int:
    length = transport.get(key, default)
    if isinstance(length, bool) or not isinstance(length, int) or not 2 <= length <= 4096:
        raise ProtocolError(f"profile {key} {length!r} is outside 2..4096")
    return length


@dataclass
class Profile:
    data: dict
    path: str

    @property
    def model(self) -> str:
        return self.data.get("model", "unknown device")

    @property
    def name(self) -> str:
        """The profile's own name -- the file stem, e.g. `gwolves-hsk-pro-4k`.

        Same name `list_profiles` reports and the one a user profile is picked
        by, so a cache key derived from it identifies exactly which profile
        vetted a given verdict.
        """
        return os.path.splitext(os.path.basename(self.path))[0]

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
    def packet_length(self) -> int:
        """The transport's packet size, refused outside a sane range.

        A profile can come from ~/.config, and this number sizes a buffer
        and an ioctl -- so it is checked like any other input, not trusted.
        """
        return _bounded_length(self.transport, "packetLength", 65)

    @property
    def read_length(self) -> int:
        """Bytes to read back per exchange; packetLength unless set apart."""
        if self.transport.get("readLength"):
            return _bounded_length(self.transport, "readLength", 65)
        return self.packet_length

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
        length = self.packet_length
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

            # Some protocols (Pulsar's Nordic memory map) checksum each
            # written value on its own, immediately after it -- separate from
            # the whole-packet checksum below, and required for the firmware
            # to accept the write at all. "valueChecksum" names one of the
            # functions in CHECKSUMS to run over just `value_bytes`.
            value_checksum = cmd.get("valueChecksum")
            if value_checksum:
                fn = CHECKSUMS.get(value_checksum)
                if fn is None:
                    raise ProtocolError(f"unknown valueChecksum kind {value_checksum!r}")
                csum_offset = offset + len(value_bytes)
                buf[csum_offset] = fn(bytes(value_bytes))

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
        length = self.packet_length
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
        width = {
            "u8": 1, "u16le": 2, "u16be": 2, "rgb": 3, "nordicDpi3": 3,
        }.get(spec.get("encoding", "u8"), 1)
        scratch = bytearray(width)
        spec["offset"] = 0
        _encode_scalar(scratch, spec, value)
        return bytes(scratch)

    def allowed(self, name: str) -> list | None:
        """The set of legal values for a field, for CLI help and the panel UI.

        `uiOptions` -- when a profile gives one -- narrows this to the values
        worth offering as a button/selector, without touching what `set`
        itself accepts. The G-Wolves polling register is the reason this
        exists: `values` maps every raw byte the hardware was measured to
        answer, including 333/200/167 Hz divisor artifacts nobody vendor-side
        exposes as a selectable rate. Dumping all eight into an unwrapped
        button row pushed 4000 Hz off the edge of the panel -- so the panel
        gets the curated five, while `hskctl set pollingRate 200` still works
        because encoding still goes through the full `values` map below.
        """
        spec = self.data.get("fields", {}).get(name) or {}
        curated = spec.get("uiOptions")
        if curated:
            return list(curated)
        values = spec.get("values")
        if values:
            return list(values.values())
        base = spec.get("dividerBase")
        if base:
            return sorted({base // d for d in range(1, 9)}, reverse=True)
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and hi is not None:
            if isinstance(lo, int) and isinstance(hi, int) and 0 <= hi - lo <= 16:
                # A small integer range enumerates one value per step: these
                # become chips in the panel (see `_derive_widget`), and chips
                # need every value, not the two endpoints a range would
                # otherwise pass. The cap matches the derivation rule exactly
                # -- the same condition sends the same field the same way.
                return list(range(lo, hi + 1))
            return [lo, hi]
        return None

    # -- presentation metadata ------------------------------------------------
    #
    # The panel used to hardcode which fields exist and what they look like:
    # one QML file knew about motionSync and angleSnap by name, and a profile
    # for a mouse with a different sensor vocabulary could not add a row
    # without a code change. The protocol engine is data-driven, and now the
    # UI on top of it is too.
    #
    # Metadata is authored inseparably from the bytes it describes -- a label
    # that survives an offset edit belongs to a different setting -- so it
    # rides in the same field spec, under `ui`, and never in a side file.

    _UI_WIDGETS = ("toggle", "chips", "slider", "color", "info")

    def field_ui(self, name: str) -> dict | None:
        """Validated presentation metadata for one field, or None.

        The `ui` block inside the field spec is the author's say, and every
        piece of it is checked here rather than trusted: strings are
        non-empty and capped, the widget override must name a control the
        panel actually has, and an invalid piece is dropped rather than
        warped into shape -- an unknown widget value falls back to the
        derived control, which cannot be wrong.
        """
        if not self.has_field(name):
            return None
        spec = self.data["fields"][name]
        raw = spec.get("ui")
        if not isinstance(raw, dict):
            raw = {}
        meta: dict[str, Any] = {}
        label = _clean_text(raw.get("label"), 40)
        if label:
            meta["label"] = label
        description = _clean_text(raw.get("description"), 120)
        if description:
            meta["description"] = description
        unit = _clean_text(raw.get("unit"), 8)
        if unit:
            meta["unit"] = unit
        widget = raw.get("widget")
        meta["widget"] = widget if widget in self._UI_WIDGETS else _derive_widget(spec)
        step = spec.get("step")
        if isinstance(step, bool) or not isinstance(step, int) or not 0 < step <= 10000:
            step = None
        if step is not None:
            meta["step"] = step
        return meta

    def ui_sections(self) -> list[dict]:
        """The profile's own panel layout: ordered sections of field names.

        A section renders only what is mapped: names pointing at fields this
        profile does not actually implement (a `_comment` typo, an
        aspiration, a paste from another vendor's file) are dropped here, so
        the layout can never volunteer a control the CLI would refuse.
        Everything is capped the way `allowed` is capped -- a profile decides
        which bytes are written to the mouse, so it is validated like any
        other untrusted file, and this list sizes an entire QML tree.
        """
        ui = self.data.get("ui")
        if not isinstance(ui, dict):
            return []
        raw = ui.get("sections")
        if not isinstance(raw, list):
            return []
        fields = self.data.get("fields", {})
        out = []
        for section in raw:
            if len(out) >= 10:
                break
            if not isinstance(section, dict):
                continue
            title = _clean_text(section.get("title"), 24)
            if not title:
                continue
            names = section.get("fields")
            if not isinstance(names, list):
                continue
            clean: list[str] = []
            for name in names:
                if (
                    isinstance(name, str)
                    and not name.startswith("_")
                    and isinstance(fields.get(name), dict)
                    and self.has_field(name)
                    and name not in clean
                ):
                    clean.append(name)
                if len(clean) >= 16:
                    break
            if clean:
                out.append({"title": title, "fields": clean})
        return out

    def stage_switch_field(self) -> str | None:
        """The field that selects which DPI stage is live, if switching is on.

        Declared as `ui.stageSwitch` in a profile and checked hard, because
        this one field gets *written* by a verification probe at panel-open:
        `field_writable` must agree, the widget must be the chip row (a stage
        picked by dragging a thumb is the wrong tool), and an unverified
        mapping refuses as usual. `None` means the mouse gets no switcher --
        which is the correct answer until someone proves the mechanism.

        The Pulsar profile refuses precisely here: its `activeDpiStage`
        writes round-trip but change nothing live (see its `_followUp`), so
        the flag has no value. The G-Wolves profile declares it; the probe
        (see `Session.probe_stage_switch`) is the runtime half of the gate.
        """
        ui = self.data.get("ui")
        if not isinstance(ui, dict):
            return None
        switch = ui.get("stageSwitch")
        if not isinstance(switch, dict):
            return None
        field = switch.get("field")
        if not isinstance(field, str) or not field or field.startswith("_"):
            return None
        try:
            if not self.has_field(field) or not self.field_writable(field):
                return None
        except ProtocolError:
            return None
        meta = self.field_ui(field)
        if not meta or meta["widget"] != "chips":
            return None
        return field

    def stage_range(self, field: str) -> tuple[int, int] | None:
        """The integer range a stage field's chip row covers, if it has one."""
        spec = self.data.get("fields", {}).get(field) or {}
        lo, hi = spec.get("min"), spec.get("max")
        if isinstance(lo, int) and isinstance(hi, int) and 0 < lo < hi <= 16:
            return lo, hi
        return None


def _clean_text(value: Any, limit: int) -> str | None:
    """Non-empty string within a cap, or nothing -- the engine's whole rule.

    One of these runs on every string a panel will render straight from the
    profile, and a profile can come from ~/.config. Length-capping here means
    the QML needs no trust and can carry no truncation bugs.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def _derive_widget(spec: dict) -> str:
    """The natural control for a field, from the encoding it already has.

    A bool is a toggle, an enum is a pick-one row, a bounded scalar is a
    slider, a color is a color. The field's byte-level shape already decides
    what can be written, so it decides what is drawn -- `ui.widget` exists
    only to override this when the natural control is wrong.

    Read-only and unverified mappings derive `info` on purpose: `_needsVerification`
    closes the write path (see `field_writable`), and the panel renders only
    what can be written, so the honest widget for a setting the panel cannot
    offer is a readout -- and if the panel ever surfaces read-only
    diagnostics, `info` is the row that carries them.
    """
    if spec.get("readOnly") or spec.get("_needsVerification"):
        return "info"
    if spec.get("encoding") == "rgb":
        return "color"
    if spec.get("type") == "bool":
        return "toggle"
    if spec.get("values") or spec.get("uiOptions"):
        return "chips"
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None and hi is not None:
        if isinstance(lo, int) and isinstance(hi, int) and 0 <= hi - lo <= 16:
            # A small integer range is one chip per step, not a slider: the
            # G-Wolves DPI stages are 1..7, and dragging a thumb across a
            # 7-stop track is the worse tool for it. `allowed` enumerates the
            # range (below) so the panel gets every chip.
            return "chips"
        return "slider"
    return "info"


def _valid_profile_name(name: str) -> bool:
    return bool(_PROFILE_NAME_RE.fullmatch(name)) and ".." not in name


def load_profile(name: str | None = None) -> Profile:
    name = name or "gwolves-hsk-pro-4k"
    if not _valid_profile_name(name):
        raise ProtocolError(f"{name!r} is not a profile name (letters, digits, . _ -)")
    filename = f"{name}.json"
    for directory in PROFILE_SEARCH_PATHS:
        candidate = os.path.join(directory, filename)
        try:
            # Root may own /usr/share; nobody else may own any of them, and
            # nobody but the owner may be able to rewrite one.
            data = read_bounded_json(
                candidate,
                _MAX_PROFILE_BYTES,
                allowed_owners=(os.geteuid(), 0),
                reject_shared_write=True,
            )
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            raise ProtocolError(f"refusing profile {candidate}: {exc}") from exc
        return Profile(data, candidate)
    raise ProtocolError(
        f"no profile named {filename!r} found in: {', '.join(PROFILE_SEARCH_PATHS)}"
    )


def list_profiles() -> list[str]:
    seen = []
    for directory in PROFILE_SEARCH_PATHS:
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        found = []
        with entries:
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                name = entry.name[:-5]
                if not _valid_profile_name(name):
                    continue
                found.append(name)
                if len(found) >= _MAX_PROFILES_PER_DIR:
                    break
        for name in sorted(found):
            if name not in seen:
                seen.append(name)
    return seen
