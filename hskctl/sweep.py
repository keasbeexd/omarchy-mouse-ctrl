"""Generic read sweep -- plan Phase 4, Tier 2 (any mouse, read discovery).

A mouse with no transport family cannot answer questions the wizard knows
how to ask, but it can still be *observed*: this module sends a bounded
set of vendor-shaped READ packets to a config-endpoint-shaped hidraw node
and records which read opcodes answer with data. It is the automated
first half of what docs/contributor/PROTOCOL-DISCOVERY.md walks a
contributor through by hand; the diff rounds (watching which byte moves
when the user acts) are built on top of what this finds.

Hard rules carried over from the plan:

- READS ONLY. Every opcode this module puts on the wire has the read bit
  set (0x80 | n). Building a write-shaped packet here would be a
  mutation of somebody's mouse, and mutations belong to the verification
  round with their own per-field consent, save/rollback and collateral
  reporting.
- BOUNDED. The opcode range and per-opcode attempt budget are constants,
  printed in the consent prompt before the first packet. No "keep trying
  things until something happens" loops.
- NO INTERPRETATION BEYOND OBSERVATION. The output names answering
  opcodes and raw reply bytes, plus the few conventions a reply set
  itself implies (opcode echo position, ack byte). Whether a byte means
  DPI is the diff round's job, on live evidence.

The packet shape is the one vendor config transports with a vendor usage
page overwhelmingly share, and the one the recovered families use: a
65-byte feature report, report id 0, `[0, 0, length, opcode, linkflag,
payload...]`, where reads echo the request header.

One property observed on hardware drives the whole acceptance logic: the
firmware ACKs *unconditionally* -- even an opcode it does not implement
gets the ack and echoes the request header back -- and flags the wrong
link with an ack plus an all-zero payload. Two discriminators survive
that: the reply's length byte is a property of the FIRMWARE (it corrects
a mismatched request length, revealing the command's true payload size
even when the value read is zero), and real payload bytes are non-zero
when the setting is non-zero. "Ack but identical header" is therefore
treated as no-data, and the other link flag is tried before the opcode
is called silent.
"""

from __future__ import annotations

import time

from .hidraw import HidrawDevice

# Read-side opcodes only -- a known families' reads clustered here
# (0x81 version, 0x82 polling, 0x83 dpi, 0x85/0x87 debounce/angle,
# 0x88 led, 0x8f battery, 0x90-92 connection/motion/wheel), but the range
# is a sweep constant, not the family's table.
READ_OPCODE_LO = 0x81
READ_OPCODE_HI = 0x9F

# (link flag, length byte) attempts per opcode, in order. Length is what
# the recovered family calls "payload length"; other transports may
# ignore it entirely, in which case the first guess answers and the rest
# never run for that opcode.
FLAG_GUESSES: tuple[int, ...] = (0, 1)
PROBE_LENGTH = 2

SETTLE_SECONDS = 0.04
_PACKET_LEN = 65


def _request(opcode: int, length: int, flag: int) -> bytes:
    """One read-shaped feature report, all payload zeroed."""
    return bytes([0x00, 0x00, length, opcode, flag]) + b"\x00" * (_PACKET_LEN - 5)


