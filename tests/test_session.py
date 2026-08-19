"""Two bugs that hid behind each other, pinned so they cannot come back.

Both were found on real hardware, and both share a shape: a heuristic that
treated a legitimate zero as "no answer".
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hskctl.device import Session  # noqa: E402
from hskctl.protocol import ProtocolError  # noqa: E402
from hskctl.protocol import load_profile  # noqa: E402

PROFILE = "gwolves-hsk-pro-4k"


class FakeInfo:
    """Just enough HidrawInfo for the code under test to name the device."""
    path = "/dev/hidraw-test"
    vidpid = "33e4:5807"


def bare_session():
    """A Session with no device behind it -- enough to exercise the logic."""
    session = Session.__new__(Session)
    session.profile = load_profile(PROFILE)
    session.info = FakeInfo()
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
    """Which link flag an endpoint wants is a fact about the endpoint.

    It used to be read off the `connection` command, which answers a different
    question -- whether the mouse is currently on RF or on a cable. Plug the
    cable in while the dongle is still in and the two disagree: `connection`
    says "wired", every packet then goes to the dongle carrying flag 0, the
    firmware acknowledges and discards all of it, and every single setting
    reads back 0 with nothing to explain why. That is what the user saw.

    So the flag is now established by probing: send a read on each flag and
    keep whichever answers with data.
    """

    OPCODE = 0x83   # the dpi read, which the profile names as the link probe

    def answering_on(self, live_flag):
        """A fake endpoint that only returns data on one link flag."""
        session = bare_session()
        session.calls = []

        def fake_exchange(packet, command=None):
            flag = bool(packet[4])
            session.calls.append(flag)
            header = bytes([0x00, 0xA1, 0x33, self.OPCODE, packet[4]])
            if flag != live_flag:
                # The firmware ACKs the wrong flag and then ignores the packet.
                return header + b"\x00" * 60
            return header + b"\x01\x07\x01\x90" + b"\x00" * 56

        session._exchange = fake_exchange
        return session

    def test_the_dongle_is_found(self):
        session = self.answering_on(True)
        self.assertTrue(session.detect_link())

    def test_the_cable_is_found(self):
        session = self.answering_on(False)
        self.assertFalse(session.detect_link())

    def test_it_tries_the_other_flag_rather_than_believing_the_first_ack(self):
        session = self.answering_on(False)
        session.wireless = True          # start on the wrong guess
        self.assertFalse(session.detect_link())
        self.assertIn(True, session.calls, "should have tried the dongle first")
        self.assertIn(False, session.calls, "should have then tried the cable")

    def test_the_answer_is_cached(self):
        session = self.answering_on(True)
        session.detect_link()
        before = len(session.calls)
        session.detect_link()
        self.assertEqual(len(session.calls), before, "should not re-probe")

    def test_an_endpoint_that_only_acks_is_rejected(self):
        """The dongle with the mouse on the cable does exactly this.

        Refusing is the whole point: returning a guess here is what produced a
        status page full of zeros presented as real readings.
        """
        session = bare_session()
        session._exchange = lambda packet, command=None: (
            bytes([0x00, 0xA1, 0x33, self.OPCODE, packet[4]]) + b"\x00" * 60
        )
        with self.assertRaises(ProtocolError) as caught:
            session.detect_link()
        self.assertIn("cable", str(caught.exception))

    def test_a_silent_endpoint_is_rejected(self):
        session = bare_session()
        session._exchange = lambda packet, command=None: b"\x00" * 65
        with self.assertRaises(ProtocolError):
            session.detect_link()


class LegacyLinkDetectionTests(unittest.TestCase):
    """Profiles with no linkProbe still fall back to asking the mouse."""

    def session_without_probe(self, payload_byte):
        session = bare_session()
        session.profile.data["transport"].pop("linkProbe", None)
        opcode = session.profile.build_request("connection", write=False)[3]
        session._exchange_checked = lambda command, packet: (
            bytes([0x00, 0xA1, 0x01, opcode, 0x00, payload_byte]) + b"\x00" * 59
        )
        return session

    def test_zero_still_means_wired_rather_than_no_answer(self):
        self.assertFalse(self.session_without_probe(0).detect_link())

    def test_one_still_means_dongle(self):
        self.assertTrue(self.session_without_probe(1).detect_link())


if __name__ == "__main__":
    unittest.main()
