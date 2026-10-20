"""G-Wolves HTS family: the HSK Pro 4K and its siblings (vendor 33e4).

Recovered from G-Wolves_Software_V1.0.20.07's .NET binary (hts_send_cmd and
the HTS_*_CMD byte arrays), then confirmed on hardware in v2. The evidence
behind each verification flag is in docs/profiles/gwolves-hsk-pro-4k.md.

Wire format: 65-byte feature reports, report id 0, no checksum.
  byte 1  ACK in replies (0xA1)
  byte 2  payload length
  byte 3  opcode; read = write + 0x80
  byte 4  link flag (1 = addressed over the dongle) for most commands
  byte 5+ payload
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..errors import InvalidValue, NoResponse, ProtocolFailure
from ..fingerprint import Fingerprint, Node
from ..hidraw import HidrawDevice
from .base import UNTESTED, VERIFIED, Capability, Driver

VENDOR_ID = 0x33E4
PRODUCT_IDS = frozenset({
    0x5403, 0x5404, 0x5407, 0x5408, 0x5703, 0x5704, 0x5707, 0x5708,
    0x5803, 0x5804, 0x5807, 0x5808, 0x5907, 0x5908,
})
# get_Battery_empty runs these through a piecewise curve; the rest report a
# plain percentage (hardware-confirmed on 5807).
CURVED_BATTERY_PRODUCTS = frozenset({0x5403, 0x5404, 0x5703, 0x5704, 0x5803, 0x5804})

CONFIG_USAGE_PAGE = 0xFFFF
PACKET_LENGTH = 65
REPORT_ID = 0
ACK_OFFSET = 1
ACK = 0xA1
# The receiver answering on the mouse's behalf when the mouse is not linked
# (asleep, off, out of range): payload all-zero. Observed 2026-10-19, it
# turned into 0xA1 with data the moment the mouse was moved.
RECEIVER_ONLY = 0xA0
OPCODE_OFFSET = 3
LINK_FLAG_OFFSET = 4
PAYLOAD_OFFSET = 5
SETTLE_SECONDS = 0.060
# 51 bytes of payload: at the vendor's 60 ms the reply is sometimes not ready
# and arrives all-zero, indistinguishable from an ignored command.
DPI_SETTLE_SECONDS = 0.140

MAX_STAGES = 7
STAGE_BASE = 7
STAGE_STRIDE = 7
DPI_MIN, DPI_MAX, DPI_STEP = 50, 26000, 50

# Measured by timing the mouse's own input reports: non-monotonic, so a map.
POLLING_HZ = {1: 1000, 2: 500, 3: 333, 4: 250, 5: 200, 6: 167, 32: 2000, 64: 4000}
POLLING_RAW = {hz: raw for raw, hz in POLLING_HZ.items()}
LIFT_OFF_MM = (1, 2)
CONNECTION = {0: "wired", 1: "dongle"}


@dataclass(frozen=True)
class _Command:
    get: bytes
    set: bytes | None = None
    # False where the vendor function never sets byte 4; for sleep it is
    # part of the sub-command and a link flag there corrupts the packet.
    link_flag: bool = True
    settle: float = SETTLE_SECONDS


COMMANDS = {
    "battery": _Command(bytes.fromhex("0000028f")),
    "connection": _Command(bytes.fromhex("00000190"), link_flag=False),
    "version": _Command(bytes.fromhex("00000481")),
    "polling": _Command(bytes.fromhex("00000282"), bytes.fromhex("00000202")),
    "liftOff": _Command(bytes.fromhex("00000186"), bytes.fromhex("00000106")),
    "angle": _Command(bytes.fromhex("00000187"), bytes.fromhex("00000107")),
    "motion": _Command(bytes.fromhex("00000191"), bytes.fromhex("00000111")),
    # The write takes a row index into a 6-row table the read does not
    # return, so debounce stays read-only until that table is modelled.
    "debounce": _Command(bytes.fromhex("00000485")),
    "dpi": _Command(bytes.fromhex("00003983"), bytes.fromhex("00003903"), settle=DPI_SETTLE_SECONDS),
    # Sub-command of opcode 0x02: byte 6 is 0x87 get / 0x07 set, value u16be at 7.
    "sleep": _Command(bytes.fromhex("00000002020087"), bytes.fromhex("00000002020007"), link_flag=False),
}

CAPABILITIES = (
    Capability("battery", "battery", "Battery", VERIFIED),
    Capability("firmware", "info", "Firmware", VERIFIED),
    Capability("connection", "info", "Connection", VERIFIED),
    # All seven entries decoded on hardware 2026-10-18; value and color writes
    # across every stage confirmed by the owner at the end of v3.
    Capability("dpiStages", "dpiStages", "DPI stages", VERIFIED, minimum=DPI_MIN,
               maximum=DPI_MAX, step=DPI_STEP, max_stages=MAX_STAGES, has_colors=True),
    Capability("pollingRate", "enum", "Polling rate", VERIFIED,
               options=(250, 500, 1000, 2000, 4000), unit="Hz", group="performance"),
    Capability("liftOff", "enum", "Lift-off distance", VERIFIED, options=LIFT_OFF_MM, unit="mm",
               group="performance"),
    Capability("motionSync", "toggle", "Motion sync", VERIFIED, group="sensor"),
    Capability("angleSnap", "toggle", "Angle snapping", VERIFIED, group="sensor"),
    Capability("sleepSeconds", "duration", "Sleep after", VERIFIED,
               minimum=30, maximum=3600, step=30, unit="s", group="power"),
    # Read-only (see COMMANDS), but shown with the settings it belongs to.
    Capability("debounce", "info", "Debounce", VERIFIED, group="performance"),
)


def config_node(fingerprint: Fingerprint) -> Node | None:
    """The vendor-page node carrying feature reports. Never the pointer node."""
    for node in fingerprint.nodes:
        desc = node.descriptor
        is_vendor_page = any(c.usage_page == CONFIG_USAGE_PAGE for c in desc.collections)
        if is_vendor_page and REPORT_ID in desc.feature_report_ids:
            return node
    return None


def curved_battery_percent(raw: int) -> int:
    """get_Battery_empty's curve for the products that do not report percent."""
    if raw <= 40:
        percent = raw / 2
    elif raw <= 70:
        percent = (raw - 40) * 25 / 30 + 20
    elif raw <= 90:
        percent = (raw - 70) * 30 / 20 + 45
    else:
        percent = (raw - 90) * 25 / 10 + 76
    return max(0, min(100, int(percent)))


