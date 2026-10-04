"""Clocked graphs: mixed-width packets through independently clocked graph
segments, one registration per mode (--param mode=internal|endpoints|
twelve|ratios).

The DUT is the three-source graph (s0 128-bit AXI4, s1 32-bit AXI4-Lite,
s2 256-bit AXI4; 32-bit targets) with selective pairs (each source reaches
its own and the next target; the others answer DECERR without target
traffic) and clock domains: internal puts the switch nodes in a domain of
their own, endpoints the targets in a third, twelve gives every source,
target set and switch node its own domain. Every BFM runs on the clock of
its endpoint's domain. One internal domain (the switch nodes) is stopped at
start: requests wait in local storage and no target sees one before it runs.

Ten phases rotate the routes so every admitted and refused pair is used,
then single beats, bursts of 16, target errors, sparse and empty strobes,
a source holding RREADY or BREADY low (it takes nothing meanwhile; all
complete after), AXI4-Lite W before AW and AW before W, and common resets
in the middle of a write packet and of a read response, after which no
target or source sees anything late. The ratios mode runs the endpoints
network at six clock ratios (formula crossing depths) and the twelve-domain
network at random periods and phases.

The ratios registration runs as a negative control: its main configuration
(label staggered) is a public network (CLI) whose three domains each have
their own reset port: two s0/s1 AXI4 sources in src, one request and one
response node in net, two AXI4 targets in tgt; rst_src_n and rst_net_n
release through each domain's two registers, rst_tgt_n on a falling edge of
the tgt clock directly. The bench asserts the three ports together and
releases each a number of its own clock edges later, a different order per
generation: (0) src, tgt, then net: no target sees anything before net
leaves reset; (1) net, src, then tgt: no target sees anything before tgt
leaves reset; (2) all within a few edges. Then, with the network idle, (3)
rst_net_n alone and (4) rst_tgt_n alone are asserted while the other domains
keep running; the crossings on both sides of the reset domain run the reset
handshake. (5) rst_src_n alone (the source BFMs reset with it) while a read
of s0 waits at t0 and a write of s1 at t1, the targets withholding R and B;
they answer the abandoned bursts after src runs again, and those responses
must not answer the next bursts. The ratios registration also runs oneside_inflight.py: a domain
reset alone while a packet crosses its boundary. The staggered network sets clock_gating none;
its gated variant (the default gating) puts a pipe at the switch
of every attachment and every endpoint pipe kind on some endpoint; its faults
also drop the link or endpoint pipe slots from a gate's idle, clock the
request entry pipes from src and clock each endpoint pipe kind from another
domain. Every generation then
completes all its writes and reads, with no stale word replayed. The faults tie a domain to another domain's reset port
in the netlist (the net release registers to rst_src_n; the tgt state to
rst_net_n): that domain leaves reset early and a target sees a request
inside its quiet window; and every crossing side's handshake enable ignores
the other side: after a reset of one side alone the other side replays words
it had already delivered; and a source endpoint takes a response while no burst of
its own waits for one: after the src reset the abandoned bursts' responses
answer the next bursts."""

import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from endpoint_ports import endpoint_ports  # noqa: E402
import oneside_inflight  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

PARAMS = globals().get("PARAMS", {})
MODE = PARAMS.get("mode", "internal")
if MODE not in ("internal", "endpoints", "twelve", "ratios"):
    raise ValueError(f"unknown clocked graph mode {MODE}")
SOURCES = ("s0", "s1", "s2")
TARGETS = ("t0", "t1", "t2")
BYTES = {"s0": 16, "s1": 4, "s2": 32}
TABLE = [10, 14, 22, 26, 13, 17, 19, 23, 29, 31, 37, 41]


def domains(mode):
    """Declared domains in order and the domain of each endpoint."""
    if mode == "internal":
        names = ["main", "middle"]
        objects = {e: "main" for e in SOURCES + TARGETS}
    elif mode == "endpoints":
        names = ["main", "middle", "target"]
        objects = dict({s: "main" for s in SOURCES}, **{t: "target" for t in TARGETS})
    else:
        names = ["main", "source1", "source2", "target"] + [f"node{i}" for i in range(8)]
        objects = dict(s0="main", s1="source1", s2="source2", **{t: "target" for t in TARGETS})
    return names, objects


def clocks(mode, ratio=None):
    """(clock table for the bench, periods for the fixture or None)."""
    names, _ = domains(mode)
    stopped = {"internal": "middle", "endpoints": "middle", "twelve": "node0"}[mode]
    if mode == "internal":
        table = dict(main=dict(period=10.0), middle=dict(period=13.0, phase=2.0, start=1200.0))
        return table, None
    periods = phases = None
    if ratio and ratio.startswith("random:"):
        rng = random.Random(int(ratio.split(":")[1]))
        periods = [2 * rng.randint(3000, 15000) for _ in names]
        phases = [rng.randint(0, p - 1) for p in periods]
    elif ratio:
        slow, fast = (int(x) for x in ratio.split(":"))
        periods = [(10000 * slow if i % 2 == 0 else 10000 * fast) // min(slow, fast)
                   for i in range(len(names))]
        phases = [0 if slow == fast else 137 * i for i in range(len(names))]
    table = {}
    for i, name in enumerate(names):
        period = periods[i] / 1000 if periods else TABLE[i]
        phase = phases[i] / 1000 if phases else round(i * 0.137, 3)
        table[name] = dict(period=period, phase=phase)
    # The stopped domain starts after 120 main clock cycles; the tape keeps
    # every target quiet through cycle 75.
    table[stopped]["start"] = 120 * table["main"]["period"]
    return table, periods


