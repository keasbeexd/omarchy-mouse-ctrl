"""The re-apply baseline must never fight the user.

`hskctl apply` is started by a udev rule every time the mouse enumerates, so a
baseline that goes stale stops being a safety net and becomes a machine that
undoes your settings on the next reconnect -- with nothing on screen to explain
it. These tests pin the two things that keep that from happening: a successful
write updates the baseline, and it never conjures one out of nowhere.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hskctl import cli  # noqa: E402


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "settings.json")
        self._saved = cli.SETTINGS_PATH
        cli.SETTINGS_PATH = self.path

    def tearDown(self):
        cli.SETTINGS_PATH = self._saved
        self.tmp.cleanup()

    def write(self, settings):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"model": "test", "settings": settings}, fh)

    def read(self):
        with open(self.path, "r", encoding="utf-8") as fh:
            return json.load(fh)["settings"]

    def test_a_write_updates_the_saved_baseline(self):
        self.write({"dpiStage1": 400, "pollingRate": 1000})
        cli._refresh_baseline("dpiStage1", 1600)
        self.assertEqual(self.read()["dpiStage1"], 1600)

    def test_other_fields_are_left_alone(self):
        self.write({"dpiStage1": 400, "pollingRate": 1000})
        cli._refresh_baseline("dpiStage1", 1600)
        self.assertEqual(self.read()["pollingRate"], 1000)

    def test_it_never_creates_a_baseline(self):
        # Opting in to restore-on-reconnect is a decision, not a side effect of
        # setting DPI.
        cli._refresh_baseline("dpiStage1", 1600)
        self.assertFalse(os.path.exists(self.path))

    def test_a_field_absent_from_the_baseline_is_not_added(self):
        self.write({"pollingRate": 1000})
        cli._refresh_baseline("dpiStage1", 1600)
        self.assertNotIn("dpiStage1", self.read())

    def test_a_corrupt_baseline_does_not_raise(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        cli._refresh_baseline("dpiStage1", 1600)

    def test_doctor_reports_whether_re_apply_is_armed(self):
        self.write({"dpiStage1": 400})
        state = cli._autoapply_state()
        self.assertEqual(state["baseline"], {"dpiStage1": 400})
        self.assertIn("armed", state)


if __name__ == "__main__":
    unittest.main()
