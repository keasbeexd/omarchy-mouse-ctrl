import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from fakes import FakeDriver, FakeSysfs

from mousectrl import fingerprint, profiles
from mousectrl.api import Api
from mousectrl.errors import (
    DeviceNotFound, InvalidValue, NoAccess, ProtocolFailure, Unsupported, WriteRefused,
)
from mousectrl.profiles import CapabilityState, Profile
from mousectrl.server import Server

KEY = "33e4:5807@usb-0000:07:00.4-1.3"
STAGES = {"active": 0, "stages": [{"dpi": 800, "color": "#00ff00"}]}


class ApiTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fs = FakeSysfs(self._tmp.name)
        self.nodes = self.fs.add_hsk()
        self.profile_dir = os.path.join(self._tmp.name, "profiles")
        FakeDriver.values = {"battery": {"percent": 82, "charging": False}, "pollingRate": 4000,
                             "motionSync": True, "dpi": STAGES}
        FakeDriver.readback_override = {}
        FakeDriver.fail_with = None
        FakeDriver.closed = []
        self.api = Api(
            families=(FakeDriver,),
            scan=lambda: fingerprint.mice(self.fs.sysfs, self.fs.dev),
            profile_dir=self.profile_dir,
            rule_file=os.path.join(self._tmp.name, "rules"),
            lock=contextlib.nullcontext,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def save_profile(self, **states):
        caps = {cap_id: CapabilityState(state) for cap_id, state in states.items()}
        profiles.save(Profile("hsk", "HSK", "family:fake", "33e4", ("5807",), caps), self.profile_dir)


class DevicesTest(ApiTestCase):
    def test_lists_family_and_profile(self):
        self.save_profile()
        (dev,) = self.api.devices()["devices"]
        self.assertEqual(dev["key"], KEY)
        self.assertEqual(dev["family"], {"id": "fake", "title": "Fake family"})
        self.assertEqual(dev["profile"], {"id": "hsk", "name": "HSK"})

    def test_unknown_mouse_has_no_family(self):
        api = Api(families=(), scan=self.api._scan, profile_dir=self.profile_dir,
                  lock=contextlib.nullcontext)
        self.assertIsNone(api.devices()["devices"][0]["family"])
        with self.assertRaises(Unsupported):
            api.read(KEY)

    def test_resolve_by_vidpid_and_unknown_key(self):
        self.assertEqual(self.api.read("33e4:5807")["device"], KEY)
        with self.assertRaises(DeviceNotFound):
            self.api.read("dead:beef")

    def test_no_access_is_reported_before_opening(self):
        os.chmod(self.nodes[2], 0o000)
        with self.assertRaises(NoAccess):
            self.api.read(KEY)


class ReadTest(ApiTestCase):
    def test_read_all_and_one(self):
        self.assertEqual(self.api.read(KEY)["values"]["pollingRate"], 4000)
        self.assertEqual(self.api.read(KEY, "motionSync")["values"], {"motionSync": True})

    def test_protocol_failure_is_reported_per_capability(self):
        FakeDriver.fail_with = ProtocolFailure("all-zero reply")
        result = self.api.read(KEY, "battery")
        self.assertEqual(result["values"], {})
        self.assertEqual(result["errors"], {"battery": "all-zero reply"})

    def test_io_error_drops_the_cached_driver(self):
        self.api.read(KEY)
        FakeDriver.fail_with = OSError(19, "No such device")
        with self.assertRaises(OSError):
            self.api.read(KEY)
        self.assertEqual(FakeDriver.closed, [KEY])


class WriteGateTest(ApiTestCase):
    def test_no_profile_refuses(self):
        with self.assertRaisesRegex(WriteRefused, "run the setup wizard"):
            self.api.write(KEY, "pollingRate", 1000)

    def test_read_only_state_refuses(self):
        self.save_profile(motionSync="readOnly")
        with self.assertRaisesRegex(WriteRefused, "readOnly"):
            self.api.write(KEY, "motionSync", False)

    def test_tested_state_writes(self):
        self.save_profile(motionSync="tested")
        self.assertIs(self.api.write(KEY, "motionSync", False)["value"], False)

    def test_value_is_validated_before_the_driver_sees_it(self):
        self.save_profile(pollingRate="verified")
        with self.assertRaises(InvalidValue):
            self.api.write(KEY, "pollingRate", 1234)
        self.assertEqual(FakeDriver.values["pollingRate"], 4000)

    def test_readback_mismatch_fails_loudly(self):
        self.save_profile(pollingRate="verified")
        FakeDriver.readback_override = {"pollingRate": 1000}
        with self.assertRaisesRegex(ProtocolFailure, "reads back 1000"):
            self.api.write(KEY, "pollingRate", 2000)

    def test_capabilities_report_writability(self):
        self.save_profile(pollingRate="verified", motionSync="readOnly", battery="verified")
        caps = {c["id"]: c for c in self.api.capabilities(KEY)["capabilities"]}
        self.assertTrue(caps["pollingRate"]["writable"])
        self.assertFalse(caps["motionSync"]["writable"])
        self.assertFalse(caps["battery"]["writable"])
        self.assertIsNone(caps["dpi"]["state"])


class ServerTest(ApiTestCase):
    def reply(self, line):
        return Server(self.api, io.StringIO()).handle_line(line.encode())

    def test_reply_carries_id(self):
        self.assertEqual(self.reply('{"id": 7, "cmd": "devices"}')["id"], 7)

    def test_errors_are_structured(self):
        cases = {
            "not json": "badRequest",
            "[1]": "badRequest",
            '{"id": true, "cmd": "devices"}': "badRequest",
            '{"cmd": "dance"}': "badRequest",
            '{"cmd": "read"}': "badRequest",
            '{"cmd": "read", "device": "dead:beef"}': "notFound",
            f'{{"cmd": "write", "device": "{KEY}", "capability": "pollingRate", "value": 1}}': "writeRefused",
            '{"cmd": "udev-rule", "extra": ["nope"]}': "invalidValue",
        }
        for line, code in cases.items():
            with self.subTest(line=line):
                reply = self.reply(line)
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["code"], code)

    def test_run_emits_one_line_per_request_and_rejects_oversized(self):
        out = io.StringIO()
        server = Server(self.api, out)
        server._watch_or_report = lambda: None
        oversized = b'{"cmd": "devices", "pad": "' + b"x" * 70000 + b'"}\n'
        server.run(io.BytesIO(b'{"id": 1, "cmd": "devices"}\n\n' + oversized + b'{"id": 2, "cmd": "devices"}\n'))
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r.get("id") for r in replies], [1, None, 2])
        self.assertEqual(replies[1]["code"], "badRequest")


