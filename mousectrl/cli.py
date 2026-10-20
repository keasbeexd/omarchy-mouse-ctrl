"""Command line: thin wrappers over api.Api, plus `serve` for the panel."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .api import Api
from .errors import MouseCtrlError


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2))


def _print_devices(payload: dict) -> None:
    if not payload["devices"]:
        print("No mice found.")
    for dev in payload["devices"]:
        family = dev["family"]["title"] if dev["family"] else "no supported family"
        profile = f"profile {dev['profile']['id']}" if dev["profile"] else "no profile"
        access = "" if dev["accessible"] else ", no access (udev rule needed)"
        print(f"{dev['name']}\n  key: {dev['key']}\n  {family}, {profile}{access}")
    for problem in payload["profileProblems"]:
        print(f"warning: {problem}", file=sys.stderr)


def _parse_value(text: str):
    """Values are JSON (true, 4000, {...}); a bare word is taken as a string."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mousectrl", description="Configure supported mice from Linux.")
    parser.add_argument("--version", action="version", version=f"mousectrl {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    devices = sub.add_parser("devices", help="list connected mice")
    devices.add_argument("--json", action="store_true")

    caps = sub.add_parser("caps", help="what a mouse supports")
    caps.add_argument("device", help="device key or vid:pid")

    read = sub.add_parser("read", help="read one capability, or all")
    read.add_argument("device")
    read.add_argument("capability", nargs="?")

    write = sub.add_parser("write", help="write a capability unlocked by the wizard")
    write.add_argument("device")
    write.add_argument("capability")
    write.add_argument("value", help="JSON value, e.g. true, 4000 or '{\"active\": 1, ...}'")

    test = sub.add_parser("test-write", help="write a nearby value, read it back, restore the original")
    test.add_argument("device")
    test.add_argument("capability")

    rule = sub.add_parser("udev-rule", help="print the udev rule file this machine needs")
    rule.add_argument("--json", action="store_true")
    rule.add_argument("--extra", action="append", default=[], metavar="VID:PID",
                      help="also grant this unsupported device")

    sub.add_parser("serve", help="JSON-lines backend for the panel (stdin/stdout)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        from .server import serve
        return serve()
    api = Api()
    try:
        if args.command == "devices":
            payload = api.devices()
            _print_json(payload) if args.json else _print_devices(payload)
        elif args.command == "caps":
            _print_json(api.capabilities(args.device))
        elif args.command == "read":
            _print_json(api.read(args.device, args.capability))
        elif args.command == "write":
            _print_json(api.write(args.device, args.capability, _parse_value(args.value)))
        elif args.command == "test-write":
            _print_json(api.test_write(args.device, args.capability))
        elif args.command == "udev-rule":
            payload = api.udev_rule(args.extra)
            _print_json(payload) if args.json else sys.stdout.write(payload["text"])
    except MouseCtrlError as exc:
        print(f"mousectrl: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"mousectrl: I/O error talking to the mouse: {exc}", file=sys.stderr)
        return 1
    finally:
        api.close()
    return 0
