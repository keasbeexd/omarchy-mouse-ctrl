"""Pulsar Nordic family: the X2H mini and its siblings (vendors 3554, 25a7).

Packet shape and address table from packerlschupfer/pulsar-mouse-linux
(drivers/nordic.py) and andrewrabert/python-pulsar-mouse-tool; ids and
factory defaults from Pulsar Fusion's Cfg.ini; transport and per-value
checksum found on hardware in v3. Evidence per flag:
docs/profiles/pulsar-x2h-mini.md.

Wire format: 17-byte output reports on report id 8, answered with an input
report of the same shape. Byte 16 is 0x55 - sum(bytes 0..15) both ways.
  byte 0     report id 0x08
  byte 1     opcode: 0x04 battery, 0x12 version, 0x08 memory get, 0x07 memory set
  bytes 3-4  memory address, big-endian
  byte 5     length
  byte 6+    value; a memory set appends 0x55 - sum(value) after the value
"""

from __future__ import annotations

import time
from typing import Any

from ..errors import InvalidValue, NoResponse, ProtocolFailure
from ..fingerprint import Fingerprint, Node
from ..hidraw import HidrawDevice
from .base import UNTESTED, VERIFIED, Capability, Driver

VENDOR_IDS = {0x3554: "Pulsar", 0x25A7: "Pulsar (Cfg.ini alternate id)"}
PRODUCT_IDS = frozenset({0xF507, 0xF508, 0xF509, 0xFA7B, 0xFA7C})
# The 1K receiver (seen 2026-10-19 as "Pulsar 1K dongle update"): same
# config interface, but rates above 1000 Hz are not reachable through it.
ONE_K_RECEIVER = 0xF508
# The identity a mouse is reached through says how it is linked: the
# receivers enumerate as f508 (1K) and f509 (4K), the X2H on its cable as
# f507 (all seen 2026-10-19). v3 once recorded a dongle as f507 too, likely
# older receiver firmware; ids not listed here report no connection.
CONNECTION_BY_PRODUCT = {0xF507: "wired", 0xF508: "dongle", 0xF509: "dongle"}

REPORT_ID = 0x08
PACKET_LENGTH = 17
CHECKSUM_OFFSET = 16
VALUE_OFFSET = 6
REPLY_TIMEOUT = 0.5
# A reply echoes report id, opcode and address. Not the length: battery and
# version requests send 0 there and the reply carries 2 (captured 2026-10-19).
ECHO_BYTES = 5

OP_BATTERY = 0x04
OP_VERSION = 0x12
OP_GET = 0x08
OP_SET = 0x07

ADDR_POLLING = 0x00
ADDR_STAGE_COUNT = 0x02
ADDR_ACTIVE_STAGE = 0x04
ADDR_LIFT_OFF = 0x0A
ADDR_DPI_BASE = 0x0C
ADDR_COLOR_BASE = 0x2C
ADDR_DEBOUNCE = 0xA9
ADDR_MOTION_SYNC = 0xAB
ADDR_ANGLE_SNAP = 0xAF
ADDR_RIPPLE = 0xB1
ADDR_TURBO = 0xB5
# Units of 10 s. The community driver also mirrors to 0xAD; v3 wrote only
# 0xB7 and the mouse honoured it, so that is what this does too.
ADDR_SLEEP = 0xB7

MAX_STAGES = 8
DPI_MIN, DPI_MAX, DPI_STEP = 50, 26000, 50
SLEEP_UNIT = 10

POLLING_HZ = {1: 1000, 2: 500, 4: 250, 8: 125, 16: 2000, 32: 4000, 64: 8000}
POLLING_RAW = {hz: raw for raw, hz in POLLING_HZ.items()}
LIFT_OFF_MM = (1, 2)

TOGGLES = {
    "motionSync": ADDR_MOTION_SYNC,
    "angleSnap": ADDR_ANGLE_SNAP,
    "rippleControl": ADDR_RIPPLE,
    "turboMode": ADDR_TURBO,
}

