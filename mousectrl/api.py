"""Request handlers shared by `serve` and the one-shot CLI commands.

Every handler returns a JSON-safe dict with "ok": true, or raises a
MouseCtrlError (or OSError from the device) for the caller to report.

The write gate lives here, in one place: a write needs a profile whose
wizard outcome for that capability is verified or tested, a capability the
driver declares writable, a value that passes validation, and a read-back
that equals what was written.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from . import fingerprint, profiles, registry, udev
from .errors import (
    DeviceNotFound, InvalidValue, NoAccess, ProtocolFailure, Unsupported, WriteRefused,
)
from .families import FAMILIES
from .families.base import Capability, Driver, validate_value
from .fingerprint import Fingerprint
from .lock import device_lock

_VIDPID_RE = re.compile(r"^([0-9a-f]{4}):([0-9a-f]{4})$")
_MAX_EXTRA_PAIRS = 8


class Api:
    def __init__(
        self,
        *,
        families: tuple[type[Driver], ...] = FAMILIES,
        scan: Callable[[], list[Fingerprint]] = fingerprint.mice,
        profile_dir: str = profiles.PROFILE_DIR,
        rule_file: str = udev.RULE_FILE,
        lock=device_lock,
    ):
        self._families = families
        self._scan = scan
        self._profile_dir = profile_dir
        self._rule_file = rule_file
        self._lock = lock
        self._drivers: dict[str, Driver] = {}
        self._caps: dict[str, dict[str, Capability]] = {}

    # --- lifecycle ---------------------------------------------------------

    def invalidate(self, key: str | None = None) -> None:
        """Close cached drivers: all of them, or one device's.

        Node numbering under /dev can change on replug, so any hotplug event
        drops the cache and the next request reopens from a fresh scan.
        """
        for k in [key] if key else list(self._drivers):
            driver = self._drivers.pop(k, None)
            self._caps.pop(k, None)
            if driver is not None:
                driver.close()

    def close(self) -> None:
        self.invalidate()

    # --- lookups -----------------------------------------------------------

    def _resolve(self, key: str) -> Fingerprint:
        """Find a device by its key, or by vid:pid when exactly one matches."""
        found = self._scan()
        exact = [fp for fp in found if fp.key == key]
        if exact:
            return exact[0]
        by_id = [fp for fp in found if fp.vidpid == key]
        if len(by_id) == 1:
            return by_id[0]
        if len(by_id) > 1:
            raise DeviceNotFound(f"{len(by_id)} mice match {key}; use the full key from `devices`")
        raise DeviceNotFound(f"no connected mouse matches {key!r}")

    def _profile_for(self, fp: Fingerprint) -> profiles.Profile | None:
        loaded, _problems = profiles.load_all(self._profile_dir)
        return profiles.find(loaded, fp)

    def _driver(self, fp: Fingerprint) -> Driver:
        cached = self._drivers.get(fp.key)
        if cached is not None and cached.fingerprint == fp:
            return cached
        self.invalidate(fp.key)
        family = registry.driver_for(fp, self._families)
        if family is None:
            raise Unsupported(f"{fp.name} ({fp.vidpid}) is not in a supported family")
        if not fp.accessible():
            raise NoAccess(
                f"no read-write access to {fp.name} ({fp.vidpid}); install the udev rule and replug"
            )
        driver = family(fp)
        self._drivers[fp.key] = driver
        return driver

    def _capabilities(self, fp: Fingerprint, driver: Driver) -> dict[str, Capability]:
        if fp.key not in self._caps:
            self._caps[fp.key] = {c.id: c for c in driver.capabilities()}
        return self._caps[fp.key]

    def _on_device(self, fp: Fingerprint, action: Callable[[Driver], Any]) -> Any:
        """Run `action` under the device lock; an I/O error drops the driver.

        An OSError mid-conversation almost always means the mouse went away
        or its node changed, so the cached handle is no longer trustworthy.
        """
        with self._lock():
            driver = self._driver(fp)
            try:
                return action(driver)
            except OSError:
                self.invalidate(fp.key)
                raise

    # --- handlers ----------------------------------------------------------

    def devices(self) -> dict:
        loaded, problems = profiles.load_all(self._profile_dir)
        found = self._scan()
        present = {fp.key for fp in found}
        for key in list(self._drivers):
            if key not in present:
                self.invalidate(key)
        out = []
        for fp in found:
            family = registry.driver_for(fp, self._families)
            profile = profiles.find(loaded, fp)
            out.append({
                **fp.as_dict(),
                "family": {"id": family.family, "title": family.title} if family else None,
                "profile": {"id": profile.id, "name": profile.name} if profile else None,
            })
        return {"ok": True, "devices": out, "profileProblems": problems}

    def capabilities(self, key: str) -> dict:
        fp = self._resolve(key)
        caps = self._on_device(fp, lambda d: self._capabilities(fp, d))
        profile = self._profile_for(fp)
        listed = []
        for cap in caps.values():
            state = profile.capabilities.get(cap.id) if profile else None
            listed.append({
                **cap.as_dict(),
                "state": state.state if state else None,
                "visible": state.visible if state else True,
                "writable": bool(state and state.writable and cap.writable_by_driver),
            })
        return {
            "ok": True,
            "device": fp.key,
            "profile": profile.as_dict() if profile else None,
            "capabilities": listed,
        }

    def read(self, key: str, capability: str | None = None) -> dict:
        """Read one capability, or all; per-capability failures are reported, not hidden."""
        fp = self._resolve(key)

        def read_all(driver: Driver) -> tuple[dict, dict]:
            caps = self._capabilities(fp, driver)
            if capability is not None and capability not in caps:
                raise InvalidValue(f"{fp.name} has no capability {capability!r}")
            values, errors = {}, {}
            for cap_id in [capability] if capability else list(caps):
                try:
                    values[cap_id] = driver.read(cap_id)
                except ProtocolFailure as exc:
                    errors[cap_id] = str(exc)
            return values, errors

        values, errors = self._on_device(fp, read_all)
        return {"ok": True, "device": fp.key, "values": values, "errors": errors}

    def write(self, key: str, capability: str, value: Any) -> dict:
        fp = self._resolve(key)
        profile = self._profile_for(fp)
        if profile is None:
            raise WriteRefused(f"{fp.name} has no profile yet; run the setup wizard first")
        state = profile.capabilities.get(capability)
        if state is None or not state.writable:
            raise WriteRefused(
                f"{capability} is not unlocked for writing on {profile.name} "
                f"(state: {state.state if state else 'unknown'})"
            )

        def do_write(driver: Driver) -> Any:
            caps = self._capabilities(fp, driver)
            if capability not in caps:
                raise InvalidValue(f"{fp.name} has no capability {capability!r}")
            clean = validate_value(caps[capability], value)
            readback = driver.write(capability, clean)
            if readback != clean:
                raise ProtocolFailure(
                    f"{capability}: wrote {clean!r} but the mouse reads back {readback!r}"
                )
            return readback

        return {"ok": True, "device": fp.key, "capability": capability,
                "value": self._on_device(fp, do_write)}

    def udev_rule(self, extra: list[str] | None = None) -> dict:
        """The rule file this machine needs; answers without touching any device."""
        extra = extra or []
        if len(extra) > _MAX_EXTRA_PAIRS:
            raise InvalidValue(f"at most {_MAX_EXTRA_PAIRS} extra vid:pid pairs")
        pairs = []
        for item in extra:
            match = _VIDPID_RE.match(item) if isinstance(item, str) else None
            if match is None:
                raise InvalidValue(f"{item!r} is not a vid:pid pair like 33e4:5807")
            pairs.append((int(match.group(1), 16), int(match.group(2), 16)))
        loaded, _problems = profiles.load_all(self._profile_dir)
        text = udev.render(self._families, loaded, tuple(pairs))
        return {
            "ok": True,
            "ruleFile": self._rule_file,
            "text": text,
            "installed": udev.installed_status(text, self._rule_file),
        }
