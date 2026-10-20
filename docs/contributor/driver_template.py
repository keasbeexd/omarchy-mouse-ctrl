"""TEMPLATE: copy to mousectrl/families/<vendor>_<protocol>.py and fill in.

Every ALL_CAPS value marked FILL IN comes from the vendor app (see
scripts/analyze-driver.py) or a USB capture -- never from a guess. Start
every capability as UNTESTED; promote to VERIFIED only after it round-trips
on real hardware, and write the evidence into docs/profiles/<model>.md.

Two shipped drivers to copy from:
- gwolves_hts.py: feature reports, a link flag, a read-modify-write block
- pulsar_nordic.py: output reports answered on the input endpoint, checksums,
  a memory-mapped get/set

Then register the class in mousectrl/families/__init__.py and add a test
module with a fake device (tests/test_gwolves_hts.py shows the pattern).
"""

from __future__ import annotations

import time
from typing import Any

from ..errors import InvalidValue, NoResponse, ProtocolFailure
from ..fingerprint import Fingerprint, Node
from ..hidraw import HidrawDevice
from .base import UNTESTED, Capability, Driver

VENDOR_ID = 0x0000              # FILL IN: USB vendor id
PRODUCT_IDS = frozenset({0x0000})  # FILL IN: every product id the app's table lists
PACKET_LENGTH = 65              # FILL IN: report length including the report id byte
REPORT_ID = 0                   # FILL IN
SETTLE_SECONDS = 0.060          # FILL IN: the app's sleep between send and read

CAPABILITIES = (
    # FILL IN: one per setting the protocol exposes. Kinds and value shapes
    # are documented in families/base.py; groups place it in the panel.
    Capability("battery", "battery", "Battery", UNTESTED),
    Capability("pollingRate", "enum", "Polling rate", UNTESTED,
               options=(1000,), unit="Hz", group="performance"),
)


def config_node(fingerprint: Fingerprint) -> Node | None:
    """The interface the app talks to. Never the pointer node."""
    for node in fingerprint.nodes:
        if REPORT_ID in node.descriptor.feature_report_ids and node.descriptor.is_vendor:
            return node
    return None


class TemplateDriver(Driver):
    family = "vendor_protocol"      # FILL IN: stable id, used in saved profiles
    title = "Vendor Protocol"       # FILL IN: shown in the panel
    vendor_ids = {VENDOR_ID: "Vendor"}

    def __init__(self, fingerprint: Fingerprint):
        super().__init__(fingerprint)
        node = config_node(fingerprint)
        if node is None:
            raise ProtocolFailure(f"{fingerprint.name} has no config node")
        self._device = HidrawDevice(node.path)

    @classmethod
    def matches(cls, fingerprint: Fingerprint) -> bool:
        return (fingerprint.vendor_id == VENDOR_ID and fingerprint.product_id in PRODUCT_IDS
                and config_node(fingerprint) is not None)

    def close(self) -> None:
        self._device.close()

    def capabilities(self) -> list[Capability]:
        return list(CAPABILITIES)

    def _exchange(self, packet: bytes) -> bytes:
        """One request and its reply. FILL IN the reply check the app does
        (an ACK byte, an echoed opcode, a checksum) and raise NoResponse when
        the mouse is silent -- never return a reply you have not checked."""
        self._device.open()
        self._device.set_feature(packet)
        time.sleep(SETTLE_SECONDS)
        reply = self._device.get_feature(REPORT_ID, PACKET_LENGTH)
        if not any(reply[1:]):
            raise NoResponse(f"{self.fingerprint.name} did not answer; move it to wake it")
        return reply

    def read(self, capability_id: str) -> Any:
        raise InvalidValue(f"FILL IN: read {capability_id}")

    def write(self, capability_id: str, value: Any) -> Any:
        # Values arrive validated against the capability. Write, then return
        # self.read(capability_id): the caller compares the read-back.
        raise InvalidValue(f"FILL IN: write {capability_id}")
