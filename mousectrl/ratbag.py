"""Read-only view of mice that libratbag's daemon (ratbagd) manages.

Mouse CTRL never writes through ratbagd: its settings stay with Piper, the
libratbag GUI. What is read here is shown so a Logitech or SteelSeries owner
sees their stages and polling rate in the panel. A wrong reading can only
show a wrong number.

D-Bus API version 2 (libratbag master, ratbagd/*.c), read with
`busctl --json` because the standard library has no D-Bus client. ratbagd
asks clients to match its API version exactly, so any other version is
treated as absent.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from typing import Any, Callable

from .fingerprint import clean_text

BUSCTL = "/usr/bin/busctl"
SERVICE = "org.freedesktop.ratbag1"
SERVICE_FILE = "/usr/share/dbus-1/system-services/org.freedesktop.ratbag1.service"
MANAGER_PATH = "/org/freedesktop/ratbag1"
API_VERSION = 2
PIPER = "/usr/bin/piper"
_IFACE = "org.freedesktop.ratbag1."
_TIMEOUT = 3.0
_MAX_OUTPUT = 65536
_MAX_PROFILES = 16
_MAX_RESOLUTIONS = 16

# Runs busctl with these arguments and returns its stdout; injectable so
# tests can stand in for the daemon.
Runner = Callable[[list[str]], str]


class RatbagError(Exception):
    pass


def _busctl(args: list[str]) -> str:
    """busctl's stdout, capped while it is read and bounded in time.

    Run in its own session so a deadline or an overflow kills the whole
    group; stderr is discarded rather than collected unbounded.
    """
    try:
        proc = subprocess.Popen(
            [BUSCTL, "--system", "--json=short", *args],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, start_new_session=True,
        )
    except OSError as exc:
        raise RatbagError(f"busctl failed: {exc}") from exc
    deadline = time.monotonic() + _TIMEOUT
    out = b""
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise RatbagError("busctl did not finish in time")
            if not select.select([proc.stdout], [], [], left)[0]:
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            out += chunk
            if len(out) > _MAX_OUTPUT:
                raise RatbagError(f"busctl output exceeded {_MAX_OUTPUT} bytes")
        code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        raise RatbagError("busctl did not exit in time") from exc
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        proc.stdout.close()
    if code != 0:
        raise RatbagError(f"busctl exited {code}")
    return out.decode("utf-8", "replace")


def _unwrap(node: Any) -> Any:
    """busctl wraps values as {"type", "data"}, variants one level deeper."""
    while isinstance(node, dict) and "data" in node:
        node = node["data"]
        if isinstance(node, list) and len(node) == 1 and isinstance(node[0], dict) and "data" in node[0]:
            node = node[0]
    return node


class Ratbag:
    def __init__(self, run: Runner = _busctl, service_file: str = SERVICE_FILE):
        self._run = run
        self._service_file = service_file

    def _get(self, path: str, iface: str, *names: str) -> list[Any]:
        out = self._run(["get-property", SERVICE, path, _IFACE + iface, *names])
        lines = [line for line in out.splitlines() if line.strip()]
        if len(lines) != len(names):
            raise RatbagError(f"expected {len(names)} values from {path}, got {len(lines)}")
        try:
            return [_unwrap(json.loads(line)) for line in lines]
        except json.JSONDecodeError as exc:
            raise RatbagError(f"unreadable busctl output for {path}") from exc

    def available(self) -> bool:
        """Installed and speaking the API version this module was written for."""
        if not os.path.exists(self._service_file):
            return False
        try:
            (version,) = self._get(MANAGER_PATH, "Manager", "APIVersion")
        except RatbagError:
            return False
        return version == API_VERSION

    def device_path(self, hidraw_names: set[str]) -> str | None:
        """ratbagd's object for one of these hidraw nodes, if it manages any.

        Device paths end in the hidraw sysname they were created for.
        """
        (paths,) = self._get(MANAGER_PATH, "Manager", "Devices")
        if not isinstance(paths, list):
            raise RatbagError("Devices is not a list")
        for path in paths:
            if isinstance(path, str) and path.rsplit("/", 1)[-1] in hidraw_names:
                return path
        return None

    def read(self, device_path: str) -> dict:
        """Name, firmware, and the active profile's polling rate and stages."""
        name, firmware, profiles = self._get(device_path, "Device", "Name", "FirmwareVersion", "Profiles")
        if not isinstance(profiles, list):
            raise RatbagError("Profiles is not a list")
        for index, profile in enumerate(profiles[:_MAX_PROFILES]):
            active, disabled = self._get(profile, "Profile", "IsActive", "Disabled")
            if active is True and disabled is not True:
                rate, resolutions = self._get(profile, "Profile", "ReportRate", "Resolutions")
                return {
                    "name": clean_text(str(name), 80),
                    "firmware": clean_text(str(firmware), 40),
                    "profile": index + 1,
                    "profiles": len(profiles),
                    "pollingRate": rate if isinstance(rate, int) else None,
                    "stages": self._stages(resolutions),
                }
        raise RatbagError("no active profile")

    def _stages(self, paths: Any) -> list[dict]:
        if not isinstance(paths, list):
            raise RatbagError("Resolutions is not a list")
        stages = []
        for path in paths[:_MAX_RESOLUTIONS]:
            active, disabled, value = self._get(path, "Resolution", "IsActive", "IsDisabled", "Resolution")
            if disabled is True:
                continue
            # A variant: u for both axes, (uu) when X and Y are separate.
            dpi = value[0] if isinstance(value, list) and value else value
            if isinstance(dpi, int):
                stages.append({"dpi": dpi, "active": active is True})
        return stages


def piper() -> str | None:
    """Piper's path when installed from the distribution; nothing searched on PATH."""
    return PIPER if os.access(PIPER, os.X_OK) else None
