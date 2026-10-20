import tempfile
import unittest
from unittest import mock

from fakes import FakeSysfs

from mousectrl import fingerprint
from mousectrl.errors import InvalidValue, NoResponse, ProtocolFailure
from mousectrl.families import gwolves_hts
from mousectrl.families.gwolves_hts import GWolvesHts, curved_battery_percent

RAMP = [(400, "ff0000"), (1600, "00ff00"), (1600, "0000ff"), (3200, "ffff00"),
        (4500, "ff00ff"), (5000, "00ffff"), (6400, "ffffff")]


class FakeMouse:
    """Answers feature reports like HTS firmware on the dongle link.

    Wrong link flag: ACK with an all-zero payload, as the real firmware does.
    """

    def __init__(self, wireless=True):
        self.wireless = wireless
        self.sent = []
        self.reply = bytes(65)
        self.stage_count = 7
        self.active = 2
        self.stages = list(RAMP)
        self.registers = {0x82: 1, 0x86: 1, 0x87: 0, 0x91: 1, 0x85: 3}
        self.battery = (0, 82)
        self.sleep = 240
        self.asleep = False

    def open(self):
        pass

    def close(self):
        pass

    def set_feature(self, packet):
        self.sent.append(bytes(packet))
        reply = bytearray(65)
        reply[1], reply[2], reply[3] = 0xA1, packet[2], packet[3]
        opcode = packet[3]
        if self.asleep:
            reply[1] = 0xA0 if packet[4] else 0xA1
            self.reply = bytes(reply)
            return
        if opcode == 0x83:
            reply[2] = 0x33  # observed: the DPI read reports 51, not the 0x39 sent
        sub = packet[6] if opcode == 0x02 and packet[4] == 0x02 else None
        if sub is None and opcode != 0x90 and bool(packet[4]) != self.wireless:
            self.reply = bytes(reply)
            return
        reply[4] = packet[4]
        if opcode == 0x83:
            reply[5], reply[6] = self.active, self.stage_count
            for i, (dpi, color) in enumerate(self.stages):
                base = 7 + i * 7
                reply[base:base + 7] = dpi.to_bytes(2, "big") * 2 + bytes.fromhex(color)
        elif opcode == 0x03:
            # Firmware takes only stage_count stages out of the write.
            self.active, self.stage_count = packet[5], packet[6]
            for i in range(self.stage_count):
                base = 7 + i * 7
                self.stages[i] = (int.from_bytes(packet[base:base + 2], "big"), packet[base + 4:base + 7].hex())
        elif opcode == 0x8F:
            reply[5], reply[6] = self.battery
        elif opcode == 0x81:
            reply[5:8] = bytes([1, 0, 7])
        elif opcode == 0x90:
            reply[5] = 1
        elif sub == 0x87:
            reply[7:9] = self.sleep.to_bytes(2, "big")
        elif sub == 0x07:
            self.sleep = int.from_bytes(packet[7:9], "big")
        elif opcode in self.registers:
            reply[5] = self.registers[opcode]
        elif opcode | 0x80 in self.registers:
            self.registers[opcode | 0x80] = packet[5]
        self.reply = bytes(reply)

    def get_feature(self, report_id, length):
        return self.reply


class GWolvesHtsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        fs = FakeSysfs(self._tmp.name)
        fs.add_hsk()
        (self.fp,) = fingerprint.mice(fs.sysfs, fs.dev)
        self.mouse = FakeMouse()
        patcher = mock.patch.object(gwolves_hts, "HidrawDevice", lambda path: self.mouse)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(gwolves_hts.time, "sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.driver = GWolvesHts(self.fp)

    def tearDown(self):
        self._tmp.cleanup()

    def test_matches_on_vendor_product_and_config_node(self):
        self.assertTrue(GWolvesHts.matches(self.fp))
        self.assertEqual(gwolves_hts.config_node(self.fp).interface, 2)

    def test_link_probe_finds_the_cable_flag(self):
        self.mouse.wireless = False
        self.assertEqual(self.driver.read("pollingRate"), 1000)
        self.assertEqual(self.mouse.sent[-1][4], 0)

    def test_silent_mouse_is_one_device_level_error(self):
        self.mouse.set_feature = lambda packet: None
        with self.assertRaisesRegex(NoResponse, "empty data on both links"):
            self.driver.read("battery")

    def test_receiver_only_reply_says_the_mouse_is_asleep(self):
        self.mouse.asleep = True
        with self.assertRaisesRegex(NoResponse, "mouse is not linked"):
            self.driver.read("battery")
        self.mouse.asleep = False
        self.assertEqual(self.driver.read("battery")["percent"], 82)

    def test_reads(self):
        self.assertEqual(self.driver.read("battery"), {"percent": 82, "charging": False})
        self.assertEqual(self.driver.read("firmware"), "1.0.7")
        self.assertEqual(self.driver.read("connection"), "dongle")
        self.assertEqual(self.driver.read("liftOff"), 1)
        self.assertIs(self.driver.read("motionSync"), True)
        self.assertIs(self.driver.read("angleSnap"), False)
        self.assertEqual(self.driver.read("sleepSeconds"), 240)
        self.assertEqual(self.driver.read("debounce"), "3 ms")

    def test_sleep_packet_never_carries_a_link_flag(self):
        self.driver.write("sleepSeconds", 300)
        # 300 s = 01 2c; byte 4 stays 0x02 (sub-command), never a link flag.
        self.assertEqual(self.mouse.sent[-2][:9], bytes.fromhex("00000002020007012c"))

    def test_dpi_block_reads_every_stage(self):
        value = self.driver.read("dpiStages")
        self.assertEqual(value["active"], 1)
        self.assertEqual(value["stages"][6], {"dpi": 6400, "color": "#ffffff"})

    def test_zero_stage_count_is_read_as_seven_and_repaired_on_write(self):
        self.mouse.stage_count = 0
        value = self.driver.read("dpiStages")
        self.assertEqual(len(value["stages"]), 7)
        value["stages"][3] = {"dpi": 800, "color": "#123456"}
        self.assertEqual(self.driver.write("dpiStages", value), value)
        self.assertEqual(self.mouse.stage_count, 7)

    def test_stage_write_keeps_axes_equal_and_unmodelled_bytes(self):
        value = self.driver.read("dpiStages")
        value["active"] = 0
        value["stages"][0] = {"dpi": 800, "color": "#abcdef"}
        self.driver.write("dpiStages", value)
        write = next(p for p in reversed(self.mouse.sent) if p[3] == 0x03)
        self.assertEqual(write[5], 1)
        self.assertEqual(write[7:14], bytes.fromhex("03200320abcdef"))
        self.assertEqual(write[2], 0x39)

    def test_stage_count_mismatch_is_refused(self):
        value = self.driver.read("dpiStages")
        value["stages"] = value["stages"][:3]
        with self.assertRaisesRegex(InvalidValue, "cannot change"):
            self.driver.write("dpiStages", value)

    def test_polling_uses_the_measured_map(self):
        self.assertEqual(self.driver.write("pollingRate", 4000), 4000)
        self.assertEqual(self.mouse.registers[0x82], 64)

    def test_unknown_register_value_is_a_protocol_failure(self):
        self.mouse.registers[0x82] = 9
        with self.assertRaises(ProtocolFailure):
            self.driver.read("pollingRate")

    def test_curved_battery_products(self):
        self.assertEqual(curved_battery_percent(40), 20)
        self.assertEqual(curved_battery_percent(70), 45)
        self.assertEqual(curved_battery_percent(255), 100)


if __name__ == "__main__":
    unittest.main()
