"""Finite seeded pressure traffic, protocol observations and transaction checks."""
from collections import defaultdict, deque
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).parent / "verif"))
from nlverif.check import Checker

LEVELS = {0: 1, 1: 1, 2: 3, 3: 3, 4: 2, 5: 2}


def observations(probes):
    text, groups = [], []
    for g in probes["network"]["groups"]:
        if g["kind"] != "arbiter":
            continue
        i = len(groups)
        clk = {"clk": "Clk", "clk_b": "Clk2"}[g["clock"]]
        fields = [(f, width) for f, _, width in g["nets"]]
        nets = ",".join("dut." + wire for _, wire, _ in g["nets"])
        text.append(f"integer pc{i}=0; always @(posedge {clk}) if(RstN) begin "
                    f"pc{i}=pc{i}+1; $display(\"PROBE {i} %0d %h\",pc{i},{{{nets}}}); end")
        groups.append(dict(g, fields=fields))
    return "\n".join(text), groups


def bench(kind, probes, seed):
    wide = kind == "wide"
    kind = "chain" if wide else kind
    template = Path(__file__).with_name(f"qos_pressure_{kind}_tb.sv").read_text()
    traffic = Path(__file__).with_name("qos_pressure_traffic.svh").read_text()
    monitor, groups = observations(probes)
    template = template.replace("@DATA_WIDTH@", "64" if wide else "32")
    template = template.replace("@S0_SIZE@", "3'd3" if wide else "3'd2")
    template = template.replace("@S0_STRB@", "8'hff" if wide else "4'hf")
    traffic = traffic.replace("@WIDE@", str(int(wide))).replace("@SEED@", str(seed))
    return template.replace("@TRAFFIC@", traffic).replace("@OBSERVATIONS@", monitor), groups


def unpack(group, value):
    row = defaultdict(list)
    for field, width in reversed(group["fields"]):
        row[field].insert(0, value & ((1 << width) - 1))
        value >>= width
    return {k: v if k in ("valid", "last", "flow", "first", "level", "eligible") else v[0]
            for k, v in row.items()}


