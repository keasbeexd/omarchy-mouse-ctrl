"""mousectrl -- Linux configuration for mice in supported protocol families."""

import os

from .safeio import read_bounded_json

# One version number, and it lives in manifest.json because that is the one the
# Omarchy marketplace displays. A second copy here would drift from it -- it
# already had: the manifest said 1.0.1 while this file still said 0.1.0, so
# `mousectrl --version` reported a build that had not existed for a while.
#
# The CLI ships inside the plugin, so the manifest is always beside it. The
# fallback covers mousectrl being copied out on its own.
_FALLBACK_VERSION = "0.0.0-unpackaged"
# The manifest is about 1.5 KiB.
_MAX_MANIFEST_BYTES = 65536


def _read_version() -> str:
    manifest = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "manifest.json"
    )
    # Through one validated O_NOFOLLOW descriptor and capped, like every other
    # file mousectrl reads: this runs on every invocation, the widget's included.
    try:
        version = read_bounded_json(
            manifest, _MAX_MANIFEST_BYTES, allowed_owners=(os.geteuid(), 0)
        ).get("version")
    except (OSError, ValueError):
        return _FALLBACK_VERSION
    if not isinstance(version, str) or not version:
        return _FALLBACK_VERSION
    return version[:32]


__version__ = _read_version()
