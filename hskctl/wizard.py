"""Guided profile setup -- plan Phase 4, rounds 0 and 3 as shipped.

The wizard's job in this increment: find the connected mouse, recognize
which *transport family* it speaks from the contributor snapshots under
`docs/contributor/families/`, prove with one read-only exchange that the
device answers, then assemble the draft into the user's own config.
Rounds 1 and 2 of the plan (guided diff rounds, consented write
verification) arrive later; a profile this wizard produces today carries
the family snapshot's identity scope and verification state untouched
(observed pair recorded in provenance; only extension pairs and
unreachable devices get narrowed to their exact VID:PID).

Everything it does to hardware is a read. The only file it writes is the
user's own profile, in their own config directory, after a confirmation.
It launches no pkexec, touches no rules file, and kills nothing --
except the panel transports, which are restarted on purpose: a `serve`
process reads the profile list once at startup, so the panels have to
come back (they do, in about two seconds, through the watchdog) to see
the profile this wizard just created.
"""

from __future__ import annotations

import copy
import datetime
import json
import os
import re
import subprocess
import sys
from typing import Any

from . import __version__
from .device import Candidate, rank_candidates
from .hidraw import HidrawInfo, enumerate_devices, sysfs_hid_devices
from .protocol import Profile, ProtocolError, _MAX_PROFILE_BYTES, list_profiles, load_profile
from .safeio import read_bounded_json

# Family snapshots the wizard is seeded from. Shipped contributor
# knowledge first (part of the plugin, outside the profile search path so
# it is not presented as a profile of its own); a user directory second
# for someone recovering a family that no longer ships.
FAMILY_SEARCH_PATHS = [
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "docs", "contributor", "families",
    ),
    os.path.expanduser("~/.config/hskctl/families"),
]

_USER_PROFILE_DIR = os.path.expanduser("~/.config/hskctl/profiles")

_MAX_FAMILY_BYTES = _MAX_PROFILE_BYTES

_SERVE_PATTERN = r"hskctl.* serve$"


def _load_families() -> list[Profile]:
    """Every family snapshot the plugin and the user can seed from.

    A file that does not parse is reported to stderr and skipped -- one
    broken snapshot must not hide the others from the wizard.
    """
    out: list[Profile] = []
    for directory in FAMILY_SEARCH_PATHS:
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for entry in sorted(entries):
            if not entry.endswith(".json"):
                continue
            path = os.path.join(directory, entry)
            try:
                data = read_bounded_json(path, _MAX_FAMILY_BYTES)
            except (OSError, ValueError) as exc:
                sys.stderr.write(f"skipping family snapshot {path}: {exc}\n")
                continue
            out.append(Profile(data, path))
    return out


def _slugify(text: str, limit: int = 48) -> str:
    """A device product string or model name into a profile file stem."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip().lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-._")
    return slug[:limit] or "mouse"


def _extension(family: Profile, vid: str, pid: str) -> bool:
    """Is this VID:PID outside what the family snapshot declared?

    Inside the declared pairs, the snapshot's own verification history is
    the family's claim on this device. Outside them, nothing hardware has
    ever confirmed this pairing -- the caller writes the profile under the
    family's own (partial) status and says so in the output, so the panel
    treats anything unverified as it always does: read-only until a
    verification round proves it.
    """
    match = family.data.get("match") or {}
    return (
        str(vid) not in [str(v) for v in (match.get("vendorIds") or [])]
        or str(pid) not in [str(p) for p in (match.get("productIds") or [])]
    )


def _existing_cover(profile: Profile, vid: str, pid: str) -> str | None:
    """Does this profile's match block already claim the device?"""
    match = profile.data.get("match") or {}
    return bool(
        str(vid) in [str(v) for v in (match.get("vendorIds") or [])]
        and str(pid) in [str(p) for p in (match.get("productIds") or [])]
    )


def _pair_rule_line(vid: str, pid: str) -> str:
    """The tight grant line for one device pair.

    Same shape `_vidpid_rule_line` in cli.py builds for the rule file
    engine; reissued here for the setup flow's instructions so the user
    sees the exact line before the profile that feeds the engine exists.
    """
    return (
        f'KERNEL=="hidraw*", ATTRS{{idVendor}}=="{vid}", '
        f'ATTRS{{idProduct}}=="{pid}", MODE="0660", TAG+="uaccess"'
    )


