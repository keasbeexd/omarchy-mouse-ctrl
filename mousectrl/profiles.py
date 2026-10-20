"""User profiles: which mouse, which family, what the wizard confirmed.

A profile holds no protocol bytes -- those belong to the driver. It records
the wizard's outcome per capability, which is what gates writes:

- verified: the driver marks it proven and the user confirmed the read
- tested:   the driver marked it untested, and the wizard's write/read-back
            test passed on this mouse
- readOnly: shown, never written (untested and not tested, or read-only kind)
- rejected: the user said the read did not match the mouse; hidden and never
            written, because a wrong read means the write mapping is wrong too

Profiles are files the user owns, so every field is validated on load.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .errors import MouseCtrlError
from .fingerprint import Fingerprint
from .safeio import read_bounded_json, write_private_json

PROFILE_DIR = os.path.expanduser("~/.config/mousectrl/profiles")
SCHEMA_VERSION = 1

STATES = ("verified", "tested", "readOnly", "rejected")
WRITABLE_STATES = ("verified", "tested")
GENERIC_BACKEND = "generic"

_MAX_FILES = 64
_MAX_FILE_BYTES = 65536
_MAX_PRODUCTS = 32
_MAX_CAPABILITIES = 64

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_CAP_ID_RE = re.compile(r"^[a-z][a-zA-Z0-9]{0,31}$")
_BACKEND_RE = re.compile(r"^(generic|family:[a-z][a-z0-9_]{0,31})$")
_HEX4_RE = re.compile(r"^[0-9a-f]{4}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ProfileError(MouseCtrlError):
    code = "profile"


@dataclass(frozen=True)
class CapabilityState:
    state: str
    visible: bool = True

    @property
    def writable(self) -> bool:
        return self.state in WRITABLE_STATES


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    backend: str
    vendor_id: str
    product_ids: tuple[str, ...]
    capabilities: dict[str, CapabilityState] = field(default_factory=dict)

    @property
    def family(self) -> str | None:
        return self.backend.split(":", 1)[1] if self.backend.startswith("family:") else None

    def matches(self, fingerprint: Fingerprint) -> bool:
        return (
            f"{fingerprint.vendor_id:04x}" == self.vendor_id
            and f"{fingerprint.product_id:04x}" in self.product_ids
        )

    def as_dict(self) -> dict:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "id": self.id,
            "name": self.name,
            "backend": self.backend,
            "match": {"vendorId": self.vendor_id, "productIds": list(self.product_ids)},
            "capabilities": {
                cap_id: {"state": s.state, "visible": s.visible}
                for cap_id, s in sorted(self.capabilities.items())
            },
        }


def _require(condition: bool, source: str, message: str) -> None:
    if not condition:
        raise ProfileError(f"{source}: {message}")


def from_dict(doc: dict, source: str = "profile") -> Profile:
    """Validate a profile document; raises ProfileError naming the bad field."""
    _require(doc.get("schemaVersion") == SCHEMA_VERSION, source,
             f"schemaVersion must be {SCHEMA_VERSION}")
    pid = doc.get("id")
    _require(isinstance(pid, str) and bool(_ID_RE.match(pid)), source,
             "id must be lowercase letters, digits and dashes, at most 64")
    name = doc.get("name")
    _require(isinstance(name, str) and 0 < len(name) <= 64 and not _CONTROL_RE.search(name),
             source, "name must be 1-64 printable characters")
    backend = doc.get("backend")
    _require(isinstance(backend, str) and bool(_BACKEND_RE.match(backend)), source,
             "backend must be 'generic' or 'family:<name>'")
    match = doc.get("match")
    _require(isinstance(match, dict), source, "match must be an object")
    vid = match.get("vendorId")
    _require(isinstance(vid, str) and bool(_HEX4_RE.match(vid)), source,
             "match.vendorId must be 4 lowercase hex digits")
    pids = match.get("productIds")
    _require(isinstance(pids, list) and 0 < len(pids) <= _MAX_PRODUCTS
             and all(isinstance(p, str) and _HEX4_RE.match(p) for p in pids),
             source, f"match.productIds must be 1-{_MAX_PRODUCTS} ids of 4 lowercase hex digits")
    caps_doc = doc.get("capabilities", {})
    _require(isinstance(caps_doc, dict) and len(caps_doc) <= _MAX_CAPABILITIES, source,
             f"capabilities must be an object of at most {_MAX_CAPABILITIES}")
    caps = {}
    for cap_id, entry in caps_doc.items():
        _require(bool(_CAP_ID_RE.match(cap_id)), source, f"bad capability id {cap_id!r}")
        _require(isinstance(entry, dict) and entry.get("state") in STATES, source,
                 f"capabilities.{cap_id}.state must be one of {list(STATES)}")
        visible = entry.get("visible", True)
        _require(isinstance(visible, bool), source, f"capabilities.{cap_id}.visible must be a boolean")
        caps[cap_id] = CapabilityState(entry["state"], visible)
    return Profile(pid, name, backend, vid, tuple(dict.fromkeys(pids)), caps)


def load_all(directory: str = PROFILE_DIR) -> tuple[list[Profile], list[str]]:
    """Every valid profile, plus one message per file that was refused.

    One broken file must not hide the user's other mice, so problems are
    returned for the panel to show rather than raised.
    """
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except FileNotFoundError:
        return [], []
    profiles: list[Profile] = []
    problems: list[str] = []
    seen: set[str] = set()
    for name in names[:_MAX_FILES]:
        path = os.path.join(directory, name)
        try:
            profile = from_dict(read_bounded_json(path, _MAX_FILE_BYTES, reject_shared_write=True), path)
        except ProfileError as exc:
            problems.append(str(exc))
            continue
        except (OSError, ValueError) as exc:
            problems.append(f"{path}: {exc}")
            continue
        if name != f"{profile.id}.json":
            problems.append(f"{path}: file name must be {profile.id}.json")
            continue
        if profile.id in seen:
            continue
        seen.add(profile.id)
        profiles.append(profile)
    if len(names) > _MAX_FILES:
        problems.append(f"{directory}: only the first {_MAX_FILES} profiles were read")
    return profiles, problems


def save(profile: Profile, directory: str = PROFILE_DIR) -> str:
    # Round-trip through validation so nothing unloadable is ever written.
    from_dict(profile.as_dict(), f"profile {profile.id}")
    path = os.path.join(directory, f"{profile.id}.json")
    write_private_json(path, profile.as_dict())
    return path


def delete(profile_id: str, directory: str = PROFILE_DIR) -> bool:
    """Remove a profile; False when there was none."""
    if not _ID_RE.match(profile_id):
        raise ProfileError(f"{profile_id!r} is not a profile id")
    try:
        os.remove(os.path.join(directory, f"{profile_id}.json"))
    except FileNotFoundError:
        return False
    return True


def unique_id(name: str, taken: set[str]) -> str:
    """A file-safe id from a display name, unique among `taken`."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:56] or "mouse"
    candidate, n = base, 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


def find(profiles: list[Profile], fingerprint: Fingerprint) -> Profile | None:
    return next((p for p in profiles if p.matches(fingerprint)), None)