if __name__ == "__main__":
    unittest.main()


class TestWriteTest(ApiTestCase):
    def test_untested_capability_passes_and_is_restored(self):
        result = self.api.test_write(KEY, "motionSync")
        self.assertTrue(result["passed"])
        self.assertIs(result["wrote"], False)
        self.assertIs(FakeDriver.values["motionSync"], True)

    def test_readback_mismatch_fails_without_recording(self):
        FakeDriver.readback_override = {"motionSync": True}
        self.assertFalse(self.api.test_write(KEY, "motionSync")["passed"])
        with self.assertRaisesRegex(InvalidValue, "not passed a write test"):
            self.api.save_profile(KEY, "HSK", {"motionSync": {"state": "tested"}})

    def test_failed_restore_names_the_original(self):
        calls = []

        def write(driver, capability_id, value):
            calls.append(value)
            if len(calls) == 2:
                raise ProtocolFailure("no ack")
            return value

        with mock.patch.object(FakeDriver, "write", write):
            with self.assertRaisesRegex(ProtocolFailure, "could not restore the original value True"):
                self.api.test_write(KEY, "motionSync")

    def test_dpi_stages_nudge_stage_one(self):
        result = self.api.test_write(KEY, "dpi")
        self.assertEqual(result["wrote"]["stages"][0]["dpi"], 850)
        self.assertEqual(FakeDriver.values["dpi"], STAGES)

    def test_read_only_capability_is_refused(self):
        with self.assertRaisesRegex(InvalidValue, "read-only"):
            self.api.test_write(KEY, "battery")


class SaveProfileTest(ApiTestCase):
    def test_tested_state_needs_a_passed_test(self):
        with self.assertRaisesRegex(InvalidValue, "not passed"):
            self.api.save_profile(KEY, "HSK", {"motionSync": {"state": "tested"}})
        self.api.test_write(KEY, "motionSync")
        saved = self.api.save_profile(KEY, "HSK", {"motionSync": {"state": "tested"}})
        self.assertEqual(saved["profile"]["capabilities"]["motionSync"]["state"], "tested")

    def test_rerun_keeps_tested_states_from_the_existing_profile(self):
        self.save_profile(motionSync="tested")
        fresh = Api(families=(FakeDriver,), scan=self.api._scan, profile_dir=self.profile_dir,
                    lock=contextlib.nullcontext)
        saved = fresh.save_profile(KEY, "HSK", {"motionSync": {"state": "tested"}})
        self.assertEqual(saved["profile"]["capabilities"]["motionSync"]["state"], "tested")

    def test_verified_state_needs_a_verified_driver_capability(self):
        with self.assertRaisesRegex(InvalidValue, "untested in the driver"):
            self.api.save_profile(KEY, "HSK", {"motionSync": {"state": "verified"}})
        self.api.save_profile(KEY, "HSK", {"pollingRate": {"state": "verified"}})

    def test_profile_covers_the_drivers_products_and_rerun_keeps_the_id(self):
        first = self.api.save_profile(KEY, "My HSK", {"pollingRate": {"state": "verified"}})
        self.assertEqual(first["profile"]["id"], "my-hsk")
        self.assertEqual(first["profile"]["match"], {"vendorId": "33e4", "productIds": ["5807"]})
        second = self.api.save_profile(KEY, "Renamed", {})
        self.assertEqual(second["profile"]["id"], "my-hsk")
        self.assertEqual(second["profile"]["name"], "Renamed")
        self.assertEqual(self.api.devices()["devices"][0]["profile"]["name"], "Renamed")

    def test_unknown_mouse_saves_a_generic_profile(self):
        api = Api(families=(), scan=self.api._scan, profile_dir=self.profile_dir,
                  lock=contextlib.nullcontext)
        saved = api.save_profile(KEY, "Mystery", {})
        self.assertEqual(saved["profile"]["backend"], "generic")
        with self.assertRaisesRegex(InvalidValue, "no capabilities"):
            api.save_profile(KEY, "Mystery", {"pollingRate": {"state": "verified"}})

    def test_bad_input_is_refused(self):
        for name, caps in ((7, {}), ("ok", []), ("ok", {"nope": {"state": "verified"}}),
                           ("ok", {"pollingRate": {"state": "verified", "visible": "yes"}}),
                           ("\x1b", {})):
            with self.subTest(name=name, caps=caps), self.assertRaises((InvalidValue, profiles.ProfileError)):
                self.api.save_profile(KEY, name, caps)

    def test_delete_profile(self):
        self.api.save_profile(KEY, "HSK", {})
        self.assertTrue(self.api.delete_profile("hsk")["deleted"])
        self.assertFalse(self.api.delete_profile("hsk")["deleted"])
        with self.assertRaises(profiles.ProfileError):
            self.api.delete_profile("../etc")
