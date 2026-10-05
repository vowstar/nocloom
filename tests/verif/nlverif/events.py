"""Event log (JSON Lines) reading and canonical hashing.

Event kinds: push (tape issued a descriptor), hs (handshake or completed
transfer), lv (VALID level edge), sb (sideband edge with progress gaps), rst
(reset edge), mk (window marker), tput (throughput window), cancel, fail,
end, ahb_addr, ahb_data, tr (AHB bus state edge). Times are integer
picoseconds; "c" is the cycle number of the emitting BFM's clock domain.
"""

import hashlib
import json
import re

HEX_FIELDS = ("a", "d", "s", "x", "u")

# A %0d field whose value holds X or Z prints as a bare x/X/z/Z, which is not
# JSON. Such a value is quoted on load and the event is marked with "_x".
BARE_UNKNOWN = re.compile(r'"(\w+)":([xXzZ?]+)(?=[,}])')


class EventError(ValueError):
    pass


def _clean(value):
    if isinstance(value, str):
        return value.replace("\x00", "").strip()
    return value


def load(path):
    events = []
    with open(path, "r", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            events.append(parse_line(line, number))
    return events


def parse_line(line, number=0):
    """One event; an unknown (X/Z) decimal field is quoted and the event
    marked "_x"; an unparsable line becomes kind "bad". Neither raises, so
    one corrupt line is judged as EVENT_X instead of ending the run."""
    unknown = []
    try:
        e = json.loads(line, strict=False)
    except json.JSONDecodeError:
        unknown = sorted({m.group(1) for m in BARE_UNKNOWN.finditer(line)})
        repaired = BARE_UNKNOWN.sub(lambda m: f'"{m.group(1)}":"{m.group(2)}"', line)
        try:
            e = json.loads(repaired, strict=False)
        except json.JSONDecodeError:
            return {"k": "bad", "raw": line[:200], "_n": number}
    if not isinstance(e, dict):
        return {"k": "bad", "raw": line[:200], "_n": number}
    e = {k: _clean(v) for k, v in e.items()}
    e["_n"] = number
    unknown = sorted(set(unknown) | {f for f in HEX_FIELDS if f in e and hexval(e, f) is None})
    if unknown:
        e["_x"] = unknown
    return e


def load_verdict(path):
    """verdict.json with the same X/Z tolerance as event lines."""
    e = parse_line(open(path, "r", errors="replace").read().strip())
    if e.get("k") == "bad":
        raise EventError(f"{path}: unreadable verdict")
    return e


def hexval(e, field):
    """Integer value of a hex field, or None when it carries X or Z."""
    text = e.get(field)
    if text is None:
        return None
    text = str(text).lower()
    if any(ch in text for ch in "xz?"):
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def canonical(events):
    """Order-independent within one timestamp, so two builds of the same
    design produce one hash regardless of always-block scheduling."""
    rows = []
    for e in events:
        if e.get("k") in ("end",):
            continue
        rows.append(json.dumps({k: v for k, v in e.items() if k != "_n"}, sort_keys=True))
    rows.sort(key=lambda r: (json.loads(r).get("t", 0), r))
    return rows


def digest(events):
    h = hashlib.sha256()
    for row in canonical(events):
        h.update(row.encode() + b"\n")
    return h.hexdigest()
