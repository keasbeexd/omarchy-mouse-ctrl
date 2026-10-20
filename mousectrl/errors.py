"""Errors that cross the serve boundary carry a stable `code` for the panel."""

from __future__ import annotations


class MouseCtrlError(Exception):
    code = "error"


class DeviceNotFound(MouseCtrlError):
    code = "notFound"


class DeviceBusy(MouseCtrlError):
    code = "busy"


class NoAccess(MouseCtrlError):
    code = "noAccess"


class Unsupported(MouseCtrlError):
    code = "unsupported"


class WriteRefused(MouseCtrlError):
    code = "writeRefused"


class InvalidValue(MouseCtrlError):
    code = "invalidValue"


class ProtocolFailure(MouseCtrlError):
    """The mouse answered, but not in a way the driver can trust."""

    code = "protocol"


class UnsafePath(MouseCtrlError):
    code = "unsafePath"
