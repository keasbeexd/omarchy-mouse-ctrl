import unittest

import fakes  # noqa: F401 -- puts the repo on sys.path

from mousectrl.errors import InvalidValue
from mousectrl.families.base import UNTESTED, VERIFIED, Capability, validate_value

DPI = Capability("dpi", "dpiStages", "DPI", VERIFIED, minimum=100, maximum=26000,
                 step=50, max_stages=3, has_colors=True)


def stages(*dpis, active=0, color="#ff0000"):
    return {"active": active, "stages": [{"dpi": d, "color": color} for d in dpis]}


class CapabilityTest(unittest.TestCase):
    def test_rejects_unknown_kind_and_verification(self):
        with self.assertRaises(ValueError):
            Capability("x", "slider", "X", VERIFIED)
        with self.assertRaises(ValueError):
            Capability("x", "toggle", "X", "probably")

    def test_enum_needs_options(self):
        with self.assertRaises(ValueError):
            Capability("x", "enum", "X", VERIFIED)

    def test_battery_is_never_writable(self):
        cap = Capability("battery", "battery", "Battery", VERIFIED)
        self.assertTrue(cap.as_dict()["readOnly"])
        with self.assertRaises(InvalidValue):
            validate_value(cap, 50)


class ValidateValueTest(unittest.TestCase):
    def test_enum_does_not_accept_bool_for_int(self):
        cap = Capability("rate", "enum", "Rate", VERIFIED, options=(1, 2))
        self.assertEqual(validate_value(cap, 1), 1)
        with self.assertRaises(InvalidValue):
            validate_value(cap, True)

    def test_toggle_needs_a_real_bool(self):
        cap = Capability("snap", "toggle", "Snap", UNTESTED)
        self.assertIs(validate_value(cap, False), False)
        with self.assertRaises(InvalidValue):
            validate_value(cap, 0)

    def test_duration_bounds_and_step(self):
        cap = Capability("sleep", "duration", "Sleep", VERIFIED, minimum=10, maximum=600, step=10)
        self.assertEqual(validate_value(cap, 60), 60)
        for bad in (5, 601, 65, "60"):
            with self.assertRaises(InvalidValue):
                validate_value(cap, bad)

    def test_dpi_stages_accepts_valid_and_normalizes(self):
        value = stages(400, 1600, active=1)
        value["extra"] = "dropped"
        self.assertEqual(validate_value(DPI, value), stages(400, 1600, active=1))

    def test_dpi_stages_rejects_out_of_range_inputs(self):
        bad_values = [
            stages(),                               # no stages
            stages(400, 800, 1600, 3200),           # over max_stages
            stages(400, active=1),                  # active out of range
            stages(90),                             # below minimum
            stages(425),                            # off step
            stages(400, color="red"),               # not #rrggbb
            stages(400, color="#FF0000"),           # uppercase
            {"active": 0, "stages": [400]},         # stage not an object
            [400],                                  # not an object
        ]
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(InvalidValue):
                validate_value(DPI, value)

    def test_colorless_mouse_refuses_colors(self):
        cap = Capability("dpi", "dpiStages", "DPI", VERIFIED, minimum=100, maximum=1000, max_stages=2)
        self.assertEqual(validate_value(cap, stages(400, color=None))["stages"][0]["color"], None)
        with self.assertRaises(InvalidValue):
            validate_value(cap, stages(400))


if __name__ == "__main__":
    unittest.main()
