"""hskctl -- Linux configuration for G-Wolves HSK-family mice."""

import json
import os

# One version number, and it lives in manifest.json because that is the one the
# Omarchy marketplace displays. A second copy here would drift from it -- it
# already had: the manifest said 1.0.1 while this file still said 0.1.0, so
# `hskctl --version` reported a build that had not existed for a while.
#
# The CLI ships inside the plugin, so the manifest is always beside it. The
# fallback covers hskctl being copied out on its own.
_FALLBACK_VERSION = "0.0.0-unpackaged"


def _read_version() -> str:
    manifest = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "manifest.json"
    )
    try:
        with open(manifest, "r", encoding="utf-8") as fh:
            return json.load(fh).get("version") or _FALLBACK_VERSION
    except (OSError, ValueError, AttributeError):
        return _FALLBACK_VERSION


__version__ = _read_version()
