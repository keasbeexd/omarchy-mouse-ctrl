"""`mousectrl serve`: one long-lived process per panel, JSON lines on stdio.

Requests are one JSON object per line, `{"id": 1, "cmd": "devices"}`; each
reply is one line carrying the same id. Events are lines with no id,
`{"event": "devices"}`, sent when a hidraw node appears, disappears or
changes permission -- the last is how the panel learns a freshly installed
udev rule took effect.

Commands:
  devices
  capabilities  {device}
  read          {device, capability?}
  write         {device, capability, value}
  probe         {device}
  measure       {device}
  test-write    {device, capability}
  save-profile  {device, name, capabilities: {id: {state, visible}}}
  delete-profile {profile}
  udev-rule     {extra?: ["vvvv:pppp", ...]}

Requests are handled strictly in arrival order. Stdin closing ends the
process; nothing is written to any mouse on shutdown.
"""

from __future__ import annotations

import json
import os
import select
import sys
import threading
import time
import traceback
from typing import BinaryIO, TextIO

from . import hidraw
from .api import Api
from .errors import MouseCtrlError

MAX_LINE_BYTES = 65536
DEBOUNCE_SECONDS = 0.3


def _error(code: str, message: str) -> dict:
    return {"ok": False, "code": code, "error": message}


class Server:
    def __init__(self, api: Api, out: TextIO):
        self._api = api
        self._out = out
        self._out_lock = threading.Lock()
        # The watcher thread invalidates drivers; requests use them.
        self._api_lock = threading.Lock()

    def emit(self, payload: dict) -> None:
        line = json.dumps(payload, separators=(",", ":"))
        with self._out_lock:
            self._out.write(line + "\n")
            self._out.flush()

    def _dispatch(self, req: dict) -> dict:
        cmd = req.get("cmd")
        device = req.get("device")
        if cmd == "devices":
            return self._api.devices()
        if cmd == "udev-rule":
            return self._api.udev_rule(req.get("extra"))
        if cmd == "delete-profile":
            return self._api.delete_profile(req.get("profile"))
        if not isinstance(device, str):
            return _error("badRequest", f"{cmd!r} needs a device key")
        if cmd == "capabilities":
            return self._api.capabilities(device)
        if cmd == "probe":
            return self._api.probe(device)
        if cmd == "measure":
            return self._api.measure(device)
        if cmd == "read":
            capability = req.get("capability")
            if capability is not None and not isinstance(capability, str):
                return _error("badRequest", "capability must be a string")
            return self._api.read(device, capability)
        if cmd == "write":
            capability = req.get("capability")
            if not isinstance(capability, str) or "value" not in req:
                return _error("badRequest", "write needs capability and value")
            return self._api.write(device, capability, req["value"])
        if cmd == "test-write":
            capability = req.get("capability")
            if not isinstance(capability, str):
                return _error("badRequest", "test-write needs a capability")
            return self._api.test_write(device, capability)
        if cmd == "save-profile":
            return self._api.save_profile(device, req.get("name"), req.get("capabilities", {}))
        return _error("badRequest", f"unknown command {cmd!r}")

    def handle_line(self, line: bytes) -> dict:
        try:
            req = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _error("badRequest", f"not JSON: {exc}")
        if not isinstance(req, dict):
            return _error("badRequest", "a request must be a JSON object")
        req_id = req.get("id")
        if not (req_id is None or isinstance(req_id, (int, str))) or isinstance(req_id, bool):
            return _error("badRequest", "id must be an integer or string")
        try:
            with self._api_lock:
                reply = self._dispatch(req)
        except MouseCtrlError as exc:
            reply = _error(exc.code, str(exc))
        except OSError as exc:
            reply = _error("io", str(exc))
        except Exception as exc:
            # One bad request must not kill the panel's backend; the traceback
            # goes to stderr and the error back to the caller.
            traceback.print_exc(file=sys.stderr)
            reply = _error("internal", f"{type(exc).__name__}: {exc}")
        if req_id is not None:
            reply["id"] = req_id
        return reply

    def watch(self, dev_dir: str = "/dev") -> None:
        """Emit a debounced devices event on any hidraw change under /dev."""
        fd = hidraw.inotify_init()
        try:
            hidraw.inotify_add_watch(fd, dev_dir, hidraw.IN_CREATE | hidraw.IN_DELETE | hidraw.IN_ATTRIB)
            while True:
                if not any(n.startswith("hidraw") for n in hidraw.read_inotify_names(fd)):
                    continue
                # A replug produces a burst (one node per interface, then
                # udev's permission changes); answer the burst once.
                time.sleep(DEBOUNCE_SECONDS)
                while select.select([fd], [], [], 0)[0]:
                    hidraw.read_inotify_names(fd)
                with self._api_lock:
                    self._api.invalidate()
                self.emit({"event": "devices"})
        finally:
            os.close(fd)

    def run(self, stdin: BinaryIO) -> int:
        threading.Thread(target=self._watch_or_report, daemon=True).start()
        while True:
            line = stdin.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                while (rest := stdin.readline(MAX_LINE_BYTES)) and not rest.endswith(b"\n"):
                    pass
                self.emit(_error("badRequest", f"request longer than {MAX_LINE_BYTES} bytes"))
                continue
            if line.strip():
                self.emit(self.handle_line(line))
        with self._api_lock:
            self._api.close()
        return 0

    def _watch_or_report(self) -> None:
        try:
            self.watch()
        except OSError as exc:
            # Without hotplug events the panel still works, it just has to
            # be told: it can fall back to polling `devices`.
            self.emit({"event": "watcherFailed", "error": str(exc)})


def serve() -> int:
    return Server(Api(), sys.stdout).run(sys.stdin.buffer)
