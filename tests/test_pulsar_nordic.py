import tempfile
import unittest
from unittest import mock

from fakes import KEYBOARD_DESC, MOUSE_DESC, FakeSysfs

from mousectrl import fingerprint
from mousectrl.errors import InvalidValue, NoResponse, ProtocolFailure
from mousectrl.families import pulsar_nordic
from mousectrl.families.pulsar_nordic import PulsarNordic, decode_dpi, encode_dpi

# Interface 1 of the real 4K receiver, cut down: vendor page, feature id 6,
# output ids 8 and 9.
CONFIG_DESC = bytes.fromhex("06ffff0900a10185060900b1028508090091028509090091 02c0".replace(" ", ""))


def checksum(data: bytes) -> int:
    return (0x55 - sum(data)) & 0xFF


class FakeReceiver:
    """Answers like the X2H mini's firmware; memory seeded from a real read."""

    def __init__(self):
        self.memory = {0x00: 1, 0x02: 4, 0x04: 0, 0x0A: 1, 0xA9: 3, 0xAB: 1, 0xAF: 1,
                       0xB1: 0, 0xB5: 0, 0xB7: 3}
        for i, (dpi, color) in enumerate([("070700", "aa0000"), ("0f0f00", "0000ff"),
                                          ("1f1f00", "ff00ff"), ("3f3f00", "ffffff")]):
            self.memory[0x0C + 4 * i] = bytes.fromhex(dpi)
            self.memory[0x2C + 4 * i] = bytes.fromhex(color)
        self.queue = []
        self.writes = []
        self.silent = False
        self.noise = True
        self.corrupt = False

    def open(self):
        pass

    def close(self):
        pass

    def write_output(self, packet):
        assert len(packet) == 17 and packet[16] == checksum(packet[:16]), packet.hex(" ")
        if self.silent:
            return
        reply = bytearray(17)
        reply[:6] = packet[:6]
        op, addr, length = packet[1], packet[4], packet[5]
        if op == 0x04:
            reply[5], reply[6], reply[7] = 2, 100, 0
        elif op == 0x12:
            reply[5], reply[6], reply[7] = 2, 2, 24
        elif op == 0x08:
            stored = self.memory[addr]
            reply[6:6 + length] = stored if isinstance(stored, bytes) else bytes([stored])
        elif op == 0x07:
            value = packet[6:6 + (3 if length == 4 else 1)]
            assert packet[6 + len(value)] == checksum(value), "per-value checksum"
            self.writes.append((addr, value))
            self.memory[addr] = value if len(value) == 3 else value[0]
        reply[16] = checksum(reply[:16])
        if self.corrupt:
            reply[16] ^= 0xFF
        if self.noise:
            self.queue.append(bytes.fromhex("00fdfffeff0000"))  # pointer report
        self.queue.append(bytes(reply))

    def read_input(self, length=64, timeout=1.0):
        return self.queue.pop(0) if self.queue else None


class PulsarNordicTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        fs = FakeSysfs(self._tmp.name)
        common = dict(vid=0x3554, pid=0xF509, name="pulsar Pulsar 4K Wireless Receiver")
        fs.add(**common, phys="usb-1/input0", desc=MOUSE_DESC, boot_protocol=2)
        fs.add(**common, phys="usb-1/input1", desc=CONFIG_DESC, boot_protocol=0)
        fs.add(**common, phys="usb-1/input2", desc=KEYBOARD_DESC, boot_protocol=1)
        (self.fp,) = fingerprint.mice(fs.sysfs, fs.dev)
        self.mouse = FakeReceiver()
        patcher = mock.patch.object(pulsar_nordic, "HidrawDevice", lambda path: self.mouse)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.driver = PulsarNordic(self.fp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_matches_and_picks_the_output_report_node(self):
        self.assertTrue(PulsarNordic.matches(self.fp))
        self.assertEqual(pulsar_nordic.config_node(self.fp).interface, 1)

    def test_reads_match_the_hardware_capture(self):
        self.assertEqual(self.driver.read("battery"), {"percent": 100, "charging": False})
        self.assertEqual(self.driver.read("firmware"), "2.24")
        self.assertEqual(self.driver.read("pollingRate"), 1000)
        self.assertEqual(self.driver.read("liftOff"), 1)
        self.assertIs(self.driver.read("motionSync"), True)
        self.assertIs(self.driver.read("rippleControl"), False)
        self.assertEqual(self.driver.read("sleepSeconds"), 30)
        self.assertEqual(self.driver.read("debounce"), 3)
        self.assertEqual(self.driver.read("connection"), "dongle")
        self.assertEqual(self.driver.read("dpiStages"), {"active": 0, "stages": [
            {"dpi": 400, "color": "#aa0000"}, {"dpi": 800, "color": "#0000ff"},
            {"dpi": 1600, "color": "#ff00ff"}, {"dpi": 3200, "color": "#ffffff"}]})

    def test_silence_is_no_response(self):
        self.mouse.silent = True
        with self.assertRaises(NoResponse):
            self.driver.read("battery")

    def test_bad_reply_checksum_is_refused(self):
        self.mouse.corrupt = True
        with self.assertRaisesRegex(ProtocolFailure, "checksum"):
            self.driver.read("battery")

    def test_scalar_write_carries_value_checksum(self):
        self.assertEqual(self.driver.write("pollingRate", 4000), 4000)
        self.assertEqual(self.mouse.writes, [(0x00, bytes([32]))])

    def test_sleep_is_stored_in_tens_of_seconds(self):
        self.assertEqual(self.driver.write("sleepSeconds", 120), 120)
        self.assertEqual(self.mouse.memory[0xB7], 12)

    def test_stage_write_touches_only_what_changed(self):
        value = self.driver.read("dpiStages")
        value["stages"][2] = {"dpi": 12850, "color": "#ff00ff"}
        self.assertEqual(self.driver.write("dpiStages", value), value)
        self.assertEqual(self.mouse.writes, [(0x14, bytes.fromhex("000044"))])

    def test_stage_switching_and_count_changes_are_refused(self):
        value = self.driver.read("dpiStages")
        with self.assertRaisesRegex(InvalidValue, "active stage"):
            self.driver.write("dpiStages", {**value, "active": 1})
        with self.assertRaisesRegex(InvalidValue, "number of stages"):
            self.driver.write("dpiStages", {**value, "stages": value["stages"][:3]})
        self.assertEqual(self.mouse.writes, [])

    def test_connection_comes_from_the_identity(self):
        for product, expected in ((0xF507, "wired"), (0xF508, "dongle"), (0xF509, "dongle")):
            driver = mock.Mock(fingerprint=mock.Mock(product_id=product))
            self.assertIn("connection", [c.id for c in PulsarNordic.capabilities(driver)])
            self.assertEqual(PulsarNordic._read_connection(driver), expected)
        unknown = mock.Mock(fingerprint=mock.Mock(product_id=0xFA7B))
        self.assertNotIn("connection", [c.id for c in PulsarNordic.capabilities(unknown)])

    def test_one_k_receiver_offers_no_high_rates(self):
        one_k = mock.Mock(product_id=0xF508)
        caps = {c.id: c for c in PulsarNordic.capabilities(mock.Mock(fingerprint=one_k))}
        self.assertEqual(caps["pollingRate"].options, (125, 250, 500, 1000))
        self.assertEqual(caps["pollingRate"].verification, "untested")
        four_k = {c.id: c for c in self.driver.capabilities()}
        self.assertIn(4000, four_k["pollingRate"].options)

    def test_dpi_codec(self):
        for dpi in (50, 400, 3200, 12800, 12850, 26000):
            self.assertEqual(decode_dpi(encode_dpi(dpi)), dpi)
        self.assertEqual(encode_dpi(400), bytes.fromhex("070700"))
        with self.assertRaises(ProtocolFailure):
            decode_dpi(bytes.fromhex("070711"))


if __name__ == "__main__":
    unittest.main()
