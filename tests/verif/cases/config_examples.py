"""Every shipped YAML example generates, deterministically, with its
artifact set.

For every ahb_v3 target the HRESP port width in the .v equals the YAML
hresp_width (default 1) and the map records it; the example with the width
written out generates the same bytes, and with the other width only that
port changes."""

import json
import re
from pathlib import Path

EXAMPLES = sorted((Path(__file__).resolve().parents[3] / "examples").glob("*.yaml"))
if len(EXAMPLES) < 20:
    raise ValueError(f"only {len(EXAMPLES)} examples found")


def target_entries(text):
    """Items of the top-level 'targets:' list, each joined into one string.
    Handles flow mappings spanning lines and block mappings."""
    entries, inside, indent = [], False, None
    for line in text.splitlines():
        if re.match(r"^targets:\s*$", line):
            inside = True
            continue
        if not inside:
            continue
        if re.match(r"^\S", line):
            break
        m = re.match(r"^(\s*)- ", line)
        if m and (indent is None or len(m.group(1)) == indent):
            indent = len(m.group(1))
            entries.append(line.strip()[2:])
        elif line.strip():
            if not entries:
                raise ValueError(f"unsupported targets layout: {line!r}")
            entries[-1] += " " + line.strip()
    return entries


def field(entry, key):
    m = re.search(rf"(?:^|[\s{{,]){key}:\s*[\"']?([\w.-]+)", entry)
    return m.group(1) if m else None


def ahb_hresp(text):
    """{target name: expected HRESP width} for every ahb_v3 target."""
    widths = {}
    for entry in target_entries(text):
        if field(entry, "protocol") != "ahb_v3":
            continue
        width = field(entry, "hresp_width")
        widths[field(entry, "name")] = int(width) if width else 1
    # Count target entries independently of initiator protocols.
    if sum(field(e, "protocol") == "ahb_v3" for e in target_entries(text)) != len(widths):
        raise ValueError("ahb_v3 targets not all parsed")
    return widths


def port_width(rtl, port):
    m = re.search(rf"^\s*input\s+wire\s+(?:\[(\d+):0\]\s+)?{re.escape(port)}\s*[,)]?\s*$",
                  rtl, flags=re.M)
    assert m, f"no input declaration for {port}"
    return int(m.group(1)) + 1 if m.group(1) else 1


def with_hresp(text, name, width):
    """The example text with hresp_width set on target `name`."""
    m = re.search(rf"\{{name:\s*[\"']?{re.escape(name)}[\"']?\s*,", text)
    if not m:
        raise ValueError(f"cannot find a flow-mapping target {name}")
    depth, end = 0, m.start()
    while True:
        depth += {"{": 1, "}": -1}.get(text[end], 0)
        end += 1
        if depth == 0:
            break
    entry = re.sub(r",\s*hresp_width:\s*\d+", "", text[m.start():end])
    entry = entry.replace(m.group(0), f"{m.group(0)} hresp_width: {width},", 1)
    return text[:m.start()] + entry + text[end:]


def rtl_of(out):
    """The generated module (the bundle top), not the cell model beside it."""
    top = json.loads(next(out.glob("*.bundle.json")).read_text())["top"]
    return (out / (top + ".v")).read_text()


def single_top(name, widths):
    def check(mapping, out, peers):
        top = f"nocloom_{name}"
        assert (out / f"{top}.v").is_file(), f"no {top}.v"
        rtl = rtl_of(out)
        assert len(re.findall(rf"^module {re.escape(top)}\b", rtl, flags=re.M)) == 1, \
            f"top module {top} not declared exactly once"
        if widths:
            recorded = mapping["definition"].get("ahb_hresp_widths")
            assert recorded == widths, f"map ahb_hresp_widths {recorded} != {widths}"
            for target, width in widths.items():
                got = port_width(rtl, f"nl_t_{target}_hresp")
                assert got == width, f"{target} HRESP port is {got} bits, configured {width}"
    return check


def other_width(stem, target, other, widths):
    def check(mapping, out, peers):
        rtl = rtl_of(out)
        assert port_width(rtl, f"nl_t_{target}_hresp") == other, \
            f"hresp_width {other} on {target} did not change the port width"
        for peer, peer_width in widths.items():
            if peer != target:
                assert port_width(rtl, f"nl_t_{peer}_hresp") == peer_width, \
                    f"hresp_width on {target} changed {peer}"
    return check


CASES = []
AHB_TARGETS = 0
for path in EXAMPLES:
    text = path.read_text()
    name = re.search(r"^name:\s*[\"']?([\w.-]+)[\"']?\s*$", text, flags=re.M).group(1)
    widths = ahb_hresp(text)
    CASES.append(dict(name=path.stem, text=text, expect="accept",
                      check=single_top(name, widths)))
    for target, width in widths.items():
        AHB_TARGETS += 1
        CASES.append(dict(name=f"{path.stem}-{target}-explicit", expect="accept",
                          commands=("generate",), text=with_hresp(text, target, width),
                          same_as=path.stem))
        CASES.append(dict(name=f"{path.stem}-{target}-other", expect="accept",
                          commands=("generate",), text=with_hresp(text, target, 3 - width),
                          check=other_width(path.stem, target, 3 - width, widths)))
if AHB_TARGETS == 0:
    raise ValueError("no example has an ahb_v3 target: the HRESP check is vacuous")
