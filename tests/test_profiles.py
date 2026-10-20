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



class HostileFilesystemTest(unittest.TestCase):
    """The attacks a same-user process can stage at the profiles path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.dir = os.path.join(self.root, "mousectrl", "profiles")
        self.victim = os.path.join(self.root, "victim")
        with open(self.victim, "w") as fh:
            fh.write("must survive")

    def tearDown(self):
        self._tmp.cleanup()

    def test_symlink_planted_at_the_profile_is_replaced_not_followed(self):
        profiles.save(HSK, self.dir)
        os.remove(os.path.join(self.dir, "hsk.json"))
        os.symlink(self.victim, os.path.join(self.dir, "hsk.json"))
        profiles.save(HSK, self.dir)
        with open(self.victim) as fh:
            self.assertEqual(fh.read(), "must survive")
        self.assertFalse(os.path.islink(os.path.join(self.dir, "hsk.json")))
        self.assertEqual(profiles.load_all(self.dir)[0], [HSK])

    def test_symlinked_profiles_directory_is_refused(self):
        elsewhere = os.path.join(self.root, "elsewhere")
        os.makedirs(elsewhere)
        os.makedirs(os.path.join(self.root, "mousectrl"))
        os.symlink(elsewhere, self.dir)
        with self.assertRaises(OSError):
            profiles.save(HSK, self.dir)
        loaded, problems = profiles.load_all(self.dir)
        self.assertEqual(loaded, [])
        self.assertIn("not a plain directory", problems[0])
        self.assertEqual(os.listdir(elsewhere), [])

    def test_hard_linked_profile_is_refused(self):
        profiles.save(HSK, self.dir)
        os.link(os.path.join(self.dir, "hsk.json"), os.path.join(self.root, "other-name"))
        loaded, problems = profiles.load_all(self.dir)
        self.assertEqual(loaded, [])
        self.assertIn("hard links", problems[0])

    def test_open_directory_is_narrowed_and_no_temporaries_remain(self):
        os.makedirs(self.dir, mode=0o777)
        os.chmod(self.dir, 0o777)
        profiles.save(HSK, self.dir)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        self.assertEqual(os.listdir(self.dir), ["hsk.json"])


if __name__ == "__main__":
    unittest.main()
