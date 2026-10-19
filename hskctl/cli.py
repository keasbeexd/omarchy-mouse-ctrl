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
import secrets
import sys
import time
from typing import Any

from . import __version__
from .device import (
    DeviceBusy,
    DeviceNotFound,
    detect_profile,
    device_lock,
    open_session,
    rank_candidates,
)
from .hidraw import HidrawError, enumerate_devices, sysfs_hid_devices
from .protocol import NotDiscovered, ProtocolError, list_profiles, load_profile
from .safeio import read_bounded_json

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
    "rippleControl": "Ripple control",
    "turboMode": "Turbo mode",
    "sleepSeconds": "Sleep timer",
    "firmwareVersion": "Firmware",
}


def _session(args, profile, path=None, lock: bool = True):
    """Open a session, carrying the user's explicit override if they gave one.

    The override exists because profiling a device the profile does not know
    about requires writing to it -- and refusing that outright would make the
    tool unable to grow. It is deliberately awkward to reach.

    `lock` is passed through to `open_session`: False inside a `serve`
    process, which scopes the device fence per request instead of once per
    process (see cmd_serve).
    """
    session = open_session(profile, path if path is not None else args.device, lock=lock)
    if getattr(args, "force_unmatched", False):
        session.allow_unidentified_writes = True
    return session


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


def _plain(text: Any, limit: int = 120) -> str:
    """Strip control characters from a string that came off the device.

    A hidraw node's product string is written by the mouse's own firmware, and
    this CLI prints it to a terminal -- where an embedded ESC can drive the
    terminal (window title, clipboard, spoofed prompts). The panel already
    strips these for the same reason (see Model.plain); this is the terminal's
    version. JSON output is left as-is: it is data, not a terminal.
    """
    out = []
    for ch in str(text):
        code = ord(ch)
        if code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F:
            continue
        out.append(ch)
        if len(out) >= limit:
            break
    return "".join(out)


# --- probe ------------------------------------------------------------------


def cmd_probe(args) -> int:
    try:
        profile = detect_profile(args.profile)
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
            print(f"    name       : {_plain(c['name'])}")
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
        print("\nSee docs/contributor/PROTOCOL-DISCOVERY.md for the capture procedure.")

    return _emit(payload, args.json, human)


# --- status -----------------------------------------------------------------


def _status_payload(args, profile, session=None) -> dict:
    """Everything `status` reports, as the payload dict; device failures become
    `ok: false` payloads, not exceptions -- the panel parses errors.

    `session` lets a long-lived caller hand in a session it keeps alive
    between requests; the default is one per call, the one-shot shape.
    """
    if not profile.discovered:
        candidates = rank_candidates(profile)
        return {
            "ok": False,
            "state": "undiscovered",
            "model": profile.model,
            "error": "Protocol not mapped yet for this device.",
            "detected": bool(candidates),
            "candidatePath": candidates[0].info.path if candidates else None,
            "settings": {},
            "version": __version__,
        }

    try:
        if session is None:
            session = _session(args, profile, args.device)
        settings = session.read_all()
    except (DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return {
            "ok": False,
            "state": "error",
            "model": profile.model,
            "error": str(exc),
            "detected": False,
            "settings": {},
            "version": __version__,
        }

    return {
        "ok": True,
        "state": "ready",
        # The panel reads its footer label from here. It used to fetch
        # manifest.json over XMLHttpRequest, which failed silently in the
        # shell and left the label hidden with nothing to debug. This rides
        # the one channel already proven to work: a process, JSON on stdout.
        "version": __version__,
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
        # The legal values for a field -- e.g. which polling rates this mouse
        # actually offers -- so the panel can build its selectors from the
        # profile instead of a hardcoded list that only fit one mouse.
        "allowed": {
            f: profile.allowed(f)
            for f in settings
            if profile.allowed(f) is not None
        },
        # How each field asks to be rendered: its label, its widget, its
        # step. The panel used to hardcode all of this per field name; now
        # the profile decides, and `hskctl fields` prints the same labels the
        # panel will. A profile with no `ui` blocks still carries the derived
        # widget, so a barebones profile still gets the right control per
        # field -- only its label degrades to the CLI's fallback table.
        "ui": {f: profile.field_ui(f) for f in settings},
        "sections": profile.ui_sections(),
        # The one field whose panel row has an extra runtime gate (see
        # `probe-stages`): the profile's claim, as a name, so the panel can
        # hide that row until a probe run answers `capable: true` -- without
        # the QML ever hardcoding the field name itself. Absent when the
        # profile refuses switching (Pulsar) or nothing about the field
        # supports it.
        "stageSwitchField": profile.stage_switch_field(),
    }


def cmd_status(args) -> int:
    try:
        profile = detect_profile(args.profile)
    except ProtocolError as exc:
        return _fail(str(exc), args.json)

    payload = _status_payload(args, profile)

    def human(p):
        if p["state"] == "undiscovered":
            print(
                f"{p['model']}: "
                + (
                    "detected, but the config protocol is not mapped yet.\n"
                    "Run `hskctl probe` and follow docs/contributor/PROTOCOL-DISCOVERY.md."
                    if p["detected"]
                    else "not detected."
                )
            )
            return
        print(f"{p['model']}  ({p.get('device', 'not found')})   hskctl {p['version']}\n")
        for key, value in p["settings"].items():
            label = _field_label(profile, key)
            if key == "batteryPercent":
                value = f"{value}%"
            elif key == "pollingRate":
                value = f"{value} Hz"
            elif key == "sleepSeconds":
                # Seconds, confirmed by timing the mouse. Show minutes too --
                # nobody thinks in 240-second units.
                value = (f"{value} s ({value / 60:.0f} min)" if value >= 60
                         else f"{value} s")
            elif key == "debounceMs":
                # Not milliseconds: this is byte 0 of a 4-byte tuple indexed by
                # a row in the driver's table. Do not imply a unit.
                value = f"{value} (raw; unit not established)"
            elif isinstance(value, bool):
                value = "on" if value else "off"
            print(f"  {label:<20} {value}")

    return _emit(payload, args.json, human)


# -- stage-switch verification -------------------------------------------------
#
# This is the plugin's first persistent state. Everything else hskctl touches
# is the mouse's own memory; the verdict of a live probe is the plugin's own
# knowledge, "this firmware switches stages when told", and re-proving it on
# every panel open would mean a brief staged write every single time. So the
# verdict is cached per mouse identity+firmware+profile, positive *and*
# negative, and a firmware update (or a different mouse on the same dongle)
# changes the key and forces a fresh probe. ~/.cache, never ~/.config: it is
# reproducible knowledge, not a setting.

_STAGE_CACHE_MAX_BYTES = 65536
_STAGE_CACHE_MAX_ENTRIES = 64


def _stage_cache_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "hskctl", "stage-probe.json")


def _stage_cache_load(path: str) -> dict:
    try:
        doc = read_bounded_json(path, _STAGE_CACHE_MAX_BYTES)
    except (OSError, ValueError):
        return {}
    entries = doc.get("stageProbes")
    return entries if isinstance(entries, dict) else {}