def check(log, groups, wide=False):
    """Check every beat and keyed order, then replay every observed arbitration edge."""
    if "CHECK_PASS pressure" not in log:
        raise RuntimeError("PRESSURE_COMPLETE")
    rows = defaultdict(list)
    for line in log.splitlines():
        fields = line.split()
        if fields and fields[0] == "PROBE":
            _, index, cycle, packed = fields
            i = int(index)
            rows[i].append((0, int(cycle), unpack(groups[i], int(packed, 16))))
    if not groups or any(not rows[i] for i in range(len(groups))):
        raise RuntimeError("PRESSURE_OBSERVER_EMPTY")
    if (any(samples[0][1] != 1 for samples in rows.values())
            or any(nxt[1] != row[1] + 1 for samples in rows.values()
                   for row, nxt in zip(samples, samples[1:]))):
        raise RuntimeError("PRESSURE_OBSERVER_GAP")
    source, target = {}, {}
    returned = defaultdict(deque)
    read_beats = defaultdict(int)
    counts = defaultdict(int)
    for line in log.splitlines():
        f = line.split()
        if not f:
            continue
        if f[0] == "SRC":
            _, c, direction, s, ident, addr, length = f
            key = (direction, int(addr, 16))
            assert key not in source, "SOURCE_DUPLICATE"
            source[key] = (int(s), int(ident), int(length))
            returned[(direction, int(s), int(ident))].append(key)
        elif f[0] == "TARGET":
            _, c, direction, ident, addr, length = f
            key = (direction, int(addr, 16))
            assert key not in target, "TARGET_DUPLICATE"
            target[key] = int(length)
        elif f[0] == "RET":
            _, c, direction, s, ident, data, last, response = f
            queue = returned[(direction, int(s), int(ident))]
            assert queue and int(response) == 0, "SOURCE_RESPONSE"
            key = queue[0]
            beat = read_beats[key]
            if direction == "R":
                words = 2 if wide and int(s) == 0 else 1
                expected = sum((0xa5000000 | (key[1] + 4 * (words * beat + j))) << (32 * j)
                               for j in range(words))
                assert int(data, 16) == expected, "SOURCE_READ_DATA"
                assert int(last) == (beat == source[key][2]), "SOURCE_READ_LAST"
                read_beats[key] += 1
            if int(last):
                queue.popleft()
                counts[(direction, int(s))] += 1
    assert len(source) == 3 * 2 * 48 and source.keys() == target.keys(), "TARGET_FOOTPRINT"
    assert all(target[key] == (entry[2] + 1) * (2 if wide and entry[0] == 0 else 1) - 1
               for key, entry in source.items()), "TARGET_LENGTH"
    assert all(counts[(d, s)] == 48 for d in ("R", "W") for s in range(3)), "SOURCE_COMPLETION"
    assert all(not q for q in returned.values()), "SOURCE_UNANSWERED"
    stats, propagated, confirmed_grants = [], 0, 0
    for i, group in enumerate(groups):
        mismatch, stat = Checker._replay(group, rows[i], "round_robin", None, LEVELS)
        assert mismatch is None, ("PRESSURE_GRANT", group["name"], mismatch)
        stats.append(stat)
        if group["graph"] == "request" and group["node"] == "B":
            for _, _, row in rows[i]:
                if (not row["held"] and row["ready"] and row["valid"][0]
                        and row["level"][0] == 7 and row["valid"][1] and row["level"][1] < 7):
                    # Once pressure is confirmed, the first free service opportunity grants it.
                    assert row["winner"] == 0 and row["transfer"], "PRESSURE_SERVICE_BOUND"
                    confirmed_grants += 1
                # A buffered low head must carry a higher waiting request's pressure.
                propagated += int(row["valid"][0] and row["first"][0] and
                                  row["flow"][0] in (0, 1) and row["level"][0] >= 7)
    assert sum(s["contended"] for s in stats), "PRESSURE_CONTENTION"
    assert sum(s["held_higher"] for s in stats), "PRESSURE_HELD_PACKET"
    assert propagated, "PRESSURE_PROPAGATION"
    assert confirmed_grants, "PRESSURE_SERVICE_COVER"
    return dict(transactions=len(source), arbitration=stats, propagated=propagated, confirmed_grants=confirmed_grants)


def mutations(rtl, bindings, kind):
    """Suppress stored pressure. Matching no state is a tool error."""
    candidates = [v for k, v in bindings["network"].items()
                  if k.startswith("level_hint/pipeline/req/")]
    if kind == "cdc":
        candidates = [v for k, v in bindings["network"].items()
                      if k.startswith("level_hint/cut/req/req_node0>req_node1/")
                      and (k.endswith(("/send", "/forward")) or re.search(r"/slot\d+$", k))]
    if kind == "wide":
        value = bindings["network"]["graph/req/req_node0>node/req_node1.level0"]
        net = "nl_internal_v" + str(value["index"])
        changed, hits = re.subn(rf"(?m)^([ \t]*assign {net} = )[^;]+;", rf"\g<1>3'd0;", rtl)
        assert hits == 1, "missing wide source pressure binding"
        return changed
    assert candidates, ("missing pressure mutation binding", kind)
    changed, hits = rtl, 0
    for value in candidates:
        net = "nl_internal_v" + str(value["index"])
        changed, n = re.subn(rf"(?m)^(    {net} <= )[^;]+;", rf"\g<1>3'd0;", changed)
        hits += n
    assert hits, ("missing pressure mutation state", kind)
    return changed


def unstable_address(rtl):
    """Change an offered address during a stall, then restore it at acceptance."""
    changed, hits = re.subn(r"(?m)^([ \t]*assign nl_t_t0_araddr = )([^;]+);",
                           r"\1(\2 ^ {32{!nl_t_t0_arready}});", rtl)
    assert hits == 1, "missing target read address assignment"
    return changed
