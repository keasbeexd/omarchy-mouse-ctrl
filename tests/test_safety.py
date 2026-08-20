"""Things that must be true before hskctl talks to hardware.

All three came out of a marketplace security review of v1.3.1. None of them
were bugs in the protocol -- the protocol was fine. They were bugs in what the
code was willing to *do*: write through a symlink, write to a device it had not
identified, and write a mapping the profile itself said was unproven.
"""

import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hskctl import device  # noqa: E402
from hskctl.hidraw import HidrawInfo  # noqa: E402
from hskctl.protocol import load_profile  # noqa: E402

PROFILE = "gwolves-hsk-pro-4k"


class LockFileTests(unittest.TestCase):
    """The lock must not be a lever for truncating someone else's file.

    `/tmp/hskctl-<uid>.lock` opened with `open(path, "w")` is a predictable
    name in a world-writable directory, opened in a mode that follows symlinks
    and truncates. Any local user could point it at a file of their choosing
    and wait.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved_tempdir = tempfile.tempdir
        self._saved_runtime = os.environ.pop("XDG_RUNTIME_DIR", None)
        tempfile.tempdir = self.tmp.name
        device._LOCK_HANDLE = None

    def tearDown(self):
        device._release_device_lock()
        tempfile.tempdir = self._saved_tempdir
        if self._saved_runtime is not None:
            os.environ["XDG_RUNTIME_DIR"] = self._saved_runtime
        self.tmp.cleanup()

    def test_the_lock_lives_in_a_directory_only_we_can_write(self):
        path = device._lock_dir()
        info = os.lstat(path)
        self.assertTrue(stat.S_ISDIR(info.st_mode))
        self.assertEqual(info.st_uid, os.getuid())
        self.assertFalse(info.st_mode & 0o077, "group/other must have no access")

    def test_it_refuses_to_write_through_a_symlink(self):
        victim = os.path.join(self.tmp.name, "precious.conf")
        with open(victim, "w") as fh:
            fh.write("IMPORTANT DATA\n")
        os.mkdir(self.expected_dir(), 0o700)
        os.symlink(victim, os.path.join(self.expected_dir(), "hskctl.lock"))

        with self.assertRaises(device.UnsafeLockPath):
            device.acquire_device_lock()

        with open(victim) as fh:
            self.assertEqual(fh.read(), "IMPORTANT DATA\n", "the file was truncated")

    def expected_dir(self):
        # Computed, not obtained from _lock_dir() -- that call creates the
        # directory, which would defeat the point of these two tests.
        return os.path.join(self.tmp.name, f"hskctl-{os.getuid()}")

    def test_it_refuses_a_lock_directory_others_can_write(self):
        os.mkdir(self.expected_dir(), 0o777)
        os.chmod(self.expected_dir(), 0o777)
        with self.assertRaises(device.UnsafeLockPath):
            device.acquire_device_lock()

    def test_it_does_not_truncate_an_existing_lock_file(self):
        """A lock file has no contents worth clearing.

        Truncating was gratuitous -- it destroyed data for no reason, which is
        precisely what made the symlink worth exploiting.
        """
        os.mkdir(self.expected_dir(), 0o700)
        with open(os.path.join(self.expected_dir(), "hskctl.lock"), "w") as fh:
            fh.write("marker")
        device.acquire_device_lock()
        with open(device._lock_path()) as fh:
            self.assertEqual(fh.read(), "marker")

    def test_a_normal_run_still_takes_the_lock(self):
        device.acquire_device_lock()
        self.assertIsNotNone(device._LOCK_HANDLE)


def fake_node(path="/dev/hidraw9", vid=0x33E4, pid=0x5807, page=0xFFFF,
              features=(0,), interface=2, name="G-Wolves HSK"):
    return HidrawInfo(
        path=path, bustype=3, vendor_id=vid, product_id=pid, name=name,
        phys="usb-0000:00:14.0-1/input0",
        usage_page=page, usage=1, interface=interface,
        feature_report_ids=list(features),
    )


class DeviceIdentityTests(unittest.TestCase):
    """Scoring ranks candidates. It must not be what authorises a write.

    A device with a vendor usage page and a feature report scores 35 in
    rank_candidates without matching a single id the profile declares, and
    open_session used to take the top of the list unconditionally. Vendor
    feature reports aimed at the wrong hardware are how devices get bricked.
    """

    def setUp(self):
        self.profile = load_profile(PROFILE)

    def rank(self, nodes):
        original = device.enumerate_devices
        device.enumerate_devices = lambda: nodes
        try:
            return device.rank_candidates(self.profile)
        finally:
            device.enumerate_devices = original

    def test_the_real_mouse_is_identified(self):
        found = self.rank([fake_node()])
        self.assertTrue(found[0].identified)
        self.assertEqual(found[0].unmatched, [])

    def test_a_stranger_with_a_vendor_page_scores_but_is_not_identified(self):
        stranger = fake_node(vid=0x1234, pid=0x9999, name="Some Other Device")
        found = self.rank([stranger])
        self.assertTrue(found, "it should still be listed for the user to see")
        self.assertGreater(found[0].score, 0, "still worth showing in probe")
        self.assertFalse(found[0].identified, "but never auto-selected")
        self.assertTrue(found[0].unmatched)

    def test_the_right_vendor_but_an_unknown_product_is_not_identified(self):
        found = self.rank([fake_node(pid=0xABCD)])
        self.assertFalse(found[0].identified)

    def test_a_node_with_no_feature_report_is_not_identified(self):
        found = self.rank([fake_node(features=())])
        self.assertFalse(found[0].identified)

    def test_open_session_refuses_to_guess(self):
        stranger = fake_node(vid=0x1234, pid=0x9999, name="Some Other Device")
        original = device.enumerate_devices
        device.enumerate_devices = lambda: [stranger]
        try:
            with self.assertRaises(device.DeviceNotFound) as caught:
                device.open_session(self.profile)
        finally:
            device.enumerate_devices = original
        message = str(caught.exception)
        self.assertIn("--device", message, "must say how to proceed deliberately")


class UnidentifiedWriteTests(unittest.TestCase):
    """Naming a node explicitly permits reading it, not writing to it."""

    def session(self, identified):
        session = device.Session.__new__(device.Session)
        session.profile = load_profile(PROFILE)
        session.info = fake_node()
        session.wireless = True
        session.identified = identified
        session.allow_unidentified_writes = False
        session._link_detected = True
        session.trace = None
        return session

    def test_an_unidentified_device_refuses_writes(self):
        with self.assertRaises(Exception) as caught:
            self.session(False)._guard_write()
        self.assertIn("--force-unmatched", str(caught.exception))

    def test_an_identified_device_writes_normally(self):
        self.session(True)._guard_write()

    def test_the_override_exists_for_profiling_new_hardware(self):
        session = self.session(False)
        session.allow_unidentified_writes = True
        session._guard_write()


class UnverifiedMappingTests(unittest.TestCase):
    """A mapping the profile calls unproven must not be writable.

    The profile shipped `dpiStageCount` and the sleep timer as writable while
    declaring both mappings unverified. That contradiction is the same shape as
    the blind write that corrupted a real mouse earlier in this project, so the
    marker now closes the write path instead of just printing a caveat.
    """

    def setUp(self):
        self.profile = load_profile(PROFILE)

    def test_no_field_is_both_writable_and_unverified(self):
        offenders = [
            name for name, spec in self.profile.data["fields"].items()
            if isinstance(spec, dict)
            and spec.get("_needsVerification")
            and self.profile.field_writable(name)
        ]
        self.assertEqual(
            offenders, [],
            "verify these on hardware and remove the marker, or mark them "
            "readOnly -- do not ship them writable",
        )

    def test_the_marker_is_what_closes_the_write(self):
        # Not readOnly, not an unwritable command -- the marker alone.
        spec = dict(self.profile.data["fields"]["pollingRate"])
        self.assertTrue(self.profile.field_writable("pollingRate"))
        self.profile.data["fields"]["pollingRate"] = {
            **spec, "_needsVerification": "pretend nobody checked",
        }
        try:
            self.assertFalse(self.profile.field_writable("pollingRate"))
        finally:
            self.profile.data["fields"]["pollingRate"] = spec


if __name__ == "__main__":
    unittest.main()