def recipe(mode, periods):
    params = dict(mode=mode)
    if periods:
        params["periods"] = ",".join(map(str, periods))
    return dict(source="fixture", recipe="graph_clocked", params=params)


def permitted(owner, target):
    return target == owner or target == (owner + 1) % 3


ROUTES = {s: [(t, 65536 * i, 65536) for i, t in enumerate(TARGETS) if permitted(k, i)]
          for k, s in enumerate(SOURCES)}
TARGET_MODEL = {t: dict(kind="axi", bus_bytes=4, cache="amba", prot=True, ids=True, raw_address=True,
                        error_data_zero=True) for t in TARGETS}


STAGGERED_SOURCES, STAGGERED_TARGETS = ("s0", "s1"), ("t0", "t1")
# Falling edges of each domain's own clock between the common release and the
# release of its port, per generation.
STAGGERED_RELEASE = dict(src=[0, 60, 8], net=[120, 0, 4], tgt=[40, 150, 0])


def staggered_configuration():
    def graph(prefix, entries, exits):
        return {'nodes': [{'name': prefix, 'clock_domain': 'net'}], 'links': [],
                'entries': [{'endpoint': e, 'node': prefix} for e in entries],
                'exits': [{'endpoint': e, 'node': prefix} for e in exits]}
    return endpoint_ports({
        'schema_version': 10, 'name': 'reset_release', 'address_width': 32, 'clock_gating': 'none',
        'reset_ports': [{'name': 'rst_src_n'}, {'name': 'rst_net_n'},
                        {'name': 'rst_tgt_n', 'release': 'on_clock_edge',
                         'release_domain': 'tgt'}],
        'clock_domains': [{'name': d, 'clock_port': 'clk_' + d, 'reset_port': f'rst_{d}_n'}
                          for d in ('src', 'net', 'tgt')],
        'initiators': [{'name': s, 'clock_domain': 'src', 'protocol': 'axi4', 'data_width': 32}
                       for s in STAGGERED_SOURCES],
        'targets': [{'name': t, 'clock_domain': 'tgt', 'protocol': 'axi4', 'data_width': 32,
                     'attributes': 'amba',
                     'mappings': [{'base': hex(0x10000 * k), 'size': '0x10000'}]}
                    for k, t in enumerate(STAGGERED_TARGETS)],
        'topology': {'request': graph('q', STAGGERED_SOURCES, STAGGERED_TARGETS),
                     'response': graph('p', STAGGERED_TARGETS, STAGGERED_SOURCES)},
        'connectivity': 'all',
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)



def rewire(domain, wrong):
    """Text fault: every use of the reset port of domain except its input
    alias names the port of domain wrong instead (resynchronized: the release
    registers; direct: the state and the logic it gates)."""
    def subs(rtl, _map):
        alias = {}
        for d in (domain, wrong):
            found = re.findall(rf"^assign (nl_internal_v\d+) = rst_{d}_n;$", rtl, re.M)
            if len(found) != 1:
                return [(r"\Zanchor-not-found", "", 1)]
            alias[d] = found[0]
        use = rf"(?<!wire )\b{alias[domain]}\b(?! = rst_{domain}_n;)"
        return [(use, alias[wrong], len(re.findall(use, rtl, re.M)))]
    return subs




def peer_unmasked(rtl, mapped):
    """Text fault: every crossing side's enable (the synchronized announce of the other
    side and its own on) is its own on alone."""
    subs = []
    for cut in mapped["network"]["cdc"]["cuts"]:
        for side in cut.get("handshake", {}).values():
            last, on = (x.split("/")[1] for x in (side["announce_sync"][-1], side["on"]))
            subs.append((rf"^assign (nl_internal_v\d+) = \(nl_internal_v{last} & nl_internal_v{on}\);$",
                         rf"assign \1 = nl_internal_v{on};", 1))
    return subs or [(r"\Zanchor-not-found", "", 1)]


def response_undropped(rtl, mapped):
    """Text fault: a source endpoint's response take (the acquire of its return storage: the
    handshake and the gate that its burst waits) is the handshake alone."""
    subs = []
    for store in mapped["network"]["storage"]:
        if store["resource"].startswith("source/") and store["resource"].endswith("/return"):
            v = store["acquire"].split("/")[1]
            subs.append((rf"^assign nl_internal_v{v} = \(nl_internal_v(\d+) & nl_internal_v\d+\);$",
                         rf"assign nl_internal_v{v} = nl_internal_v\1;", 1))
    return subs or [(r"\Zanchor-not-found", "", 1)]


def staggered_config():
    cfg = staggered_configuration()
    text = dict(op="text", contract="AXI.RESET.DOMAIN")
    return dict(
        label="staggered", dut=dict(source="cli", config=cfg), main_clock="src",
        clocks=dict(src=10.0, net=dict(period=13.0, phase=1.3), tgt=dict(period=17.0, phase=2.9)),
        resets={f"rst_{d}_n": dict(clock=d, release=STAGGERED_RELEASE[d]) for d in STAGGERED_RELEASE},
        scenario_args=dict(mode="staggered"), max_cycles=60000,
        bfms=[dict(name=s, kind="axi_master", endpoint=f"initiator:{s}", clock="src",
                   reset="rst_src_n") for s in STAGGERED_SOURCES]
        + [dict(name=t, kind="axi_slave", endpoint=f"target:{t}", clock="tgt", salt=k)
           for k, t in enumerate(STAGGERED_TARGETS)],
        model=dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg),
                   targets=nlconfig.targets(cfg, cache="amba", prot=True)),
        faults=[dict(id="net_on_source_reset", subs=rewire("net", "src"),
                     expect=["CONTRACT.QUIET.AW", "CONTRACT.QUIET.AR"],
                     note="the net release registers reset from rst_src_n: net leaves reset with src",
                     **text),
                dict(id="target_on_net_reset", subs=rewire("tgt", "net"),
                     expect=["CONTRACT.QUIET.AW", "CONTRACT.QUIET.AR"],
                     note="the tgt state resets from rst_net_n: tgt leaves reset with net",
                     **text),
                dict(id="crossing_enable_ignores_peer", subs=peer_unmasked,
                     expect=["TARGET_WRITE_BYTES", "TARGET_DESCRIPTOR", "TARGET_FOOTPRINT",
                             "TARGET_UNATTRIBUTED", "TARGET_ORDER", "AXI.W.LAST", "SOURCE_B_RESP",
                             "SOURCE_R_DATA", "SOURCE_EXTRA_B", "SOURCE_EXTRA_R", "SOURCE_UNANSWERED",
                             "TAPE.DRAIN_TIMEOUT:GENERATION_3", "TAPE.DRAIN_TIMEOUT:GENERATION_4"],
                     note="after a reset of one side alone the other side keeps its count: stale "
                          "words leave the crossing again",
                     **text),
                dict(id="source_takes_stale_response", subs=response_undropped,
                     expect=["AXI.B.UNEXPECTED", "AXI.R.UNEXPECTED", "SOURCE_EXTRA_B",
                             "SOURCE_EXTRA_R", "SOURCE_EARLY", "SOURCE_R_DATA"],
                     note="the src endpoints take a response while no burst waits: the "
                          "abandoned bursts' responses reach the source or a later burst",
                     **text)])


