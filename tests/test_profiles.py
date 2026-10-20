import json
import os
import stat
import tempfile
import unittest

import fakes  # noqa: F401 -- puts the repo on sys.path

from mousectrl import profiles
from mousectrl.profiles import CapabilityState, Profile, ProfileError

HSK = Profile("hsk", "HSK", "family:gwolves_hts", "33e4", ("5807", "5808"),
              {"dpi": CapabilityState("verified"), "debounce": CapabilityState("readOnly", False)})


class ProfilesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self._tmp.name, "profiles")

    def tearDown(self):
        self._tmp.cleanup()

    def write_raw(self, name, doc, mode=0o600):
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            json.dump(doc, fh)
        os.chmod(path, mode)
        return path

    def test_round_trip_is_private(self):
        path = profiles.save(HSK, self.dir)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        loaded, problems = profiles.load_all(self.dir)
        self.assertEqual(problems, [])
        self.assertEqual(loaded, [HSK])
        self.assertEqual(loaded[0].family, "gwolves_hts")

    def test_missing_directory_is_no_profiles(self):
        self.assertEqual(profiles.load_all(self.dir), ([], []))

    def test_each_bad_field_is_named(self):
        cases = {
            "schemaVersion": {"schemaVersion": 2},
            "id": {"id": "../etc"},
            "name": {"name": "a\x1bb"},
            "backend": {"backend": "family:../x"},
            "vendorId": {"match": {"vendorId": "33E4", "productIds": ["5807"]}},
            "productIds": {"match": {"vendorId": "33e4", "productIds": []}},
            "state": {"capabilities": {"dpi": {"state": "yolo"}}},
            "visible": {"capabilities": {"dpi": {"state": "verified", "visible": "yes"}}},
        }
        for field, override in cases.items():
            doc = {**HSK.as_dict(), **override}
            with self.subTest(field=field), self.assertRaisesRegex(ProfileError, field):
                profiles.from_dict(doc)

    def test_one_broken_file_does_not_hide_the_others(self):
        profiles.save(HSK, self.dir)
        self.write_raw("broken.json", {"schemaVersion": 1})
        loaded, problems = profiles.load_all(self.dir)
        self.assertEqual([p.id for p in loaded], ["hsk"])
        self.assertEqual(len(problems), 1)
        self.assertIn("broken.json", problems[0])

    def test_file_name_must_match_id(self):
        self.write_raw("other.json", HSK.as_dict())
        loaded, problems = profiles.load_all(self.dir)
        self.assertEqual(loaded, [])
        self.assertIn("must be hsk.json", problems[0])

    def test_group_writable_file_is_refused(self):
        self.write_raw("hsk.json", HSK.as_dict(), mode=0o620)
        loaded, problems = profiles.load_all(self.dir)
        self.assertEqual(loaded, [])
        self.assertIn("writable by other users", problems[0])

    def test_writable_states(self):
        self.assertTrue(CapabilityState("verified").writable)
        self.assertTrue(CapabilityState("tested").writable)
        self.assertFalse(CapabilityState("readOnly").writable)
        self.assertFalse(CapabilityState("rejected").writable)


if __name__ == "__main__":
    unittest.main()
