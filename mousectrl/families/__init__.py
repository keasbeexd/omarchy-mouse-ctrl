"""Protocol family drivers. Each family is one module; register it here."""

from __future__ import annotations

from .base import Driver
from .gwolves_hts import GWolvesHts

FAMILIES: tuple[type[Driver], ...] = (GWolvesHts,)