def divided_config():
    """The staggered network with its clock features: the request switch's domain net divides
    its port by four (internal_divider, a counter and a gate). The response switch's domain net2 gates
    its port by the enable port clk_en_net2 (edge_qualified, the bench pulses it every third net2
    cycle). Endpoint ports stay on the direct domains. The tape completes every transaction
    unchanged. The faults tie a divider gate's enable low (the domain never runs) or the
    divider counter's next to itself (the count never reaches zero)."""
    base = staggered_config()
    cfg = base["dut"]["config"]
    cfg["test_ports"] = [{"name": "test"}]
    cfg["enable_ports"] = [{"name": "clk_en_net2"}]
    cfg["clock_domains"].append({"name": "net2", "clock_port": "clk_net2", "reset_port": "rst_net_n"})
    for d in cfg["clock_domains"]:
        d["test_port"] = "test"
        if d["name"] == "net":
            d.update(clock_type="internal_divider", ratio=4)
        if d["name"] == "net2":
            d.update(clock_type="edge_qualified", enable_port="clk_en_net2")
    for n in cfg["topology"]["response"]["nodes"]:
        n["clock_domain"] = "net2"

    def stuck(domain):
        """Text fault: the enable of the divider gate on the domain's clock port (the one gate
        whose clock_in is the port) is low."""
        def subs(rtl, _map):
            alias = re.findall(rf"^assign (nl_internal_v\d+) = clk_{domain};$", rtl, re.M)
            gates = re.findall(rf"^nocloom_clock_gate \w+ \(\.clock_in\({alias[0]}\), "
                               rf"\.enable\((nl_internal_v\d+)\), ", rtl, re.M) if alias else []
            if len(gates) != 1:
                return [(r"\Zanchor-not-found", "", 1)]
            return [(rf"^(nocloom_clock_gate \w+ \(\.clock_in\({alias[0]}\), )"
                     rf"\.enable\({gates[0]}\), ", rf"\1.enable(1'b0), ", 1)]
        return subs

    def counter_stuck(domain):
        """Text fault: the divider counter of the domain holds its value (its next is itself),
        so the enable never opens."""
        def subs(rtl, _map):
            alias = re.findall(rf"^assign (nl_internal_v\d+) = clk_{domain};$", rtl, re.M)
            gates = re.findall(rf"^nocloom_clock_gate \w+ \(\.clock_in\({alias[0]}\), "
                               rf"\.enable\((nl_internal_v\d+)\), ", rtl, re.M) if alias else []
            selects = (re.findall(rf"^assign (nl_internal_v\d+) = \({gates[0]} \? .*;$", rtl, re.M)
                       if gates else [])
            loads = [l for s in selects
                     for l in re.findall(rf"^    (nl_internal_v\d+) <= {s};$", rtl, re.M)]
            if len(loads) != 1:
                return [(r"\Zanchor-not-found", "", 1)]
            return [(rf"^    {loads[0]} <= {selects[0]};$", f"    {loads[0]} <= {loads[0]};", 1)]
        return subs

    text = dict(op="text", contract="AXI.CLOCK.GATE")
    hang = ["TAPE.DRAIN_TIMEOUT:GENERATION_0"]
    return dict(base, label="staggered-divided",
            clocks=dict(base["clocks"], net2=dict(period=11.0, phase=0.7)),
            enables={"clk_en_net2": dict(clock="net2", ratio=3)}, faults=[
        dict(id="divider_gate_stuck", subs=stuck("net"),
             expect=hang, note="the divider gate of net never opens: its switches never run",
             **text),
        dict(id="divider_counter_stuck", subs=counter_stuck("net"),
             expect=hang, note="the divider counter of net holds: its enable never opens",
             **text),
        dict(id="enable_stuck", subs=stuck("net2"),
             expect=hang, note="the enable gate of net2 never opens: its switches never run",
             **text)])