def sweep_node(path: str, progress=None) -> dict:
    """Sweep `path` with read-shaped packets; never mutates the device.

    Returns the observation payload: every (opcode, flag, length) that
    answered with any non-zero byte, the raw head of each reply, and the
    conventions the reply set itself implies. `progress` gets a string
    per opcode for interactive display.
    """
    answers: list[dict] = []
    votes: list[tuple[bool, bool, bool]] = []
    packets_sent = 0
    with HidrawDevice(path) as dev:
        for opcode in range(READ_OPCODE_LO, READ_OPCODE_HI + 1):
            if progress:
                progress(f"opcode {opcode:#04x}")
            for flag in FLAG_GUESSES:
                packet = _request(opcode, PROBE_LENGTH, flag)
                packets_sent += 1
                dev.set_feature(packet)
                time.sleep(SETTLE_SECONDS)
                reply = dev.get_feature(0, _PACKET_LEN)
                # Unconditional acks make "any nonzero" alone useless --
                # a void opcode still returns ack + echo. Data is either
                # non-zero payload or a corrected length byte.
                payload_alive = any(reply[5:])
                length_reveals = reply[2] != PROBE_LENGTH
                if payload_alive or length_reveals:
                    votes.append((
                        reply[3] == opcode,
                        reply[1] == 0xA1,
                        True,
                    ))
                    answers.append({
                        "opcode": f"{opcode:#04x}",
                        "flag": flag,
                        "trueLength": reply[2],
                        "head": reply[:16].hex(" "),
                        "nonzero": [i for i, b in enumerate(reply) if b],
                        "wakeNote": "" if payload_alive else
                            "payload all zero -- the value itself may "
                            "legitimately be zero, or the mouse is asleep",
                    })
                    break
            # An opcode that acked on every flag with an echoed header and
            # no correction is reported only in aggregate (the count of
            # silent ones), not listed.
    return _assess(path, answers, packets_sent, votes)


def read_round(path: str, alive: list[dict]) -> dict[str, bytes]:
    """One re-read of exactly the opcodes the sweep found alive.

    `alive` is the sweep's own answers list (opcode strings with their
    winning flags); the flags are reused as observed, not re-guessed. A
    reply that comes back empty/void this time is still recorded as
    bytes -- the DIFF is the point, and a setting that vanished mid-round
    is itself an observation the caller should see.
    """
    out: dict[str, bytes] = {}
    with HidrawDevice(path) as dev:
        for entry in alive:
            opcode = int(entry["opcode"], 16)
            packet = _request(opcode, PROBE_LENGTH, entry["flag"])
            dev.set_feature(packet)
            time.sleep(SETTLE_SECONDS)
            out[entry["opcode"]] = dev.get_feature(0, _PACKET_LEN)
    return out


def seek_values(path: str, answers: list[dict], values: list[int]) -> dict:
    """Find where the user's OWN declared values live on the wire.

    The user is the sensor: they know their DPI stage values, polling
    rate, whatever their mouse shipped declaring. This reads every alive
    opcode once and reports every (opcode, offset, encoding) position
    where one of the declared values appears inside the trusted window.

    Interpreted encodings are deliberately small: u8, u16 little-endian,
    u16 big-endian. Anything more exotic is the verification round's
    business, not the search's.

    Honest framing, which the caller must keep: hits are CANDIDATES.
    A 16-bit number band hits by chance in sixty bytes; the report
    counts how many declared values occur in the SAME opcode (a stage
    table holding 3 declared values in one block is a very different
    kind of candidate than a single distant match), and every hit rides
    into the draft profile unverified, with its provenance, or not at
    all.
    """
    replies = read_round(path, answers)
    hits: list[dict] = []
    per_opcode: dict[str, list[int]] = {}
    windows = payload_windows(answers)
    for opcode, reply in replies.items():
        lo, hi = windows.get(opcode, (5, 5))
        for value in values:
            # u8
            for i in range(lo, hi):
                if reply[i] == (value & 0xFF) and 0 <= value <= 0xFF:
                    hits.append({"value": value, "opcode": opcode,
                                 "index": i, "encoding": "u8"})
                    per_opcode.setdefault(opcode, []).append(value)
            # u16 le / be -- aligned to whole bytes inside the window
            if 0 <= value <= 0xFFFF and hi - lo >= 2:
                for i in range(lo, hi - 1):
                    pair = reply[i] | (reply[i + 1] << 8)
                    swapped = (reply[i] << 8) | reply[i + 1]
                    if pair == value and value > 0xFF:
                        hits.append({"value": value, "opcode": opcode,
                                     "index": i, "encoding": "u16le"})
                        per_opcode.setdefault(opcode, []).append(value)
                    elif swapped == value and value > 0xFF:
                        hits.append({"value": value, "opcode": opcode,
                                     "index": i, "encoding": "u16be"})
                        per_opcode.setdefault(opcode, []).append(value)
    # Deduplicate identical (value, opcode, index, encoding) sightings and
    # rank opcodes by how many DISTINCT declared values they contain.
    seen = set()
    unique = []
    for hit in hits:
        key = (hit["value"], hit["opcode"], hit["index"], hit["encoding"])
        if key not in seen:
            seen.add(key)
            unique.append(hit)
    ranked = sorted(
        per_opcode.items(),
        key=lambda kv: (-len(set(kv[1])), kv[0]),
    )
    return {
        "values": values,
        "hits": unique,
        "blocksByCoverage": [
            {"opcode": opcode, "distinct": len(set(vals)), "of": len(values)}
            for opcode, vals in ranked
        ],
    }


