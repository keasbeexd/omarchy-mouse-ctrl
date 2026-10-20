"""Protocol family drivers. Each family is one module; register it here."""

from __future__ import annotations

from .base import Driver

FAMILIES: tuple[type[Driver], ...] = ()
