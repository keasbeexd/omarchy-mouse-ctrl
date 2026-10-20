"""Map a fingerprint to the family driver that speaks its protocol."""

from __future__ import annotations

from .families.base import Driver
from .fingerprint import Fingerprint


def driver_for(fingerprint: Fingerprint, families: tuple[type[Driver], ...]) -> type[Driver] | None:
    """The first family that claims the device; families must not overlap."""
    for family in families:
        if family.matches(fingerprint):
            return family
    return None


def family_named(name: str, families: tuple[type[Driver], ...]) -> type[Driver] | None:
    return next((f for f in families if f.family == name), None)
