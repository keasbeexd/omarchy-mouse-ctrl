import json
import os
import tempfile
import unittest

import fakes  # noqa: F401 -- puts the repo on sys.path

from mousectrl.ratbag import Ratbag, RatbagError

ROOT = "/org/freedesktop/ratbag1"
DEVICE = ROOT + "/device/hidraw3"


def v(t, data):
    return {"type": t, "data": data}


class FakeDaemon:
    """Answers `busctl --json=short get-property` like ratbagd API 2."""

    def __init__(self):
        res = ROOT + "/resolution/hidraw3/p0/r"
        self.props = {
            (ROOT, "Manager"): {"APIVersion": v("i", 2), "Devices": v("ao", [DEVICE])},
            (DEVICE, "Device"): {"Name": v("s", "Logitech G Pro"), "FirmwareVersion": v("s", "12.1"),
                                 "Profiles": v("ao", [ROOT + "/profile/hidraw3/p0", ROOT + "/profile/hidraw3/p1"])},
            (ROOT + "/profile/hidraw3/p0", "Profile"): {"IsActive": v("b", False), "Disabled": v("b", False)},
            (ROOT + "/profile/hidraw3/p1", "Profile"): {
                "IsActive": v("b", True), "Disabled": v("b", False), "ReportRate": v("u", 1000),
                "Resolutions": v("ao", [res + "0", res + "1", res + "2"])},
            (res + "0", "Resolution"): {"IsActive": v("b", False), "IsDisabled": v("b", False),
                                        "Resolution": v("v", v("u", 800))},
            (res + "1", "Resolution"): {"IsActive": v("b", True), "IsDisabled": v("b", False),
                                        "Resolution": v("v", [v("(uu)", [1600, 1600])])},
            (res + "2", "Resolution"): {"IsActive": v("b", False), "IsDisabled": v("b", True),
                                        "Resolution": v("v", v("u", 3200))},
        }
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        assert args[0] == "get-property" and args[1] == "org.freedesktop.ratbag1"
        path, iface, names = args[2], args[3].rsplit(".", 1)[-1], args[4:]
        if (path, iface) not in self.props:
            raise RatbagError("Unknown object")
        return "".join(json.dumps(self.props[(path, iface)][n]) + "\n" for n in names)


class RatbagTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.service_file = os.path.join(self._tmp.name, "org.freedesktop.ratbag1.service")
        open(self.service_file, "w").close()
        self.daemon = FakeDaemon()
        self.ratbag = Ratbag(self.daemon, self.service_file)

    def tearDown(self):
        self._tmp.cleanup()

    def test_available_needs_the_service_and_api_2(self):
        self.assertTrue(self.ratbag.available())
        self.daemon.props[(ROOT, "Manager")]["APIVersion"] = v("i", 3)
        self.assertFalse(self.ratbag.available())
        self.assertFalse(Ratbag(self.daemon, "/nonexistent").available())

    def test_device_matched_by_hidraw_name(self):
        self.assertEqual(self.ratbag.device_path({"hidraw2", "hidraw3"}), DEVICE)
        self.assertIsNone(self.ratbag.device_path({"hidraw9"}))

    def test_reads_the_active_profile_and_skips_disabled_stages(self):
        self.assertEqual(self.ratbag.read(DEVICE), {
            "name": "Logitech G Pro", "firmware": "12.1", "profile": 2, "profiles": 2,
            "pollingRate": 1000,
            "stages": [{"dpi": 800, "active": False}, {"dpi": 1600, "active": True}],
        })

    def test_no_active_profile_is_an_error(self):
        self.daemon.props[(ROOT + "/profile/hidraw3/p1", "Profile")]["IsActive"] = v("b", False)
        with self.assertRaisesRegex(RatbagError, "no active profile"):
            self.ratbag.read(DEVICE)

    def test_device_strings_are_cleaned(self):
        self.daemon.props[(DEVICE, "Device")]["Name"] = v("s", "G Pro\x1b]52;c;evil\x07\u202e")
        self.assertEqual(self.ratbag.read(DEVICE)["name"], "G Pro]52;c;evil")

    def test_short_output_is_an_error(self):
        ratbag = Ratbag(lambda args: '{"type":"s","data":"x"}\n', self.service_file)
        with self.assertRaisesRegex(RatbagError, "expected 3 values"):
            ratbag.read(DEVICE)



class BusctlTest(unittest.TestCase):
    """The real runner against stand-in programs: caps and deadlines hold."""

    def run_with(self, script):
        from unittest import mock
        from mousectrl import ratbag
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        fake = os.path.join(tmp.name, "busctl")
        with open(fake, "w") as fh:
            fh.write("#!/usr/bin/bash\n" + script)
        os.chmod(fake, 0o700)
        with mock.patch.object(ratbag, "BUSCTL", fake), mock.patch.object(ratbag, "_TIMEOUT", 1.0):
            return ratbag._busctl(["get-property"])

    def test_normal_output(self):
        self.assertEqual(self.run_with('echo \'{"type":"i","data":2}\''), '{"type":"i","data":2}\n')

    def test_oversized_output_is_refused(self):
        with self.assertRaisesRegex(RatbagError, "exceeded"):
            self.run_with("head -c 200000 /dev/zero")

    def test_hanging_busctl_is_killed(self):
        with self.assertRaisesRegex(RatbagError, "in time"):
            self.run_with("sleep 30")

    def test_failure_exit_is_an_error(self):
        with self.assertRaisesRegex(RatbagError, "exited 1"):
            self.run_with("exit 1")


if __name__ == "__main__":
    unittest.main()