CAPABILITIES = (
    Capability("battery", "battery", "Battery", VERIFIED),
    Capability("firmware", "info", "Firmware", VERIFIED),
    # 0x04 only reports the live stage; switching needs a command not yet known.
    Capability("dpiStages", "dpiStages", "DPI stages", VERIFIED, minimum=DPI_MIN,
               maximum=DPI_MAX, step=DPI_STEP, max_stages=MAX_STAGES, has_colors=True,
               switchable=False),
    # 8000 is in the register map (a Nordic sibling) but untested on a 4K
    # dongle, so it is not offered.
    Capability("pollingRate", "enum", "Polling rate", VERIFIED,
               options=(125, 250, 500, 1000, 2000, 4000), unit="Hz", group="performance"),
    Capability("liftOff", "enum", "Lift-off distance", VERIFIED, options=LIFT_OFF_MM, unit="mm",
               group="performance"),
    Capability("motionSync", "toggle", "Motion sync", VERIFIED, group="sensor"),
    Capability("angleSnap", "toggle", "Angle snapping", VERIFIED, group="sensor"),
    Capability("rippleControl", "toggle", "Ripple control", VERIFIED, group="sensor"),
    Capability("turboMode", "toggle", "Turbo mode", VERIFIED, group="sensor"),
    Capability("sleepSeconds", "duration", "Sleep after", VERIFIED,
               minimum=30, maximum=2550, step=30, unit="s", group="power"),
    Capability("debounce", "duration", "Debounce", VERIFIED, minimum=0, maximum=30, unit="ms",
               group="performance"),
)


def config_node(fingerprint: Fingerprint) -> Node | None:
    """The interface that takes output report 8. Never the pointer node."""
    return next((n for n in fingerprint.nodes if REPORT_ID in n.descriptor.output_report_ids), None)


def _sum_minus_55(data: bytes) -> int:
    return (0x55 - sum(data)) & 0xFF


def _rot2(nibble: int) -> int:
    """Rotate a 4-bit value left by 2 (self-inverse), as the firmware packs
    the DPI record's 2-bit mode and 2-bit page."""
    return ((nibble << 2) | (nibble >> 2)) & 0x0F


def decode_dpi(record: bytes) -> int:
    """[low, low, mode/page nibbles] -> DPI. Single-mode sensor: mode 0."""
    high = _rot2(record[2] & 0x0F)
    mode, page = high >> 2, high & 0x03
    if mode != 0:
        raise ProtocolFailure(f"DPI record {record.hex(' ')} uses mode {mode}, which this driver does not map")
    return (record[0] + 1 + 256 * page) * DPI_STEP