def gated_config():
    """The staggered network with every domain gated under the test port test (each switch, and
    each endpoint's protocol and transport sides): the same tape with the test port low, then
    high (every clock passes; the event log must not change). Every attachment has a pipe at its
    switch in net, past the crossing from src and before the crossing to tgt (each mode on each
    side), and every endpoint pipe kind is set on some endpoint (each mode on some kind). The
    faults break a gate's enable or idle, on every gate or on the endpoint gates, drop the pipe
    slots from a gate's idle, and clock each endpoint pipe kind from another domain."""
    base = staggered_config()
    cfg = base["dut"]["config"]
    del cfg["clock_gating"]
    cfg.update(test_ports=[{"name": "test"}], ungate_ports=[{"name": "scan_ug"}])
    for d in cfg["clock_domains"]:
        d.update(test_port="test", ungate_port="scan_ug")
    both, forward, backward = (dict(forward=True, backward=True), dict(forward=True),
                               dict(backward=True))
    for graph, modes in ((cfg["topology"]["request"], (both, forward, backward, both)),
                         (cfg["topology"]["response"], (forward, both, both, backward))):
        for attachment, mode in zip(graph["entries"] + graph["exits"], modes):
            attachment["pipeline"] = mode
    # Every endpoint pipe kind, each mode on some endpoint.
    for endpoint, pipes in zip(cfg["initiators"] + cfg["targets"], (
            dict(request=dict(both), packets=dict(forward), issue=True),
            dict(request=dict(backward), packets=dict(both)),
            dict(arrival=dict(backward), dispatch=True, response=dict(both), request=dict(forward)),
            dict(arrival=dict(both), response=dict(forward), request=dict(both)))):
        endpoint["pipes"] = pipes
    hang =[f"TAPE.DRAIN_TIMEOUT:GENERATION_{g}" for g in range(5)] + [
        f"TAPE.DRAIN_TIMEOUT:STALL_{d}{n}" for n in range(5, 13) for d in "RW"]
    corrupt = ["TARGET_WRITE_BYTES", "TARGET_DESCRIPTOR", "TARGET_FOOTPRINT",
               "TARGET_UNATTRIBUTED", "TARGET_ORDER", "AXI.W.LAST", "SOURCE_B_RESP",
               "SOURCE_R_DATA", "SOURCE_EXTRA_B", "SOURCE_EXTRA_R", "SOURCE_UNANSWERED"]
    text = dict(op="text", contract="AXI.CLOCK.GATE", expect=hang + corrupt)
    return dict(base, label="staggered-gated", test_lockstep=True, ungate_lockstep=True,
                scenario_args=dict(mode="staggered", stall=True), faults=[
        dict(id="wake_registered", subs=gate_fault("enable", lambda rtl, g: rf"\1 = \2;"),
             note="the enable sees a wake only through its registers, which a closed gate stops: "
                  "a word reaching a closed switch waits forever", **text),
        dict(id="idle_while_holding", subs=gate_fault("idle", lambda rtl, g: r"\1 = 1'b1;"),
             note="a switch reads idle while it holds a word: its gate closes with the word "
                  "still in the switch", **text),
        dict(id="endpoint_wake_registered",
             subs=gate_fault("enable", lambda rtl, g: rf"\1 = \2;", endpoints=True),
             note="an endpoint side's enable sees a wake (an AXI valid, a response) only through "
                  "its registers: a request or response reaching a closed side waits forever",
             **text),
        dict(id="endpoint_idle_while_holding",
             subs=gate_fault("idle", lambda rtl, g: r"\1 = 1'b1;", endpoints=True),
             note="an endpoint side reads idle while it holds a beat or a burst: its gate closes "
                  "with the beat still held", **text),
        dict(id="idle_ignores_pipes", subs=pipe_idle_ignored("pipeline/"),
             note="a gate's idle ignores its link pipe slots: it closes with a word in a pipe",
             **text),
        dict(id="entry_pipes_on_source_clock", subs=pipes_on_clock("pipeline/req/s", "clk_src"),
             note="the request entry pipes past the src to net crossing run on the src clock",
             **dict(text, contract="AXI.CDC.CROSSING")),
        dict(id="idle_ignores_endpoint_pipes", subs=pipe_idle_ignored("source/", "target/"),
             note="an endpoint side's idle ignores its pipe slots: it closes with a word in a "
                  "pipe", **text)] + [
        dict(id=f"{side}_{place}_pipes_stuck_empty",
             subs=pipes_stuck_empty(tuple(f"{side}/{e}/pipe/{p}" for e in ends for p in places)),
             note=f"the slots of the {place} pipes of every {side} endpoint never fill: a "
                  "forward slot never offers its word, a backward slot drops a stalled one",
             **dict(text, contract="AXI.PIPE.SLOTS"))
        for side, ends in (("source", STAGGERED_SOURCES), ("target", STAGGERED_TARGETS))
        for place, places in ENDPOINT_PIPE_KINDS[side]])


# The endpoint pipe kinds of the gated staggered network, each with the places of its pipes.
ENDPOINT_PIPE_KINDS = dict(source=[("request", ("aw", "w", "ar")), ("packets", ("packets/",)),
                                   ("issue", ("issue",))],
                           target=[("arrival", ("arrival",)), ("dispatch", ("dispatch",)),
                                   ("request", ("aw", "w", "ar")), ("response", ("response",))])


