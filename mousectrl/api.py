"""Request handlers shared by `serve` and the one-shot CLI commands.

Every handler returns a JSON-safe dict with "ok": true, or raises a
MouseCtrlError (or OSError from the device) for the caller to report.

The write gate lives here, in one place: a write needs a profile whose
wizard outcome for that capability is verified or tested, a capability the
driver declares writable, a value that passes validation, and a read-back
that equals what was written.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable

from . import fingerprint, generic, profiles, ratbag, registry, udev
from .errors import (
    DeviceNotFound, InvalidValue, MouseCtrlError, NoAccess, ProtocolFailure, Unsupported,
    WriteRefused,
)
from .families import FAMILIES
from .families.base import VERIFIED, Capability, Driver, validate_value
from .fingerprint import Fingerprint
from .lock import device_lock

_VIDPID_RE = re.compile(r"^([0-9a-f]{4}):([0-9a-f]{4})$")
_MAX_EXTRA_PAIRS = 8
_MAX_PROFILE_CAPS = 64


def nearby_value(cap: Capability, current: Any) -> Any:
    """A value one step from `current`, for a write test that then restores.

    Small on purpose: the test proves the write path, it does not explore.
    """
    if cap.kind == "toggle":
        return not current
    if cap.kind == "enum":
        options = list(cap.options)
        i = options.index(current) if current in options else -1
        return options[i + 1] if i + 1 < len(options) else options[i - 1]
    step = cap.step or 1
    if cap.kind == "duration":
        return current + step if current + step <= cap.maximum else current - step
    if cap.kind == "dpiStages":
        stages = [dict(s) for s in current["stages"]]
        dpi = stages[0]["dpi"]
        stages[0]["dpi"] = dpi + step if dpi + step <= cap.maximum else dpi - step
        return {"active": current["active"], "stages": stages}
    raise InvalidValue(f"{cap.id}: a {cap.kind} cannot be write-tested")


class Api:
    def __init__(
        self,
        *,
        families: tuple[type[Driver], ...] = FAMILIES,
        scan: Callable[[], list[Fingerprint]] = fingerprint.mice,
        profile_dir: str = profiles.PROFILE_DIR,
        rule_file: str = udev.RULE_FILE,
        lock=device_lock,
        ratbagd: ratbag.Ratbag | None = None,
    ):
        self._families = families
        self._scan = scan
        self._profile_dir = profile_dir
        self._rule_file = rule_file
        self._lock = lock
        self._ratbag = ratbagd if ratbagd is not None else ratbag.Ratbag()
        self._drivers: dict[str, Driver] = {}
        self._caps: dict[str, dict[str, Capability]] = {}
        # Device key -> capability ids whose write test passed in this
        # process. save_profile accepts "tested" only for these.
        self._tested: dict[str, set[str]] = {}

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

    def test_write(self, key: str, capability: str) -> dict:
        """Write a nearby value, read it back, restore the original.

        Used by the wizard for capabilities the driver marks untested; needs
        no profile, because it is how a profile's "tested" state is earned.
        The original is restored even when the test write fails.
        """
        fp = self._resolve(key)

        def run(driver: Driver) -> dict:
            caps = self._capabilities(fp, driver)
            cap = caps.get(capability)
            if cap is None:
                raise InvalidValue(f"{fp.name} has no capability {capability!r}")
            if not cap.writable_by_driver:
                raise InvalidValue(f"{capability} is read-only")
            original = driver.read(capability)
            probe = validate_value(cap, nearby_value(cap, original))
            try:
                seen = driver.write(capability, probe)
            finally:
                try:
                    restored = driver.write(capability, original)
                except (MouseCtrlError, OSError) as exc:
                    raise ProtocolFailure(
                        f"{capability}: could not restore the original value "
                        f"{original!r} after the test: {exc}"
                    ) from exc
            passed = seen == probe and restored == original
            if passed:
                self._tested.setdefault(fp.key, set()).add(capability)
            return {"ok": True, "device": fp.key, "capability": capability, "passed": passed,
                    "original": original, "wrote": probe, "readBack": seen, "restored": restored}

        return self._on_device(fp, run)

    def save_profile(self, key: str, name: Any, capabilities: Any) -> dict:
        """Write the wizard's outcome as a profile for this mouse.

        Server-side checks keep the panel honest: "verified" only for what
        the driver marks verified, "tested" only for what passed a write test
        in this process or is already "tested" in the mouse's existing
        profile (a re-run keeps what an earlier run proved). A mouse with an
        existing profile keeps its id, so a re-run replaces it.
        """
        if not isinstance(name, str):
            raise InvalidValue("name must be a string")
        if not isinstance(capabilities, dict) or len(capabilities) > _MAX_PROFILE_CAPS:
            raise InvalidValue(f"capabilities must be an object of at most {_MAX_PROFILE_CAPS}")
        fp = self._resolve(key)
        family = registry.driver_for(fp, self._families)
        states: dict[str, profiles.CapabilityState] = {}
        loaded, _problems = profiles.load_all(self._profile_dir)
        existing = profiles.find(loaded, fp)
        if family is None:
            if capabilities:
                raise InvalidValue(f"{fp.name} has no capabilities to save")
        else:
            available = self._on_device(fp, lambda d: self._capabilities(fp, d))
            tested = set(self._tested.get(fp.key, set()))
            if existing:
                tested |= {k for k, v in existing.capabilities.items() if v.state == "tested"}
            for cap_id, entry in capabilities.items():
                cap = available.get(cap_id)
                if cap is None or not isinstance(entry, dict):
                    raise InvalidValue(f"{fp.name} has no capability {cap_id!r}")
                state, visible = entry.get("state"), entry.get("visible", True)
                if state not in profiles.STATES or not isinstance(visible, bool):
                    raise InvalidValue(f"{cap_id}: bad state or visibility")
                if state == "verified" and cap.verification != VERIFIED:
                    raise InvalidValue(f"{cap_id} is untested in the driver; it can only be 'tested'")
                if state == "tested" and cap_id not in tested:
                    raise InvalidValue(f"{cap_id} has not passed a write test")
                states[cap_id] = profiles.CapabilityState(state, visible)
        products = family.profile_products(fp) if family else (fp.product_id,)
        profile = profiles.Profile(
            id=existing.id if existing else profiles.unique_id(name, {p.id for p in loaded}),
            name=name.strip(),
            backend=f"family:{family.family}" if family else profiles.GENERIC_BACKEND,
            vendor_id=f"{fp.vendor_id:04x}",
            product_ids=tuple(f"{p:04x}" for p in products),
            capabilities=states,
        )
        path = profiles.save(profile, self._profile_dir)
        return {"ok": True, "device": fp.key, "profile": profile.as_dict(), "path": path}

    def delete_profile(self, profile_id: Any) -> dict:
        if not isinstance(profile_id, str):
            raise InvalidValue("profile must be a string id")
        return {"ok": True, "deleted": profiles.delete(profile_id, self._profile_dir)}

    def probe(self, key: str) -> dict:
        """What any mouse shows without its protocol; needs no access.

        A mouse no family driver claims is also looked up in ratbagd, read
        only: its settings stay with Piper.
        """
        fp = self._resolve(key)
        payload = {
            "ok": True,
            "device": fp.key,
            "connection": generic.connection(fp),
            "buttons": fp.buttons,
            "battery": generic.kernel_battery(fp),
            "report": generic.report(fp),
            "ratbag": None,
            "piper": ratbag.piper(),
        }
        if registry.driver_for(fp, self._families) is None and self._ratbag.available():
            try:
                path = self._ratbag.device_path({os.path.basename(n.path) for n in fp.nodes})
                payload["ratbag"] = self._ratbag.read(path) if path else None
            except ratbag.RatbagError as exc:
                payload["ratbagError"] = str(exc)[:200]
        return payload

    def measure(self, key: str) -> dict:
        """Polling rate from the mouse's own reports; the user moves it."""
        fp = self._resolve(key)
        return {"ok": True, "device": fp.key, **generic.measure_polling(fp)}

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
