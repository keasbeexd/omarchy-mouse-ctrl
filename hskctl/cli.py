"""hskctl command line.

Design note: every subcommand supports --json, and in JSON mode we *always*
emit a well-formed object with an "ok" boolean -- never a bare traceback, never
an empty stdout. The Omarchy panel parses this output, and a panel that has to
guess why stdout was empty is a panel that shows the wrong thing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from . import __version__
from .device import DeviceBusy, DeviceNotFound, open_session, rank_candidates
from .hidraw import HidrawError, enumerate_devices
from .protocol import NotDiscovered, ProtocolError, list_profiles, load_profile

FRIENDLY_LABELS = {
    "batteryPercent": "Battery",
    "charging": "Charging",
    "connection": "Connection",
    "activeDpiStage": "Active DPI stage",
    "pollingRate": "Polling rate",
    "motionSync": "Motion Sync",
    "liftOffDistance": "Lift-off distance",
    "debounceMs": "Debounce (raw)",
    "angleSnap": "Angle snapping",
    "sleepMinutes": "Sleep timer (raw)",
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
    except (DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
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
                # Not milliseconds: this is byte 0 of a 4-byte tuple indexed by
                # a row in the driver's table. Do not imply a unit.
                value = f"{value} (raw; unit not established)"
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
    except (NotDiscovered, DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
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
        if getattr(args, "verbose", False):
            session.trace = []
        if getattr(args, "raw", False):
            session.set_raw(args.field, int(args.value))
            readback = session.get_raw(args.field)
            value = int(args.value)
        else:
            session.set(args.field, value)
            readback = session.get(args.field)
    except (NotDiscovered, DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return _fail(str(exc), args.json, field=args.field, requested=value)

    if getattr(args, "verbose", False) and session.trace:
        print(f"link: {'dongle' if session.wireless else 'wired'}", file=sys.stderr)
        for entry in session.trace:
            tokens = entry["data"].split()
            print(f"  {entry['step']}:", file=sys.stderr)
            for i in range(0, min(len(tokens), 32), 16):
                print(f"    {' '.join(tokens[i:i + 16])}", file=sys.stderr)

    ok = str(readback) == str(value)
    payload = {
        "ok": ok,
        "field": args.field,
        "requested": value,
        "value": readback,
        "link": "dongle" if session.wireless else "wired",
    }
    if not ok:
        # "reads back 0" on its own sends people hunting for a mapping bug. The
        # usual cause is a mouse that was asleep when the packet landed, which
        # looks identical unless the message says so.
        payload["error"] = (
            f"wrote {value!r} to {args.field} but the mouse reads back "
            f"{readback!r} (link: {payload['link']}). If that is 0 or blank the "
            f"mouse was probably asleep -- move it and try again. If it is a "
            f"different real value, the mapping in the profile is wrong."
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
    except (DeviceBusy, DeviceNotFound, ProtocolError) as exc:
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
            for label, blob in (("tx", att["request"]), ("rx", att.get("reply", ""))):
                tokens = blob.split()
                # 16 bytes a line, and stop at the last non-zero so a 65-byte
                # packet with a short payload does not print four blank rows.
                last = max(
                    (i for i, t in enumerate(tokens) if t != "00"), default=-1
                )
                shown = tokens[: max(8, last + 2)]
                for i in range(0, len(shown), 16):
                    prefix = label if i == 0 else "  "
                    print(f"    {prefix} {' '.join(shown[i:i + 16])}")
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


def cmd_measure_polling(args) -> int:
    """Time the mouse's input reports to measure the real polling rate.

    This settles the one thing static analysis could not: the raw byte the
    firmware reports for polling rate means nothing until you know which Hz it
    corresponds to. Rather than trust a guessed enum, count the reports.
    """
    import statistics
    import time

    from .hidraw import HidrawDevice, enumerate_devices

    try:
        profile = load_profile(args.profile)
    except ProtocolError as exc:
        return _fail(str(exc), args.json)

    vids = {v.lower() for v in profile.match.get("vendorIds") or []}
    pointer = None
    for info in enumerate_devices():
        if vids and f"{info.vendor_id:04x}" not in vids:
            continue
        # The pointer node is Generic Desktop / Mouse, and it is the only one
        # that streams movement.
        if info.usage_page == 0x01 and info.usage == 0x02:
            pointer = info
            break
    if pointer is None:
        return _fail(
            "could not find the mouse's pointer node (Generic Desktop / Mouse). "
            "Run `hskctl probe` to see what is present.",
            args.json,
        )

    seconds = max(1.0, float(args.seconds))
    stamps: list[float] = []
    print(
        f"Move the mouse continuously for {seconds:.0f} seconds...",
        file=sys.stderr,
    )
    try:
        with HidrawDevice(pointer.path) as dev:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                data = dev.read_input(64, timeout=0.2)
                if data:
                    stamps.append(time.monotonic())
    except (OSError, HidrawError) as exc:
        return _fail(str(exc), args.json, device=pointer.path)

    if len(stamps) < 20:
        return _fail(
            f"only {len(stamps)} reports seen -- the mouse has to be moving "
            f"throughout. Try again and keep it moving.",
            args.json,
            device=pointer.path,
            samples=len(stamps),
        )

    gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
    median_gap = statistics.median(gaps)
    measured = 1.0 / median_gap if median_gap else 0.0
    # Snap to the rate the firmware actually offers.
    ladder = [125, 250, 500, 1000, 2000, 4000, 8000]
    nearest = min(ladder, key=lambda r: abs(r - measured))

    payload = {
        "ok": True,
        "device": pointer.path,
        "samples": len(stamps),
        "measuredHz": round(measured, 1),
        "nearestRate": nearest,
    }

    def human(p):
        print(f"\nsamples      : {p['samples']}")
        print(f"measured     : {p['measuredHz']} Hz")
        print(f"nearest rate : {p['nearestRate']} Hz")
        print()
        print("Compare that with `hskctl get pollingRate`. If they disagree, the")
        print("raw->Hz map in the profile is wrong; tell me both numbers and I")
        print("will correct it.")

    return _emit(payload, args.json, human)


def _sample_polling(profile, seconds: float) -> float:
    """Median report rate from the pointer node, in Hz. 0.0 if too few samples."""
    import statistics
    import time

    from .hidraw import HidrawDevice, enumerate_devices

    vids = {v.lower() for v in profile.match.get("vendorIds") or []}
    pointer = None
    for info in enumerate_devices():
        if vids and f"{info.vendor_id:04x}" not in vids:
            continue
        if info.usage_page == 0x01 and info.usage == 0x02:
            pointer = info
            break
    if pointer is None:
        return 0.0

    stamps: list[float] = []
    with HidrawDevice(pointer.path) as dev:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if dev.read_input(64, timeout=0.2):
                stamps.append(time.monotonic())
    if len(stamps) < 20:
        return 0.0
    gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
    median = statistics.median(gaps)
    return (1.0 / median) if median else 0.0


def cmd_calibrate_polling(args) -> int:
    """Sweep the polling register and measure what each raw value actually does.

    Static analysis gave us the command but not the meaning of its argument.
    Instead of guessing the table, write each candidate and time the mouse's
    own reports. The original value is restored at the end.
    """
    import time

    try:
        profile = load_profile(args.profile)
        session = open_session(profile, args.device)
        original = session.get_raw("pollingRate")
    except (NotDiscovered, DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return _fail(str(exc), args.json)

    results: list[dict] = []
    try:
        sweep = [int(v) for v in str(args.values).split(",") if v.strip()]
    except ValueError:
        return _fail("--values must be a comma-separated list of integers", args.json)
    print(
        f"Sweeping raw {sweep}. Keep the mouse moving the whole time "
        f"(about {len(sweep) * (args.seconds + 1):.0f} seconds).\n",
        file=sys.stderr,
    )
    try:
        for raw in sweep:
            try:
                session.set_raw("pollingRate", raw)
            except (ProtocolError, OSError) as exc:
                results.append({"raw": raw, "error": str(exc)})
                continue
            time.sleep(0.6)
            readback = session.get_raw("pollingRate")
            measured = _sample_polling(profile, args.seconds)
            results.append(
                {
                    "raw": raw,
                    "accepted": readback == raw,
                    "readback": readback,
                    "measuredHz": round(measured, 1),
                }
            )
            print(
                f"  raw {raw}: readback {readback}  "
                f"{'~' + str(round(measured)) + ' Hz' if measured else 'no samples'}",
                file=sys.stderr,
            )
    finally:
        try:
            session.set_raw("pollingRate", original)
            print(f"\nrestored raw {original}", file=sys.stderr)
        except (ProtocolError, OSError):
            print(
                f"\n!! could not restore raw {original} -- set it in the vendor app",
                file=sys.stderr,
            )

    # Fit a divider before assuming a lookup table: if every measurement matches
    # base/raw, the register is a divisor of a base clock and an enum would be
    # the wrong shape entirely.
    good = [r for r in results if r.get("accepted") and r.get("measuredHz")]
    divider = None
    if len(good) >= 3:
        # The base is whatever raw 1 clocks (or raw*Hz, which is constant).
        bases = [r["measuredHz"] * max(r["raw"], 1) for r in good]
        candidate = round(sum(bases) / len(bases))
        worst = max(
            abs(candidate / max(r["raw"], 1) - r["measuredHz"])
            / (candidate / max(r["raw"], 1))
            for r in good
        )
        if worst < 0.05:
            divider = {"base": candidate, "worstErrorPct": round(worst * 100, 2)}

    payload = {
        "ok": bool(good),
        "original": original,
        "results": results,
        "dividerFit": divider,
        "derivedValues": {str(r["raw"]): round(r["measuredHz"]) for r in good},
    }

    def human(p):
        print()
        if not p["derivedValues"]:
            print("No raw value produced a usable measurement.")
            print("The mouse has to be moving throughout the sweep.")
            return
        fit = p["dividerFit"]
        if fit:
            print(
                f"This register is a DIVIDER, not a lookup table: every measurement "
                f"fits {fit['base']} Hz / raw to within {fit['worstErrorPct']}%."
            )
            print(f'Set fields.pollingRate.dividerBase to {fit["base"]}.')
            print(f'Ceiling is {fit["base"]} Hz -- no divisor goes above it.')
        else:
            print("No divider fits. Measured values, for a lookup table:")
            print(json.dumps(p["derivedValues"], indent=2))

    return _emit(payload, args.json, human)


def cmd_fix_dpi(args) -> int:
    """Repair a DPI block containing out-of-range values.

    Reads the block, clamps any stage whose X or Y is outside the sensor's
    range, forces Y to match X, and writes the whole thing back in one go.
    Setting stages one at a time cannot fix this: every write rewrites the
    entire block, so a corrupt neighbour would be carried straight back.
    """
    try:
        profile = load_profile(args.profile)
        session = open_session(profile, args.device)
    except (DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return _fail(str(exc), args.json)

    lo, hi = 50, 26000
    changes: list[dict] = []
    try:
        for n in range(1, 8):
            x_field, y_field = f"dpiStage{n}", f"dpiStage{n}Y"
            if not profile.has_field(x_field):
                continue
            x = session.get(x_field)
            y = session.get(y_field) if profile.has_field(y_field) else x
            new_x = x if lo <= x <= hi else args.default
            new_y = new_x if not (lo <= y <= hi) or y != new_x else y
            if new_x != x:
                session.set(x_field, new_x)
            if profile.has_field(y_field) and new_y != y:
                session.set(y_field, new_y)
            if new_x != x or new_y != y:
                changes.append(
                    {"stage": n, "wasX": x, "wasY": y, "nowX": new_x, "nowY": new_y}
                )
    except (NotDiscovered, HidrawError, ProtocolError, OSError) as exc:
        return _fail(str(exc), args.json, changes=changes)

    payload = {"ok": True, "changes": changes}

    def human(p):
        if not p["changes"]:
            print("Every DPI stage is already in range. Nothing changed.")
            return
        for c in p["changes"]:
            print(
                f"  stage {c['stage']}: {c['wasX']}x{c['wasY']} -> "
                f"{c['nowX']}x{c['nowY']}"
            )
        print(f"\nRepaired {len(p['changes'])} stage(s).")

    return _emit(payload, args.json, human)


SETTINGS_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "hskctl",
    "settings.json",
)


def cmd_save(args) -> int:
    """Record the mouse's current writable settings to disk.

    The mouse does not keep DPI across a power cycle. Neither does the vendor's
    Windows software rely on it to: `in_Queue_Close` and `in_Queue_Open` call
    ReadFileProlie/WriteFileProlie, so the app stores settings in local files
    and pushes them back when the mouse reconnects. This is the same approach,
    without needing a tray icon.
    """
    try:
        profile = load_profile(args.profile)
        session = open_session(profile, args.device)
        settings = session.read_all()
    except (DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return _fail(str(exc), args.json)

    keep = {k: v for k, v in settings.items() if profile.field_writable(k)}
    path = args.file or SETTINGS_PATH
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"model": profile.model, "settings": keep}, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        return _fail(f"could not write {path}: {exc}", args.json)

    payload = {"ok": True, "path": path, "saved": keep}
    return _emit(
        payload,
        args.json,
        lambda p: print(f"Saved {len(p['saved'])} settings to {p['path']}"),
    )


def cmd_apply(args) -> int:
    """Push saved settings back to the mouse."""
    path = args.file or SETTINGS_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            saved = json.load(fh).get("settings", {})
    except OSError as exc:
        return _fail(f"could not read {path}: {exc}", args.json, path=path)
    except ValueError as exc:
        return _fail(f"{path} is not valid JSON: {exc}", args.json, path=path)

    try:
        profile = load_profile(args.profile)
        session = open_session(profile, args.device)
    except (DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return _fail(str(exc), args.json)

    applied, skipped = [], []
    for field, value in saved.items():
        if not profile.field_writable(field):
            skipped.append({"field": field, "why": "not writable"})
            continue
        try:
            if session.get(field) == value:
                continue
            session.set(field, value)
            applied.append({"field": field, "value": value})
        except (NotDiscovered, HidrawError, ProtocolError, OSError) as exc:
            skipped.append({"field": field, "why": str(exc)})

    payload = {"ok": True, "path": path, "applied": applied, "skipped": skipped}

    def human(p):
        if not p["applied"]:
            print("Mouse already matches the saved settings.")
        for a in p["applied"]:
            print(f"  {a['field']} -> {a['value']}")
        for sk in p["skipped"]:
            print(f"  skipped {sk['field']}: {sk['why']}")

    return _emit(payload, args.json, human)


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

    save = sub.add_parser("save", help="record current settings to disk")
    save.add_argument("--file")
    save.set_defaults(func=cmd_save)

    apply_p = sub.add_parser("apply", help="push saved settings back to the mouse")
    apply_p.add_argument("--file")
    apply_p.set_defaults(func=cmd_apply)

    fix = sub.add_parser(
        "fix-dpi", help="clamp out-of-range DPI stages and re-link Y to X"
    )
    fix.add_argument("--default", type=int, default=800)
    fix.set_defaults(func=cmd_fix_dpi)

    measure = sub.add_parser(
        "measure-polling", help="time the mouse's reports to measure real Hz"
    )
    measure.add_argument("--seconds", type=float, default=3.0)
    measure.set_defaults(func=cmd_measure_polling)

    calib = sub.add_parser(
        "calibrate-polling",
        help="sweep the polling register and measure what each raw value means",
    )
    calib.add_argument("--seconds", type=float, default=2.0)
    calib.add_argument(
        "--values",
        default="1,2,4,8,16,32,64",
        help="comma-separated raw values to sweep. The vendor slider emits "
             "1<<position for positions >= 0, and position*-32 (32, 64) for "
             "the two above the base rate -- so a linear sweep misses them.",
    )
    calib.set_defaults(func=cmd_calibrate_polling)

    get_p = sub.add_parser("get", help="read one setting")
    get_p.add_argument("field")
    get_p.set_defaults(func=cmd_get)

    set_p = sub.add_parser("set", help="write one setting")
    set_p.add_argument("field")
    set_p.add_argument("value")
    set_p.add_argument(
        "--verbose",
        action="store_true",
        help="dump the bytes of the exchange, for diagnosing a write that does nothing",
    )
    set_p.add_argument(
        "--raw",
        action="store_true",
        help="write the wire value directly, bypassing the friendly-value table",
    )
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