def _rgb(color: str) -> bytes:
    return bytes.fromhex(color[1:])


class GWolvesHts(Driver):
    family = "gwolves_hts"
    title = "G-Wolves HTS"
    vendor_ids = {VENDOR_ID: "G-Wolves"}

    def __init__(self, fingerprint: Fingerprint):
        super().__init__(fingerprint)
        node = config_node(fingerprint)
        if node is None:
            raise ProtocolFailure(f"{fingerprint.name} has no vendor config node")
        self._device = HidrawDevice(node.path)
        self._wireless: bool | None = None

    @classmethod
    def matches(cls, fingerprint: Fingerprint) -> bool:
        return (
            fingerprint.vendor_id == VENDOR_ID
            and fingerprint.product_id in PRODUCT_IDS
            and config_node(fingerprint) is not None
        )

    def close(self) -> None:
        self._device.close()

    def capabilities(self) -> list[Capability]:
        if self.fingerprint.product_id not in CURVED_BATTERY_PRODUCTS:
            return list(CAPABILITIES)
        # The curve is read out of vendor code, not checked on a mouse.
        return [Capability("battery", "battery", "Battery", UNTESTED), *CAPABILITIES[1:]]

    # --- wire -------------------------------------------------------------

    def _packet(
        self, template: bytes, command: _Command, value: bytes = b"", at: int = PAYLOAD_OFFSET
    ) -> bytearray:
        packet = bytearray(PACKET_LENGTH)
        packet[:len(template)] = template
        if command.link_flag and self._wireless:
            packet[LINK_FLAG_OFFSET] = 1
        packet[at:at + len(value)] = value
        return packet

    def _exchange(self, packet: bytes, settle: float) -> bytes:
        self._device.open()
        time.sleep(SETTLE_SECONDS)
        self._device.set_feature(bytes(packet))
        time.sleep(settle)
        return self._device.get_feature(REPORT_ID, PACKET_LENGTH)

    def _acked(self, packet: bytes, reply: bytes) -> bool:
        return (
            len(reply) == PACKET_LENGTH
            and reply[ACK_OFFSET] == ACK
            and reply[OPCODE_OFFSET] == packet[OPCODE_OFFSET]
        )

    def _send(self, packet: bytes, command: _Command, name: str) -> bytes:
        """One exchange, resent once if unacknowledged: a sleeping mouse
        misses the first packet, and that packet is what wakes it."""
        for _attempt in range(2):
            reply = self._exchange(packet, command.settle)
            if self._acked(packet, reply):
                return reply
        raise ProtocolFailure(
            f"the mouse did not acknowledge {name} (reply starts {reply[:6].hex(' ')})"
        )

    def _detect_link(self) -> None:
        """Probe which link flag this endpoint answers on.

        The flag describes the endpoint opened, not where the mouse is. On
        the wrong flag the firmware ACKs and replies all-zero, exactly like a
        mouse that ignored the packet, so deduce nothing: read DPI on each
        flag and keep the one with data. DPI is the probe because a working
        mouse cannot have an all-zero DPI block. Two rounds, for a mouse
        asleep on the first.
        """
        command = COMMANDS["dpi"]
        receiver_only = False
        for _round in range(2):
            for wireless in (True, False):
                self._wireless = wireless
                packet = self._packet(command.get, command)
                reply = self._exchange(packet, command.settle)
                if self._acked(packet, reply) and any(reply[PAYLOAD_OFFSET:]):
                    return
                receiver_only |= reply[ACK_OFFSET] == RECEIVER_ONLY
            time.sleep(0.12)
        self._wireless = None
        if receiver_only:
            raise NoResponse(
                f"{self.fingerprint.name}: the receiver is answering but the mouse "
                f"is not linked. Move it to wake it, or check it is switched on."
            )
        raise NoResponse(
            f"{self.fingerprint.name} answers with empty data on both links. If the "
            f"cable is plugged in, the mouse may be answering on the cable's node."
        )

    def _read(self, name: str) -> bytes:
        if self._wireless is None:
            self._detect_link()
        command = COMMANDS[name]
        packet = self._packet(command.get, command)
        reply = self._send(packet, command, name)
        # A mouse that just woke answers with an empty payload; reads are
        # idempotent, so one more is cheap.
        if not any(reply[PAYLOAD_OFFSET:]):
            reply = self._send(packet, command, name)
        return reply

    def _write(self, name: str, value: bytes, at: int = PAYLOAD_OFFSET) -> None:
        if self._wireless is None:
            self._detect_link()
        command = COMMANDS[name]
        self._send(self._packet(command.set, command, value, at), command, name)

    # --- capabilities -----------------------------------------------------

    def read(self, capability_id: str) -> Any:
        reader = {
            "battery": self._read_battery,
            "firmware": lambda: "{}.{}.{}".format(*self._read("version")[5:8]),
            "connection": self._read_connection,
            "dpiStages": self._read_stages,
            "pollingRate": self._read_polling,
            "liftOff": lambda: self._read_choice("liftOff", LIFT_OFF_MM),
            "motionSync": lambda: self._read_bool("motion"),
            "angleSnap": lambda: self._read_bool("angle"),
            "sleepSeconds": lambda: int.from_bytes(self._read("sleep")[7:9], "big"),
            "debounce": lambda: f"{self._read('debounce')[5]} ms",
        }.get(capability_id)
        if reader is None:
            raise InvalidValue(f"unknown capability {capability_id!r}")
        return reader()

    def write(self, capability_id: str, value: Any) -> Any:
        if capability_id == "dpiStages":
            self._write_stages(value)
        elif capability_id == "pollingRate":
            self._write("polling", bytes([POLLING_RAW[value]]))
        elif capability_id == "liftOff":
            self._write("liftOff", bytes([value]))
        elif capability_id == "motionSync":
            self._write("motion", bytes([int(value)]))
        elif capability_id == "angleSnap":
            self._write("angle", bytes([int(value)]))
        elif capability_id == "sleepSeconds":
            self._write("sleep", value.to_bytes(2, "big"), at=7)
        else:
            raise InvalidValue(f"{capability_id} cannot be written")
        return self.read(capability_id)

    def _read_battery(self) -> dict:
        reply = self._read("battery")
        raw = reply[6]
        if self.fingerprint.product_id in CURVED_BATTERY_PRODUCTS:
            percent = curved_battery_percent(raw)
        elif raw <= 100:
            percent = raw
        else:
            raise ProtocolFailure(f"battery byte {raw} is not a percentage")
        return {"percent": percent, "charging": reply[5] > 0}

    def _read_connection(self) -> str:
        raw = self._read("connection")[5]
        if raw not in CONNECTION:
            raise ProtocolFailure(f"unknown connection state {raw}")
        return CONNECTION[raw]

    def _read_polling(self) -> int:
        raw = self._read("polling")[5]
        if raw not in POLLING_HZ:
            raise ProtocolFailure(f"unknown polling rate code {raw}")
        return POLLING_HZ[raw]

    def _read_choice(self, name: str, allowed: tuple) -> int:
        raw = self._read(name)[5]
        if raw not in allowed:
            raise ProtocolFailure(f"{name} reads {raw}, expected one of {list(allowed)}")
        return raw

    def _read_bool(self, name: str) -> bool:
        return bool(self._read_choice(name, (0, 1)))

    @staticmethod
    def _stage_count(block: bytes) -> int:
        """Stages the firmware takes out of a write.

        A count of 0 makes the firmware discard every stage value and color
        while the active-stage byte keeps working -- and a read-modify-write
        copies the 0 back forever. v2 found this mouse at 0; writing 7
        fixed it (hardware-confirmed 2026-08-18). So 0 is read as 7, and
        every DPI write carries the repaired count.
        """
        count = block[6]
        return count if 1 <= count <= MAX_STAGES else MAX_STAGES

    def _read_stages(self) -> dict:
        block = self._read("dpi")
        count = self._stage_count(block)
        stages = []
        for i in range(count):
            base = STAGE_BASE + i * STAGE_STRIDE
            # X only: writes keep Y equal to X, as the vendor app does unless
            # its separate-axes box is ticked.
            stages.append({
                "dpi": int.from_bytes(block[base:base + 2], "big"),
                "color": "#" + block[base + 4:base + 7].hex(),
            })
        active = block[5] - 1
        if not 0 <= active < count:
            raise ProtocolFailure(f"active stage {block[5]} is outside 1..{count}")
        return {"active": active, "stages": stages}

    def _block_for_write(self) -> bytearray:
        """The mouse's own DPI block in a write packet: read-modify-write, so
        bytes not modelled here are sent back as they were."""
        current = self._read("dpi")
        command = COMMANDS["dpi"]
        packet = self._packet(command.set, command)
        packet[PAYLOAD_OFFSET:] = current[PAYLOAD_OFFSET:]
        packet[6] = self._stage_count(current)
        return packet

    def _write_stages(self, value: dict) -> None:
        packet = self._block_for_write()
        count = packet[6]
        if len(value["stages"]) != count:
            # The count is not offered: the HSK has no DPI button, so which
            # stages exist only matters to G-Wolves' own app.
            raise InvalidValue(f"the mouse has {count} stages; the panel cannot change that")
        packet[5] = value["active"] + 1
        for i, stage in enumerate(value["stages"]):
            base = STAGE_BASE + i * STAGE_STRIDE
            dpi = stage["dpi"].to_bytes(2, "big")
            packet[base:base + 7] = dpi + dpi + _rgb(stage["color"])
        self._send(bytes(packet), COMMANDS["dpi"], "dpi")
