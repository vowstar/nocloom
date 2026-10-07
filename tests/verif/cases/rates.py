"""Beat rates through the public CLI, one registration per mode
(--param mode=...).

One AXI4 source s0 offers back-to-back full-width INCR bursts on both
directions (W beats without waiting for AW) and always takes B and R; the
target t0 accepts every address and W beat at once, answers B the cycle
after a burst's last W beat and streams R beats back to back. The offline
checker judges every byte, strobe, response and footprint; the rates come
from the cycle stamps of the handshakes:

  within   per W or R burst at one port: beats over the cycles from its
           first to its last beat, the lowest of the phase;
  overall  per phase at one port: handshakes over the cycles from the
           first to the last;
  span     per source burst: its target beats over the cycles from the
           first to the last of them (all pieces or packets).

Width modes: s0 reaches t0 at a width of its own (t1, never addressed,
selects per-target widths). The gate is within >= 0.95 at the narrow side
(the target when the source is wider, the source when the target is wider,
both at equal widths), for Modifiable (AxCACHE 3) and Non-modifiable
(AxCACHE 0) bursts; on the source side only R, as a source W beat may wait
while reads and writes fill the source's shared bursts in flight. Modes
that cut bursts also gate the target span.
*_deep modes put two clock crossings each way between source and target;
bounded_stall holds RREADY low at random (data only, no gate).

Graph modes: one AXI4 source and target over a one-node graph. The source
keeps two bursts in flight (reads and writes share them) in the single and
wide modes, sixteen in deep. Read and write packets share each link, so
n-beat bursts pass at most n/(n+1) beats per cycle: within >= min(0.95,
n/(n+1)) at every point but the source W face (reported only: a W beat may
wait while the shared bursts in flight are full); 256-beat bursts overall >=
0.95; with several bursts in flight 16-beat bursts overall >=
0.9 and a phase of one-beat bursts follows; deep (sixteen in flight) gates
that phase (AW, AR, W and R >= 0.45 per cycle) and a fourth phase holding
every B until all sixteen writes are in (target and source B >= 0.95). The
target holds one in-flight pool shared by both directions, so at capacity
one (single and wide) a burst of one direction makes queued bursts of the
other wait and the per-direction spans interleave: those modes gate the
combined two-direction beat rate of each port instead (>= 0.8 for 16-beat
bursts, >= 0.95 for 256-beat ones)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

# mode: (source bits, t0 bits, t1 bits or None, network bits or None, len bits,
#        target span gate or None, switch nodes per direction (0: none),
#        RREADY stall)
WIDTH = {
    'perwidth32': (32, 32, 64, None, 8, None, 0, False),
    'perwidth64': (64, 64, 32, 64, 8, None, 0, False),
    'perwidth_up': (32, 128, 64, None, 8, None, 0, False),
    'perwidth_cut': (1024, 32, 1024, None, 8, None, 0, False),
    'perwidth_cut128': (128, 32, 64, None, 4, None, 0, False),
    'downsize64': (64, 32, None, None, 8, None, 0, False),
    'downsize128': (128, 32, None, None, 4, None, 0, False),
    'downsize1024': (1024, 32, None, None, 8, None, 0, False),
    'bounded32': (32, 32, None, None, 8, None, 0, False),
    'bounded_perwidth': (32, 32, 64, None, 8, None, 0, False),
    'bounded_down64': (64, 32, None, None, 8, 0.9, 0, False),
    'bounded_cut': (128, 32, 64, None, 8, 0.9, 0, False),
    # Deep modes: the per-crossing receiver register widens a burst's target
    # beat window by about one cycle per crossing (the register loads in the
    # drain cycle, so throughput is intact), so the span floor is 0.94.
    'cut_deep': (1024, 32, 1024, None, 8, 0.94, 2, False),
    'down_deep': (128, 32, None, None, 8, 0.94, 2, False),
    'bounded_deep': (128, 32, 64, None, 8, 0.94, 2, False),
    'bounded_down_deep': (64, 32, None, None, 8, 0.94, 2, False),
    'bounded_stall': (128, 32, 64, None, 8, None, 2, True),
}
# graph mode: (data bits, bursts in flight at the target; the source shares
# its own bursts in flight between reads and writes, at least two)
GRAPH = {'graph_single': (32, 1), 'graph_multi': (32, 4), 'graph_wide': (64, 1),
         'graph_deep': (32, 16)}

PARAMS = globals().get("PARAMS", {})
MODE = PARAMS.get("mode", "perwidth32")
if MODE not in WIDTH and MODE not in GRAPH:
    raise ValueError(f"unknown rate mode {MODE}")


def width_configuration():
    source_bits, target_bits, other_bits, network_bits, len_bits, _, hops, _ = WIDTH[MODE]
    transport = {'data_width': network_bits} if network_bits else {}
    targets = [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4',
                'data_width': target_bits, 'mappings': [{'base': '0x0', 'size': '0x100000'}]}]
    if other_bits:
        targets.append({'name': 't1', 'clock_domain': 'main', 'protocol': 'axi4',
                        'data_width': other_bits,
                        'mappings': [{'base': '0x100000', 'size': '0x1000'}]})
    cfg = {'schema_version': 10, 'name': 'wrate', 'address_width': 32,
           'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
           'initiators': [{'name': 's0', 'clock_domain': 'main', 'protocol': 'axi4',
                           'data_width': source_bits}],
           'targets': targets, 'transport': transport, 'connectivity': 'all',
           'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    if hops:
        # Switch nodes in a domain of their own and the targets in a third
        # (all clocks of one period) put two crossings each way between s0
        # and t0: a long network round trip.
        cfg['clock_domains'] += [{'name': 'net', 'clock_port': 'clk_net', 'reset_port': 'reset_n', 'test_port': 'test'},
                                 {'name': 'far', 'clock_port': 'clk_far', 'reset_port': 'reset_n', 'test_port': 'test'}]
        for t in targets:
            t['clock_domain'] = 'far'

        def chain(prefix, entries, exits):
            names = [f'{prefix}{k}' for k in range(hops)]
            return {'nodes': [{'name': x, 'clock_domain': 'net'} for x in names],
                    'links': [{'from': a, 'to': b} for a, b in zip(names, names[1:])],
                    'entries': [{'endpoint': e, 'node': names[0]} for e in entries],
                    'exits': [{'endpoint': e, 'node': names[-1]} for e in exits]}
        ends = [t['name'] for t in targets]
        cfg['topology'] = {'request': chain('q', ['s0'], ends),
                           'response': chain('p', ends, ['s0'])}
    return endpoint_ports(cfg, id_width=2, len_width=len_bits)


def graph_configuration():
    data, capacity = GRAPH[MODE]
    source = {'name': 's0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': data}
    target = {'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': data,
              'mappings': [{'base': '0x0', 'size': '0x100000'}]}
    if capacity > 1:
        source['max_outstanding'] = capacity
        target['max_outstanding'] = capacity
    else:
        # Reads and writes share the source's bursts in flight: two keep a
        # write and a read burst in flight together, as the bench offers both.
        source['max_outstanding'] = 2
    return endpoint_ports({
        'schema_version': 10, 'name': 'rate', 'address_width': 32,
        'arbitration': {'policy': 'round_robin'},
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
        'initiators': [source], 'targets': [target], 'connectivity': 'all',
        'transport': {'max_outstanding': max(capacity, 2), 'data_width': data,
                      'transport_word_bytes': data // 8},
        'topology': {
            'request': {'nodes': [{'name': 'q0'}], 'links': [],
                        'entries': [{'endpoint': 's0', 'node': 'q0'}],
                        'exits': [{'endpoint': 't0', 'node': 'q0'}]},
            'response': {'nodes': [{'name': 'p0'}], 'links': [],
                         'entries': [{'endpoint': 't0', 'node': 'p0'}],
                         'exits': [{'endpoint': 's0', 'node': 'p0'}]}},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


CONFIG = width_configuration() if MODE in WIDTH else graph_configuration()
FAR = "far" if MODE in WIDTH and WIDTH[MODE][6] else "main"
T1 = MODE in WIDTH and WIDTH[MODE][2]

CASE = dict(
    name=f"rate_{MODE}",
    contracts=["AXI.RATE.BEATS", "AXI.WIDTH.DOWNSIZE.PIECES", "AXI.ROUTE.LOCAL_OFFSET"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
          dict(name="t0", kind="axi_slave", endpoint="target:t0", clock=FAR, salt=2)]
    + ([dict(name="t1", kind="axi_slave", endpoint="target:t1", clock=FAR, salt=3)]
       if T1 else []),
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, cache="amba", prot=True)),
    max_cycles=400000,
    run_timeout=600,
    # The source W face takes a beat only every other cycle (legal AXI, half
    # the rate): the value behind WREADY, which the network's own handshake
    # also uses, is gated. The within-burst gate must name it.
    faults=[dict(id="s0_wready_half", op="text", contract="AXI.RATE.BEATS",
                 subs=[(r"^assign (nl_internal_v\d+) = ([^;]+);"
                        r"(?=[\s\S]*^assign nl_i_s0_wready = \1;$)",
                        "reg nlv_half = 1'b0;\nalways @(posedge clk_main) nlv_half <= ~nlv_half;\n"
                        r"assign \g<1> = (\g<2>) & nlv_half;", 1)],
                 expect=["RATE_LOW"])],
)


def phase(t, k, beats, bursts, wbase, rbase, cache, size, hold_b=False):
    """One phase: bursts writes and reads of beats beats, back to back."""
    t.mark("phase", k)
    if hold_b:
        t.gate("t0", "b", False)
        applied = t.snap("t0", "applied")
    step = beats << size
    for b in range(bursts):
        t.write("s0", wbase + b * step, beats=beats, size=size, id=1, cache=cache, prot=2)
    for b in range(bursts):
        t.read("s0", rbase + b * step, beats=beats, size=size, id=2, cache=cache, prot=2)
    if hold_b:
        t.wait("t0", "applied", "==", bursts, since=applied, limit=100000, label="HELD_WRITES")
        t.gate("t0", "b", True)
    t.drain(limit=200000, label=f"PHASE_{k}")
    t.mark("phase_end", k)


def gate(t, k, port, ch, minimum, per="burst", label=None):
    t.check(kind="rate", port=port, ch=ch, per=per, min=minimum, after_mark="phase",
            mark_value=k, until_mark="phase_end", until_value=k,
            label=label or f"{port}.{ch} phase {k}")


def scenario(t, seed):
    for ch in ("aw", "w", "ar") if T1 else ():
        t.quiet("t1", ch, True)
    t.reset(4)
    t.delay(2)
    if MODE in WIDTH:
        width_phases(t)
    else:
        graph_phases(t)
    t.end()


def width_phases(t):
    source_bits, target_bits, _, _, len_bits, span, _, stall = WIDTH[MODE]
    sb, tb = source_bits // 8, target_bits // 8
    size = sb.bit_length() - 1
    long = min(1 << len_bits, 4096 // sb)
    if stall:
        # RREADY low or high at random: a burst's last gathered beat waits
        # while the next packet's words arrive (data checks only).
        t.ready("s0", "r", "random", 50)
    for k, (beats, bursts, cache, wbase, rbase) in enumerate((
            (16, 4, 3, 0x0, 0x80000), (long, 2, 3, 0x10000, 0x90000),
            (16, 4, 0, 0x20000, 0xa0000), (long, 2, 0, 0x30000, 0xb0000)), 1):
        phase(t, k, beats, bursts, wbase, rbase, cache, size)
        if stall:
            continue
        if sb >= tb:
            gate(t, k, "t0", "w", 0.95)
            gate(t, k, "t0", "r", 0.95)
            if span:
                for d in ("w", "r"):
                    t.check(kind="span", port="s0", dir=d, min=span, after_mark="phase",
                            mark_value=k, until_mark="phase_end", until_value=k)
        if sb <= tb:
            # The source W rate is reported only: a W beat may wait in the
            # source while its reads and writes fill its shared bursts in
            # flight.
            gate(t, k, "s0", "w", 0.0)
            gate(t, k, "s0", "r", 0.95)


def graph_phases(t):
    data, capacity = GRAPH[MODE]
    size = (data // 8).bit_length() - 1
    # phases: (beats, bursts, wbase, rbase, per-direction overall floor, aw/ar floor,
    #          hold Bs, b floor, combined two-direction overall floor).
    # A one-pool target serializes opposite-direction bursts: the per-direction
    # spans interleave, so there the per-direction overall floors are off and the
    # phases gate the combined rate instead (near 0.66 per direction, near 1.0
    # combined).
    phases = [(16, 8, 0x0, 0x80000, 0.9 if capacity > 1 else 0.0, 0.0, False, 0.0,
               0.8 if capacity == 1 else 0.0),
              (256, 2, 0x40000, 0xc0000, 0.95 if capacity > 1 else 0.0, 0.0, False, 0.0,
               0.95 if capacity == 1 else 0.0)]
    if capacity > 1:
        phases.append((1, 64, 0x20000, 0xa0000, 0.45 if capacity >= 16 else 0.0,
                       0.45 if capacity >= 16 else 0.0, False, 0.0, 0.0))
    if capacity >= 16:
        phases.append((1, 16, 0x30000, 0xb0000, 0.0, 0.0, True, 0.95, 0.0))
    for k, (beats, bursts, wbase, rbase, floor, ar_floor, hold, b_floor, agg) in \
            enumerate(phases, 1):
        phase(t, k, beats, bursts, wbase, rbase, 3, size, hold_b=hold)
        within = min(0.95, beats / (beats + 1))
        for port, ch in (("t0", "w"), ("t0", "r"), ("s0", "w"), ("s0", "r")):
            # Reported only at the source W face: a W beat may wait while the
            # source's reads and writes fill its shared bursts in flight.
            gate(t, k, port, ch, 0.0 if (port, ch) == ("s0", "w") else within)
            if floor:
                gate(t, k, port, ch, floor, per="window", label=f"{port}.{ch} overall {k}")
        if agg:
            for port in ("t0", "s0"):
                t.check(kind="rate_all", port=port, min=agg, after_mark="phase",
                        mark_value=k, until_mark="phase_end", until_value=k,
                        label=f"{port}.w+r overall {k}")
        if ar_floor:
            for ch in ("aw", "ar"):
                gate(t, k, "s0", ch, ar_floor, per="window", label=f"s0.{ch} overall {k}")
        if b_floor:
            for port in ("t0", "s0"):
                gate(t, k, port, "b", b_floor, per="window", label=f"{port}.b overall {k}")