def _stage_cache_store(path: str, entries: dict) -> None:
    entries = dict(entries)
    while len(entries) > _STAGE_CACHE_MAX_ENTRIES:
        # Drop the oldest verdict, not an arbitrary one: eviction needs to be
        # deterministic, and pinned-by-recency is the next best thing to
        # pinning by usefulness.
        oldest = min(
            entries,
            key=lambda k: entries[k].get("checkedAt", 0) if isinstance(entries[k], dict) else 0,
        )
        del entries[oldest]
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    body = json.dumps(
        {"stageProbes": entries}, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    tmp = os.path.join(directory, f".stage-probe.{os.getpid()}.tmp")
    with open(tmp, "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _probe_stages_payload(args, profile, session=None) -> dict:
    """The probe's verdict tree, shared by `probe-stages` and `serve`.

    `ok: true` always; a verdict of `null` means inconclusive (transient
    failure, or the profile offers no switcher) -- never cached, retried on
    the next call.
    """
    field = profile.stage_switch_field() if profile.discovered else None
    if field is None:
        return {
            "ok": True,
            "field": field,
            "capable": None,
            "cached": False,
            "reason": (
                "the profile offers no stage switcher"
                if profile.discovered
                else "the protocol is not mapped for this device"
            ),
        }

    try:
        if session is None:
            session = open_session(profile)
    except (DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return {"ok": False, "field": field, "capable": None, "cached": False,
                "error": str(exc)}

    vidpid = session.info.vidpid
    firmware = ""
    if profile.has_field("firmwareVersion"):
        try:
            firmware = str(session.get("firmwareVersion"))
        except (DeviceBusy, ProtocolError, OSError):
            firmware = ""
    key = f"{vidpid}|{firmware}|{profile.name}"
    path = _stage_cache_path()
    entries = _stage_cache_load(path)
    cached = entries.get(key)
    if isinstance(cached, dict) and cached.get("capable") in (True, False):
        return {
            "ok": True,
            "field": field,
            "capable": cached["capable"],
            "cached": True,
            "reason": cached.get("reason", ""),
        }

    try:
        result = session.probe_stage_switch(field)
    except (DeviceBusy, ProtocolError, OSError) as exc:
        return {"ok": False, "field": field, "capable": None, "cached": False,
                "error": f"stage probe failed: {exc}"}

    if result.get("capable") in (True, False) and result.get("reverted"):
        entries[key] = {
            "capable": result["capable"],
            "reason": result.get("reason", ""),
            "checkedAt": int(time.time()),
        }
        try:
            _stage_cache_store(path, entries)
        except OSError as exc:
            # A cache write failure degrades to re-probing next time; the
            # probe itself already happened and the verdict is real.
            if not args.json:
                print(f"(cache write failed: {exc})", file=sys.stderr)

    return {
        "ok": True,
        "field": field,
        "capable": result.get("capable"),
        "cached": False,
        "reason": result.get("reason", ""),
        "reverted": result.get("reverted", True),
        "stages": result.get("stages"),
    }


def cmd_probe_stages(args) -> int:
    """Answer, once per mouse+firmware: does switching DPI stages actually work?

    Two gates, on purpose. The profile gates (see `stage_switch_field`):
    a read-only or unmapped stage field never gets probed, which is how the
    Pulsar profile keeps the switcher off despite writes that round-trip.
    The live probe gates: a staged write happens, the readback moves, nothing
    else in the block may change, and the original stage must come back.

    A verdict of `true` or `false` is cached under ~/.cache/hskctl keyed by
    VID:PID, firmware and profile, so the mouse sees the probe's few staged
    writes exactly once per firmware. `null` means inconclusive (transient
    failure, or the profile offers no switcher) -- never cached, retried on
    the next call.
    """
    try:
        profile = detect_profile(args.profile)
    except ProtocolError as exc:
        return _fail(str(exc), args.json)

    return _emit(_probe_stages_payload(args, profile), args.json, _print_probe)


def _print_probe(p: dict) -> None:
    verdict = p.get("capable")
    cache_note = " (cached)" if p.get("cached") else ""
    if verdict is True:
        stages = p.get("stages")
        stage_note = f", {stages} stage(s)" if isinstance(stages, int) and stages else ""
        print(f"Stage switching works{stage_note}{cache_note}.")
    elif verdict is False:
        print(f"Stage switching does NOT work{cache_note}: {p.get('reason') or 'unproven'}")
    else:
        print(p.get("reason") or "inconclusive; will re-probe next time")


def _battery_payload(args, profile, session=None) -> dict:
    """batteryPercent + charging only -- one exchange, not the whole profile.

    `status` reads every mapped field, one exchange per distinct command; for
    a widget polling every few seconds just to notice a charging-cable flip,
    that is DPI stages, polling rate and every sensor toggle re-read for no
    reason. This is the same idea as `get`, but batteryPercent and charging
    share one command on every shipped profile, so asking for both here still
    costs one exchange rather than two separate process spawns.
    """
    if not profile.discovered:
        return {"ok": False, "state": "undiscovered", "settings": {}}

    try:
        if session is None:
            session = _session(args, profile, args.device)
        settings = session.read_some(["batteryPercent", "charging"])
    except (DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return {"ok": False, "state": "error", "settings": {}, "error": str(exc)}

    return {"ok": True, "state": "ready", "settings": settings}


def cmd_battery(args) -> int:
    try:
        profile = detect_profile(args.profile)
    except ProtocolError as exc:
        return _fail(str(exc), args.json)

    return _emit(
        _battery_payload(args, profile), args.json,
        lambda p: print(
            f"{p['settings'].get('batteryPercent', '?')}%"
            + (" (charging)" if p["settings"].get("charging") else "")
        ),
    )


# --- watch-link ---------------------------------------------------------


def _watch_link_loop(profile, emit) -> None:
    """Emit one event per plug/unplug of this profile's HID nodes.

    Deliberately never talks to the mouse: a hidraw node for this profile's
    vendor/product IDs appearing or disappearing under /dev is the kernel's
    own signal that something changed, and watching for that with inotify
    costs nothing on the wire. A caller that wants to know *what* changed
    (battery percent, charging) still has to ask the mouse itself -- this
    just tells it when it is worth asking right now instead of on a timer.

    `emit(event, present)` receives a snapshot set of node paths. The one-shot
    `watch-link` command passes a printer; `serve` passes a thread-safe
    stream writer.
    """
    from . import hidraw

    def snapshot() -> set[str]:
        return {c.info.path for c in rank_candidates(profile) if c.identified}

    last = snapshot()
    emit("initial", last)

    try:
        fd = hidraw.inotify_init()
    except OSError:
        return

    try:
        hidraw.inotify_add_watch(
            fd, "/dev", hidraw.IN_CREATE | hidraw.IN_DELETE | hidraw.IN_ATTRIB
        )
        while True:
            names = hidraw.read_inotify_events(fd)
            if not any(name.startswith("hidraw") for name in names):
                continue
            # udev still has to run its rules (permissions, symlinks) after
            # the node appears; without this a snapshot taken immediately on
            # IN_CREATE can catch the node before it is readable yet.
            time.sleep(0.15)
            current = snapshot()
            if current != last:
                last = current
                emit("changed", current)
    except OSError:
        # The watch fd died (an unmount, a kill race). Stop quietly: for the
        # one-shot that is an exit, for serve the restart machinery notices.
        return
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def cmd_watch_link(args) -> int:
    """Print one JSON line whenever the mouse's cable or dongle is plugged
    or unplugged, for as long as this process runs.
    """
    try:
        profile = detect_profile(args.profile)
    except ProtocolError as exc:
        return _fail(str(exc), args.json)

    def emit(event: str, present: set[str]) -> None:
        line = json.dumps({"ok": True, "event": event, "connected": bool(present)})
        print(line, flush=True)

    _watch_link_loop(profile, emit)
    return 0


# --- cross-bar activity ------------------------------------------------------


def _activity_path() -> str | None:
    """Where settings-changes are published to other hskctl processes.

    One mouse, many consumers: a serve process per bar, plus one-shot `set`
    commands from a terminal. Whichever of them writes, the writer is the
    only holder of the authoritative readback, so instead of having every
    other panel re-read the device, the writer publishes what it did to a
    small file under XDG_RUNTIME_DIR (per-user, wiped at logout, mode 0700)
    and every serve process watching it relays the fact onto its own stream,
    where the panels merge it into their values locally. One notification,
    zero device traffic, and no panel shows a stale stage after its twin
    changes one.

    Without XDG_RUNTIME_DIR (a bare terminal through su, say) publishing is
    skipped: the panels fall back to what they already had until they next
    refresh, exactly as before this file existed.
    """
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        return None
    return os.path.join(base, "hskctl", "activity.json")


def _notify_activity(payload: dict) -> None:
    """Publish a change; failures are silent by design.

    This is a courtesy signal, not a transaction: a failed publish leaves
    the sibling panels on their next scheduled refresh, which is exactly
    what they did before the file existed.
    """
    path = _activity_path()
    if path is None:
        return
    try:
        _atomic_write_json(path, payload, private=True)
    except (OSError, ValueError):
        pass


def _watch_activity_loop(emit) -> None:
    """Relay published changes onto a stream, in house style.

    The reader half of _notify_activity: watches the activity directory with
    inotify and re-emits the file's payload as one event per change. Writes
    are atomic renames, so what is read is always a complete document; a
    short settle covers the rename landing. Never talks to the mouse and
    never writes the file -- exchanging the reader and writer halves would
    be a feedback loop.
    """
    from . import hidraw

    path = _activity_path()
    if path is None:
        return
    directory = os.path.dirname(path)
    name = os.path.basename(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd = hidraw.inotify_init()
    except OSError:
        return

    try:
        hidraw.inotify_add_watch(
            fd, directory, hidraw.IN_CREATE | hidraw.IN_MOVED_TO
        )
        while True:
            names = hidraw.read_inotify_events(fd)
            if not any(n == name for n in names):
                continue
            time.sleep(0.03)
            try:
                with open(path, "rb") as handle:
                    doc = json.loads(handle.read(4096))
            except (OSError, ValueError):
                # A torn or alien file: ignore it. The next atomic rename is
                # a fresh, complete document again.
                continue
            if not isinstance(doc, dict):
                continue
            event = doc.get("event")
            if event not in ("set-applied", "refresh"):
                continue
            out = {"ok": True, "event": event}
            if event == "set-applied":
                if not isinstance(doc.get("field"), str):
                    continue
                out["field"] = doc["field"]
                out["value"] = doc.get("value")
            emit(out)
    except OSError:
        return
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# --- serve -------------------------------------------------------------------


def cmd_serve(args) -> int:
    """Long-lived transport for the panel: one process per widget instance.

    The panel writes one JSON request per line to this process's stdin --
    `{"id": 1, "cmd": "status"}`, `{"id": 2, "cmd": "set", "field": ...,
    "value": ...}`, `{"id": 3, "cmd": "battery"}`, `{"id": 4, "cmd":
    "probe-stages"}` -- and reads one JSON line back per request on stdout,
    carrying the same `id`. `{"cmd": "udev-rule"}` is the one request that
    skips the lock and the device entirely: it answers what rule file the
    machine needs, unreadable-device case included. watch-link events ride
    the same stream as `{"event": ...}` lines with no `id`, so the two are
    told apart by shape.

    The point is cost: a click is one exchange over pipes that are already
    open, instead of a Python interpreter launch, a profile detect and a
    link re-probe. Byte counters, watchdogs and backoff-restart live with
    the panel's supervisor (Service.qml), which still treats this process's
    death as something to restart, not to mourn.

    Two serializations stack, on purpose. Within the panel, requests are
    handled strictly in arrival order by this loop -- the QML side no longer
    has to be the serialization point. Across processes, the same `device_lock`
    fence the one-shot commands take is taken here per request, not held for
    the process lifetime: a serve process holds the panel alive for the whole
    session, and holding the fence that long would deadlock every CLI one-shot
    the user runs from a terminal while the panel happens to exist.

    A `changed` link event drops the cached session: node numbering under
    /dev can move on a replug, so the next request re-establishes path and
    link flag rather than trusting a stale path. DeviceBusy is the one
    exception that keeps the session -- another hskctl process merely held
    the fence; the path it guarded is still real. Stdin closing ends the
    process; nothing is written to the mouse on shutdown.
    """
    import threading

    try:
        profile = detect_profile(args.profile)
    except ProtocolError as exc:
        # One parseable line, then a nonzero exit: the panel's restart
        # machinery sees the exit and backs off, and the line explains why.
        sys.stdout.write(
            json.dumps({"ok": False, "state": "error", "error": str(exc)}) + "\n"
        )
        sys.stdout.flush()
        return 1

    out_lock = threading.Lock()

    def emit(payload: dict) -> None:
        line = json.dumps(payload, separators=(",", ":"))
        with out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    holder = {"session": None}

    # The link watcher runs on a daemon thread beside the request loop: it
    # only walks /dev and sleeps, never touching the mouse, which is what
    # makes running it here without extra locking safe.
    def watcher_emit(event: str, present: set[str]) -> None:
        emit({"ok": True, "event": event, "connected": bool(present)})
        if event == "changed":
            holder["session"] = None

    threading.Thread(
        target=_watch_link_loop, args=(profile, watcher_emit), daemon=True
    ).start()

    # A second watcher relays changes published by *other* hskctl processes
    # (the sibling bars' serve processes, a terminal `set`): the writer
    # already holds the authoritative readback, so the panel gets told the
    # value rather than asked to re-read the device.
    def activity_emit(payload: dict) -> None:
        emit(payload)

    threading.Thread(
        target=_watch_activity_loop, args=(activity_emit,), daemon=True
    ).start()

    # A serve session must not take the process-lifetime lock (see docstring),
    # so it is built with lock disabled and every request scopes its own.
    def get_session():
        if holder["session"] is None:
            holder["session"] = _session(args, profile, args.device, lock=False)
        return holder["session"]

    def handle(req: dict) -> dict:
        cmd = str(req.get("cmd") or "")
        if cmd == "udev-rule":
            # Pure sysfs/text work -- no device open, no fence. It must answer
            # even when the device is unreachable, because "here is the rule
            # you are missing" is exactly the permission-denied case.
            return _udev_payload()
        with device_lock():
            try:
                if cmd == "status":
                    return _status_payload(args, profile, get_session())
                if cmd == "battery":
                    return _battery_payload(args, profile, get_session())
                if cmd == "probe-stages":
                    return _probe_stages_payload(args, profile, get_session())
                if cmd == "set":
                    field = req.get("field")
                    if not isinstance(field, str):
                        return {"ok": False, "error": "set needs a field name"}
                    raw_value = req.get("value")
                    if isinstance(raw_value, (dict, list)) or raw_value is None:
                        return {"ok": False, "field": field,
                                "error": "set needs a scalar value"}
                    # Same string-coercion the argparse path applies -- the
                    # panel sends "1600", "true", "#ffa500", exactly the
                    # spellings its queue used to hand the CLI.
                    return _set_payload(
                        args, profile, field, _coerce(str(raw_value)), get_session()
                    )
            except (DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
                # A vanished node means the cached session might still name a
                # path something else owns; drop it so the next request
                # re-establishes. A fence timeout means the node is fine --
                # another hskctl process came and went.
                if not isinstance(exc, DeviceBusy):
                    holder["session"] = None
                return {"ok": False, "error": str(exc)}
            return {"ok": False, "error": f"unknown command {cmd!r}"}

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        if len(line) > 4096:
            emit({"ok": False, "error": "request line too long"})
            continue
        try:
            req = json.loads(line)
        except ValueError:
            emit({"ok": False, "error": "request is not valid JSON"})
            continue
        if not isinstance(req, dict):
            emit({"ok": False, "error": "request is not an object"})
            continue
        out = handle(req)
        if req.get("id") is not None:
            out["id"] = req["id"]
        emit(out)

    return 0


# --- get / set --------------------------------------------------------------


def cmd_get(args) -> int:
    try:
        profile = detect_profile(args.profile)
        session = _session(args, profile, args.device)
        if getattr(args, "verbose", False):
            session.trace = []
        value = session.get(args.field)
    except (NotDiscovered, DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return _fail(str(exc), args.json, field=args.field)
    if getattr(args, "verbose", False) and session.trace:
        _print_trace(session)
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


def _print_trace(session) -> None:
    """Dump every byte that crossed the wire, to stderr.

    Trailing zeros are trimmed rather than the packet being truncated to a
    fixed width: a DPI packet carries seven stages across 56 bytes, and cutting
    it at 32 hides the half that matters.
    """
    print(f"link: {'dongle' if session.wireless else 'wired'}", file=sys.stderr)
    for entry in session.trace:
        tokens = entry["data"].split()
        while len(tokens) > 16 and tokens[-1] == "00":
            tokens.pop()
        print(f"  {entry['step']}:", file=sys.stderr)
        for i in range(0, len(tokens), 16):
            print(f"    {i:>3}  {' '.join(tokens[i:i + 16])}", file=sys.stderr)


def _set_payload(args, profile, field: str, value: Any, session=None) -> dict:
    """Write one field, read it back, report both -- the readback is the
    authoritative value, so the panel needs no separate re-read.
    """
    try:
        if session is None:
            session = _session(args, profile, args.device)
        session.set(field, value)
        readback = session.get(field)
    except (NotDiscovered, DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return {
            "ok": False,
            "field": field,
            "requested": value,
            "error": str(exc),
        }

    ok = str(readback) == str(value)
    payload = {
        "ok": ok,
        "field": field,
        "requested": value,
        "value": readback,
        "link": "dongle" if session.wireless else "wired",
    }
    if not ok:
        # "reads back 0" on its own sends people hunting for a mapping bug. The
        # usual cause is a mouse that was asleep when the packet landed, which
        # looks identical unless the message says so.
        payload["error"] = (
            f"wrote {value!r} to {field} but the mouse reads back "
            f"{readback!r} (link: {payload['link']}). If that is 0 or blank the "
            f"mouse was probably asleep -- move it and try again. If it is a "
            f"different real value, the mapping in the profile is wrong."
        )
    else:
        # Other panels holding this mouse show the old value until they are
        # told. The readback above is the truth; publish it.
        _notify_activity({"event": "set-applied", "field": field, "value": readback})
    return payload


def cmd_set(args) -> int:
    value = _coerce(args.value)
    try:
        profile = detect_profile(args.profile)
        if getattr(args, "raw", False):
            # The wire value can be handed over directly (calibration work):
            # bypass the friendly-value table, write the number the mouse
            # expects, read the raw number back. Not reachable through the
            # panel, deliberately -- it is for humans profiling hardware.
            session = _session(args, profile, args.device)
            if getattr(args, "verbose", False):
                session.trace = []
            session.set_raw(args.field, int(args.value))
            value = int(args.value)
            readback = session.get_raw(args.field)
            payload = {
                "ok": str(readback) == str(value),
                "field": args.field,
                "requested": value,
                "value": readback,
                "link": "dongle" if session.wireless else "wired",
            }
            if not payload["ok"]:
                payload["error"] = (
                    f"wrote raw {value} to {args.field} but the mouse reads "
                    f"back {readback}"
                )
        elif not getattr(args, "verbose", False):
            payload = _set_payload(args, profile, args.field, value)
        else:
            session = _session(args, profile, args.device)
            session.trace = []
            payload = _set_payload(args, profile, args.field, value, session=session)
    except (NotDiscovered, DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return _fail(str(exc), args.json, field=args.field, requested=value)

    if getattr(args, "verbose", False) and session.trace:
        _print_trace(session)

    return _emit(payload, args.json, lambda p: print(f"{p['field']} = {p.get('value', p.get('error'))}"))


# --- fields -----------------------------------------------------------------


def _field_label(profile, name: str) -> str:
    """The name shown to a user for a field: the profile says first.

    The profile's `ui.label` travels with the bytes it names, so a profile
    authoring a mouse with a sensor setting hskctl has never heard of can
    call it what the vendor calls it. FRIENDLY_LABELS remains the fallback
    for profiles written before this existed and for a label the validation
    dropped.
    """
    meta = profile.field_ui(name)
    if meta and isinstance(meta.get("label"), str) and meta["label"]:
        return meta["label"]
    return FRIENDLY_LABELS.get(name, name)


def cmd_fields(args) -> int:
    try:
        profile = detect_profile(args.profile)
    except ProtocolError as exc:
        return _fail(str(exc), args.json)

    rows = []
    for name, spec in profile.data.get("fields", {}).items():
        if name.startswith("_") or not isinstance(spec, dict):
            continue
        meta = profile.field_ui(name) or {}
        rows.append(
            {
                "field": name,
                "label": _field_label(profile, name),
                "description": meta.get("description", ""),
                "widget": meta.get("widget", "info"),
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


def cmd_probe_write(args) -> int:
    """Write one field, then report what moved *anywhere* in its block.

    `set` reads back the one field it was asked about, which answers "did my
    change take" and nothing else. When a write is refused that is the wrong
    question -- the useful one is whether the block changed at all, and where.
    A value that lands on the neighbouring stage, a block the firmware accepts
    and then rewrites, and a packet it drops on the floor all look identical
    through a single field, and completely different through the whole block.

    Read, write, read. Nothing is retried and nothing is restored.
    """
    try:
        profile = detect_profile(args.profile)
        session = _session(args, profile, args.device)
        command = profile.field_command(args.field)
        before = bytes(session._read(command))
    except (NotDiscovered, DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return _fail(str(exc), args.json, field=args.field)

    value = _coerce(args.value)
    write_error = None
    try:
        session.set(args.field, value)
    except (NotDiscovered, HidrawError, ProtocolError, OSError) as exc:
        write_error = str(exc)

    try:
        after = bytes(session._read(command))
    except (HidrawError, ProtocolError, OSError) as exc:
        return _fail(f"could not read {command!r} back: {exc}", args.json, field=args.field)

    # Every field carried by the same command, decoded on both sides. This is
    # profile-driven rather than DPI-specific: whatever the block holds, the
    # comparison covers it.
    siblings = []
    for name, spec in (profile.data.get("fields") or {}).items():
        if (spec.get("command") or spec.get("from")) != command:
            continue
        try:
            was, now = profile.decode(name, before), profile.decode(name, after)
        except (NotDiscovered, ProtocolError, IndexError, ValueError):
            continue
        siblings.append({"field": name, "before": was, "after": now,
                         "changed": was != now})

    width = max(len(before), len(after))
    moved = [i for i in range(width)
             if (before[i] if i < len(before) else None)
             != (after[i] if i < len(after) else None)]

    # Nothing is restored (that is the point of a probe), so the write stuck:
    # tell every panel to re-read rather than drift on a stale rendering.
    if write_error is None:
        _notify_activity({"event": "refresh"})

    payload = {
        "ok": write_error is None,
        "command": command,
        "field": args.field,
        "requested": value,
        "writeError": write_error,
        "before": before.hex(" "),
        "after": after.hex(" "),
        "bytesChanged": moved,
        "fields": siblings,
    }

    def human(p):
        if p["writeError"]:
            print(f"the write itself failed: {p['writeError']}")
        print(f"command {p['command']}, asked for {p['field']} = {p['requested']!r}")
        print()
        for label, blob in (("before", p["before"]), ("after ", p["after"])):
            tokens = blob.split()
            while len(tokens) > 16 and tokens[-1] == "00":
                tokens.pop()
            for i in range(0, len(tokens), 16):
                print(f"  {label if i == 0 else '      '} {i:>3}  "
                      f"{' '.join(tokens[i:i + 16])}")
        print()
        if not p["bytesChanged"]:
            print("  no byte in the block changed -- the mouse dropped the write")
        else:
            print(f"  bytes that moved: {p['bytesChanged']}")
        print()
        for s in p["fields"]:
            mark = "*" if s["changed"] else " "
            print(f"  {mark} {s['field']:<20} {s['before']!r} -> {s['after']!r}")

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
        profile = detect_profile(args.profile)
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

    # Which candidate is *alive* is not something the descriptors can answer.
    # A dongle whose mouse has moved onto the cable looks identical to one
    # whose mouse is awake, and it acknowledges every command while returning
    # nothing -- so sweep them and say which ones actually reply with data.
    report["liveness"] = []
    if not args.device:
        for candidate in candidates:
            entry = {"path": candidate.info.path, "vidpid": candidate.info.vidpid,
                     "wired": False, "dongle": False, "error": None}
            try:
                probe = _session(args, profile, candidate.info.path)
                for flag, key in ((False, "wired"), (True, "dongle")):
                    entry[key] = probe.probe_link(flag)
            except (DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
                entry["error"] = str(exc)
            report["liveness"].append(entry)

        alive = [e for e in report["liveness"] if e["wired"] or e["dongle"]]
        if alive:
            path = alive[0]["path"]
        else:
            path = candidates[0].info.path
    else:
        path = args.device
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
        session = _session(args, profile, path)
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
            # Not a warning -- this is fatal. Feature-report ioctls need the
            # node opened read-write, so without it nothing below will work,
            # including the reads.
            print("  !! NOT WRITABLE -- nothing can talk to the mouse.")
            print("     Every exchange is a HID feature report, and those need")
            print("     O_RDWR even to read. Install the udev rule:")
            print("       ./install.sh --udev")
            print("     then unplug and replug the mouse or its dongle.")
        if p.get("liveness"):
            print()
            print("which candidates actually answer with data:")
            for entry in p["liveness"]:
                if entry["error"]:
                    state = f"ERROR {entry['error'].splitlines()[0]}"
                elif entry["wired"] or entry["dongle"]:
                    flags = [n for n in ("wired", "dongle") if entry[n]]
                    state = "ALIVE on " + " and ".join(flags)
                else:
                    state = "acknowledges but returns no data"
                print(f"  {entry['path']:<16} {entry['vidpid']}  {state}")
            if not any(e["wired"] or e["dongle"] for e in p["liveness"]):
                print("  !! nothing answered. The mouse is asleep, or on a cable")
                print("     that carries power but not data.")
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


def cmd_watch_battery(args) -> int:
    """Sample the battery over time, so the reading can be judged rather than guessed.

    A single number tells you nothing about whether it is right. What tells you
    is whether it *moves sensibly*: monotonically down while in use, up on the
    cable, and in steps the hardware plausibly reports. This prints one line per
    sample and never writes to the mouse.

    Both the raw byte and the vendor's rendering of it are shown, because the
    Windows app does not display the byte directly and comparing against it is
    the obvious sanity check.
    """
    import datetime
    import time as _time

    try:
        profile = detect_profile(args.profile)
    except ProtocolError as exc:
        return _fail(str(exc), args.json)

    samples: list[dict] = []
    started = _time.monotonic()
    deadline = None if args.duration <= 0 else _time.monotonic() + args.duration

    if not args.json:
        print(f"Sampling every {args.interval}s. Ctrl-C to stop.\n")
        header = f"  {'time':<10} {'raw':>4} {'vendor':>7}  charging"
        if args.raw:
            header += "  reply bytes"
        print(header)

    try:
        while True:
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            entry: dict = {"time": stamp,
                           "elapsed": round(_time.monotonic() - started, 1)}
            try:
                session = _session(args, profile, args.device)
                # The whole reply, not just the byte we decode. "Are we even
                # watching the right byte?" is a fair question and this answers
                # it with data: the battery reply declares two payload bytes,
                # so if the percentage is stuck while something else moves,
                # that shows up here rather than in an argument.
                reply = session._read("battery")
                raw = reply[6] if len(reply) > 6 else 0
                charging = bool(reply[5]) if len(reply) > 5 else False
                entry.update({"raw": raw, "charging": charging,
                              "vendor": _vendor_battery(raw, charging),
                              "reply": bytes(reply[:10]).hex(" ")})
            except (DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
                entry["error"] = str(exc)
            samples.append(entry)

            if not args.json:
                if "error" in entry:
                    print(f"  {stamp:<10} {'--':>4} {'--':>7}  {entry['error']}")
                else:
                    line = (f"  {stamp:<10} {entry['raw']:>4} {entry['vendor']:>6}% "
                            f"  {'yes' if entry['charging'] else 'no':<4}")
                    if args.raw:
                        line += f"  {entry['reply']}"
                    print(line)

            if deadline is not None and _time.monotonic() >= deadline:
                break
            _time.sleep(args.interval)
    except KeyboardInterrupt:
        if not args.json:
            print()

    payload = {"ok": True, "samples": samples}
    if args.json:
        json.dump(payload, sys.stdout)
        sys.stdout.write("\n")
        return 0

    readings = [s["raw"] for s in samples if "raw" in s]
    if len(readings) < 2:
        return 0

    print(f"\n  {len(readings)} readings, {min(readings)}..{max(readings)}, "
          f"{len(set(readings))} distinct")

    # Which byte of the reply actually moved. If the percentage is pinned but
    # another byte is varying, we are decoding the wrong offset -- and that is
    # worth knowing without having to take anyone's word for it.
    replies = [bytes.fromhex(s["reply"]) for s in samples if "reply" in s]
    if len(replies) >= 2:
        width = min(len(r) for r in replies)
        moved = [i for i in range(width) if len({r[i] for r in replies}) > 1]
        if moved:
            print("  bytes that varied: " + ", ".join(
                f"rx[{i}] {min(r[i] for r in replies)}..{max(r[i] for r in replies)}"
                for i in moved))
        else:
            print("  no byte of the reply changed at all")

    # Whether it moved is the easy question. Whether it moved at a *plausible
    # rate* is the one that would catch a bad reading, and that needs the clock.
    timed = [s for s in samples if "raw" in s]
    hours = (timed[-1]["elapsed"] - timed[0]["elapsed"]) / 3600.0 if timed else 0.0
    drop = timed[0]["raw"] - timed[-1]["raw"] if timed else 0

    if len(set(readings)) > 1 and hours > 0 and drop > 0:
        rate = drop / hours
        print(f"  fell {drop} points over {hours:.1f}h -> {rate:.1f} %/hour")
        print(f"  at that rate a full charge lasts about {100 / rate:.0f} hours")
        print("  Sanity-check that against the rated life. An order of magnitude")
        print("  out either way is worth investigating.")
    elif len(set(readings)) == 1:
        print()
        if any(s.get("charging") for s in samples):
            print("  It was charging during this run, so a steady reading says")
            print("  nothing about discharge.")
        print(f"  Held at {readings[0]} for {hours:.1f}h. Near a full charge that")
        print("  is expected -- the byte has 1% resolution and lithium cells sit")
        print("  at the top of their range for a long time. An idle mouse draws")
        print("  far less than one in use, too.")
        print()
        print("  To make this test able to fail: run it off the cable, while")
        print("  using the mouse, starting from below full.")
    return 0


def _vendor_battery(raw: int, charging: bool) -> int:
    """What the Windows app would display for this raw byte.

    Read out of `get_Battery_empty`. It branches on product id: the 5407/5408,
    5707/5708, 5807/5808 and 5907/5908 family -- which includes the HSK Pro 4K
    -- takes the simple path, where the byte really is a percentage and the app
    merely rounds it down to a multiple of 5. The other family runs it through
    a four-segment piecewise curve instead, so do not assume this holds for a
    mouse you have not checked.

    The one oddity is at the top: a full battery reads 100, but the app shows
    95 while charging, presumably so it does not sit at 100% for an hour.
    """
    if raw == 100:
        return 95 if charging else 100
    return (raw // 5) * 5


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
        profile = detect_profile(args.profile)
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
        profile = detect_profile(args.profile)
        session = _session(args, profile, args.device)
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
        profile = detect_profile(args.profile)
        session = _session(args, profile, args.device)
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

    # Other panels display the repaired block; tell them to re-read it (the
    # repair touches several fields; one event is cheaper than several).
    if changes:
        _notify_activity({"event": "refresh"})

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

# Cap the settings file at 64 KiB, which is more than three orders of magnitude
# above what `save` actually writes (~500 bytes). The reviewer's rule is that a
# size ceiling belongs at the producer boundary, and this is that boundary for
# `apply`.
_MAX_SETTINGS_BYTES = 65536


def _open_state_dir(path: str, private: bool) -> int:
    """Open the parent of `path` as a held directory descriptor.

    For hskctl's own directory (`private`), create it 0700 if missing, refuse
    it if the name is a symlink or not ours, and set it to 0700 on *every*
    run through the descriptor -- not only when it looks wider, and not by
    pathname. Its contents need no repair: the settings file inside is always
    replaced by a fresh 0600 inode (see _atomic_write_json), and the
    directory is shared with user profiles, so nothing in it is deleted.

    For a directory the user named with --file it is only opened and checked
    for being a real directory: it is theirs, and hskctl has no business
    changing its mode.
    """
    parent = os.path.dirname(path) or "."
    if private:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    try:
        dir_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise OSError(f"{parent} is not a directory hskctl can use: {exc.strerror}") from exc
    try:
        if private:
            st = os.fstat(dir_fd)
            if st.st_uid != os.geteuid():
                raise OSError(f"{parent} is owned by uid {st.st_uid}, not you")
            os.fchmod(dir_fd, 0o700)
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


def _atomic_write_json(path: str, payload: dict, private: bool = True) -> None:
    """Write JSON to `path` atomically, never writing through a symlink.

    A fresh, randomly named 0600 file is created O_EXCL|O_NOFOLLOW relative to
    the held directory descriptor, written and fsynced, then renamed over the
    target relative to the same descriptor -- rename(2) replaces a symlink at
    the destination rather than following it -- and the directory is fsynced.
    """
    encoded = json.dumps(payload, indent=2).encode("utf-8") + b"\n"
    if len(encoded) > _MAX_SETTINGS_BYTES:
        raise OSError(
            f"settings serialise to {len(encoded)} bytes, over the {_MAX_SETTINGS_BYTES} cap"
        )
    name = os.path.basename(path)
    if not name or name in (".", ".."):
        raise OSError(f"{path} does not name a file")
    dir_fd = _open_state_dir(path, private)
    try:
        tmp = f".hskctl-settings-{secrets.token_hex(8)}.tmp"
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dir_fd,
        )
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(encoded)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.fsync(dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def _read_bounded_json(path: str) -> dict:
    """The settings file, through one validated O_NOFOLLOW descriptor, capped."""
    return read_bounded_json(path, _MAX_SETTINGS_BYTES)


def cmd_save(args) -> int:
    """Record the mouse's current writable settings to disk.

    A manual backup, not a sync mechanism. Settings live in the mouse's own
    storage and survive a power cycle, so nothing needs to restore them -- but
    a snapshot is worth having before you experiment, and `apply` puts it back.

    This was once wired to a systemd unit that re-applied it on every
    reconnect. That was written when writes were not persisting, and once they
    were it became a machine for silently undoing your changes. It is gone. If
    you want these values back on the mouse, ask for them.
    """
    try:
        profile = detect_profile(args.profile)
        session = _session(args, profile, args.device)
        settings = session.read_all()
    except (DeviceBusy, DeviceNotFound, HidrawError, ProtocolError, OSError) as exc:
        return _fail(str(exc), args.json)

    keep = {k: v for k, v in settings.items() if profile.field_writable(k)}
    path = args.file or SETTINGS_PATH
    try:
        _atomic_write_json(
            path, {"model": profile.model, "settings": keep}, private=not args.file
        )
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
        saved_doc = _read_bounded_json(path)
    except FileNotFoundError:
        return _fail(f"no saved settings at {path}", args.json, path=path)
    except OSError as exc:
        return _fail(f"could not read {path}: {exc}", args.json, path=path)
    except ValueError as exc:
        return _fail(f"{path} is not valid JSON: {exc}", args.json, path=path)
    if not isinstance(saved_doc, dict):
        return _fail(f"{path} is not a JSON object", args.json, path=path)
    saved = saved_doc.get("settings") or {}
    if not isinstance(saved, dict):
        return _fail(f"{path} has no valid 'settings' object", args.json, path=path)

    try:
        profile = detect_profile(args.profile)
        session = _session(args, profile, args.device)
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

    # Every field written changed a value some panel may be showing.
    if applied:
        _notify_activity({"event": "refresh"})

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


# --- udev rule ---------------------------------------------------------------


# Single source of truth for the udev rule file. install.sh used to carry this
# text as a heredoc; generating it in the engine means the actually connected
# hardware and per VID:PID lines for user-authored profiles land in the same
# file the panel's pkexec helper writes, with one place to review the wording.
# install.sh keeps a heredoc fallback for a machine where the engine will not
# run, and validates every generated line before anything reaches root.

UDEV_RULE_FILE = "/etc/udev/rules.d/60-mousectrl.rules"

_USER_PROFILE_DIR = os.path.expanduser("~/.config/hskctl/profiles")

_UDEV_HEADER = """\
# Mouse CTRL plugin -- hidraw access for every supported mouse vendor.
#
# Configuring the mouse means sending HID *feature* reports, and the hidraw
# ioctls for those (HIDIOCSFEATURE / HIDIOCGFEATURE) require the device node to
# be opened read-write. The default mode on /dev/hidraw* is root-only, so
# without this rule hskctl and the Omarchy panel cannot talk to the mouse at
# all -- not even to read the battery.
#
# uaccess hands read-write to whoever is logged in at the seat, which is the
# same mechanism your sound card and webcam use. Each line below is scoped to
# one vendor id, or one vendor+product pair -- adding a mouse means adding a
# line here, never widening one to match everything.
"""

# Reading order for vendor ids that have no label of their own, and fallback
# labels. Kept in step with the list install.sh carried before this command
# absorbed it.
VENDOR_RULE_LABELS = {
    "33e4": "G-Wolves (HSK Pro 4K and siblings)",
    "3710": "Pulsar (wired models, and the X2A/Feinmann wireless dongles)",
    "3554": "Pulsar (Nordic wireless dongle -- X2A/X2 V2 Mini family, X2H mini until confirmed)",
}

# Bounds on generated output: list_profiles is already capped, this keeps a
# pathological user profile from making the rule file absurd.
_MAX_RULES = 256
_MAX_TEXT_BYTES = 65536


def _hex4(value) -> str | None:
    """Normalize any spelling of a 16-bit USB id to four lowercase hex digits."""
    try:
        number = int(str(value).strip(), 16)
    except ValueError:
        return None
    if not 0 <= number <= 0xFFFF:
        return None
    return f"{number:04x}"


def _vendor_rule_line(vid: str) -> str:
    return f'KERNEL=="hidraw*", ATTRS{{idVendor}}=="{vid}", MODE="0660", TAG+="uaccess"'


def _vidpid_rule_line(vid: str, pid: str) -> str:
    return (
        f'KERNEL=="hidraw*", ATTRS{{idVendor}}=="{vid}", '
        f'ATTRS{{idProduct}}=="{pid}", MODE="0660", TAG+="uaccess"'
    )


def _profile_vendor_products(profile) -> list[tuple[str, str | None]]:
    """(vendor, product) pairs a profile's match block declares.

    product is None when the block names vendors but no products -- then a
    vendor-scoped line is the only scope the profile actually claims.
    """
    match = profile.data.get("match") or {}
    vendors = [v for v in (_hex4(x) for x in (match.get("vendorIds") or [])) if v]
    products = [p for p in (_hex4(x) for x in (match.get("productIds") or [])) if p]
    if not vendors:
        return []
    if not products:
        return [(vendor, None) for vendor in vendors]
    return [(vendor, product) for vendor in vendors for product in products]


def _installed_udev_rules() -> set[str]:
    """The rule lines currently in /etc, read permission-free (rules are 0644).

    Comparing content against the generated text is what lets the panel tell
    "no rule at all" from "a rule from an older version that predates these
    grants" -- the second is the common case after a profile grows a new
    identity, and its fix (rewrite in place) is the same button.
    """
    try:
        with open(UDEV_RULE_FILE, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(65536)
    except OSError:
        return set()
    lines = set()
    for line in data.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            lines.add(line)
    return lines


def _udev_payload() -> dict:
    """Build the whole udev rule file this machine needs, as data and as text.

    Three kinds of line, most specific last:
    - vendor lines: every vendor a *bundled* profile declares, exactly the
      whole-vendor grants install.sh always wrote (superset safety -- an old
      rule file never grants less than before);
    - profile lines: one VID:PID pair per line for *user-authored* profiles,
      scoped tight, tagged with the profile that claims it;
    - device lines: every hidraw node on the machine whose ids match any known
      profile, read permission-free from sysfs. A device already covered by a
      vendor line is listed but not duplicated in the text.
    """
    vendor_grants: dict[str, str] = {}
    known_vendors: set[str] = set()

    # Seed with the exact list of vendors install.sh has always granted
    # whole-vendor access for. A bundled profile no longer declaring one of
    # them (or declaring one not shipped when that list was written, like the
    # pulsar profile's Cfg.ini alternate ids) must not silently narrow or
    # miss a grant on regenerating this file.
    vendor_grants.update(VENDOR_RULE_LABELS)
    known_vendors.update(VENDOR_RULE_LABELS)

    for name in list_profiles():
        try:
            profile = load_profile(name)
        except ProtocolError:
            continue
        pairs = _profile_vendor_products(profile)
        bundled = os.path.dirname(profile.path) != _USER_PROFILE_DIR
        for vendor, _product in pairs:
            known_vendors.add(vendor)
            if not bundled or vendor in vendor_grants:
                continue
            vendor_grants[vendor] = VENDOR_RULE_LABELS.get(
                vendor, f"vendor {vendor} (from profile {profile.name} -- {profile.model})"
            )

    device_rules: list[dict] = []
    pair_lines: dict[str, dict] = {}
    for device in sysfs_hid_devices():
        if device["vid"] not in known_vendors:
            continue
        covered = device["vid"] in vendor_grants
        device_rules.append({
            "kind": "device",
            "node": device["node"],
            "vid": device["vid"],
            "pid": device["pid"],
            "line": _vidpid_rule_line(device["vid"], device["pid"]),
            "coveredByVendor": covered,
            "usb": device["bus"] == "0003",
        })

    for name in list_profiles():
        try:
            profile = load_profile(name)
        except ProtocolError:
            continue
        if os.path.dirname(profile.path) != _USER_PROFILE_DIR:
            continue
        for vendor, product in _profile_vendor_products(profile):
            if product is None:
                line = _vendor_rule_line(vendor)
            else:
                line = _vidpid_rule_line(vendor, product)
            if line not in pair_lines:
                pair_lines[line] = {
                    "kind": "profile",
                    "vid": vendor,
                    "pid": product or "",
                    "profile": profile.name,
                    "model": profile.model,
                    "line": line,
                }

    profile_only = list(pair_lines.values())
    known_lines = set(pair_lines)
    vendor_rules = [
        {"kind": "vendor", "vid": vid, "label": label, "line": _vendor_rule_line(vid)}
        for vid, label in sorted(vendor_grants.items())
    ]
    known_lines.update(r["line"] for r in vendor_rules)
    rules = (vendor_rules + profile_only + device_rules)[:_MAX_RULES]
    dropped = set()
    for rule in device_rules:
        if rule["line"] in known_lines:
            dropped.add(rule["line"])
    if dropped:
        device_rules = [r for r in device_rules if r["line"] not in dropped]
        rules = (vendor_rules + profile_only + device_rules)[:_MAX_RULES]

    sections = [_UDEV_HEADER]
    for rule in vendor_rules:
        sections.append(f"# {rule['label']}\n{rule['line']}\n")
    if profile_only:
        sections.append(
            "# Per-device lines from user-authored profiles (~/.config/hskctl/profiles)\n"
        )
        for rule in profile_only:
            sections.append(f"# {rule['model']} ({rule['profile']})\n{rule['line']}\n")
    uncovered = [r for r in device_rules if not r["coveredByVendor"]]
    if uncovered:
        sections.append(
            "# Devices on this machine whose ids match a known profile and are\n"
            "# not already granted by a vendor line above\n"
        )
        for rule in uncovered:
            sections.append(f"# {rule['node']}: {rule['vid']}:{rule['pid']}\n")
            if not rule["usb"]:
                sections.append(
                    "# Non-USB HID: an ATTRS{idVendor} rule may not match this node\n"
                )
            sections.append(f"{rule['line']}\n")
    text = "".join(sections)
    if len(text.encode()) > _MAX_TEXT_BYTES:
        text = text[:_MAX_TEXT_BYTES]

    installed = _installed_udev_rules()
    # The comparison is against the text itself, not the `rules` metadata:
    # device lines covered by a vendor line are listed as data but never
    # written to the file, so comparing the metadata would mark a perfectly
    # current file outdated forever.
    text_lines = {
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    up_to_date = bool(installed) and text_lines <= installed

    return {
        "ok": True,
        "ruleFile": UDEV_RULE_FILE,
        "rules": rules,
        "text": text,
        # present=false: no rule file at all. present+not upToDate: an older
        # file rewritten by a previous version -- still a superset of its own
        # grants, but missing whatever this version would add. Either way the
        # fix is the same button; the two states only change the wording.
        "installed": {"present": bool(installed), "upToDate": up_to_date},
    }


def cmd_udev_rule(args) -> int:
    payload = _udev_payload()
    return _emit(payload, args.json, lambda p: sys.stdout.write(p["text"]))


# --- entry point ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hskctl",
        description="Configure a supported G-Wolves or Pulsar mouse from Linux.",
    )
    parser.add_argument("--version", action="version", version=f"hskctl {__version__}")
    parser.add_argument(
        "--force-unmatched",
        action="store_true",
        help="allow writes to a --device that does not match the profile "
             "(for profiling new hardware; can brick the wrong device)",
    )
    parser.add_argument("--profile", help="device profile name", default=None)
    parser.add_argument("--device", help="force a specific /dev/hidrawN", default=None)
    parser.add_argument("--json", action="store_true", help="machine-readable output")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="find and rank candidate HID endpoints").set_defaults(
        func=cmd_probe
    )
    sub.add_parser("status", help="read every mapped setting").set_defaults(func=cmd_status)
    sub.add_parser(
        "probe-stages",
        help="verify that switching DPI stages works (one staged write, cached)",
    ).set_defaults(func=cmd_probe_stages)
    sub.add_parser(
        "battery", help="read only battery percent and charging (one exchange)"
    ).set_defaults(func=cmd_battery)
    sub.add_parser(
        "watch-link",
        help="print a line whenever the cable/dongle is plugged or unplugged "
             "(runs until killed; never talks to the mouse)",
    ).set_defaults(func=cmd_watch_link)
    sub.add_parser(
        "serve",
        help="panel transport: JSON requests on stdin, replies on stdout "
             "(one process per widget; not for interactive use)",
    ).set_defaults(func=cmd_serve)
    sub.add_parser("fields", help="show which settings are mapped").set_defaults(
        func=cmd_fields
    )
    sub.add_parser("profiles", help="list available device profiles").set_defaults(
        func=cmd_profiles
    )
    sub.add_parser(
        "udev-rule",
        help="print the udev rule file this machine needs "
             "(reads /sys, never the device, needs no root)",
    ).set_defaults(func=cmd_udev_rule)
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

    watch = sub.add_parser(
        "watch-battery",
        help="sample the battery over time (read-only) to judge the reading",
    )
    watch.add_argument("--interval", type=float, default=60.0,
                       help="seconds between samples (default 60)")
    watch.add_argument("--duration", type=float, default=0.0,
                       help="stop after this many seconds (default: until Ctrl-C)")
    watch.add_argument("--raw", action="store_true",
                       help="show the raw bytes of each battery reply")
    watch.set_defaults(func=cmd_watch_battery)

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
    get_p.add_argument(
        "--verbose",
        action="store_true",
        help="dump the bytes of the exchange",
    )
    get_p.set_defaults(func=cmd_get)

    pw = sub.add_parser(
        "probe-write",
        help="write one field and report every byte that moved in its block",
    )
    pw.add_argument("field")
    pw.add_argument("value")
    pw.set_defaults(func=cmd_probe_write)

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
