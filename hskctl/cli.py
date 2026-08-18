"""hskctl command line.

Design note: every subcommand supports --json, and in JSON mode we *always*
emit a well-formed object with an "ok" boolean -- never a bare traceback, never
an empty stdout. The Omarchy panel parses this output, and a panel that has to
guess why stdout was empty is a panel that shows the wrong thing.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__
from .device import DeviceNotFound, open_session, rank_candidates
from .hidraw import HidrawError, enumerate_devices
from .protocol import NotDiscovered, ProtocolError, list_profiles, load_profile

FRIENDLY_LABELS = {
    "batteryPercent": "Battery",
    "charging": "Charging",
    "connection": "Connection",
    "activeDpiStage": "Active DPI stage",
    "dpiStageCount": "DPI stages",
    "pollingRate": "Polling rate",
    "motionSync": "Motion Sync",
    "liftOffDistance": "Lift-off distance",
    "debounceMs": "Debounce",
    "angleSnap": "Angle snapping",
    "rippleControl": "Ripple control",
    "sleepMinutes": "Sleep timer",
    "firmwareVersion": "Firmware",
}


def _emit(payload: dict, as_json: bool, human) -> int:
    if as_json:
        json.dump(payload, sys.stdout, indent=None)
        sys.stdout.write("\n")
    else:
        human(payload)
    return 0 if payload.get("ok") else 1


def _fail(message: str, as_json: bool, **extra) -> int:
    payload = {"ok": False, "error": message}
    payload.update(extra)
    if as_json:
        json.dump(payload, sys.stdout)
        sys.stdout.write("\n")
    else:
        print(f"error: {message}", file=sys.stderr)
    return 1


# --- probe ------------------------------------------------------------------


def cmd_probe(args) -> int:
    try:
        profile = load_profile(args.profile)
    except ProtocolError:
        profile = None

    candidates = rank_candidates(profile)
    everything = [i.as_dict() for i in enumerate_devices()]
    payload = {
        "ok": True,
        "profile": profile.model if profile else None,
        "profileStatus": profile.status if profile else None,
        "candidates": [c.as_dict() for c in candidates],
        "allDevices": everything,
    }

    def human(p):
        print(f"hidraw nodes found: {len(p['allDevices'])}\n")
        if not p["candidates"]:
            print("No likely configuration endpoint found.")
            print("Plug the mouse in (or its 4K dongle) and try again.")
            return
        print("Ranked candidates for the config endpoint:\n")
        for c in p["candidates"]:
            print(f"  {c['path']}  [{c['vidpid']}]  score {c['score']}")
            print(f"    name       : {c['name']}")
            print(f"    interface  : {c['interface']}")
            page = c["usagePage"]
            print(f"    usage page : {f'0x{page:04x}' if page is not None else '-'}")
            print(f"    feature ids: {c['featureReportIds'] or '-'}")
            for reason in c["reasons"]:
                print(f"    · {reason}")
            print()
        top = p["candidates"][0]
        print("Next step: record this in the profile, then capture traffic.")
        print(f"  vendorIds  : [\"{top['vidpid'].split(':')[0]}\"]")
        print(f"  productIds : [\"{top['vidpid'].split(':')[1]}\"]")
        print(f"  interface  : {top['interface']}")
        print("\nSee docs/PROTOCOL-DISCOVERY.md for the capture procedure.")

    return _emit(payload, args.json, human)


# --- status -----------------------------------------------------------------


def cmd_status(args) -> int:
    try:
        profile = load_profile(args.profile)
    except ProtocolError as exc:
        return _fail(str(exc), args.json)

    if not profile.discovered:
        candidates = rank_candidates(profile)
        payload = {
            "ok": False,
            "state": "undiscovered",
            "model": profile.model,
            "error": "Protocol not mapped yet for this device.",
            "detected": bool(candidates),
            "candidatePath": candidates[0].info.path if candidates else None,
            "settings": {},
        }
        return _emit(
            payload,
            args.json,
            lambda p: print(
                f"{p['model']}: "
                + (
                    "detected, but the config protocol is not mapped yet.\n"
                    "Run `hskctl probe` and follow docs/PROTOCOL-DISCOVERY.md."
                    if p["detected"]
                    else "not detected."
                )
            ),
        )

    try:
        session = open_session(profile, args.device)
        settings = session.read_all()
    except (DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return _fail(str(exc), args.json, state="error", model=profile.model, settings={})

    payload = {
        "ok": True,
        "state": "ready",
        "model": profile.model,
        "profileStatus": profile.status,
        "device": session.info.path,
        "settings": settings,
        # The panel renders a control only for fields it can actually write,
        # so the profile stays the single source of truth about what works.
        "writable": sorted(
            f for f in settings if profile.field_writable(f)
        ),
        "unverified": sorted(
            f
            for f in settings
            if (profile.data["fields"].get(f) or {}).get("_needsVerification")
        ),
    }

    def human(p):
        print(f"{p['model']}  ({p['device']})\n")
        for key, value in p["settings"].items():
            label = FRIENDLY_LABELS.get(key, key)
            if key == "batteryPercent":
                value = f"{value}%"
            elif key == "pollingRate":
                value = f"{value} Hz"
            elif key == "debounceMs":
                value = f"{value} ms"
            elif isinstance(value, bool):
                value = "on" if value else "off"
            print(f"  {label:<20} {value}")

    return _emit(payload, args.json, human)


# --- get / set --------------------------------------------------------------


def cmd_get(args) -> int:
    try:
        profile = load_profile(args.profile)
        session = open_session(profile, args.device)
        value = session.get(args.field)
    except (NotDiscovered, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return _fail(str(exc), args.json, field=args.field)
    payload = {"ok": True, "field": args.field, "value": value}
    return _emit(payload, args.json, lambda p: print(p["value"]))


def _coerce(text: str) -> Any:
    lowered = text.strip().lower()
    if lowered in ("on", "true", "yes", "enabled"):
        return True
    if lowered in ("off", "false", "no", "disabled"):
        return False
    try:
        return int(text)
    except ValueError:
        return text


def cmd_set(args) -> int:
    value = _coerce(args.value)
    try:
        profile = load_profile(args.profile)
        session = open_session(profile, args.device)
        session.set(args.field, value)
        readback = session.get(args.field)
    except (NotDiscovered, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return _fail(str(exc), args.json, field=args.field, requested=value)

    ok = str(readback) == str(value)
    payload = {
        "ok": ok,
        "field": args.field,
        "requested": value,
        "value": readback,
    }
    if not ok:
        payload["error"] = (
            f"wrote {value!r} but the mouse reads back {readback!r} -- "
            f"the field mapping in the profile is probably wrong"
        )
    return _emit(payload, args.json, lambda p: print(f"{p['field']} = {p['value']}"))


# --- fields -----------------------------------------------------------------


def cmd_fields(args) -> int:
    try:
        profile = load_profile(args.profile)
    except ProtocolError as exc:
        return _fail(str(exc), args.json)

    rows = []
    for name, spec in profile.data.get("fields", {}).items():
        if name.startswith("_") or not isinstance(spec, dict):
            continue
        rows.append(
            {
                "field": name,
                "label": FRIENDLY_LABELS.get(name, name),
                "mapped": profile.has_field(name),
                "writable": profile.field_writable(name),
                "unverified": bool(spec.get("_needsVerification")),
                "note": spec.get("_needsVerification"),
                "encoding": spec.get("encoding"),
                "allowed": profile.allowed(name),
            }
        )
    payload = {
        "ok": True,
        "model": profile.model,
        "profileStatus": profile.status,
        "fields": rows,
        "mappedCount": sum(1 for r in rows if r["mapped"]),
        "totalCount": len(rows),
    }

    def human(p):
        print(f"{p['model']} -- {p['mappedCount']}/{p['totalCount']} fields mapped\n")
        for row in p["fields"]:
            if not row["mapped"]:
                mark = "·"
            elif row["writable"]:
                mark = "✎"
            else:
                mark = "r"
            allowed = row["allowed"]
            hint = ""
            if allowed and len(allowed) == 2 and all(isinstance(a, int) for a in allowed):
                hint = f"  ({allowed[0]}–{allowed[1]})"
            elif allowed:
                hint = "  (" + ", ".join(str(a) for a in allowed) + ")"
            flag = "  [unverified]" if row["unverified"] else ""
            print(f"  {mark} {row['field']:<18}{hint}{flag}")
        print("\n  ✎ = readable and writable   r = read-only   · = not mapped")
        unverified = [r for r in p["fields"] if r["unverified"]]
        if unverified:
            print("\n  [unverified] fields read fine, but their value mapping came from")
            print("  the vendor UI rather than the command table -- check each against")
            print("  the Windows app before trusting a write:")
            for row in unverified:
                print(f"    {row['field']}: {row['note']}")

    return _emit(payload, args.json, human)


def cmd_doctor(args) -> int:
    """Read-only diagnostic: dump the raw bytes of every read command.

    Sends nothing but read opcodes, on both link flags, and prints exactly what
    came back. This is what to paste when something reads wrong -- it shows
    whether the mouse is answering at all, whether it is the right endpoint,
    and whether a value really is zero or just never arrived.
    """
    import os

    try:
        profile = load_profile(args.profile)
    except ProtocolError as exc:
        return _fail(str(exc), args.json)

    candidates = rank_candidates(profile)
    report: dict = {
        "ok": True,
        "model": profile.model,
        "profileStatus": profile.status,
        "profilePath": profile.path,
        "candidates": [c.as_dict() for c in candidates],
        "attempts": [],
    }

    if not candidates and not args.device:
        report["ok"] = False
        report["error"] = "no candidate HID node found"
        return _emit(report, args.json, lambda p: print(p["error"]))

    path = args.device or candidates[0].info.path
    report["device"] = path

    # Permissions matter more than anything else here: every exchange needs the
    # node opened read-write, so a udev rule that never landed looks exactly
    # like a mouse that will not answer.
    access = {"path": path, "exists": os.path.exists(path)}
    for mode, label in ((os.R_OK, "read"), (os.W_OK, "write")):
        access[label] = os.access(path, mode)
    try:
        st = os.stat(path)
        access["mode"] = oct(st.st_mode & 0o777)
        access["uid"] = st.st_uid
        access["gid"] = st.st_gid
    except OSError as exc:
        access["error"] = str(exc)
    report["access"] = access

    try:
        session = open_session(profile, path)
    except (DeviceNotFound, ProtocolError) as exc:
        report["ok"] = False
        report["error"] = str(exc)
        return _emit(report, args.json, lambda p: print(p["error"]))

    commands = [
        name
        for name in profile.data.get("commands", {})
        if not name.startswith("_") and profile.has_command(name)
    ]
    for name in commands:
        for wireless in (False, True):
            report["attempts"].append(session.trial_read(name, wireless))

    def human(p):
        print(f"{p['model']}   profile: {p['profileStatus']}   {p['profilePath']}")
        print()
        print("candidates:")
        for c in p["candidates"]:
            mark = "->" if c["path"] == p.get("device") else "  "
            page = c["usagePage"]
            print(
                f"  {mark} {c['path']}  {c['vidpid']}  iface={c['interface']}  "
                f"usagePage={f'0x{page:04x}' if page is not None else '-'}  "
                f"featureIds={c['featureReportIds'] or '-'}  score={c['score']}"
            )
        a = p["access"]
        print()
        print(
            f"access: {a['path']}  mode={a.get('mode')}  "
            f"readable={a.get('read')}  writable={a.get('write')}"
        )
        if not a.get("write"):
            print("  !! not writable -- every exchange needs O_RDWR.")
            print("     Run ./install.sh --udev, then replug the mouse or dongle.")
        print()
        print("read attempts (nothing below writes to the mouse):")
        for att in p["attempts"]:
            tag = f"{att['command']}/{'dongle' if att['wireless'] else 'wired'}"
            if "error" in att:
                print(f"  {tag:<24} ERROR {att['error']}")
                continue
            ack = "ACK" if att.get("ack") else "no-ack"
            zero = " ALL-ZERO" if att.get("allZero") else ""
            byte = att.get("ackByte")
            print(
                f"  {tag:<24} {ack:<7} byte1="
                f"{f'0x{byte:02x}' if byte is not None else '--'}{zero}"
            )
            print(f"    tx {att['request'][:47]}")
            print(f"    rx {att.get('reply', '')[:47]}")
        acked = [a for a in p["attempts"] if a.get("ack")]
        print()
        if not acked:
            print("Nothing acknowledged. Either this is the wrong hidraw node,")
            print("or the mouse is asleep -- move it and run this again.")
        else:
            links = {a["wireless"] for a in acked}
            print(
                f"{len(acked)} of {len(p['attempts'])} attempts acknowledged; "
                f"working link flag: {'dongle' if True in links else 'wired'}"
            )

    return _emit(report, args.json, human)


def cmd_profiles(args) -> int:
    names = list_profiles()
    payload = {"ok": True, "profiles": names}
    return _emit(payload, args.json, lambda p: print("\n".join(p["profiles"]) or "(none)"))


# --- entry point ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hskctl",
        description="Configure a G-Wolves HSK Pro 4K (and friends) from Linux.",
    )
    parser.add_argument("--version", action="version", version=f"hskctl {__version__}")
    parser.add_argument("--profile", help="device profile name", default=None)
    parser.add_argument("--device", help="force a specific /dev/hidrawN", default=None)
    parser.add_argument("--json", action="store_true", help="machine-readable output")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="find and rank candidate HID endpoints").set_defaults(
        func=cmd_probe
    )
    sub.add_parser("status", help="read every mapped setting").set_defaults(func=cmd_status)
    sub.add_parser("fields", help="show which settings are mapped").set_defaults(
        func=cmd_fields
    )
    sub.add_parser("profiles", help="list available device profiles").set_defaults(
        func=cmd_profiles
    )
    sub.add_parser(
        "doctor", help="dump the raw bytes of every read command (writes nothing)"
    ).set_defaults(func=cmd_doctor)

    get_p = sub.add_parser("get", help="read one setting")
    get_p.add_argument("field")
    get_p.set_defaults(func=cmd_get)

    set_p = sub.add_parser("set", help="write one setting")
    set_p.add_argument("field")
    set_p.add_argument("value")
    set_p.set_defaults(func=cmd_set)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # last-resort: still emit parseable JSON
        return _fail(f"{type(exc).__name__}: {exc}", getattr(args, "json", False))


if __name__ == "__main__":
    sys.exit(main())