def pipe_idle_ignored(*prefixes):
    """Text fault: in the idle of every clock gate, each term that inverts a slot register of a
    pipe whose storage resource starts with one of prefixes reads 1."""
    return lambda rtl, mapped: idle_terms_dropped(rtl, mapped, prefixes)


def idle_terms_dropped(rtl, mapped, prefixes):
    assigns = dict(re.findall(r"^assign (nl_internal_v\d+) = (.*);$", rtl, re.M))
    net = lambda value: "nl_internal_v" + value.split("/")[1]  # noqa: E731
    slots = {net(v) for s in mapped["network"]["storage"]
             if s["resource"].startswith(prefixes) and ("/pipe/" in s["resource"]
                                                         or s["resource"].startswith("pipeline/"))
             for v in s["control"]}
    subs = []
    for g in mapped["network"].get("clock_gates", []):
        work, seen = [net(g["idle"])], set()
        while work:
            n = work.pop()
            if n in seen or n not in assigns:
                continue
            seen.add(n)
            term = re.fullmatch(r"\(~(nl_internal_v\d+)\)", assigns[n])
            if term and term[1] in slots:
                subs.append((rf"^assign {n} = \(~{term[1]}\);$", f"assign {n} = 1'b1;", 1))
            else:
                work += re.findall(r"nl_internal_v\d+", assigns[n])
    return subs or [(r"\Zanchor-not-found", "", 1)]


def pipes_stuck_empty(prefix):
    """Text fault: every slot flag (full or spare) of every pipe whose storage resource starts
    with prefix loads 0 out of reset."""
    def subs(rtl, mapped):
        flags = ["nl_internal_v" + v.split("/")[1] for s in mapped["network"]["storage"]
                 if s["resource"].startswith(prefix) for v in s["control"]]
        out = []
        for r in flags:
            lines = re.findall(rf"^    {r} <= (?!1'd0;).*;$", rtl, re.M)
            if len(lines) != 1:
                return [(r"\Zanchor-not-found", "", 1)]
            out.append((rf"^{re.escape(lines[0])}$", f"    {r} <= 1'd0;", 1))
        return out or [(r"\Zanchor-not-found", "", 1)]
    return subs


def pipes_on_clock(prefix, port):
    """Text fault: the registers of every pipe whose storage resource starts with prefix (a
    string or a tuple) move to a block clocked by the input port `port` (same reset and
    values)."""
    def subs(rtl, mapped):
        clock = re.findall(rf"^assign (nl_internal_v\d+) = {port};$", rtl, re.M)
        regs = ["nl_internal_v" + v.split("/")[1] for s in mapped["network"]["storage"]
                if s["resource"].startswith(prefix) for v in s["control"] + s["payload"]]
        if len(clock) != 1 or not regs:
            return [(r"\Zanchor-not-found", "", 1)]
        out, reset, plain = [], {}, []
        for r in regs:
            lines = [(m.start(), m[0], m[1]) for m in re.finditer(rf"^    {r} <= (.*);\n", rtl, re.M)]
            out += [(re.escape(line), "", 1) for _, line, _ in lines]
            if len(lines) == 2:
                head = re.findall(r"^always @\(posedge \w+ or negedge (\w+)\) begin$",
                                  rtl[:lines[0][0]], re.M)[-1]
                reset.setdefault(head, []).append((r, lines[0][2], lines[1][2]))
            else:
                plain.append((r, lines[0][2]))
        blocks = [f"always @(posedge {clock[0]} or negedge {n}) begin\n  if (!{n}) begin\n"
                  + "".join(f"    {r} <= {z};\n" for r, z, _ in rs) + "  end else begin\n"
                  + "".join(f"    {r} <= {d};\n" for r, _, d in rs) + "  end\nend\n"
                  for n, rs in reset.items()]
        if plain:
            blocks.append(f"always @(posedge {clock[0]}) begin\n"
                          + "".join(f"    {r} <= {d};\n" for r, d in plain) + "end\n")
        return out + [(r"^endmodule$", "".join(blocks).replace("\\", r"\\") + "endmodule", 1)]
    return subs


def gate_fault(key, replacement, endpoints=False):
    """Text fault on every clock gate of the map, or on every endpoint side's: the assignment of
    its net `key` (enable: the first operand of its last OR, without the wake; idle: a
    constant)."""
    def subs(rtl, mapped):
        out = []
        for g in mapped["network"].get("clock_gates", []):
            if endpoints and "zone" not in g:
                continue
            net = "nl_internal_v" + g[key].split("/")[1]
            wake = "nl_internal_v" + g["wake"].split("/")[1]
            pattern = (rf"^(assign {net}) = \((nl_internal_v\d+) \| {wake}\);$" if key == "enable"
                       else rf"^(assign {net}) = .*;$")
            out.append((pattern, replacement(rtl, g), 1))
        return out or [(r"\Zanchor-not-found", "", 1)]
    return subs