def create_profile(args, emit, fail) -> int:
    """The guided flow. `emit`/`fail` are cli._emit/cli._fail.

    Deliberately not a REPL: two confirmations, one live read, one file
    write. The diff rounds will add their own prompts under the same
    consent discipline; nothing here ever writes the device.
    """
    families = _load_families()
    if not families:
        return fail(
            "no transport family snapshots found (looked in "
            + ", ".join(FAMILY_SEARCH_PATHS) + ")",
            getattr(args, "json", False),
        )

    device_pick = getattr(args, "device", None)
    target = None
    if device_pick:
        target = device_pick if device_pick.startswith("/dev/") else f"/dev/{device_pick}"
    from_sysfs = False
    best: tuple[Profile, Candidate] | None = None
    for family in families:
        for candidate in rank_candidates(family):
            if target and candidate.info.path != target:
                continue
            if candidate.identified and (best is None or candidate.score > best[1].score):
                best = (family, candidate)

    if best is None:
        # The ungranted-mouse path. describe()-based enumeration cannot see
        # a root-only node at all (that is the entire reason the udev rule
        # exists) -- but the permission-free sysfs survey can, and a family
        # whose vendor/product pair is listed there means the mouse is
        # PRESENT, just not yet openable. Its live check will fail with the
        # permission error and take the consent branch below; the profile
        # it writes is what makes the engine's rule-file output name the
        # device, closing the chicken-and-egg in the profile-first order.
        from_sysfs = False
        for family in families:
            match = family.data.get("match") or {}
            vids = [str(v).lower() for v in (match.get("vendorIds") or [])]
            pids = [str(p).lower() for p in (match.get("productIds") or [])]
            for device in sysfs_hid_devices():
                if target and device["node"] != target:
                    continue
                if device["vid"] not in vids:
                    continue
                if pids and device["pid"] not in pids:
                    continue
                info = HidrawInfo(
                    path=device["node"], bustype=int(device.get("bus", "3"), 16),
                    vendor_id=int(device["vid"], 16), product_id=int(device["pid"], 16),
                    name="", phys="",
                )
                candidate = Candidate(
                    info,
                    score=1,
                    reasons=["sysfs survey: vendor/product match the family "
                             "but the node is not openable (no udev grant)"],
                    identified=True,
                    unmatched=[],
                )
                best = (family, candidate)
                from_sysfs = True
                break
            if best is not None:
                break
            # sysfs cannot report usage page or feature reports; a found
            # node therefore needs no further ranking -- the udev branch
            # is the whole flow from here.

    if device_pick and best is None:
        visible = any(i.path == target for i in enumerate_devices()) or any(
            d["node"] == target for d in sysfs_hid_devices()
        )
        why = "" if visible else f" (no hidraw node named {device_pick} is visible)"
        return fail(
            f"no config-endpoint candidate at {target} speaks a known "
            f"transport family{why}; hskctl probe shows the scores",
            getattr(args, "json", False),
        )

    if device_pick is None and best is not None and not getattr(args, "yes", False) \
            and not getattr(args, "json", False):
        # One panel button, one prompt for identity: probe's score table
        # decides WHICH node the setup targets, and the user taps the final
        # pick. Everything after it remains consent-gated as before.
        from .cli import _print_candidate_scores

        scored: dict[str, dict] = {}
        for family in families:
            for candidate in rank_candidates(family):
                entry = candidate.as_dict()
                prior = scored.get(entry["path"])
                if prior is None or entry["score"] > prior["score"]:
                    if prior:
                        entry["reasons"] = prior["reasons"] + entry["reasons"]
                    scored[entry["path"]] = entry
        _print_candidate_scores(sorted(scored.values(), key=lambda c: -c["score"]))
        if from_sysfs:
            info = best[1].info
            print(f"  {info.path}  [{info.vidpid}]  unopenable -- no udev grant "
                  "yet; the setup proceeds blind and asks for the rule next")
        print(f"\ncontinuing sets up {best[1].info.path} as your mouse.")
        try:
            answer = input("that one? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\ncancelled.")
            return 0
        if answer in ("n", "no"):
            print("cancelled. Re-run with --device /dev/hidrawN to pick a "
                  "node yourself, or hskctl probe for the scores again.")
            return 0

    if best is None:
        # Plan Round 0, stated limit: no opcode guessing at unknown
        # transports. The setup ends with what was observed and what to do
        # next, rather than a fix attempt.
        if not getattr(args, "json", False) and not getattr(args, "yes", False):
            print("No connected device matches a known transport family.")
            print("Here is what the machine can see, scores included "
                  "(read-only diagnostics, nothing is written):\n")
            from .cli import _print_candidate_scores
            scored = {}
            for family in families:
                for candidate in rank_candidates(family):
                    entry = candidate.as_dict()
                    prior = scored.get(entry["path"])
                    if prior is None or entry["score"] > prior["score"]:
                        if prior:
                            entry["reasons"] = prior["reasons"] + entry["reasons"]
                        scored[entry["path"]] = entry
            _print_candidate_scores(sorted(scored.values(), key=lambda c: -c["score"]))
        hint = (
            "hskctl probe --json"
            if hasattr(args, "json")
            else "hskctl probe"
        )
        return fail(
            "no connected device matches a known transport family -- "
            "the wizard never guesses opcodes at an unknown mouse. Run "
            f"{hint} and docs/contributor/PROTOCOL-DISCOVERY.md for the "
            "contributor path.",
            getattr(args, "json", False),
        )

    family, candidate = best
    info = candidate.info
    vid, pid = f"{info.vendor_id:04x}", f"{info.product_id:04x}"

    # A profile that already claims this device gets reported, not
    # duplicated. The user profiles write as tight a match as possible, so
    # "some profile matches" and "this one is mine" cannot silently clash.
    for name in list_profiles():
        try:
            existing = load_profile(name)
        except ProtocolError:
            continue
        if _existing_cover(existing, vid, pid):
            blind = (existing.data.get("provenance") or {}).get(
                "deviceNotReadAtCreation"
            )
            detail = (
                " It was created without the live check (its udev grant was "
                "missing then) -- remove it and re-run this setup to get a "
                "live-verified one." if blind else ""
            )
            return fail(
                f"profile {name!r} already maps {vid}:{pid} ({existing.model}). "
                f"Remove it first: hskctl remove-profile {name}.{detail}",
                getattr(args, "json", False),
            )

    device_line = (
        f"{info.path} ({vid}:{pid}, {info.name or 'unnamed device'})"
    )

    if not getattr(args, "yes", False):
        if not sys.stdin.isatty() or getattr(args, "json", False):
            return fail(
                "the guided flow needs a visible terminal to ask its "
                "questions -- run it interactively, or re-run with --yes "
                "to take every default",
                getattr(args, "json", False),
            )
        print(f"device:  {device_line}")
        print(f"family:  {family.model} (from {os.path.basename(family.path)})")
        try:
            answer = input("Continue? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            print("\naborted.")
            return 1
        if answer.strip().lower() not in ("y", "yes"):
            print("cancelled.")
            return 0

    extension = _extension(family, vid, pid)

    # One real exchange, before anything is written anywhere. Reads only;
    # a mouse that is asleep or a dongle that is wedged ends the setup
    # with the same honest error every other surface does. A node the
    # udev rules do not cover yet is the one predictable non-sleep
    # failure -- it can actually happen to a family-identified device
    # (the family knows the protocol, the grant list does not know the
    # vendor), and it breaks the chicken-and-egg the other way: the
    # VID:PID pair line the engine can emit only appears once a profile
    # claims the device, which is the thing this wizard is here to
    # create. So that case gets its own branch below, not a bare abort.
    from .cli import _session  # imported late: cli imports this module

    try:
        session = _session(args, family, info.path)
        read = session.read_some(["batteryPercent", "charging"]) or {}
    except (ProtocolError, OSError) as exc:
        text = str(exc)
        lowered = text.lower()
        perm_shape = (
            isinstance(exc, PermissionError)
            or "no write access" in lowered
            or "permission denied" in lowered
            or "errno 13" in lowered
            or "eacces" in lowered
            or "not a readable" in lowered
        )
        if not perm_shape:
            return fail(
                "the device could not be read (protocol family fits, but "
                "the mouse is asleep or the cable is stale): " + text,
                getattr(args, "json", False),
            )

        line = _pair_rule_line(vid, pid)
        if not getattr(args, "yes", False):
            print()
            print("The mouse part of the family, but the udev rules do not")
            print(f"grant {vid}:{pid} read-write hidraw access yet. The grant")
            print("line it needs:")
            print(f"    {line}")
            print()
            print("The profile itself can be written now -- nothing needs the")
            print("device before the rule exists, and writing the profile is")
            print("what brings that line into the engine's rule-file output")
            print("(then the panel's Fix button installs it).")
            try:
                answer = input("Write the profile without the live check? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                print("\naborted.")
                return 1
            if answer.strip().lower() not in ("y", "yes"):
                return fail(
                    "no udev rule grants "
                    f"{vid}:{pid} read-write hidraw access yet. Install the rule "
                    f"first -- `hskctl udev-rule` shows what is missing, and the "
                    "panel's Fix button (or install.sh --udev) installs it -- "
                    "then re-run this setup after replugging the mouse.",
                    getattr(args, "json", False),
                )
        read_blocked = True
    else:
        read_blocked = False

    if read_blocked:
        read = {}
    elif not getattr(args, "yes", False):
        battery = read.get("batteryPercent")
        if battery is not None:
            print(f"live:    battery {battery}%, the device answered in the "
                  f"family's own language")

    name = args.name
    if not name:
        data_model = str(family.model or "mouse")
        name = _slugify(data_model) + ("", f"-{pid}")[extension]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        return fail(f"{name!r} is not a profile name", getattr(args, "json", False))

    dest_dir = _USER_PROFILE_DIR
    dest = os.path.join(dest_dir, name + ".json")
    if os.path.exists(dest):
        if not getattr(args, "yes", False):
            print(f"a profile named {name!r} already exists: {dest}")
            try:
                overwrite = input("overwrite? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                print("\naborted.")
                return 1
            if overwrite.strip().lower() not in ("y", "yes"):
                print("cancelled -- pick another name with --name.")
                return 0
        else:
            return fail(
                f"profile file already exists: {dest} -- pass a fresh "
                "--name to write a different profile",
                getattr(args, "json", False),
            )

    data = copy.deepcopy(family.data)
    family_match = family.data.get("match") or {}
    if extension or read_blocked:
        # Identity beyond what the family vouches for (or unreachable for
        # the live check): the tight pair is all that is known, so the
        # profile claims exactly that.
        data["match"] = {
            "vendorIds": [vid],
            "productIds": [pid],
        }
        for key in ("interface", "usagePage", "requireFeatureReport"):
            if family_match.get(key) is not None:
                data["match"][key] = family_match[key]
    else:
        # The family's identity scope IS the trusted knowledge. A receiver
        # mouse changes product id between cable and wireless -- the HSK
        # Pro 4K is 5808 on the cable, 5807 behind the dongle -- and this
        # setup ran at whichever pair happened to be live. The profile
        # therefore matches every identity the family declares, under the
        # same usage-page/feature-report shape, and the pair observed at
        # setup time rides along in provenance (the udev grant line, the
        # thing that must be tight, is generated from the rule engine's
        # own survey either way).
        data["match"] = copy.deepcopy(family_match) or {
            "vendorIds": [vid],
            "productIds": [pid],
        }
    data["provenance"] = {
        "createdBy": f"hskctl {__version__} create-profile",
        "created": datetime.date.today().isoformat(),
        "family": os.path.splitext(os.path.basename(family.path))[0],
        "extension": extension,
        "observedAtSetup": f"{vid}:{pid}",
    }
    if extension:
        data["status"] = "partial"
        note = (
            data.get("_source")
            or data.get("_pollingNote")
            or ""
        )
        data["_setupNote"] = (
            "This profile was assembled by the guided setup for a VID:PID "
            "outside the family snapshot's declared identities: the "
            "protocol family matched, but no hardware has confirmed this "
            "pairing. Writable mappings stay unverified until a "
            "verification run confirms them."
            + (f" Source note: {note}" if note else "")
        )
    if read_blocked:
        data["provenance"]["deviceNotReadAtCreation"] = (
            "the node existed but was not openable -- the udev rules did "
            "not grant read-write hidraw access to it yet. Verify the "
            "profile against the live device after the rule is installed."
        )
    if not os.path.isdir(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
    try:
        fd = open(dest + ".tmp", "w", encoding="utf-8")
        json.dump(data, fd, indent=2)
        fd.write("\n")
        fd.close()
        os.replace(dest + ".tmp", dest)
    except OSError as exc:
        return fail(f"could not write {dest}: {exc}", getattr(args, "json", False))

    # Panels pick the profile list up only at serve startup; the watchdog
    # restarts every killed transport in ~2 s, and the restarted ones see
    # the mouse through the profile this wizard just wrote.
    subprocess.run(
        ["pkill", "-f", _SERVE_PATTERN],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )

    payload = {
        "ok": True,
        "profile": name,
        "path": dest,
        "device": info.path,
        "vid": vid,
        "pid": pid,
        "family": family.name,
        "status": data.get("status"),
        "extension": extension,
    }

    def human(p):
        print(f"profile written: {p['path']}  status: {p['status']}")
        print("panel transports are restarting and will pick it up in a moment.")
        print("if the udev rule file needs the new VID:PID line, run "
              "`hskctl udev-rule` and use the panel's Fix button to install")
        if p.get("deviceNotReadAtCreation"):
            print("note: the device itself was not readable at creation "
                  "(missing udev grant) -- the values come from the family "
                  "snapshot; after the rule is installed, replug the mouse "
                  "and the panel's own reads take over.")

    return emit(payload, getattr(args, "json", False), human)