def encode_dpi(dpi: int) -> bytes:
    page, low = divmod(dpi // DPI_STEP - 1, 256)
    nibble = _rot2(page)
    return bytes([low, low, (nibble << 4) | nibble])


class PulsarNordic(Driver):
    family = "pulsar_nordic"
    title = "Pulsar Nordic"
    vendor_ids = VENDOR_IDS

    def __init__(self, fingerprint: Fingerprint):
        super().__init__(fingerprint)
        node = config_node(fingerprint)
        if node is None:
            raise ProtocolFailure(f"{fingerprint.name} has no config node (output report 8)")
        self._device = HidrawDevice(node.path)

    @classmethod
    def matches(cls, fingerprint: Fingerprint) -> bool:
        return (
            fingerprint.vendor_id in VENDOR_IDS
            and fingerprint.product_id in PRODUCT_IDS
            and config_node(fingerprint) is not None
        )

    @classmethod
    def profile_products(cls, fingerprint: Fingerprint) -> tuple[int, ...]:
        """The cable and both receivers reach the same mouse; the Cfg.ini
        alternate ids (25a7) stay on their own."""
        if fingerprint.product_id in CONNECTION_BY_PRODUCT:
            return tuple(sorted(CONNECTION_BY_PRODUCT))
        return (fingerprint.product_id,)

    def close(self) -> None:
        self._device.close()

    def capabilities(self) -> list[Capability]:
        caps = list(CAPABILITIES)
        if self.fingerprint.product_id in CONNECTION_BY_PRODUCT:
            caps.append(Capability("connection", "info", "Connection", VERIFIED))
        if self.fingerprint.product_id == ONE_K_RECEIVER:
            one_k = Capability("pollingRate", "enum", "Polling rate", UNTESTED,
                               options=(125, 250, 500, 1000), unit="Hz", group="performance")
            caps = [one_k if c.id == "pollingRate" else c for c in caps]
        return caps

    # --- wire -------------------------------------------------------------

    @staticmethod
    def _packet(head: bytes, value: bytes = b"") -> bytes:
        packet = bytearray(PACKET_LENGTH)
        packet[:len(head)] = head
        if value:
            packet[VALUE_OFFSET:VALUE_OFFSET + len(value)] = value
            packet[VALUE_OFFSET + len(value)] = _sum_minus_55(value)
        packet[CHECKSUM_OFFSET] = _sum_minus_55(packet[:CHECKSUM_OFFSET])
        return bytes(packet)

    def _drain(self) -> None:
        """Drop replies left over from an earlier exchange."""
        while self._device.read_input(64, timeout=0):
            pass

    def _await_reply(self, packet: bytes) -> bytes | None:
        """The reply echoing this packet's header, skipping anything else.

        This interface also carries pointer and unsolicited reports, so the
        first report to arrive is not necessarily the answer.
        """
        deadline = time.monotonic() + REPLY_TIMEOUT
        while (left := deadline - time.monotonic()) > 0:
            reply = self._device.read_input(64, timeout=left)
            if not reply or reply[:ECHO_BYTES] != packet[:ECHO_BYTES]:
                continue
            if len(reply) != PACKET_LENGTH or reply[CHECKSUM_OFFSET] != _sum_minus_55(reply[:CHECKSUM_OFFSET]):
                raise ProtocolFailure(f"reply with a bad checksum: {reply.hex(' ')}")
            return reply
        return None

    def _exchange(self, packet: bytes) -> bytes:
        """Send, and resend once: a sleeping mouse misses the first packet."""
        self._device.open()
        self._drain()
        for _attempt in range(2):
            self._device.write_output(packet)
            reply = self._await_reply(packet)
            if reply is not None:
                return reply
        raise NoResponse(
            f"{self.fingerprint.name} did not answer. Move the mouse to wake it, "
            f"or check it is paired to this receiver."
        )

    def _get(self, address: int, length: int = 1) -> bytes:
        reply = self._exchange(self._packet(bytes([REPORT_ID, OP_GET, 0, 0, address, length])))
        return reply[VALUE_OFFSET:VALUE_OFFSET + length]

    def _set(self, address: int, value: bytes) -> None:
        # The length byte counts the stored checksum for multi-byte records
        # (DPI and color: 3 + 1) but not for scalars, as the firmware expects.
        length = len(value) + 1 if len(value) > 1 else 1
        self._exchange(self._packet(bytes([REPORT_ID, OP_SET, 0, 0, address, length]), value))

    # --- capabilities -----------------------------------------------------

    def read(self, capability_id: str) -> Any:
        if capability_id in TOGGLES:
            return self._read_choice(TOGGLES[capability_id], (0, 1)) == 1
        reader = {
            "battery": self._read_battery,
            "firmware": lambda: "{}.{}".format(*self._exchange(self._packet(bytes([REPORT_ID, OP_VERSION])))[6:8]),
            "dpiStages": self._read_stages,
            "pollingRate": self._read_polling,
            "liftOff": lambda: self._read_choice(ADDR_LIFT_OFF, LIFT_OFF_MM),
            "sleepSeconds": lambda: self._get(ADDR_SLEEP)[0] * SLEEP_UNIT,
            "debounce": lambda: self._get(ADDR_DEBOUNCE)[0],
            "connection": self._read_connection,
        }.get(capability_id)
        if reader is None:
            raise InvalidValue(f"unknown capability {capability_id!r}")
        return reader()

    def write(self, capability_id: str, value: Any) -> Any:
        if capability_id in TOGGLES:
            self._set(TOGGLES[capability_id], bytes([int(value)]))
        elif capability_id == "dpiStages":
            self._write_stages(value)
        elif capability_id == "pollingRate":
            self._set(ADDR_POLLING, bytes([POLLING_RAW[value]]))
        elif capability_id == "liftOff":
            self._set(ADDR_LIFT_OFF, bytes([value]))
        elif capability_id == "sleepSeconds":
            self._set(ADDR_SLEEP, bytes([value // SLEEP_UNIT]))
        elif capability_id == "debounce":
            self._set(ADDR_DEBOUNCE, bytes([value]))
        else:
            raise InvalidValue(f"{capability_id} cannot be written")
        return self.read(capability_id)

    def _read_connection(self) -> str:
        connection = CONNECTION_BY_PRODUCT.get(self.fingerprint.product_id)
        if connection is None:
            raise InvalidValue(f"connection is not known for product {self.fingerprint.product_id:04x}")
        return connection

    def _read_battery(self) -> dict:
        reply = self._exchange(self._packet(bytes([REPORT_ID, OP_BATTERY])))
        if reply[6] > 100:
            raise ProtocolFailure(f"battery byte {reply[6]} is not a percentage")
        return {"percent": reply[6], "charging": reply[7] > 0}

    def _read_polling(self) -> int:
        raw = self._get(ADDR_POLLING)[0]
        if raw not in POLLING_HZ:
            raise ProtocolFailure(f"unknown polling rate code {raw}")
        return POLLING_HZ[raw]

    def _read_choice(self, address: int, allowed: tuple) -> int:
        raw = self._get(address)[0]
        if raw not in allowed:
            raise ProtocolFailure(f"address {address:#04x} reads {raw}, expected one of {list(allowed)}")
        return raw

    def _stage_count(self) -> int:
        count = self._get(ADDR_STAGE_COUNT)[0]
        if not 1 <= count <= MAX_STAGES:
            raise ProtocolFailure(f"stage count {count} is outside 1..{MAX_STAGES}")
        return count

    def _read_stages(self) -> dict:
        count = self._stage_count()
        active = self._get(ADDR_ACTIVE_STAGE)[0]
        if not 0 <= active < count:
            raise ProtocolFailure(f"active stage {active} is outside 0..{count - 1}")
        stages = [
            {
                "dpi": decode_dpi(self._get(ADDR_DPI_BASE + 4 * i, 3)),
                "color": "#" + self._get(ADDR_COLOR_BASE + 4 * i, 3).hex(),
            }
            for i in range(count)
        ]
        return {"active": active, "stages": stages}

    def _write_stages(self, value: dict) -> None:
        current = self._read_stages()
        if len(value["stages"]) != len(current["stages"]):
            # Writing 0x02 stores safely but the mouse keeps cycling its own
            # count (hardware, 2026-10-19), so it is not offered.
            raise InvalidValue("changing the number of stages is not supported on this mouse")
        if value["active"] != current["active"]:
            # 0x04 reports the live stage (it follows the DPI button) but a
            # write stores without switching (hardware, 2026-10-19).
            raise InvalidValue("switching the active stage is not supported on this mouse")
        for i, (new, old) in enumerate(zip(value["stages"], current["stages"])):
            if new["dpi"] != old["dpi"]:
                self._set(ADDR_DPI_BASE + 4 * i, encode_dpi(new["dpi"]))
            if new["color"] != old["color"]:
                self._set(ADDR_COLOR_BASE + 4 * i, bytes.fromhex(new["color"][1:]))
