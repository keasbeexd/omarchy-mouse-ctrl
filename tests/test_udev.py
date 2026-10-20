import os
import tempfile
import unittest

from fakes import FakeDriver

from mousectrl import udev
from mousectrl.profiles import Profile

UNKNOWN = Profile("acme", "Acme", "generic", "1234", ("0001",))
COVERED = Profile("hsk", "HSK", "family:fake", "33e4", ("5807",))


class RenderTest(unittest.TestCase):
    def test_family_vendor_line_covers_its_profiles(self):
        text = udev.render((FakeDriver,), [COVERED])
        self.assertIn('ATTRS{idVendor}=="33e4", MODE="0660"', text)
        self.assertNotIn("idProduct", text)

    def test_unknown_vendor_gets_a_tight_pair_line(self):
        text = udev.render((FakeDriver,), [UNKNOWN], ((0xABCD, 0x0002),))
        self.assertIn('ATTRS{idVendor}=="1234", ATTRS{idProduct}=="0001"', text)
        self.assertIn('ATTRS{idVendor}=="abcd", ATTRS{idProduct}=="0002"', text)
        self.assertNotIn('ATTRS{idVendor}=="1234", MODE', text)


class InstalledStatusTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.rule_file = os.path.join(self._tmp.name, "60-mousectrl.rules")
        self.text = udev.render((FakeDriver,), [UNKNOWN])

    def tearDown(self):
        self._tmp.cleanup()

    def install(self, text):
        with open(self.rule_file, "w") as fh:
            fh.write(text)

    def test_absent(self):
        self.assertEqual(udev.installed_status(self.text, self.rule_file),
                         {"present": False, "upToDate": False})

    def test_comment_changes_do_not_make_it_stale(self):
        self.install(self.text.replace("# Mouse CTRL", "# An older header"))
        self.assertTrue(udev.installed_status(self.text, self.rule_file)["upToDate"])

    def test_missing_line_is_stale(self):
        self.install(udev.render((FakeDriver,), []))
        self.assertEqual(udev.installed_status(self.text, self.rule_file),
                         {"present": True, "upToDate": False})


if __name__ == "__main__":
    unittest.main()