def diff_maps(before: dict[str, bytes], after: dict[str, bytes],
              windows: dict[str, tuple[int, int]] | None = None
              ) -> list[dict]:
    """Which (opcode, byte index) pairs moved between two read rounds.

    Pure function on the round payloads, so the interpretation discipline
    is testable without hardware. `windows` restricts judgment to the
    meaningful payload range per opcode, computed from the sweep's own
    length oracle: scalar replies trust exactly their declared length
    (family evidence: a 2-byte battery reports in only those bytes),
    while the long block replies UNDERCOUNT -- the DPI stage byte sits
    past the length byte the firmware echoes for short request
    variants -- so longer replies are watched to the packet edge.
    Changes outside a window are still reported, flagged `"trusted":
    False`: observations the draft assembler must not build on.
    """
    changes = []
    for opcode in sorted(before):
        was, now = before[opcode], after.get(opcode, b"")
        lo, hi = (windows or {}).get(opcode, (5, 65))
        for i in range(min(len(was), len(now))):
            if was[i] != now[i]:
                changes.append({
                    "opcode": opcode,
                    "index": i,
                    "from": was[i],
                    "to": now[i],
                    "trusted": lo <= i < hi,
                })
    return changes


def payload_windows(answers: list[dict]) -> dict[str, tuple[int, int]]:
    """The trusted window per opcode, from the sweep's length oracle.

    The boundary is the firmware's own declared length, period. The
    ambient-round evidence settled this the hard way: bytes past
    5 + declaredLength are driver tails that move on their own (the
    earlier "long blocks undercount" rule would have trusted them), and
    every observed tail byte -- 0x82@6, 0x88@11, 0x83@56 with len 51 --
    violated exactly that boundary.
    """
    windows = {}
    for a in answers:
        windows[a["opcode"]] = (5, 5 + a["trueLength"])
    return windows


def _assess(path: str, answers: list[dict], packets_sent: int,
            votes: list[tuple[bool, bool, bool]]) -> dict:
    """Reduce the raw sweep to the conventions the replies imply."""
    payload = {
        "ok": True,
        "node": path,
        "packetsSent": packets_sent,
        "answers": answers,
        "hypotheses": {},
    }
    if not answers:
        payload["hypotheses"]["note"] = (
            "no read-shaped packet got a non-zero reply in the swept range; "
            "this transport is either not the feature-report shape, guarded, "
            "or the mouse is asleep"
        )
        return payload

    # Convention votes over the answering set -- each is a hypothesis for
    # the diff rounds to confirm, never a claim.
    votes = {
        "echoOpcodeAt3": sum(1 for v in votes if v[0]),
        "ackA1At1": sum(1 for v in votes if v[1]),
    }
    total = len(answers)
    hyp = {}
    notes = []
    if votes["echoOpcodeAt3"] == total:
        hyp["opcodeEchoIndex"] = 3
        notes.append("every reply echoes the requested opcode at byte 3")
    if votes["ackA1At1"] == total:
        hyp["ack"] = {"offset": 1, "value": 0xA1}
        notes.append("every answer keeps 0xa1 at byte 1 -- the ack offset the "
                     "recovered profiles name")
    notes.append(
        "byte 2 of every reply is the firmware's own payload length (it "
        "corrects a mismatched request; the length oracle makes the size "
        "of every command observable even when the value read is zero)"
    )
    if notes:
        hyp["_note"] = "; ".join(notes)
    payload["hypotheses"] = hyp
    return payload
