"""The contract every protocol family driver implements.

A driver owns everything about one family's wire protocol: which node to
open, link probing, settle times, byte layouts. The rest of mousectrl only
sees Capabilities and plain JSON values, so the panel can render any family
without knowing its name.

Value shapes by capability kind (all JSON-safe):
- battery:   {"percent": int 0-100, "charging": bool | None}
- dpiStages: {"active": int (0-based), "stages": [{"dpi": int, "color": "#rrggbb" | None}]}
- enum:      one of Capability.options
- toggle:    bool
- duration:  int in Capability.unit (s, ms), minimum..maximum
- info:      str (firmware version, connection type)
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from ..errors import InvalidValue
from ..fingerprint import Fingerprint

KINDS = ("battery", "dpiStages", "enum", "toggle", "duration", "info")
# Panel sections, in display order. "device" holds read-only facts shown in
# the footer.
GROUPS = ("dpi", "performance", "sensor", "power", "device")
_DEFAULT_GROUP = {"battery": "device", "info": "device", "dpiStages": "dpi"}
READ_ONLY_KINDS = ("battery", "info")

# Proven on hardware by whoever wrote the driver, or not yet.
VERIFIED = "verified"
UNTESTED = "untested"

_ID_RE = re.compile(r"^[a-z][a-zA-Z0-9]{0,31}$")
_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")


@dataclass(frozen=True)
class Capability:
    id: str
    kind: str
    label: str
    verification: str
    read_only: bool = False
    options: tuple = ()
    minimum: int | None = None
    maximum: int | None = None
    step: int | None = None
    max_stages: int | None = None
    has_colors: bool = False
    # dpiStages only: whether a write may change the active stage. The panel
    # uses it to decide if clicking a stage switches to it or only selects it.
    switchable: bool = True
    unit: str = ""
    # Panel section; defaults by kind where the kind decides it.
    group: str = ""

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.id):
            raise ValueError(f"capability id {self.id!r} must be camelCase, 1-32 chars")
        if self.kind not in KINDS:
            raise ValueError(f"capability {self.id}: unknown kind {self.kind!r}")
        if self.verification not in (VERIFIED, UNTESTED):
            raise ValueError(f"capability {self.id}: unknown verification {self.verification!r}")
        if self.kind == "enum" and not self.options:
            raise ValueError(f"capability {self.id}: an enum needs options")
        if self.kind == "dpiStages" and (self.minimum is None or self.maximum is None or not self.max_stages):
            raise ValueError(f"capability {self.id}: dpiStages needs minimum, maximum and max_stages")
        if self.kind == "duration" and (self.minimum is None or self.maximum is None):
            raise ValueError(f"capability {self.id}: a duration needs minimum and maximum")
        if not self.group:
            if self.kind not in _DEFAULT_GROUP:
                raise ValueError(f"capability {self.id}: a {self.kind} needs a group")
            object.__setattr__(self, "group", _DEFAULT_GROUP[self.kind])
        if self.group not in GROUPS:
            raise ValueError(f"capability {self.id}: unknown group {self.group!r}")

    @property
    def writable_by_driver(self) -> bool:
        return not self.read_only and self.kind not in READ_ONLY_KINDS

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "verification": self.verification,
            "readOnly": not self.writable_by_driver,
            "options": list(self.options),
            "minimum": self.minimum,
            "maximum": self.maximum,
            "step": self.step,
            "maxStages": self.max_stages,
            "hasColors": self.has_colors,
            "switchable": self.switchable,
            "unit": self.unit,
            "group": self.group,
        }


def _require_int(
    cap: Capability, value: Any, name: str, low: int, high: int, step: int | None = None
) -> int:
    # bool is an int subclass; True is not a DPI.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidValue(f"{cap.id}: {name} must be an integer, got {value!r}")
    if not low <= value <= high:
        raise InvalidValue(f"{cap.id}: {name} {value} is outside {low}..{high}")
    if step and (value - low) % step:
        raise InvalidValue(f"{cap.id}: {name} {value} is not in steps of {step} from {low}")
    return value


def _validate_stages(cap: Capability, value: Any) -> dict:
    if not isinstance(value, dict):
        raise InvalidValue(f"{cap.id}: expected an object with active and stages")
    stages = value.get("stages")
    if not isinstance(stages, list) or not 1 <= len(stages) <= cap.max_stages:
        raise InvalidValue(f"{cap.id}: stages must be a list of 1..{cap.max_stages}")
    active = _require_int(cap, value.get("active"), "active", 0, len(stages) - 1)
    clean = []
    for stage in stages:
        if not isinstance(stage, dict):
            raise InvalidValue(f"{cap.id}: each stage must be an object")
        dpi = _require_int(cap, stage.get("dpi"), "dpi", cap.minimum, cap.maximum, cap.step)
        color = stage.get("color")
        if cap.has_colors:
            if not isinstance(color, str) or not _COLOR_RE.match(color):
                raise InvalidValue(f"{cap.id}: color must be #rrggbb lowercase, got {color!r}")
        elif color is not None:
            raise InvalidValue(f"{cap.id}: this mouse has no stage colors")
        clean.append({"dpi": dpi, "color": color})
    return {"active": active, "stages": clean}


def validate_value(cap: Capability, value: Any) -> Any:
    """Check a value from the panel against what the capability declares.

    Runs before any driver sees the value, so drivers can trust their input
    and the boundary is in one place.
    """
    if not cap.writable_by_driver:
        raise InvalidValue(f"{cap.id} is read-only")
    if cap.kind == "toggle":
        if not isinstance(value, bool):
            raise InvalidValue(f"{cap.id}: expected true or false, got {value!r}")
        return value
    if cap.kind == "enum":
        # Membership by equality alone would let True match an option of 1.
        if not any(type(value) is type(o) and value == o for o in cap.options):
            raise InvalidValue(f"{cap.id}: {value!r} is not one of {list(cap.options)}")
        return value
    if cap.kind == "duration":
        return _require_int(cap, value, "value", cap.minimum, cap.maximum, cap.step)
    if cap.kind == "dpiStages":
        return _validate_stages(cap, value)
    raise InvalidValue(f"{cap.id}: kind {cap.kind} cannot be written")


class Driver(ABC):
    """One open connection to one mouse of a family.

    Subclasses set the class attributes, implement the abstract methods, and
    raise errors from mousectrl.errors -- never return a guess.
    """

    family: ClassVar[str]
    title: ClassVar[str]
    # USB vendor id -> label for the udev rule comment.
    vendor_ids: ClassVar[dict[int, str]]

    def __init__(self, fingerprint: Fingerprint):
        self.fingerprint = fingerprint

    @classmethod
    @abstractmethod
    def matches(cls, fingerprint: Fingerprint) -> bool:
        """True when this family speaks to this device. Must not touch the device."""

    @abstractmethod
    def capabilities(self) -> list[Capability]:
        """What this mouse supports. May read from the device."""

    @abstractmethod
    def read(self, capability_id: str) -> Any:
        """The current value, in the shape its kind defines."""

    @abstractmethod
    def write(self, capability_id: str, value: Any) -> Any:
        """Write an already-validated value and return what the mouse reads back."""

    def close(self) -> None:
        """Release the device. Called on unplug and at shutdown."""
