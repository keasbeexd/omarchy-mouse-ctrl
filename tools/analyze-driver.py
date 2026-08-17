#!/usr/bin/env python3
"""Pull the HID protocol out of the G-Wolves Windows driver.

These vendor apps are usually C#/.NET or Electron, and in both cases the config
protocol is sitting in the binary in readable form -- as byte-array literals
(C# stores those as raw blobs in the PE, via FieldRVA) or as JavaScript. That
makes static analysis dramatically faster than USB capture: no Windows box, no
Wine, no clicking through the UI setting by setting.

Usage:
    ./tools/analyze-driver.py HSK_SW_FW20240320.zip
    ./tools/analyze-driver.py extracted/ --json report.json

Read-only. It never runs the vendor binary, only parses it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field

# Names that tell us how the app talks to the mouse. Finding HidD_SetFeature
# means the protocol rides on Feature reports, which is what hskctl's
# transport.kind = "feature" implements.
HID_API_NAMES = [
    "HidD_SetFeature",
    "HidD_GetFeature",
    "HidD_SetOutputReport",
    "HidD_GetInputReport",
    "HidD_GetAttributes",
    "HidD_GetPreparsedData",
    "HidP_GetCaps",
    "HidD_GetHidGuid",
    "CreateFileW",
    "CreateFileA",
    "WriteFile",
    "ReadFile",
    "DeviceIoControl",
]

# .NET HID wrapper libraries, in rough order of popularity for this class of app.
HID_LIBRARIES = [
    "HidLibrary",
    "HidSharp",
    "HidApi",
    "hidapi",
    "LibUsbDotNet",
    "UsbLibrary",
    "Device.Net",
    "node-hid",
    "usb-detection",
]

SETTING_KEYWORDS = [
    "dpi", "polling", "report rate", "reportrate", "debounce", "liftoff",
    "lift_off", "lod", "motionsync", "motion_sync", "ripple", "anglesnap",
    "angle_snap", "battery", "sleep", "firmware", "sensor", "profile",
    "macro", "rgb", "led", "keymap", "button",
]

VIDPID_PATTERNS = [
    re.compile(rb"vid_([0-9a-fA-F]{4})&pid_([0-9a-fA-F]{4})", re.I),
    re.compile(rb"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})"),
]


@dataclass
class Finding:
    path: str
    kind: str = "unknown"
    detail: dict = field(default_factory=dict)

    def as_dict(self):
        return {"path": self.path, "kind": self.kind, **self.detail}


# --- extraction -------------------------------------------------------------


def extract(archive: str, dest: str) -> str:
    lower = archive.lower()
    if os.path.isdir(archive):
        return archive
    os.makedirs(dest, exist_ok=True)
    if lower.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
        return dest
    for tool, args in (("7z", ["x", "-y", f"-o{dest}"]), ("unrar", ["x", "-y"])):
        if shutil.which(tool):
            cmd = [tool] + args + [os.path.abspath(archive)]
            result = subprocess.run(
                cmd, cwd=dest if tool == "unrar" else None,
                capture_output=True, text=True
            )
            if result.returncode == 0:
                return dest
    raise SystemExit(
        f"could not extract {archive}. Install p7zip-full (for .rar/.exe) "
        f"or unzip, or extract it yourself and point me at the folder."
    )


def walk_files(root: str):
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            yield os.path.join(dirpath, name)


# --- generic helpers --------------------------------------------------------


def ascii_strings(data: bytes, minimum: int = 5):
    out = []
    current = bytearray()
    for byte in data:
        if 0x20 <= byte < 0x7F:
            current.append(byte)
        else:
            if len(current) >= minimum:
                out.append(current.decode("ascii", "replace"))
            current = bytearray()
    if len(current) >= minimum:
        out.append(current.decode("ascii", "replace"))
    return out


def utf16_strings(data: bytes, minimum: int = 5):
    out = []
    current = bytearray()
    for i in range(0, len(data) - 1, 2):
        lo, hi = data[i], data[i + 1]
        if hi == 0 and 0x20 <= lo < 0x7F:
            current.append(lo)
        else:
            if len(current) >= minimum:
                out.append(current.decode("ascii", "replace"))
            current = bytearray()
    if len(current) >= minimum:
        out.append(current.decode("ascii", "replace"))
    return out


def find_vidpid(data: bytes) -> list[str]:
    found = set()
    for pattern in VIDPID_PATTERNS:
        for match in pattern.finditer(data):
            vid = match.group(1).decode().lower()
            pid = match.group(2).decode().lower()
            found.add(f"{vid}:{pid}")
    # Also catch UTF-16LE spellings, which is how .NET stores them.
    text = data.decode("utf-16-le", errors="ignore")
    for match in re.finditer(r"vid_([0-9a-fA-F]{4})&pid_([0-9a-fA-F]{4})", text, re.I):
        found.add(f"{match.group(1).lower()}:{match.group(2).lower()}")
    return sorted(found)


def interesting_strings(strings: list[str]) -> list[str]:
    hits = []
    for s in strings:
        lowered = s.lower()
        if any(keyword in lowered for keyword in SETTING_KEYWORDS):
            # Upper bound is generous because a single source line can hold a
            # whole label table -- which is exactly the kind of line we want.
            if 3 < len(s) < 400:
                hits.append(s)
    seen = set()
    unique = []
    for s in hits:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


# --- .NET -------------------------------------------------------------------


def analyse_dotnet(path: str, data: bytes) -> dict | None:
    """Pull string literals and, crucially, byte-array blobs out of a .NET PE.

    C# compiles `new byte[]{0x08,0x04,...}` into a blob in the PE that a
    FieldRVA record points at. Those blobs are almost always the protocol's
    packet templates, which is exactly what the profile's `commands` need.
    """
    try:
        import dnfile
    except ImportError:
        return None
    try:
        pe = dnfile.dnPE(data=data)
    except Exception:
        return None
    if not getattr(pe, "net", None):
        return None

    detail: dict = {"runtime": ".NET"}

    # String literals live in the #US heap.
    literals = []
    try:
        us = pe.net.user_strings
        offset = 1
        raw = us.__data__ if hasattr(us, "__data__") else b""
        while offset < len(raw):
            try:
                item = us.get_us(offset)
            except Exception:
                break
            if item is None:
                break
            value = getattr(item, "value", None)
            if value:
                literals.append(str(value))
            size = getattr(item, "raw_size", None) or (len(value or "") * 2 + 1)
            offset += max(1, size)
            if len(literals) > 20000:
                break
    except Exception:
        pass

    detail["stringCount"] = len(literals)
    detail["interestingStrings"] = interesting_strings(literals)[:60]

    # Which HID wrapper is in use?
    referenced = set()
    try:
        for row in pe.net.mdtables.TypeRef.rows:
            name = f"{row.TypeNamespace}.{row.TypeName}".strip(".")
            referenced.add(name)
    except Exception:
        pass
    try:
        for row in pe.net.mdtables.AssemblyRef.rows:
            referenced.add(str(row.Name))
    except Exception:
        pass
    detail["hidLibraries"] = sorted(
        {lib for lib in HID_LIBRARIES
         if any(lib.lower() in r.lower() for r in referenced)}
    )
    detail["pinvoke"] = sorted(
        {api for api in HID_API_NAMES
         if any(api.lower() in r.lower() for r in referenced)}
    )

    # FieldRVA -> initialized byte arrays. This is the payload.
    blobs = []
    try:
        for row in pe.net.mdtables.FieldRva.rows:
            rva = int(row.Rva)
            try:
                offset = pe.get_offset_from_rva(rva)
            except Exception:
                continue
            for size in (8, 16, 17, 32, 33, 64, 65):
                chunk = data[offset : offset + size]
                if len(chunk) < size:
                    continue
                blobs.append({"rva": hex(rva), "size": size, "bytes": chunk.hex(" ")})
    except Exception:
        pass

    # Keep the blobs that look like packets rather than lookup tables: a packet
    # has a small leading report id and is not mostly zeros or mostly identical.
    packets = []
    seen = set()
    for blob in blobs:
        raw = bytes.fromhex(blob["bytes"].replace(" ", ""))
        if raw in seen:
            continue
        seen.add(raw)
        zeros = raw.count(0)
        if zeros > len(raw) * 0.85:
            continue
        if len(set(raw)) < 3:
            continue
        packets.append(blob)
    detail["byteArrayBlobs"] = packets[:80]
    detail["byteArrayBlobCount"] = len(packets)
    return detail


# --- native PE --------------------------------------------------------------


def analyse_native(path: str, data: bytes) -> dict | None:
    try:
        import pefile
    except ImportError:
        return None
    try:
        pe = pefile.PE(data=data, fast_load=True)
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        )
    except Exception:
        return None

    imports = []
    for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []) or []:
        dll = entry.dll.decode(errors="replace") if entry.dll else "?"
        for imp in entry.imports:
            if imp.name:
                imports.append(f"{dll}!{imp.name.decode(errors='replace')}")

    hid_imports = sorted(
        {i for i in imports if any(api.lower() in i.lower() for api in HID_API_NAMES)}
    )
    strings = ascii_strings(data) + utf16_strings(data)
    return {
        "runtime": "native",
        "machine": hex(pe.FILE_HEADER.Machine),
        "importCount": len(imports),
        "hidImports": hid_imports,
        "usesHid": bool(hid_imports),
        "interestingStrings": interesting_strings(strings)[:60],
    }


# --- Electron ---------------------------------------------------------------


def analyse_asar(path: str, data: bytes) -> dict:
    """asar = 8-byte header, a JSON directory, then concatenated files."""
    detail = {"runtime": "Electron (asar)"}
    text = data.decode("utf-8", errors="ignore")
    scripts = re.findall(r"[\w./-]+\.js", text)
    detail["jsFileCount"] = len(set(scripts))
    detail["mentionsNodeHid"] = "node-hid" in text
    detail["sendFeatureCalls"] = len(re.findall(r"sendFeatureReport|getFeatureReport", text))
    detail["writeCalls"] = len(re.findall(r"\.write\s*\(", text))
    # Byte arrays written in source, e.g. [0x08, 0x04, 0x00, ...]
    arrays = re.findall(r"\[\s*(?:0x[0-9a-fA-F]{1,2}\s*,\s*){5,}0x[0-9a-fA-F]{1,2}\s*\]", text)
    detail["byteArrayLiterals"] = arrays[:40]
    detail["byteArrayLiteralCount"] = len(arrays)
    detail["interestingStrings"] = interesting_strings(ascii_strings(data))[:60]
    return detail


# --- driver -----------------------------------------------------------------


def classify(path: str) -> Finding:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return Finding(path, "unreadable", {"error": str(exc)})

    finding = Finding(path)
    finding.detail["size"] = len(data)

    vidpid = find_vidpid(data)
    if vidpid:
        finding.detail["vidpid"] = vidpid

    lower = path.lower()
    if lower.endswith(".asar") or data[:4] == b"\x04\x00\x00\x00":
        finding.kind = "electron"
        finding.detail.update(analyse_asar(path, data))
        return finding

    if data[:2] == b"MZ":
        dotnet = analyse_dotnet(path, data)
        if dotnet:
            finding.kind = "dotnet"
            finding.detail.update(dotnet)
            return finding
        native = analyse_native(path, data)
        if native:
            finding.kind = "native"
            finding.detail.update(native)
            return finding
        finding.kind = "pe"
        return finding

    if lower.endswith((".js", ".json", ".txt", ".ini", ".xml", ".cfg")):
        finding.kind = "text"
        # Source files are one continuous ASCII run, so the null-separated
        # extraction used for binaries would collapse the whole file into a
        # single "string". Split on lines instead.
        text = data.decode("utf-8", errors="replace")
        strings = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]
        hits = interesting_strings(strings)
        if hits:
            finding.detail["interestingStrings"] = hits[:40]
        arrays = re.findall(
            r"\[\s*(?:0x[0-9a-fA-F]{1,2}\s*,\s*){5,}0x[0-9a-fA-F]{1,2}\s*\]",
            data.decode("utf-8", errors="ignore"),
        )
        if arrays:
            finding.detail["byteArrayLiterals"] = arrays[:40]
        return finding

    if lower.endswith((".bin", ".hex", ".fw", ".img")):
        finding.kind = "firmware"
        return finding

    return finding


def rank(findings: list[Finding]) -> list[Finding]:
    def score(f: Finding) -> int:
        s = 0
        if f.kind == "dotnet":
            s += 50 + min(f.detail.get("byteArrayBlobCount", 0), 40)
        if f.kind == "electron":
            s += 45 + min(f.detail.get("byteArrayLiteralCount", 0), 40)
        if f.kind == "native" and f.detail.get("usesHid"):
            s += 40
        s += len(f.detail.get("hidLibraries", [])) * 10
        s += len(f.detail.get("pinvoke", [])) * 5
        s += len(f.detail.get("hidImports", [])) * 5
        s += min(len(f.detail.get("interestingStrings", [])), 20)
        if f.detail.get("vidpid"):
            s += 25
        return s

    return sorted(findings, key=score, reverse=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive", help="the vendor .zip/.rar/.exe, or an extracted folder")
    ap.add_argument("--json", help="write the full report here")
    ap.add_argument("--limit", type=int, default=12, help="how many files to detail")
    args = ap.parse_args(argv)

    tmp = tempfile.mkdtemp(prefix="hsk-driver-")
    try:
        root = extract(args.archive, tmp)
        findings = [classify(p) for p in walk_files(root)]
        findings = [f for f in findings if f.kind != "unreadable"]
        ranked = rank(findings)

        all_vidpid = Counter()
        for f in findings:
            for v in f.detail.get("vidpid", []):
                all_vidpid[v] += 1

        report = {
            "archive": args.archive,
            "fileCount": len(findings),
            "kinds": dict(Counter(f.kind for f in findings)),
            "vidpid": all_vidpid.most_common(),
            "files": [f.as_dict() for f in ranked],
        }

        print(f"files examined : {report['fileCount']}")
        print(f"kinds          : {report['kinds']}")
        print()
        if all_vidpid:
            print("USB ids found in the binaries (put these in the profile's match block):")
            for vidpid, count in all_vidpid.most_common(8):
                vid, pid = vidpid.split(":")
                print(f"  {vidpid}   vendorIds: [\"{vid}\"]  productIds: [\"{pid}\"]  ({count} refs)")
            print()

        for f in ranked[: args.limit]:
            rel = os.path.relpath(f.path, root)
            print(f"--- {rel}  [{f.kind}, {f.detail.get('size', 0)} bytes]")
            for key in (
                "runtime", "vidpid", "hidLibraries", "pinvoke", "hidImports",
                "mentionsNodeHid", "sendFeatureCalls",
                "byteArrayBlobCount", "byteArrayLiteralCount",
            ):
                if f.detail.get(key):
                    print(f"    {key}: {f.detail[key]}")
            strings = f.detail.get("interestingStrings") or []
            if strings:
                print(f"    strings ({len(strings)}):")
                for s in strings[:12]:
                    print(f"      {s!r}")
            blobs = f.detail.get("byteArrayBlobs") or []
            if blobs:
                print("    candidate packet templates:")
                for blob in blobs[:8]:
                    print(f"      {blob['rva']} ({blob['size']}B) {blob['bytes']}")
            arrays = f.detail.get("byteArrayLiterals") or []
            if arrays:
                print("    byte-array literals:")
                for a in arrays[:8]:
                    print(f"      {a[:110]}")
            print()

        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2)
            print(f"full report written to {args.json}")
        else:
            print("Re-run with --json report.json to keep the full detail.")
        return 0
    finally:
        if root != args.archive:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
