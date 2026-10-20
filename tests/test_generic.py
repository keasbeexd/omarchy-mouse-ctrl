import os
import tempfile
import unittest
from unittest import mock

from fakes import FakeSysfs

from mousectrl import fingerprint, generic
from mousectrl.errors import NoAccess, NoResponse
from mousectrl.fingerprint import parse_descriptor

# The HSK's real pointer descriptor head: Button page, Usage Min 1, Max 5.
HSK_POINTER = bytes.fromhex(
    "05010902a1010901a10005091901290515002501750195058102750195038103"
    "050109381581257f7508950181060930093116008026ff7f751095028106c0c0"
)


class ButtonsTest(unittest.TestCase):
    def test_counts_the_button_range_of_an_input_item(self):
        self.assertEqual(parse_descriptor(HSK_POINTER).buttons, 5)

    def test_no_button_page_is_zero(self):
        self.assertEqual(parse_descriptor(bytes.fromhex("06ffff0900a1010900b102c0")).buttons, 0)


class GenericTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fs = FakeSysfs(self._tmp.name)
        common = dict(vid=0x1234, pid=0x0001, name="Mystery Mouse")
        self.pointer = self.fs.add(**common, phys="usb-1/input0", desc=HSK_POINTER, boot_protocol=2)
        self.fs.add(**common, phys="usb-1/input1", desc=bytes.fromhex("06ffff0900a1010900b102c0"), boot_protocol=0)
        (self.fp,) = fingerprint.mice(self.fs.sysfs, self.fs.dev)

    def tearDown(self):
        self._tmp.cleanup()

    def add_battery(self, capacity, status):
        base = os.path.join(self.fs.sysfs, "hidraw0", "device", "power_supply", "hid-mystery-battery")
        os.makedirs(base)
        for name, value in (("capacity", capacity), ("status", status)):
            with open(os.path.join(base, name), "w") as fh:
                fh.write(value + "\n")

    def test_pointer_node_and_buttons(self):
        self.assertEqual(self.fp.pointer_node.path, self.pointer)
        self.assertEqual(self.fp.buttons, 5)

    def test_kernel_battery(self):
        self.assertIsNone(generic.kernel_battery(self.fp, self.fs.sysfs))
        self.add_battery("64", "Charging")
        self.assertEqual(generic.kernel_battery(self.fp, self.fs.sysfs), {"percent": 64, "charging": True})

    def test_garbage_battery_is_ignored(self):
        self.add_battery("lots", "Full")
        self.assertIsNone(generic.kernel_battery(self.fp, self.fs.sysfs))

    def test_report_names_every_interface(self):
        text = generic.report(self.fp)
        self.assertIn("1234:0001  (usb)", text)
        self.assertIn("buttons    5 declared", text)
        self.assertEqual(text.count("descriptor "), 2)

    def measure_with(self, gaps):
        clock = {"t": 0.0}
        reports = list(gaps)

        class Device:
            def __init__(self, path): pass
            def __enter__(self): return self
            def __exit__(self, *exc): pass
            def read_input(self, length, timeout):
                if not reports:
                    clock["t"] += timeout
                    return None
                clock["t"] += reports.pop(0)
                return b"\x00"

        with mock.patch.object(generic, "HidrawDevice", Device), \
             mock.patch.object(generic.time, "monotonic", lambda: clock["t"]):
            return generic.measure_polling(self.fp, seconds=2.5)

    def test_measures_the_report_rate(self):
        self.assertEqual(self.measure_with([0.004] * 500)["hz"], 250)

    def test_too_few_reports_says_to_move_the_mouse(self):
        with self.assertRaisesRegex(NoResponse, "move the mouse"):
            self.measure_with([0.1] * 5)

    def test_no_access_is_reported(self):
        os.chmod(self.pointer, 0o000)
        with self.assertRaises(NoAccess):
            generic.measure_polling(self.fp)