def config(mode, ratio=None, lockstep=False, pipes=False):
    table, periods = clocks(mode, ratio)
    _, objects = domains(mode)
    label = ((mode if not ratio else f"{mode}-{ratio.replace(':', '_')}")
             + ("-lockstep" if lockstep else "") + ("-piped" if pipes else ""))
    dut = recipe(mode, periods)
    if pipes:
        dut["params"]["pipes"] = 1
    return dict(label=label, dut=dut, clocks=table, main_clock="main", test_lockstep=lockstep,
                resets={}, max_cycles=400000, scenario_args=dict(mode=mode),
                model=dict(routes=ROUTES, error_codes=True, targets=TARGET_MODEL),
                bfms=[dict(name=s, kind="axi_master", endpoint=f"initiator:{s}",
                           clock=objects[s], lite=s == "s1") for s in SOURCES]
                + [dict(name=t, kind="axi_slave", endpoint=f"target:{t}", clock=objects[t],
                        salt=i) for i, t in enumerate(TARGETS)])


def faults(mode):
    out = [
        dict(id="target_address", component="network", op="set_driver", port="nl_t_t2_awaddr",
             **{"with": {"add": 1}}, contract="AXI.ROUTE.LOCAL_OFFSET",
             expect=["TARGET_DESCRIPTOR", "TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED",
                     "TARGET_WRITE_BYTES"]),
        dict(id="cut_count", component="network", op="set_next",
             at={"binding": "nlv/cut_count"}, **{"with": {"self": True}},
             contract="AXI.CDC.CROSSING",
             expect=["TAPE.DRAIN_TIMEOUT:PHASE", "SOURCE_UNANSWERED"]),
    ]
    if mode == "internal":
        return out
    return out + [
        dict(id="fifo_full_late", component="network", op="replace",
             at={"binding": "nlv/cut_full_constant"}, **{"with": {"add": 1}},
             contract="AXI.CDC.CROSSING",
             expect=["TARGET_WRITE_BYTES", "TARGET_DESCRIPTOR", "TARGET_FOOTPRINT",
                     "TARGET_UNATTRIBUTED", "AXI.W.LAST", "SOURCE_B_RESP", "SOURCE_R_DATA",
                     "TAPE.DRAIN_TIMEOUT:PHASE", "SOURCE_UNANSWERED"]),
        dict(id="fifo_read_slot0", component="network", op="set_next",
             at={"binding": "nlv/cut_received"},
             **{"with": {"op": "Select", "args": [{"select": {"binding": "nlv/cut_read"}},
                                                  {"select": {"binding": "nlv/cut_slot0"}},
                                                  {"self": True}]}},
             contract="AXI.CDC.CROSSING",
             expect=["TARGET_WRITE_BYTES", "TARGET_DESCRIPTOR", "TARGET_FOOTPRINT",
                     "TARGET_UNATTRIBUTED", "AXI.W.LAST", "SOURCE_B_RESP", "SOURCE_R_DATA",
                     "TAPE.DRAIN_TIMEOUT:PHASE", "SOURCE_UNANSWERED"]),
        dict(id="fifo_receive_every_cycle", component="network", op="set_next",
             at={"binding": "nlv/cut_received"},
             **{"with": {"select": {"binding": "nlv/cut_selected"}}},
             contract="AXI.CDC.CROSSING",
             expect=["TARGET_WRITE_BYTES", "TARGET_DESCRIPTOR", "TARGET_FOOTPRINT",
                     "TARGET_UNATTRIBUTED", "AXI.W.LAST", "SOURCE_B_RESP", "SOURCE_R_DATA",
                     "TAPE.DRAIN_TIMEOUT:PHASE", "SOURCE_UNANSWERED"]),
        dict(id="fifo_pop_while_stalled", component="network", op="replace",
             at={"binding": "nlv/cut_read"},
             **{"with": {"select": {"binding": "nlv/cut_nonempty"}}},
             contract="AXI.CDC.CROSSING",
             expect=["TARGET_WRITE_BYTES", "TARGET_DESCRIPTOR", "TARGET_FOOTPRINT",
                     "TARGET_UNATTRIBUTED", "AXI.W.LAST", "SOURCE_B_RESP", "SOURCE_R_DATA",
                     "TAPE.DRAIN_TIMEOUT:PHASE", "SOURCE_UNANSWERED"]),
        dict(id="reset_quiet", component="network", op="set_driver", port="nl_t_t0_awvalid",
             **{"with": {"op": "BitOr", "args": [{"self": True},
                                                 {"select": {"input": "reset_n"}}]}},
             contract="AXI.RESET.CANCEL", expect=["CONTRACT.QUIET.AW", "AXI.AW.HOLD"]),
    ]


def synchronous_config():
    """The gated staggered network with output.register_reset synchronous: every register,
    synchronizer and reset release register clears on its clock edge and every gate opens
    while its domain is in reset. The same tape, with the test port low and high. The fault
    drops the reset from every switch and endpoint gate's enable: a domain reset alone while
    its gates are closed keeps the registers they clock."""
    base = gated_config()
    cfg = json.loads(json.dumps(base["dut"]["config"]))
    cfg["output"]["register_reset"] = "synchronous"

    def reset_ignored(rtl, mapped):
        out = []
        for g in mapped["network"].get("clock_gates", []):
            net = "nl_internal_v" + g["enable"].split("/")[1]
            out.append((rf"^(assign {net}) = \((nl_internal_v\d+) \| nl_internal_v\d+\);$",
                        r"\1 = \2;", 1))
        return out or [(r"\Zanchor-not-found", "", 1)]
    fault = next(f for f in base["faults"] if f["id"] == "wake_registered")
    return dict(base, label="staggered-gated-sync", dut=dict(base["dut"], config=cfg),
                ungate_lockstep=False, faults=[
                    dict(fault, id="gate_closed_in_reset", subs=reset_ignored,
                         note="every gate's enable ignores its domain reset: a domain reset "
                              "alone while its gates are closed keeps their registers")])


