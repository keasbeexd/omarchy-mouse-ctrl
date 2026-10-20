import tempfile
import unittest

from fakes import COMPOSITE_DESC, KEYBOARD_DESC, MOUSE_DESC, VENDOR_DESC, FakeSysfs

from mousectrl import fingerprint
from mousectrl.fingerprint import Collection, parse_descriptor


class ParseDescriptorTest(unittest.TestCase):
    def test_mouse_collection(self):
        desc = parse_descriptor(MOUSE_DESC)
        self.assertEqual(desc.collections, (Collection(0x01, 0x02),))
        self.assertTrue(desc.is_only_mouse)
        self.assertFalse(desc.is_vendor)

    def test_composite_lists_every_top_level_collection(self):
        desc = parse_descriptor(COMPOSITE_DESC)
        self.assertEqual(desc.collections, (Collection(0x01, 0x02), Collection(0x01, 0x06)))
        self.assertTrue(desc.has_mouse)
        self.assertFalse(desc.is_only_mouse)

    def test_vendor_feature_report(self):
        desc = parse_descriptor(VENDOR_DESC)
        self.assertTrue(desc.is_vendor)
        self.assertEqual(desc.feature_report_ids, (0,))

    def test_extended_usage_carries_its_own_page(self):
        desc = parse_descriptor(bytes.fromhex("0b01000c00a101c0"))
        self.assertEqual(desc.collections, (Collection(0x0C, 0x01),))

    def test_truncated_descriptor_does_not_raise(self):
        parse_descriptor(bytes.fromhex("050109"))  # usage item missing its data byte
        parse_descriptor(bytes.fromhex("fe"))


class ScanTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fs = FakeSysfs(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def mice(self):
        return fingerprint.mice(self.fs.sysfs, self.fs.dev)

    def test_receiver_interfaces_group_into_one_mouse(self):
        self.fs.add_hsk()
        (fp,) = self.mice()
        self.assertEqual(fp.vidpid, "33e4:5807")
        self.assertEqual(fp.key, "33e4:5807@usb-0000:07:00.4-1.3")
        self.assertEqual([n.interface for n in fp.nodes], [0, 1, 2])
        self.assertTrue(fp.accessible())

    def test_keyboard_with_mouse_keys_is_not_a_mouse(self):
        phys = "usb-0000:07:00.4-1.2/input"
        common = dict(vid=0x03A8, pid=0xA649, name="Drop Preonic")
        self.fs.add(**common, phys=phys + "0", desc=KEYBOARD_DESC, boot_protocol=1)
        self.fs.add(**common, phys=phys + "2", desc=COMPOSITE_DESC, boot_protocol=0)
        self.assertEqual(self.mice(), [])

    def test_usb_mouse_without_boot_support_falls_back_to_mouse_only_interface(self):
        self.fs.add(vid=0x1234, pid=0x0001, name="Bootless", phys="usb-1/input0",
                    desc=MOUSE_DESC, boot_protocol=0)
        self.assertEqual(len(self.mice()), 1)

    def test_bluetooth_mouse_is_found_and_keyed_by_uniq(self):
        for uniq in ("aa:bb", "cc:dd"):
            self.fs.add(vid=0x1234, pid=0x0002, name="BT", phys="11:22:33:44:55:66",
                        desc=MOUSE_DESC, bus=5, uniq=uniq)
        keys = sorted(fp.key for fp in self.mice())
        self.assertEqual(keys, ["1234:0002@11:22:33:44:55:66#aa:bb", "1234:0002@11:22:33:44:55:66#cc:dd"])

    def test_malformed_hid_id_is_skipped(self):
        self.fs.add(vid=1, pid=1, name="x", phys="usb-1/input0", desc=MOUSE_DESC,
                    boot_protocol=2, hid_id="garbage")
        self.assertEqual(fingerprint.scan(self.fs.sysfs, self.fs.dev), [])

    def test_control_characters_are_stripped_from_names(self):
        self.fs.add(vid=1, pid=1, name="Evil\x1b[2JMouse", phys="usb-1/input0",
                    desc=MOUSE_DESC, boot_protocol=2)
        self.assertEqual(self.mice()[0].name, "Evil[2JMouse")

    def test_missing_sysfs_is_an_empty_machine(self):
        self.assertEqual(fingerprint.scan("/nonexistent/hidraw", self.fs.dev), [])


if __name__ == "__main__":
    unittest.main()
