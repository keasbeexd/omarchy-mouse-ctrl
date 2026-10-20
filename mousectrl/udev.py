"""The udev rule file that grants hidraw access, generated in one place.

Feature-report ioctls need the hidraw node opened read-write, and
/dev/hidraw* is root-only by default. `uaccess` grants read-write to whoever
is logged in at the seat, the same mechanism sound cards and webcams use.

Lines, broadest first:
- one per vendor a family driver claims (every model of that vendor works
  without regenerating the file);
- one per vendor:product pair from a profile or an explicit request whose
  vendor no family claims (unknown mice get a tight per-device grant).

Every id is formatted from an int, so no profile or device text can reach
the rule file.
"""

from __future__ import annotations

from .families.base import Driver
from .profiles import Profile

RULE_FILE = "/etc/udev/rules.d/60-mousectrl.rules"

_MAX_RULE_FILE_BYTES = 65536

_HEADER = """\
# Mouse CTRL -- hidraw access for supported mice.
#
# Configuring a mouse means sending HID feature reports, which needs the
# hidraw node opened read-write. uaccess grants that to whoever is logged in
# at this seat. Each line is scoped to one vendor or one vendor:product pair;
# regenerate with `mousectrl udev-rule`.
"""


def _vendor_line(vid: int) -> str:
    return f'KERNEL=="hidraw*", ATTRS{{idVendor}}=="{vid:04x}", MODE="0660", TAG+="uaccess"'


def _pair_line(vid: int, pid: int) -> str:
    return (
        f'KERNEL=="hidraw*", ATTRS{{idVendor}}=="{vid:04x}", '
        f'ATTRS{{idProduct}}=="{pid:04x}", MODE="0660", TAG+="uaccess"'
    )


def render(
    families: tuple[type[Driver], ...],
    profiles: list[Profile],
    extra_pairs: tuple[tuple[int, int], ...] = (),
) -> str:
    vendors: dict[int, str] = {}
    for family in families:
        for vid, label in family.vendor_ids.items():
            vendors.setdefault(vid, f"{label} ({family.title})")

    pairs: dict[tuple[int, int], str] = {}
    for profile in profiles:
        vid = int(profile.vendor_id, 16)
        for pid in profile.product_ids:
            pairs.setdefault((vid, int(pid, 16)), f"profile {profile.id}")
    for vid, pid in extra_pairs:
        pairs.setdefault((vid, pid), "requested")

    sections = [_HEADER]
    for vid, label in sorted(vendors.items()):
        sections.append(f"\n# {label}\n{_vendor_line(vid)}\n")
    for (vid, pid), why in sorted(pairs.items()):
        if vid not in vendors:
            sections.append(f"\n# {vid:04x}:{pid:04x} ({why})\n{_pair_line(vid, pid)}\n")
    return "".join(sections)


def _rule_lines(text: str) -> set[str]:
    return {
        line.strip() for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def installed_status(text: str, rule_file: str = RULE_FILE) -> dict:
    """Whether the installed file already grants everything `text` would.

    Compared by rule lines, not bytes, so a file whose comments changed is
    still current. Rule files are 0644, so this needs no privilege.
    """
    try:
        with open(rule_file, "r", encoding="utf-8", errors="replace") as fh:
            installed = _rule_lines(fh.read(_MAX_RULE_FILE_BYTES))
    except FileNotFoundError:
        return {"present": False, "upToDate": False}
    return {"present": True, "upToDate": _rule_lines(text) <= installed}