if MODE == "ratios":
    # Clock gating (the default; only the staggered network sets clock_gating none): the gated
    # staggered network with its faults, its synchronous register reset copy (no faults; the test
    # port low and high), and endpoints 7:5 and the twelve network (no faults),
    # each run with the test port low and high. Link pipes on both sides of every link and at
    # every attachment: the gated staggered network, endpoints random:17 and the twelve network.
    # The divided network uses internal_divider and edge_qualified domains with ungate ports.
    # It repeats its tape with ungate high. Faults freeze a divider gate or counter.
    CONFIGS = [staggered_config(), gated_config(), synchronous_config(), divided_config()] + [
        dict(config("endpoints", r, lockstep=r == "7:5", pipes=r == "random:17"), faults=[])
        for r in ("1:1", "1:3", "3:1", "7:5", "random:5", "random:17")
    ] + [dict(config("twelve", "random:29", lockstep=True, pipes=True), faults=[])]
    # A domain reset alone with a packet in flight across its boundary (oneside_inflight.py).
    CONFIGS += oneside_inflight.configurations(peer_unmasked, response_undropped, rewire)
else:
    CONFIGS = [config(MODE)]
CASE = dict(
    name=f"graph_clocked_{MODE}",
    contracts=["AXI.ROUTE.LOCAL_OFFSET", "AXI.ERR.UNMAPPED_DECERR", "AXI.ERR.READ_ZERO",
               "AXI.RESET.CANCEL", "AXI.CDC.CROSSING"]
    + (["AXI.RESET.DOMAIN", "AXI.CLOCK.GATE", "AXI.PIPE.SLOTS"] if MODE == "ratios" else []),
    model=dict(routes=ROUTES, error_codes=True, targets=TARGET_MODEL),
    configs=CONFIGS,
    negative_runs_all=True,
    negative_faults_all=MODE == "ratios",
    max_cycles=400000,
    run_timeout=600,
    faults=CONFIGS[0]["faults"] if MODE == "ratios" else faults(MODE),
)
CASE.update({k: v for k, v in CONFIGS[0].items() if k != "label"})


def phase_traffic(t, phase):
    """One write and one read per source on this phase's routes."""
    for k, s in enumerate(SOURCES):
        target = ((k + phase) % 3 if phase < 3 else phase - 3 if phase < 6
                  else 0 if phase < 8 else k)
        beats = 1 if s == "s1" else (16 if phase == 8 else (1 if s == "s0" else 2))
        strobe = "full" if phase < 3 else ("none" if phase == 5 and s == "s2" else "sparse")
        common = dict(beats=beats, prot=k, cache=0 if s == "s1" else 2)
        t.write(s, target * 65536 + 0x1000 + k * 256, id=0 if s == "s1" else k + 1,
                strobe=strobe, **common)
        t.read(s, target * 65536 + 0x2000 + k * 256, id=0 if s == "s1" else k + 5, **common)


def quiet_all(t, on):
    for tg in TARGETS:
        for ch in ("aw", "w", "ar"):
            t.quiet(tg, ch, on)
    for s in SOURCES:
        t.quiet(s, "b", on)
        t.quiet(s, "r", on)


def common_reset(t):
    """The common reset, then a window in which nothing may arrive late."""
    t.reset(40)
    quiet_all(t, True)
    t.delay(100)
    quiet_all(t, False)


def staggered_quiet(t, on):
    for tg in STAGGERED_TARGETS:
        for ch in ("aw", "w", "ar"):
            t.quiet(tg, ch, on)


def staggered_traffic(t, generation):
    for k, s in enumerate(STAGGERED_SOURCES):
        for j, tg in enumerate(STAGGERED_TARGETS):
            base = 0x10000 * j + 0x100 * (4 * generation + 2 * k)
            t.write(s, base, beats=4, id=k, prot=k, cache=2)
            t.read(s, base + 0x80, beats=4, id=k + 2, prot=k, cache=2)


def staggered_scenario(t, stall=False):
    for tg in STAGGERED_TARGETS:
        t.ready(tg, "aw", "modulo", 3, 0)
        t.ready(tg, "w", "modulo", 2, 0)
        t.ready(tg, "ar", "modulo", 3, 0)
    for s in STAGGERED_SOURCES:
        t.ready(s, "b", "always")
        t.ready(s, "r", "modulo", 4, 0)
    # Quiet windows in src cycles: net leaves reset after about 160 (12 edges
    # of margin), tgt after about 260.
    for generation, window in enumerate((140, 230, 0)):
        t.mark("generation", generation)
        if window:
            staggered_quiet(t, True)
        t.reset(4)
        staggered_traffic(t, generation)
        if window:
            t.delay(window)
            staggered_quiet(t, False)
        t.drain(limit=20000, label=f"GENERATION_{generation}")
    # One reset port alone, the network idle; the port indexes follow the name order.
    ports = sorted(STAGGERED_RELEASE)
    for generation, domain in ((3, "net"), (4, "tgt")):
        t.mark("generation", generation)
        t.port_reset(ports.index(domain), 40)
        t.delay(300)
        staggered_traffic(t, generation)
        t.drain(limit=20000, label=f"GENERATION_{generation}")
    # rst_src_n alone while a read of s0 waits at t0 and a write of s1 at t1 (R and B withheld, the
    # requests whole at the targets); they answer once src runs again, before generation 5's own
    # traffic.
    t.mark("generation", 5)
    held = [("t0", "r", "ar"), ("t1", "b", "aw")]
    since = [t.snap(tg, ch) for tg, _, ch in held]
    for tg, ch, _ in held:
        t.gate(tg, ch, False)
    t.read("s0", 0x1880, beats=4, id=2, prot=0, cache=2)
    t.write("s1", 0x11a00, beats=4, id=1, prot=1, cache=2)
    for (tg, _, ch), mark in zip(held, since):
        t.wait(tg, ch, ">=", 1, since=mark, limit=2000, label="GENERATION_5")
    t.delay(100)
    t.port_reset(ports.index("src"), 40)
    t.delay(300)
    for tg, ch, _ in held:
        t.gate(tg, ch, True)
    t.delay(600)
    staggered_traffic(t, 5)
    t.drain(limit=20000, label="GENERATION_5")
    if stall:
        staggered_stalls(t)
    t.end()


