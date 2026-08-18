"""Two bugs that hid behind each other, pinned so they cannot come back.

Both were found on real hardware, and both share a shape: a heuristic that
treated a legitimate zero as "no answer".
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hskctl.device import Session  # noqa: E402
from hskctl.protocol import load_profile  # noqa: E402

PROFILE = "gwolves-hsk-pro-4k"


def bare_session():
    """A Session with no device behind it -- enough to exercise the logic."""
    session = Session.__new__(Session)
    session.profile = load_profile(PROFILE)
    session.info = None
    session.wireless = False
    session._link_detected = False
    session.trace = None
    return session


class StageCountRepairTests(unittest.TestCase):
    """A DPI write must not echo a stage count of 0 back at the mouse.

    The firmware writes that many stages out of the packet, so a 0 discards
    every DPI value and every colour -- and read-modify-write then restores the
    0, making it permanent. Confirmed on hardware: setting the count to 7 made
    writes land immediately.
    """

    def setUp(self):
        self.session = bare_session()
        self.spec = self.session.profile.data["commands"]["dpi"]

    def test_a_zero_count_is_replaced(self):
        packet = bytearray(b"\x00\x00\x39\x03\x01\x01\x00" + b"\x00" * 58)
        self.session._repair_header(packet, self.spec)
        self.assertEqual(packet[6], 7)

    def test_a_plausible_count_is_left_alone(self):
        for count in range(1, 8):
            packet = bytearray(b"\x00\x00\x39\x03\x01\x01" + bytes([count]))
            packet += b"\x00" * 58
            self.session._repair_header(packet, self.spec)
            self.assertEqual(packet[6], count, f"count {count} must survive")

    def test_the_repair_touches_nothing_else(self):
        original = bytearray(range(65))
        original[6] = 0
        packet = bytearray(original)
        self.session._repair_header(packet, self.spec)
        self.assertEqual(packet[:6], original[:6])
        self.assertEqual(packet[7:], original[7:])

    def test_the_profile_still_declares_the_repair(self):
        # If this disappears, DPI writes silently stop working again.
        fixes = self.spec.get("repairOnWrite")
        self.assertTrue(fixes, "the dpi command must declare repairOnWrite")
        self.assertEqual(fixes[0]["offset"], 6)
        self.assertIn(0, fixes[0]["invalid"])


class LinkDetectionTests(unittest.TestCase):
    """A wired mouse reports 0, and 0 is an answer.

    detect_link used to require a non-zero payload before believing the reply.
    That is precisely what a mouse on the cable sends, so a wired mouse could
    never be detected: every command died with "it is probably asleep", which
    is why charging never read while it was plugged in. The echoed opcode is
    what makes a reply real.
    """

    def replying(self, payload_byte):
        session = bare_session()
        opcode = session.profile.build_request("connection", write=False)[3]

        def fake(command, packet):
            return bytes([0x00, 0xA1, 0x01, opcode, 0x00, payload_byte]) + b"\x00" * 59

        session._exchange_checked = fake
        return session

    def test_the_cable_is_detected_even_though_it_reports_zero(self):
        session = self.replying(0)
        self.assertFalse(session.detect_link())
        self.assertTrue(session._link_detected)

    def test_the_dongle_is_still_detected(self):
        session = self.replying(1)
        self.assertTrue(session.detect_link())

    def test_a_reply_that_echoes_no_opcode_is_not_believed(self):
        session = bare_session()
        session._exchange_checked = lambda command, packet: b"\x00" * 65
        with self.assertRaises(Exception):
            session.detect_link()


if __name__ == "__main__":
    unittest.main()
