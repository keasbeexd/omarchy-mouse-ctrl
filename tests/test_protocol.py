"""Round-trip tests for the protocol engine.

These do not need a mouse. They prove that once the profile is filled in with
real offsets, the encode/decode/checksum machinery does the right thing -- so
when the capture work lands, the only variable is the captured bytes.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hskctl.hidraw import parse_report_descriptor  # noqa: E402
from hskctl.protocol import (  # noqa: E402
    NotDiscovered,
    Profile,
    ProtocolError,
    parse_template,
)

# A plausible-shaped but entirely synthetic profile, used only to exercise the
# engine. Do NOT copy these offsets into the real profile.
FAKE = {
    "profileVersion": 1,
    "model": "Fake Mouse",
    "status": "verified",
    "transport": {
        "kind": "feature",
        "reportId": 8,
        "packetLength": 17,
        "readLength": 17,
        "payloadRange": [4, 16],
        "checksum": {"kind": "sum8_complement", "offset": 16, "range": [1, 16]},
    },
    "commands": {
        "readSettings": {"get": "08 04 00 00", "set": "08 04 00 00", "params": {}},
        "writeSettings": {"get": "08 05 00 00", "set": "08 05 00 00", "params": {}},
        "readStage": {
            "get": "08 06 00 00", "set": "08 06 00 00",
            "params": {"stage": {"offset": 3, "encoding": "u8", "min": 1, "max": 6}},
        },
    },
    "fields": {
        "pollingRate": {
            "command": "readSettings",
            "offset": 5,
            "encoding": "u8",
            "values": {"0": 125, "1": 250, "2": 500, "3": 1000, "4": 2000, "5": 4000},
        },
        "dpiStage1": {
            "command": "readSettings",
            "offset": 6,
            "encoding": "u16le",
            "scale": 50,
            "min": 50,
            "max": 26000,
        },
        "motionSync": {
            "command": "readSettings",
            "offset": 8,
            "encoding": "bit",
            "bit": 2,
            "type": "bool",
        },
        "liftOffDistance": {
            "command": "readSettings",
            "offset": 9,
            "encoding": "u8",
            "values": {"0": "1mm", "1": "2mm"},
        },
        "unmapped": {"command": "notDecodedYet", "offset": None, "encoding": "u8"},
    },
}


class TemplateTests(unittest.TestCase):
    def test_pads_and_skips(self):
        buf = parse_template("08 04 .. ff", 6)
        self.assertEqual(bytes(buf), b"\x08\x04\x00\xff\x00\x00")

    def test_truncates_to_length(self):
        buf = parse_template("01 02 03 04 05", 3)
        self.assertEqual(bytes(buf), b"\x01\x02\x03")


class ChecksumTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile(json.loads(json.dumps(FAKE)), "<memory>")

    def test_sum8_complement_makes_packet_sum_to_zero(self):
        packet = self.profile.build_request("readSettings", write=False)
        self.assertEqual(len(packet), 17)
        body_plus_checksum = sum(packet[1:17]) & 0xFF
        self.assertEqual(body_plus_checksum, 0)

    def test_checksum_covers_parameter_changes(self):
        a = self.profile.build_request("readStage", write=False)
        b = bytearray(a); b[3] = 4; b = bytes(self.profile.checksum(bytearray(b)))
        self.assertNotEqual(a, b)
        self.assertEqual(sum(b[1:17]) & 0xFF, 0)

    def test_xor_and_plain_sum(self):
        for kind, expect in (("xor8", lambda d: 0), ("sum8", lambda d: None)):
            data = json.loads(json.dumps(FAKE))
            data["transport"]["checksum"]["kind"] = kind
            p = Profile(data, "<memory>")
            packet = p.build_request("readSettings", write=False)
            self.assertEqual(len(packet), 17)


class FieldCodecTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile(json.loads(json.dumps(FAKE)), "<memory>")

    def _blank(self):
        return bytearray(17)

    def test_enum_round_trip(self):
        buf = self._blank()
        self.profile.encode_into(buf, "pollingRate", 4000)
        self.assertEqual(buf[5], 5)
        self.assertEqual(self.profile.decode("pollingRate", bytes(buf)), 4000)

    def test_enum_rejects_unknown_value(self):
        buf = self._blank()
        with self.assertRaises(ProtocolError) as ctx:
            self.profile.encode_into(buf, "pollingRate", 8000)
        self.assertIn("8000", str(ctx.exception))

    def test_scaled_u16_round_trip(self):
        buf = self._blank()
        self.profile.encode_into(buf, "dpiStage1", 1600)
        self.assertEqual(int.from_bytes(buf[6:8], "little"), 32)
        self.assertEqual(self.profile.decode("dpiStage1", bytes(buf)), 1600)

    def test_scaled_rejects_non_multiple(self):
        buf = self._blank()
        with self.assertRaises(ProtocolError):
            self.profile.encode_into(buf, "dpiStage1", 1601)

    def test_range_is_enforced(self):
        buf = self._blank()
        with self.assertRaises(ProtocolError):
            self.profile.encode_into(buf, "dpiStage1", 40000)

    def test_bit_field_does_not_clobber_neighbours(self):
        buf = self._blank()
        buf[8] = 0b1001_0001
        self.profile.encode_into(buf, "motionSync", True)
        self.assertEqual(buf[8], 0b1001_0101)
        self.assertIs(self.profile.decode("motionSync", bytes(buf)), True)
        self.profile.encode_into(buf, "motionSync", False)
        self.assertEqual(buf[8], 0b1001_0001)
        self.assertIs(self.profile.decode("motionSync", bytes(buf)), False)

    def test_string_enum_round_trip(self):
        buf = self._blank()
        self.profile.encode_into(buf, "liftOffDistance", "2mm")
        self.assertEqual(buf[9], 1)
        self.assertEqual(self.profile.decode("liftOffDistance", bytes(buf)), "2mm")

    def test_unmapped_field_raises_not_discovered(self):
        with self.assertRaises(NotDiscovered):
            self.profile.field("unmapped")
        self.assertFalse(self.profile.has_field("unmapped"))


class RealProfileTests(unittest.TestCase):
    """Guards on the shipped profile, now that it carries a real protocol.

    Everything asserted here was read out of G-Wolves' own binary, so these
    tests fail loudly if an edit drifts away from what the firmware expects.
    """

    def setUp(self):
        from hskctl.protocol import load_profile

        self.profile = load_profile()

    def test_transport_matches_hts_send_cmd(self):
        t = self.profile.transport
        self.assertEqual(t["kind"], "feature")
        self.assertEqual(t["packetLength"], 65)
        self.assertEqual(t["readLength"], 65)
        self.assertEqual(t["reportId"], 0)
        self.assertEqual(t["settleMs"], 60)
        self.assertEqual(t["checksum"]["kind"], "none")
        self.assertEqual(t["ack"], {"offset": 1, "value": 161})
        self.assertEqual(t["wirelessFlagOffset"], 4)

    def test_read_opcode_is_write_opcode_plus_0x80(self):
        """The firmware's one universal rule.

        Most commands carry the opcode at byte 3. `sleep` is a sub-command of
        opcode 0x02 and carries its own selector at byte 6 instead -- so rather
        than hard-coding the position, assert that get and set differ in
        exactly one byte and that the difference is the +0x80 read bit.
        """
        for name, cmd in self.profile.data["commands"].items():
            if name.startswith("_") or not isinstance(cmd, dict):
                continue
            if not (cmd.get("get") and cmd.get("set")):
                continue
            get_bytes = self.profile.build_request(name, write=False)
            set_bytes = self.profile.build_request(name, write=True)
            diff = [i for i in range(len(get_bytes)) if get_bytes[i] != set_bytes[i]]
            self.assertEqual(
                len(diff), 1, f"{name}: get and set differ in {len(diff)} bytes, expected 1"
            )
            i = diff[0]
            self.assertEqual(
                get_bytes[i],
                (set_bytes[i] + 0x80) & 0xFF,
                f"{name}: byte {i} read {get_bytes[i]:#04x} is not write "
                f"{set_bytes[i]:#04x} + 0x80",
            )

    def test_sleep_carries_its_selector_at_byte_6(self):
        """Pin the one command that breaks the byte-3 convention."""
        get_bytes = self.profile.build_request("sleep", write=False)
        set_bytes = self.profile.build_request("sleep", write=True)
        self.assertEqual(get_bytes[3], 0x02)
        self.assertEqual(set_bytes[3], 0x02)
        self.assertEqual(get_bytes[6], 0x87)
        self.assertEqual(set_bytes[6], 0x07)

    def test_every_request_is_a_full_packet(self):
        for name in self.profile.data["commands"]:
            if name.startswith("_"):
                continue
            if not self.profile.has_command(name):
                continue
            packet = self.profile.build_request(name, write=False)
            self.assertEqual(len(packet), 65, f"{name} built a {len(packet)}-byte packet")
            self.assertEqual(packet[0], 0, f"{name} must use report id 0")

    def test_link_flag_is_the_only_difference_between_wired_and_wireless(self):
        wired = self.profile.build_request("polling", write=False, wireless=False)
        wireless = self.profile.build_request("polling", write=False, wireless=True)
        diff = [i for i in range(65) if wired[i] != wireless[i]]
        self.assertEqual(diff, [4])
        self.assertEqual(wireless[4], 1)

    def test_ack_gate(self):
        self.assertTrue(self.profile.check_ack(bytes([0, 0xA1] + [0] * 63)))
        self.assertFalse(self.profile.check_ack(bytes([0, 0x00] + [0] * 63)))
        self.assertFalse(self.profile.check_ack(b""))

    def test_write_lands_the_value_at_the_command_value_offset(self):
        packet = self.profile.build_request(
            "polling",
            write=True,
            value_bytes=self.profile.encode_value("pollingRate", 1000),
        )
        self.assertEqual(packet[3], 0x02, "write must use the set opcode")
        # Measured on hardware: raw 1 clocks 1000 Hz.
        self.assertEqual(packet[5], 1, "1000 Hz encodes as raw 1 at byte 5")

    def test_only_measured_polling_rates_are_claimed(self):
        """An unmeasured raw value must not decode to an invented rate."""
        values = self.profile.data["fields"]["pollingRate"]["values"]
        self.assertEqual(values, {"1": 1000})
        reply = bytearray(65)
        reply[1] = 0xA1
        reply[5] = 4
        # Falls through to the raw number rather than pretending to know.
        self.assertEqual(self.profile.decode("pollingRate", bytes(reply)), 4)

    def test_sleep_uses_its_subcommand_and_big_endian_value(self):
        packet = self.profile.build_request(
            "sleep", write=True, value_bytes=self.profile.encode_value("sleepMinutes", 15)
        )
        self.assertEqual(packet[3], 0x02)
        self.assertEqual(packet[6], 0x07, "byte 6 selects set vs get for sleep")
        self.assertEqual(packet[7:9], b"\x00\x0f")

    def test_dpi_decodes_big_endian(self):
        reply = bytearray(65)
        reply[1] = 0xA1
        reply[7:9] = (1600).to_bytes(2, "big")
        self.assertEqual(self.profile.decode("dpiStage1", bytes(reply)), 1600)

    def test_battery_reads_the_byte_the_mouse_actually_uses(self):
        """Observed on hardware: rx[5] is status, rx[6] is the percentage."""
        reply = bytes.fromhex("00a1028f01006300") + bytes(57)
        self.assertEqual(self.profile.decode("batteryPercent", reply), 99)
        self.assertIs(self.profile.decode("charging", reply), False)

    def test_firmware_version_is_three_bytes(self):
        reply = bytes.fromhex("00a106810101000700 5808".replace(" ", "")) + bytes(54)
        self.assertEqual(self.profile.decode("firmwareVersion", reply), "1.0.7")

    def test_all_seven_dpi_stages_map_at_stride_seven(self):
        """Confirmed on hardware: X u16be, Y u16be, then RGB, seven bytes apart.

        The arithmetic is the proof -- 2 header bytes plus 7*7 stage bytes is
        51, exactly the payload length the firmware reports in rx[2].
        """
        for n in range(1, 8):
            base = 7 + (n - 1) * 7
            self.assertEqual(self.profile.data["fields"][f"dpiStage{n}"]["offset"], base)
            self.assertEqual(
                self.profile.data["fields"][f"dpiStage{n}Y"]["offset"], base + 2
            )
            self.assertEqual(
                self.profile.data["fields"][f"dpiStage{n}Color"]["offset"], base + 4
            )
        self.assertEqual(7 + 7 * 7, 56)

    def test_dpi_block_decodes_the_captured_reply(self):
        raw = (
            "00 a1 33 83 01 01 01 01 90 01 90 aa 00 00 06 40 06 40 ff a5 00 "
            "06 40 06 40 ff ff 00 0c 80 0c 80 00 ff 00 11 94 11 94 00 ff ff "
            "13 88 13 88 00 00 ff 19 00 19 00 80 00 80 00"
        )
        buf = bytearray(65)
        vals = [int(x, 16) for x in raw.split()]
        buf[: len(vals)] = vals
        reply = bytes(buf)
        self.assertEqual(
            [self.profile.decode(f"dpiStage{n}", reply) for n in range(1, 8)],
            [400, 1600, 1600, 3200, 4500, 5000, 6400],
        )
        self.assertEqual(self.profile.decode("dpiStage1Color", reply), "#aa0000")
        self.assertEqual(self.profile.decode("dpiStage7Color", reply), "#800080")

    def test_writing_one_stage_leaves_its_neighbours_and_colour_alone(self):
        buf = bytearray(65)
        buf[7:9] = (400).to_bytes(2, "big")
        buf[14:16] = (1600).to_bytes(2, "big")
        buf[18:21] = bytes.fromhex("ffa500")
        self.profile.encode_into(buf, "dpiStage1", 800)
        self.assertEqual(self.profile.decode("dpiStage2", bytes(buf)), 1600)
        self.assertEqual(self.profile.decode("dpiStage2Color", bytes(buf)), "#ffa500")

    def test_read_only_fields_refuse_writes(self):
        # Readings the firmware only reports, plus stage count, whose value
        # reshapes the stage list rather than setting one.
        # debounce is here because its read and write are asymmetric: the read
        # returns byte 0 of a 4-byte tuple, the write takes a row index into the
        # driver's table. Writing the number you just read would select a
        # different row.
        for name in ("batteryPercent", "charging", "connection",
                     "firmwareVersion", "dpiStageCount", "debounceMs"):
            self.assertFalse(
                self.profile.field_writable(name), f"{name} must stay read-only"
            )

    def test_writable_set_is_exactly_what_was_verified(self):
        writable = {
            f
            for f in self.profile.data["fields"]
            if not f.startswith("_") and self.profile.field_writable(f)
        }
        expected = {
            "pollingRate",
            "liftOffDistance",
            "motionSync",
            "angleSnap",
            "sleepMinutes",
            "activeDpiStage",
        }
        for n in range(1, 8):
            expected |= {f"dpiStage{n}", f"dpiStage{n}Y", f"dpiStage{n}Color"}
        self.assertEqual(writable, expected)

    def test_link_flag_is_omitted_where_the_firmware_omits_it(self):
        """`hts_get_connect_state` and `hts_get_set_sleep` never set byte 4.

        For sleep this matters twice over: byte 4 carries part of the
        sub-command, so stamping a link flag into it corrupts the packet.
        """
        for name in ("connection", "sleep"):
            wired = self.profile.build_request(name, write=False, wireless=False)
            dongle = self.profile.build_request(name, write=False, wireless=True)
            self.assertEqual(
                wired, dongle, f"{name} must not vary with the link flag"
            )
        # And the sleep sub-command byte survives untouched.
        self.assertEqual(
            self.profile.build_request("sleep", write=False, wireless=True)[4], 0x02
        )

    def test_commands_that_carry_a_block_are_read_modify_write(self):
        """DPI holds seven stages plus colours; a synthesised packet would zero
        everything this profile has not decoded."""
        dpi = self.profile.data["commands"]["dpi"]
        self.assertTrue(dpi.get("readModifyWrite"))
        self.assertEqual(dpi.get("payloadRange"), [5, 65])
        # Scalar commands must NOT be, or a write costs a needless extra read.
        for name in ("polling", "motion", "angle", "liftOff"):
            self.assertFalse(
                self.profile.data["commands"][name].get("readModifyWrite"),
                f"{name} carries a single value and needs no read first",
            )

    def test_reset_is_not_reachable_from_any_field(self):
        """Factory reset exists in the firmware; nothing should bind to it."""
        for name, spec in self.profile.data["fields"].items():
            if name.startswith("_"):
                continue
            self.assertNotEqual(spec.get("command"), "reset")
        self.assertNotIn("reset", self.profile.data["commands"])

    def test_every_field_names_a_declared_command(self):
        commands = set(self.profile.data["commands"])
        for name, spec in self.profile.data["fields"].items():
            if name.startswith("_"):
                continue
            self.assertIn(spec["command"], commands, f"{name} reads from an unknown command")

    def test_ui_sections_only_reference_real_fields(self):
        fields = set(self.profile.data["fields"])
        ui = self.profile.data["ui"]
        for section in ui["sections"]:
            for field in section["fields"]:
                self.assertIn(field, fields, f"UI section references unknown field {field}")
        for field in ui["dpiStages"] + ui["quickToggles"]:
            self.assertIn(field, fields)

    def test_unverified_fields_carry_an_explanation(self):
        for name, spec in self.profile.data["fields"].items():
            if name.startswith("_"):
                continue
            note = spec.get("_needsVerification")
            if note is not None:
                self.assertIsInstance(note, str)
                self.assertGreater(len(note), 20, f"{name}'s caveat is too vague to act on")


class ReportDescriptorTests(unittest.TestCase):
    def test_finds_vendor_page_and_feature_report(self):
        # Usage Page (Vendor 0xFF00), Usage (0x01), Collection (Application),
        #   Report ID (8), Usage (0x02), Logical min/max, Report Size/Count,
        #   Feature (Data,Var,Abs), End Collection
        desc = bytes(
            [
                0x06, 0x00, 0xFF,
                0x09, 0x01,
                0xA1, 0x01,
                0x85, 0x08,
                0x09, 0x02,
                0x15, 0x00,
                0x26, 0xFF, 0x00,
                0x75, 0x08,
                0x95, 0x40,
                0xB1, 0x02,
                0xC0,
            ]
        )
        parsed = parse_report_descriptor(desc)
        self.assertEqual(parsed["usage_page"], 0xFF00)
        self.assertEqual(parsed["usage"], 0x01)
        self.assertEqual(parsed["feature_report_ids"], [8])
        self.assertEqual(parsed["output_report_ids"], [])

    def test_pointer_node_is_recognised(self):
        # Generic Desktop / Mouse, Input only -- the node we must NOT write to.
        desc = bytes(
            [
                0x05, 0x01,
                0x09, 0x02,
                0xA1, 0x01,
                0x85, 0x01,
                0x09, 0x01,
                0x75, 0x08,
                0x95, 0x03,
                0x81, 0x02,
                0xC0,
            ]
        )
        parsed = parse_report_descriptor(desc)
        self.assertEqual(parsed["usage_page"], 0x01)
        self.assertEqual(parsed["usage"], 0x02)
        self.assertEqual(parsed["feature_report_ids"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