def staggered_stalls(t):
    """Bursts of 5 to 12 beats into a stalled endpoint channel (R at the source, W at the
    target), released after the switch has gone quiet: the last words wait in the switch while
    nothing arrives, at some length with every crossing into the switch empty."""
    for beats in range(5, 13):
        t.mark("stall", beats)
        t.ready("s0", "r", "never")
        t.read("s0", 0x80 * beats, beats=beats, id=2, prot=0, cache=2)
        t.delay(300)
        t.ready("s0", "r", "modulo", 4, 0)
        t.drain(limit=20000, label=f"STALL_R{beats}")
        t.ready("t1", "w", "never")
        t.write("s1", 0x10000 + 0x80 * beats, beats=beats, id=1, prot=1, cache=2)
        t.delay(300)
        t.ready("t1", "w", "modulo", 2, 0)
        t.drain(limit=20000, label=f"STALL_W{beats}")


def scenario(t, seed, mode="internal", stall=False, **inflight):
    if mode == "inflight":
        oneside_inflight.scenario(t, seed, **inflight)
        return
    if mode == "staggered":
        staggered_scenario(t, stall)
        return
    for s in SOURCES:
        t.ready(s, "b", "always")
        t.ready(s, "r", "modulo", 5, 0)
    for tg in TARGETS:
        t.ready(tg, "aw", "modulo", 3, 0)
        t.ready(tg, "w", "modulo", 4, 0)
        t.ready(tg, "ar", "modulo", 4, 0)
    t.reset(4)
    t.delay(2)
    for phase in range(10):
        if phase == 9:
            cancel(t)
            continue
        t.mark("phase", phase)
        if phase == 0:
            # The switch nodes' clock is stopped: requests are accepted into
            # local storage and no target sees one before it runs.
            for tg in TARGETS:
                for ch in ("aw", "w", "ar"):
                    t.quiet(tg, ch, True)
            aw0 = t.snap("s0", "aw")
            w0 = t.snap("s0", "w")
        if phase == 4:
            t.set("t1", "reg0_lo", 0)
            t.set("t1", "reg0_hi", 0x10000)
            t.set("t1", "reg0_w", 2)
            t.set("t1", "reg0_r", 2)
        if phase in (0, 1):
            # AXI4-Lite W before AW, then AW before W.
            t.gate("s1", "aw" if phase == 0 else "w", False)
        if phase == 6:
            t.ready("s0", "r", "never")
        if phase == 7:
            t.ready("s0", "b", "never")
        held = t.snap("s0", "r" if phase == 6 else "b")
        phase_traffic(t, phase)
        if phase in (0, 1):
            t.delay(9)
            t.gate("s1", "aw" if phase == 0 else "w", True)
        if phase == 0:
            t.delay(40)
            t.expect_now("s0", "aw", "==", 1, "STOPPED_LOCAL_ACCEPT", since=aw0)
            t.expect_now("s0", "w", ">", 0, "STOPPED_LOCAL_ACCEPT", since=w0)
            t.delay(20)
            for tg in TARGETS:
                for ch in ("aw", "w", "ar"):
                    t.quiet(tg, ch, False)
        if phase in (6, 7):
            t.delay(2000)
            ch = "r" if phase == 6 else "b"
            t.expect_now("s0", ch, "==", 0, "HOLD_SOURCE", since=held)
            t.expect_now("s0", "rvalid" if phase == 6 else "bvalid", "==", 1, "HOLD_SOURCE")
            t.ready("s0", ch, "modulo" if ch == "r" else "always", 5 if ch == "r" else 0, 0)
        t.drain(limit=60000, label="PHASE")
        if phase == 4:
            t.set("t1", "reg0_hi", 0)
        common_reset(t)
    t.end()


def cancel(t):
    """Common resets in the middle of a 16-beat write packet at t0 and of a
    16-beat read response at s0: nothing replays after them."""
    t.mark("phase", 9)
    w, wlast = t.snap("t0", "w"), t.snap("t0", "wlast")
    t.write("s0", 0x1000, beats=16, id=1, prot=0, cache=2)
    t.wait("t0", "w", ">=", 1, since=w, limit=30000, label="RESET_MID_PACKET")
    t.expect_now("t0", "wlast", "==", 0, "RESET_MID_PACKET", since=wlast)
    common_reset(t)
    r = t.snap("s0", "r")
    t.read("s0", 0x2000, beats=16, id=5, prot=0, cache=2)
    t.wait("s0", "r", ">=", 1, since=r, limit=30000, label="RESET_MID_RESPONSE")
    common_reset(t)
