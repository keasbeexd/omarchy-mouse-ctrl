#!/usr/bin/env python3
"""Turn a USB capture into device-profile offsets.

The workflow this supports:

  1. Start a capture while the vendor's Windows app is running.
  2. Change exactly ONE setting, note what you changed.
  3. Repeat for each setting, each value.
  4. Feed the capture plus your notes to this script.

It then diffs the config packets between labelled moments and tells you which
byte moved when you changed polling rate from 1000 to 4000 -- which is exactly
the offset the profile needs.

Input formats accepted:
  * tshark JSON   : tshark -r cap.pcapng -T json        (Linux usbmon or USBPcap)
  * tshark fields : tshark -r cap.pcapng -T fields -e frame.time_epoch -e usb.capdata
  * plain hexdump : one packet per line, "label: 08 04 00 ff ..."

Nothing here talks to a mouse; it is pure analysis, so it is safe to run
anywhere.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

CHECKSUM_KINDS = {
    "sum8": lambda d: sum(d) & 0xFF,
    "sum8_complement": lambda d: (0x100 - (sum(d) & 0xFF)) & 0xFF,
    "sum8_minus_55": lambda d: (0x55 - (sum(d) & 0xFF)) & 0xFF,
    "xor8": lambda d: _xor(d),
}


def _xor(data: bytes) -> int:
    acc = 0
    for b in data:
        acc ^= b
    return acc


@dataclass
class Packet:
    data: bytes
    label: str = ""
    direction: str = ""
    timestamp: float = 0.0

    @property
    def report_id(self) -> int:
        return self.data[0] if self.data else -1

    def hex(self) -> str:
        return " ".join(f"{b:02x}" for b in self.data)


# --- input parsing ----------------------------------------------------------


def _hexstring_to_bytes(text: str) -> bytes:
    cleaned = re.sub(r"[^0-9a-fA-F]", "", text)
    if len(cleaned) % 2:
        cleaned = cleaned[:-1]
    return bytes.fromhex(cleaned)


def parse_tshark_json(path: str) -> list[Packet]:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        doc = json.load(fh)
    packets: list[Packet] = []
    for entry in doc:
        layers = entry.get("_source", {}).get("layers", {})
        usb = layers.get("usb", {})
        payload = None
        for key in ("usb.capdata", "usb.data_fragment", "usbhid.data"):
            if key in layers:
                payload = layers[key]
                break
            if key in usb:
                payload = usb[key]
                break
        # Setup-stage control transfers carry the report in a separate field.
        if payload is None:
            setup = layers.get("Setup Data", {})
            payload = setup.get("usb.data_fragment")
        if payload is None:
            continue
        if isinstance(payload, list):
            payload = payload[0]
        data = _hexstring_to_bytes(str(payload))
        if not data:
            continue
        direction = ""
        endpoint = usb.get("usb.endpoint_address", "")
        if isinstance(endpoint, str) and endpoint:
            try:
                direction = "IN" if int(endpoint, 0) & 0x80 else "OUT"
            except ValueError:
                direction = ""
        ts = 0.0
        try:
            ts = float(usb.get("usb.time", 0) or layers.get("frame", {}).get("frame.time_epoch", 0))
        except (TypeError, ValueError):
            ts = 0.0
        packets.append(Packet(data=data, direction=direction, timestamp=ts))
    return packets


def parse_tshark_fields(path: str) -> list[Packet]:
    packets = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if not parts:
                continue
            ts = 0.0
            payload = parts[-1]
            if len(parts) > 1:
                try:
                    ts = float(parts[0])
                except ValueError:
                    ts = 0.0
            data = _hexstring_to_bytes(payload)
            if data:
                packets.append(Packet(data=data, timestamp=ts))
    return packets


def parse_labelled_hex(path: str) -> list[Packet]:
    """'pollingRate=4000: 08 05 00 ...' -- one packet per line."""
    packets = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            label = ""
            if ":" in line:
                head, _, tail = line.partition(":")
                if not re.fullmatch(r"[0-9a-fA-F\s]+", head):
                    label, line = head.strip(), tail
            data = _hexstring_to_bytes(line)
            if data:
                packets.append(Packet(data=data, label=label))
    return packets


def load_packets(path: str, fmt: str) -> list[Packet]:
    if fmt == "auto":
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(2048).lstrip()
        if head.startswith("[") or head.startswith("{"):
            fmt = "tshark-json"
        elif "\t" in head:
            fmt = "tshark-fields"
        else:
            fmt = "hex"
    if fmt == "tshark-json":
        return parse_tshark_json(path)
    if fmt == "tshark-fields":
        return parse_tshark_fields(path)
    return parse_labelled_hex(path)


def apply_labels(packets: list[Packet], labels_path: str) -> None:
    """labels file: '<epoch_seconds> <label>' per line, applied forward in time."""
    marks: list[tuple[float, str]] = []
    with open(labels_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ts_text, _, label = line.partition(" ")
            try:
                marks.append((float(ts_text), label.strip()))
            except ValueError:
                continue
    marks.sort()
    for pkt in packets:
        current = ""
        for ts, label in marks:
            if pkt.timestamp >= ts:
                current = label
            else:
                break
        pkt.label = current


# --- analysis ---------------------------------------------------------------


def score_length_group(packets: list[Packet]) -> float:
    """How much does this group look like config traffic rather than motion?

    Counting packets is the wrong test -- in any real capture the mouse emits
    thousands of motion reports and a handful of config packets, so the most
    common length is almost always the one we want to throw away.

    Config packets are instead distinguished by being highly repetitive (the
    app re-reads the same settings block over and over) and longer. Motion
    reports are near-unique because they carry deltas.
    """
    if not packets:
        return 0.0
    distinct = len({bytes(p.data) for p in packets})
    repetition = 1.0 - (distinct / len(packets))  # 0 = all unique, ~1 = all same
    length = len(packets[0].data)
    length_bonus = min(length, 64) / 64.0
    return repetition * 3.0 + length_bonus


def dominant_length(packets: Iterable[Packet], min_length: int = 8) -> int:
    groups: dict[int, list[Packet]] = defaultdict(list)
    for pkt in packets:
        groups[len(pkt.data)].append(pkt)
    if not groups:
        return 0
    eligible = {n: g for n, g in groups.items() if n >= min_length and len(g) >= 2}
    if not eligible:
        eligible = {n: g for n, g in groups.items() if len(g) >= 2} or groups
    best = max(eligible.items(), key=lambda kv: (score_length_group(kv[1]), kv[0]))
    return best[0]


def filter_config_packets(
    packets: list[Packet], length: int | None, report_id: int | None, min_length: int = 8
):
    """Drop the pointer-motion traffic; keep the fixed-size config packets."""
    target_len = length or dominant_length(packets, min_length)
    kept = [p for p in packets if len(p.data) == target_len]
    if report_id is not None:
        kept = [p for p in kept if p.report_id == report_id]
    return kept, target_len


def length_breakdown(packets: list[Packet]) -> list[dict]:
    """Show the user every length group and why we picked the one we picked."""
    groups: dict[int, list[Packet]] = defaultdict(list)
    for pkt in packets:
        groups[len(pkt.data)].append(pkt)
    rows = []
    for length, group in sorted(groups.items()):
        distinct = len({bytes(p.data) for p in group})
        rows.append(
            {
                "length": length,
                "count": len(group),
                "distinct": distinct,
                "repetition": round(1.0 - distinct / len(group), 3),
                "score": round(score_length_group(group), 3),
            }
        )
    rows.sort(key=lambda r: -r["score"])
    return rows


def guess_checksum(packets: list[Packet]) -> list[dict]:
    """Try every checksum over every plausible (offset, range) and see what holds."""
    if not packets:
        return []
    length = len(packets[0].data)
    findings = []
    for kind, fn in CHECKSUM_KINDS.items():
        for offset in (length - 1, 1, 2, 3):
            if offset >= length:
                continue
            for start in (0, 1, 2):
                for end in (offset, length - 1, length):
                    if end <= start or start > offset or end > length:
                        continue
                    if start <= offset < end:
                        continue  # checksum byte cannot be inside its own range
                    hits = 0
                    for pkt in packets:
                        if fn(pkt.data[start:end]) == pkt.data[offset]:
                            hits += 1
                    ratio = hits / len(packets)
                    # A range of one or two constant bytes satisfies almost any
                    # checksum by accident. Require the range to actually span
                    # the packet body, and to contain bytes that vary.
                    span = end - start
                    if span < max(4, length // 3):
                        continue
                    varying_in_range = any(
                        len({p.data[i] for p in packets}) > 1 for i in range(start, end)
                    )
                    if not varying_in_range:
                        continue
                    if ratio >= 0.98:
                        findings.append(
                            {
                                "kind": kind,
                                "offset": offset,
                                "range": [start, end],
                                "span": span,
                                "confidence": round(ratio, 3),
                                "packets": len(packets),
                            }
                        )
    # De-duplicate identical (kind, offset, range) triples.
    seen = set()
    unique = []
    for f in findings:
        key = (f["kind"], f["offset"], tuple(f["range"]))
        if key not in seen:
            seen.add(key)
            unique.append(f)
    # Prefer full confidence, then the widest coverage.
    unique.sort(key=lambda f: (-f["confidence"], -f["span"]))
    return unique[:5]


def byte_variance(packets: list[Packet]) -> dict[int, set]:
    columns: dict[int, set] = defaultdict(set)
    for pkt in packets:
        for i, b in enumerate(pkt.data):
            columns[i].add(b)
    return columns


def diff_by_label(packets: list[Packet]) -> dict:
    """The payoff: which byte index changed between labelled groups."""
    groups: dict[str, list[Packet]] = defaultdict(list)
    for pkt in packets:
        if pkt.label:
            groups[pkt.label].append(pkt)
    if len(groups) < 2:
        return {}

    # For each label, the set of values seen at each byte index.
    per_label: dict[str, dict[int, set]] = {
        label: byte_variance(pkts) for label, pkts in groups.items()
    }
    length = max((len(p.data) for p in packets), default=0)

    results = {}
    labels = sorted(groups)
    for i in range(length):
        values_by_label = {}
        for label in labels:
            vals = per_label[label].get(i, set())
            if len(vals) == 1:
                values_by_label[label] = next(iter(vals))
        if len(values_by_label) < 2:
            continue
        distinct = set(values_by_label.values())
        if len(distinct) > 1:
            results[i] = values_by_label
    return results


def suggest_field_specs(packets: list[Packet]) -> list[dict]:
    """Attribute each varying byte to the setting that actually moved it.

    Labels are expected to read 'fieldName=value'. For each field we look at
    only that field's own labels: a byte belongs to the field if it varies
    across them, and it is disqualified if it also varies across a different
    field's labels (that makes it a checksum or a sequence counter, not a
    setting).
    """
    by_field: dict[str, dict[str, list[Packet]]] = defaultdict(lambda: defaultdict(list))
    for pkt in packets:
        if not pkt.label or "=" not in pkt.label:
            continue
        name, _, value = pkt.label.partition("=")
        by_field[name.strip()][value.strip()].append(pkt)

    # Which bytes move for each field?
    moved: dict[str, dict[int, dict[str, int]]] = {}
    for field_name, groups in by_field.items():
        if len(groups) < 2:
            continue
        per_value: dict[str, dict[int, set]] = {
            value: byte_variance(pkts) for value, pkts in groups.items()
        }
        length = max((len(p.data) for pkts in groups.values() for p in pkts), default=0)
        changes: dict[int, dict[str, int]] = {}
        for i in range(length):
            stable: dict[str, int] = {}
            for value, columns in per_value.items():
                vals = columns.get(i, set())
                if len(vals) == 1:
                    stable[value] = next(iter(vals))
            if len(stable) >= 2 and len(set(stable.values())) > 1:
                changes[i] = stable
        moved[field_name] = changes

    # A byte that moves for two different settings is not a setting byte.
    offset_owners: Counter = Counter()
    for changes in moved.values():
        for offset in changes:
            offset_owners[offset] += 1

    suggestions = []
    for field_name, changes in sorted(moved.items()):
        for offset, observed in sorted(changes.items()):
            shared = offset_owners[offset] > 1
            spec: dict = {
                "field": field_name,
                "offset": offset,
                "encoding": "u8",
                "observed": observed,
            }
            if shared:
                spec["warning"] = (
                    "this byte also moves for other settings -- likely a checksum, "
                    "a sequence counter, or a packed bitfield"
                )
                spec["confidence"] = "low"
            else:
                spec["confidence"] = "high"

            pairs = sorted(observed.items())
            labels = [k for k, _ in pairs]
            raw = [v for _, v in pairs]
            numeric: list[int] = []
            for label in labels:
                try:
                    numeric.append(int(label))
                except ValueError:
                    numeric = []
                    break

            if numeric:
                scales = {n // r for n, r in zip(numeric, raw) if r}
                if len(scales) == 1:
                    k = scales.pop()
                    if k > 1 and all(r * k == n for n, r in zip(numeric, raw)):
                        spec["scale"] = k
                        spec.pop("values", None)
                    elif all(r == n for n, r in zip(numeric, raw)):
                        pass  # raw == value, plain u8
                    else:
                        spec["values"] = {str(r): n for n, r in zip(numeric, raw)}
                else:
                    spec["values"] = {str(r): n for n, r in zip(numeric, raw)}
                # Only an identity mapping (raw == value) can outgrow one byte.
                # With an enum or a scale the raw byte stays small by design, so
                # promoting those to u16le would be wrong.
                identity = "scale" not in spec and "values" not in spec
                if identity and any(n > 255 for n in numeric):
                    spec["encoding"] = "u16le"
                    spec["note"] = "value exceeds 255 -- check whether offset+1 also moves"
                elif "scale" in spec and any(n // spec["scale"] > 255 for n in numeric):
                    spec["encoding"] = "u16le"
                    spec["note"] = "scaled value exceeds one byte -- confirm offset+1 moves too"
            else:
                spec["values"] = {str(r): label for label, r in zip(labels, raw)}

            suggestions.append(spec)

    suggestions.sort(key=lambda s: (s["confidence"] != "high", s["field"], s["offset"]))
    return suggestions


# --- report -----------------------------------------------------------------


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture", help="tshark JSON/fields export, or a labelled hexdump")
    ap.add_argument("--format", choices=["auto", "tshark-json", "tshark-fields", "hex"], default="auto")
    ap.add_argument("--labels", help="file of '<epoch> <label>' markers")
    ap.add_argument("--length", type=int, help="only consider packets of this length")
    ap.add_argument(
        "--min-length",
        type=int,
        default=8,
        help="ignore packets shorter than this when auto-picking (default 8)",
    )
    ap.add_argument("--report-id", type=int, help="only consider this report id")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--emit-profile", help="write a partially-filled profile to this path")
    args = ap.parse_args(argv)

    packets = load_packets(args.capture, args.format)
    if not packets:
        print("No packets with payloads found in that capture.", file=sys.stderr)
        return 1
    if args.labels:
        apply_labels(packets, args.labels)

    breakdown = length_breakdown(packets)
    config, length = filter_config_packets(
        packets, args.length, args.report_id, args.min_length
    )
    report_ids = Counter(p.report_id for p in config)
    checksums = guess_checksum(config)
    columns = byte_variance(config)
    constant = sorted(i for i, vals in columns.items() if len(vals) == 1)
    varying = sorted(i for i, vals in columns.items() if len(vals) > 1)
    diffs = diff_by_label(config)
    suggestions = suggest_field_specs(config)

    payload = {
        "totalPackets": len(packets),
        "configPackets": len(config),
        "packetLength": length,
        "lengthGroups": breakdown,
        "reportIds": dict(report_ids),
        "constantBytes": constant,
        "varyingBytes": varying,
        "checksumCandidates": checksums,
        "labelDiffs": {str(k): v for k, v in diffs.items()},
        "fieldSuggestions": suggestions,
    }

    if args.emit_profile:
        profile = {
            "profileVersion": 1,
            "model": "G-Wolves HSK Pro 4K",
            "status": "partial",
            "transport": {
                "kind": "feature",
                "reportId": report_ids.most_common(1)[0][0] if report_ids else None,
                "packetLength": length,
                "readLength": length,
                "settleMs": 30,
                "checksum": (
                    {
                        "kind": checksums[0]["kind"],
                        "offset": checksums[0]["offset"],
                        "range": checksums[0]["range"],
                    }
                    if checksums
                    else {"kind": "none", "offset": None, "range": None}
                ),
            },
            # Only high-confidence attributions go into the profile. Bytes that
            # move for more than one setting are checksums or counters and would
            # poison the mapping.
            "fields": {
                s["field"]: {
                    k: v
                    for k, v in s.items()
                    if k not in ("field", "observed", "confidence", "warning", "note")
                }
                for s in suggestions
                if s["confidence"] == "high"
            },
        }
        with open(args.emit_profile, "w", encoding="utf-8") as fh:
            json.dump(profile, fh, indent=2)
        payload["profileWritten"] = args.emit_profile

    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        print()
        return 0

    print(f"packets              : {payload['totalPackets']}")
    print(f"config-shaped packets: {payload['configPackets']} (length {length})")
    print(f"report ids           : {dict(report_ids)}")
    print()
    print("packet length groups (config traffic repeats; motion traffic does not):")
    for row in breakdown[:6]:
        mark = "<- using" if row["length"] == length else ""
        print(
            f"  len {row['length']:<4} count {row['count']:<6} distinct {row['distinct']:<6}"
            f" repetition {row['repetition']:<6} score {row['score']:<6} {mark}"
        )
    print()
    print(f"bytes that never change: {constant}")
    print(f"bytes that do change   : {varying}")
    print()
    if checksums:
        print("checksum candidates (highest confidence first):")
        for c in checksums:
            print(
                f"  {c['kind']:<16} byte {c['offset']:<3} over {c['range']}"
                f"   {c['confidence'] * 100:.0f}% of {c['packets']} packets"
            )
    else:
        print("no checksum found -- the protocol may not use one, or the range is unusual")
    print()
    if diffs:
        print("bytes that changed between your labelled steps:")
        for offset, values in sorted(diffs.items()):
            rendered = ", ".join(f"{k} -> 0x{v:02x} ({v})" for k, v in sorted(values.items()))
            print(f"  byte {offset:<3}: {rendered}")
        print()
        if suggestions:
            print("attributed to a setting:")
            for s in suggestions:
                mark = "✓" if s["confidence"] == "high" else "?"
                extra = ""
                if "scale" in s:
                    extra = f"  scale x{s['scale']}"
                elif "values" in s:
                    extra = "  enum " + json.dumps(s["values"])
                print(f"  {mark} {s['field']:<18} byte {s['offset']:<3} {s['encoding']}{extra}")
                if "warning" in s:
                    print(f"      ! {s['warning']}")
                if "note" in s:
                    print(f"      note: {s['note']}")
            print()
            print("profile fields (high-confidence only):")
            print(
                json.dumps(
                    {
                        s["field"]: {
                            k: v
                            for k, v in s.items()
                            if k not in ("field", "observed", "confidence", "warning", "note")
                        }
                        for s in suggestions
                        if s["confidence"] == "high"
                    },
                    indent=2,
                )
            )
    else:
        print("no labelled diffs -- pass --labels, or label lines in the hexdump")
        print("as 'pollingRate=4000: 08 05 ...' so the diff has something to compare.")
    if args.emit_profile:
        print(f"\nwrote {args.emit_profile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
