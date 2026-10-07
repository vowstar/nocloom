"""Response runs gather target words on a wide link and keep per-beat codes.

On the 32-byte link of the first configuration, s1 sees no beat of an INCR read
until t1 answered the word. A target word that answers otherwise than the
words gathered before it ends their gather, and they leave first. Wide and narrow response links,
interleaved IDs, WRAP containers and dedicated bus targets check data, LAST and
exact per-beat codes. The negative control claims the same gather on a 4-byte
link, where no word gathers.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402
from nlverif import config as nlconfig  # noqa: E402

T0, T1, T2 = 0x0000, 0x8000, 0x10000


def config(word):
    return endpoint_ports({
        'schema_version': 10, 'name': 'response_runs', 'address_width': 32,
        'arbitration': {'policy': 'round_robin'},
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [{'name': 's0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 128},
                       {'name': 's1', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32}],
        'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 256,
                     'attributes': 'amba', 'mappings': [{'base': hex(T0), 'size': '0x8000'}]},
                    {'name': 't1', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'attributes': 'amba', 'mappings': [{'base': hex(T1), 'size': '0x8000'}]},
                    # No segment marker: read-only without an ID, so only the WRAP top ends a run.
                    {'name': 't2', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'attributes': 'amba', 'access': 'read_only', 'id_width': 0,
                     'mappings': [{'base': hex(T2), 'size': '0x8000'}]}],
        'connectivity': 'all',
        'transport': {'transport_word_bytes': word},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


def interleaved():
    return endpoint_ports({
        'schema_version': 10, 'name': 'response_runs_ilv', 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [{'name': 's1', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                        'max_outstanding': 4}],
        'targets': [{'name': 't1', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'attributes': 'amba', 'max_outstanding': 4, 'id_width': 8,
                     'mappings': [{'base': hex(T1), 'size': '0x8000'}]}],
        'connectivity': 'all',
        'transport': {'transport_word_bytes': 16},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


def narrow_beats():
    return endpoint_ports({
        'schema_version': 10, 'name': 'response_runs_beats', 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [{'name': 's1', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 64}],
        'targets': [{'name': 't1', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 64,
                     'attributes': 'amba', 'mappings': [{'base': hex(T1), 'size': '0x8000'}]}],
        'connectivity': 'all',
        'transport': {'transport_word_bytes': 32},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


def wider_source():
    """A word narrower than the sources' on both faces, so the source's own link gathers."""
    return endpoint_ports({
        'schema_version': 10, 'name': 'response_runs_clean', 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [{'name': 's0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 128},
                       {'name': 's1', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 128}],
        'targets': [{'name': 't1', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 128,
                     'attributes': 'amba', 'mappings': [{'base': hex(T1), 'size': '0x8000'}]}],
        'connectivity': 'all',
        'transport': {'data_width': 32, 'transport_word_bytes': 8},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


def bus_target(protocol, bits=32):
    """A dedicated target narrower than the transport word preserves per-beat errors."""
    return endpoint_ports({
        'schema_version': 10, 'name': 'response_runs_bus', 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [{'name': 's1', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32}],
        'targets': [{'name': 't1', 'clock_domain': 'main', 'protocol': protocol, 'data_width': bits,
                     'attributes': 'amba', 'mappings': [{'base': hex(T1), 'size': '0x8000'}]}],
        'connectivity': 'all',
        'transport': {'transport_word_bytes': 32},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


def chained():
    """Two widening response links, 8 then 32 bytes, so a flushed run reaches a second gather."""
    nodes = lambda *names: [{'name': n, 'clock_domain': 'main', 'transport_word_bytes': b}
                            for n, b in names]
    return endpoint_ports({
        'schema_version': 10, 'name': 'response_runs_chain', 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [{'name': 's1', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32}],
        'targets': [{'name': 't1', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'attributes': 'amba', 'mappings': [{'base': hex(T1), 'size': '0x8000'}]}],
        'connectivity': 'all',
        'topology': {
            'request': {'nodes': nodes(('q', 32)), 'links': [],
                        'entries': [{'endpoint': 's1', 'node': 'q'}],
                        'exits': [{'endpoint': 't1', 'node': 'q'}]},
            'response': {'nodes': nodes(('near', 8), ('far', 32)),
                         'links': [{'from': 'near', 'to': 'far'}],
                         'entries': [{'endpoint': 't1', 'node': 'near'}],
                         'exits': [{'endpoint': 's1', 'node': 'far'}]}},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)


def bus_recipe(protocol, bits=32):
    cfg = bus_target(protocol, bits)
    slave = {"apb4": "apb_slave", "ahb_v3": "ahb_slave", "axi4_lite": "axi_slave"}[protocol]
    return dict(dut=dict(source="cli", config=cfg),
                bfms=[dict(name="s1", kind="axi_master", endpoint="initiator:s1"),
                      dict(name="t1", kind=slave, endpoint="target:t1", salt=0)],
                model=dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg),
                           targets=nlconfig.targets(cfg)))


def clean_lanes(checker, events, sources, targets):
    """No word of an earlier run in a later one: s0's narrow reads in the high half of its bus show
    zero below the half, whatever the gather on its link held there before."""
    out = []
    for source in sources:
        if source.port == "s0" and source.dir == "r" and source.size == 2:
            for data, _, _ in source.beats:
                if data & ((1 << 64) - 1):
                    out.append(dict(rule="ORACLE_STALE_LANE", port=source.port, tag=source.tag,
                                    data=hex(data)))
    return out


def clipped_words(capacity, bits, effects_target=None):
    cfg = interleaved()
    cfg['transport']['transport_word_bytes'] = bits // 8
    for endpoint in cfg['initiators'] + cfg['targets']:
        endpoint['max_outstanding'] = capacity
        endpoint['data_width'] = bits
    if effects_target == "other":
        cfg['targets'].append(dict(cfg['targets'][0], name='unused',
                                   mappings=[dict(base='0x10000', size='0x8000')]))
    if effects_target:
        cfg['targets'][-1 if effects_target == "other" else 0]['read_side_effects'] = True
    return recipe(cfg, ("s1",), tuple(t['name'] for t in cfg['targets']))


def clipped_lanes(checker, events, sources, targets, single=False):
    """Replayed packed INCR words contain only the burst's requested bytes."""
    out = []
    for source in sources:
        if source.dir != 'r' or source.burst != 1 or (source.length == 1 and not single):
            continue
        step = 1 << source.size
        base = source.addr // step * step
        container = source.length * step
        wrap_base = base // container * container
        covered = set()
        addresses = []
        for k in range(source.length):
            addr = (wrap_base + (base - wrap_base + k * step) % container
                    if source.burst == 2 else base + k * step)
            addresses.append(addr)
            covered.update(range(source.addr if k == 0 else addr, addr + step))
        word_bytes = checker.bench['bfms'][source.port]['data_bits'] // 8
        for addr, (data, _, _) in zip(addresses, source.beats):
            word = addr // word_bytes * word_bytes
            for lane in range(word_bytes):
                if word + lane not in covered and (data >> (8 * lane)) & 255:
                    out.append(dict(rule='ORACLE_READ_UNUSED_BYTE', port=source.port,
                                    tag=source.tag, address=hex(word + lane)))
    return out


def fragment_words(capacity, bits):
    result = clipped_words(capacity, bits)
    result['dut']['config']['transport']['transport_word_bytes'] = 4
    result['dut']['config']['transport']['data_width'] = 32
    return result


def fragment_lanes(checker, events, sources, targets):
    return clipped_lanes(checker, events, sources, targets, single=True)


def first_lanes(checker, events, sources, targets):
    """The first INCR response clears bytes below its requested start."""
    out = []
    for source in sources:
        if source.dir != 'r' or source.burst != 1 or not source.beats:
            continue
        word_bytes = checker.bench['bfms'][source.port]['data_bits'] // 8
        data, result, _ = source.beats[0]
        if result == 0 and data & ((1 << (8 * (source.addr % word_bytes))) - 1):
            out.append(dict(rule='ORACLE_READ_FIRST_BYTE', port=source.port, tag=source.tag))
    return out


def first_bus(protocol, capacity):
    result = bus_recipe(protocol)
    result['dut']['config']['initiators'][0]['max_outstanding'] = capacity
    return result


def recipe(cfg, masters, slaves):
    return dict(dut=dict(source="cli", config=cfg),
                bfms=[dict(name=n, kind="axi_master", endpoint=f"initiator:{n}") for n in masters]
                + [dict(name=n, kind="axi_slave", endpoint=f"target:{n}", salt=k)
                   for k, n in enumerate(slaves)],
                model=dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg),
                           targets=nlconfig.targets(cfg, cache="amba", prot=True)))


BOTH = recipe(config(32), ("s0", "s1"), ("t0", "t1", "t2"))
CASE = dict(
    name="response_runs",
    contracts=["AXI.TARGET.R_INTERLEAVE", "AXI.ROUTE.LOCAL_OFFSET", "AXI.OUTLET.FRAMING"],
    **BOTH,
    max_cycles=60000,
    scenario_args=dict(shape="runs"),
    faults=[],
    configs=[
        dict(label=""),
        dict(label="interleaved", scenario_args=dict(shape="interleaved"),
             **recipe(interleaved(), ("s1",), ("t1",))),
        dict(label="narrow_beats", scenario_args=dict(shape="narrow_beats"),
             **recipe(narrow_beats(), ("s1",), ("t1",))),
        dict(label="clean_lanes", scenario_args=dict(shape="clean_lanes"), oracle=clean_lanes,
             **recipe(wider_source(), ("s0", "s1"), ("t1",))),
        *[dict(label=f"clipped_{bits}_{capacity}", oracle=clipped_lanes,
               scenario_args=dict(shape="clipped", word_bytes=bits // 8),
               **clipped_words(capacity, bits)) for bits in (32, 64) for capacity in (1, 4)],
        *[dict(label=f"clipped_effects_{target}_{bits}_{capacity}", oracle=clipped_lanes,
               scenario_args=dict(shape="clipped", word_bytes=bits // 8),
               **clipped_words(capacity, bits, target))
          for target in ("addressed", "other") for bits in (32, 64) for capacity in (1, 4)],
        *[dict(label=f"fragments_{bits}_{capacity}", oracle=fragment_lanes,
               scenario_args=dict(shape="fragments", word_bytes=bits // 8),
               **fragment_words(capacity, bits)) for bits in (64, 128) for capacity in (1, 4)],
        *[dict(label=f"first_bus_{protocol}_{capacity}", oracle=first_lanes,
               scenario_args=dict(shape="first_bus", capacity=capacity),
               **first_bus(protocol, capacity))
          for protocol in ("apb4", "axi4_lite", "ahb_v3") for capacity in (1, 4)],
        dict(label="chained", scenario_args=dict(shape="chained"),
             **recipe(chained(), ("s1",), ("t1",))),
        dict(label="bus_apb", scenario_args=dict(shape="bus_apb"), **bus_recipe("apb4")),
        *[dict(label=f"bus_apb{bits}", scenario_args=dict(shape="bus_apb", word_bytes=bits // 8),
                **bus_recipe("apb4", bits)) for bits in (8, 16)],
        dict(label="bus_lite", scenario_args=dict(shape="bus_lite"), **bus_recipe("axi4_lite")),
        dict(label="bus_ahb", scenario_args=dict(shape="bus_ahb"), **bus_recipe("ahb_v3")),
        dict(label="narrow", negative_run=True, scenario_args=dict(shape="narrow"),
             **recipe(config(4), ("s0", "s1"), ("t0", "t1", "t2")),
             faults=[dict(id="gather_claim", op="scenario", params=dict(claim="runs"),
                          expect=["TAPE.EXPECT:RUN_HELD"], contract="AXI.OUTLET.FRAMING",
                          note="a word that does not gather shows the beat before the run ends")]),
    ],
    negative_faults_all=True,
)


def held(t, expected):
    """With runs, s1 sees no beat of an INCR read while t1 answers the first half of the word."""
    last = t.snap("s1", "r")
    answered = t.snap("t1", "r")
    t.read("s1", T1 + 0x4000, beats=8, id=0, cache=3)
    t.wait("t1", "r", ">=", 5, limit=500, label="RUN_STARTED", since=answered)
    if expected == "runs":
        t.expect_now("s1", "r", "==", 0, "RUN_HELD", since=last)
    else:
        t.expect_now("s1", "r", ">", 0, "BEATS_ALL_SEEN", since=last)
    t.wait("s1", "rlast", ">=", 1, limit=500, label="RUN_DONE", since=t.snap("s1", "rlast"))


def wrap_top(t):
    """t2 stops after a WRAP's container top, below the word boundary: the run has left."""
    beats = t.snap("s1", "r")
    t.set("t2", "r_stop", 4)  # three beats: 4, 8 and 12 of a 16-byte container
    t.read("s1", T2 + 0x4004, beats=4, burst="wrap", id=0, cache=3)
    t.wait("s1", "r", ">=", 3, limit=200, label="WRAP_TOP_ENDS_RUN", since=beats)
    t.set("t2", "r_stop", 0)
    t.wait("s1", "r", ">=", 4, limit=200, label="WRAP_DONE", since=beats)


def scenario(t, seed, shape="runs", claim=None, word_bytes=4, capacity=1):
    t.reset(4)
    t.delay(2)
    if shape == "first_bus":
        for size in range(3):
            for count in (1, 3, 6):
                before = t.snap("s1", "ar")
                t.ready("s1", "r", "never" if capacity > 1 else "always")
                for offset in (1, 2, 3):
                    t.read("s1", T1 + 0x400 + 0x40 * offset + offset, beats=count, size=size,
                           id=offset, cache=0)
                if capacity > 1:
                    t.wait("s1", "ar", ">=", 2, since=before, limit=1000,
                           label="FIRST_READS_ACCEPTED")
                t.ready("s1", "r", "modulo", 3, 0)
                t.drain(limit=2000, label="first_reads")
        t.end()
        return
    if shape in ("clipped", "fragments"):
        for size in range(word_bytes.bit_length() - (shape == "clipped")):
            for offset in range(word_bytes):
                for count in (1, 3, 6):
                    t.read("s1", T1 + 0x400 + offset, beats=count, size=size, cache=3)
                    t.drain(limit=2000, label="clipped_incr")
            for count in (2, 4, 8):
                for start in range(count):
                    t.read("s1", T1 + 0x800 + (start << size), beats=count, size=size,
                           burst="wrap", cache=3)
                    t.drain(limit=2000, label="clipped_wrap")
        t.end()
        return
    if shape == "interleaved":
        t.set("t1", "r_ilv", 1)
        for k in range(16):
            t.read("s1", T1 + 0x100 * k, beats=1 << (k % 4), id=k % 4, cache=3)
        t.drain(limit=20000, label="drain")
        t.end()
        return
    if shape.startswith("bus_"):
        t.set("t1", "reg0_lo", 0x3d08)
        t.set("t1", "reg0_hi", 0x3d0c)
        for reg in ("reg0_r", "reg0_w") if shape == "bus_lite" else ("reg0_c",):
            t.set("t1", reg, 2 if shape == "bus_lite" else 1)
        t.read("s1", T1 + 0x3d00, beats=16, id=0, cache=0,
               beat_resp={str(i): 2 if i == 2 else 0 for i in range(16)})
        t.drain(limit=2000, label="drain")
        # The target marks each run's end: FIXED beats, beats narrower than its word, a WRAP's top.
        t.read("s1", T1 + 0x1000, beats=4, size=2, burst="fixed", id=1, cache=3)
        t.read("s1", T1 + 0x1100, beats=4, size=1, id=2, cache=0)
        for beats in (2, 4, 8):
            for start in range(beats):
                t.read("s1", T1 + 0x2000 + 0x40 * beats + start * 4, beats=beats, burst="wrap",
                       id=start % 4, cache=3)
        if shape == "bus_ahb":
            t.set("t1", "reg0_lo", 0x3e01)
            t.set("t1", "reg0_hi", 0x3e02)
            t.read("s1", T1 + 0x3e01, beats=3, size=0, cache=2)
            t.drain(limit=2000, label="AHB_SUFFIX_READ_BEATS")
        # Distinct physical APB words must not share a failed read response code.
        if word_bytes < 4:
            t.set("t1", "reg0_lo", 0x3e00)
            t.set("t1", "reg0_hi", 0x3e00 + word_bytes)
            size = word_bytes.bit_length() - 1
            for burst in ("incr", "wrap"):
                for start in (0, word_bytes):
                    t.read("s1", T1 + 0x3e00 + start, beats=2, size=size,
                           burst=burst, cache=3)
                    t.drain(limit=2000, label="PHYSICAL_APB_READ_BEATS")
        t.drain(limit=8000, label="runs_done")
        t.end()
        return
    if shape == "clean_lanes":
        for k in range(8):
            t.read("s0", T1 + 0x100 * k, beats=2, id=0, cache=3)
            t.drain(limit=2000, label="full_done")
            t.read("s0", T1 + 0x4008 + 0x100 * k, beats=1, size=2, id=1, cache=0)
            t.drain(limit=2000, label="narrow_done")
        t.end()
        return
    if shape == "chained":
        # Failed words at several places of both gathers. The words gathered before a failed
        # word leave first on both links, and the words after it gather again.
        for failed in (0, 1, 2, 3, 7, 9):
            at = 0x200 * failed
            t.set("t1", "err_addr", at + 4 * failed)
            t.set("t1", "err_on", 1)
            t.read("s1", T1 + at, beats=16, id=0, cache=3,
                   beat_resp={str(i): 2 if i == failed else 0 for i in range(16)})
            t.drain(limit=2000, label="CHAINED_FAILED_WORD")
        t.set("t1", "err_on", 0)
        held(t, "runs")
        t.end()
        return
    if shape == "narrow_beats":
        # Beats narrower than the target's word stay apart: one source word per beat.
        for k, size in enumerate((2, 1, 0, 3)):
            for cache in (0, 3):
                t.read("s1", T1 + 0x100 * k + 0x40 * cache, beats=16, size=size, id=k, cache=cache)
        t.drain(limit=20000, label="drain")
        t.end()
        return
    held(t, claim or shape)
    wrap_top(t)
    # The sources keep to their own words: the checker tells reads of one ID apart by their words.
    apart = {"s0": 0, "s1": 0x800}
    for k in range(4):
        for s, beat in (("s0", 16), ("s1", 4)):
            for base in (T0, T1):
                at = base + 0x1000 + apart[s] + 0x100 * k
                t.read(s, at, beats=1 << k, id=k % 4, cache=3)
                t.read(s, at + beat, beats=1, size=2, burst="fixed", id=k % 4, cache=3)
    for beats in (2, 4):
        for start in range(beats):
            for s, beat in (("s0", 16), ("s1", 4)):
                for base in (T0, T1):
                    t.read(s, base + 0x2000 + apart[s] + start * beat, beats=beats, burst="wrap",
                           id=start % 4, cache=3)
    for start in range(8):
        t.read("s1", T1 + 0x3000 + start * 4, beats=8, burst="wrap", id=start % 4, cache=3)
        t.read("s1", T1 + 0x3400 + start * 4, beats=16, burst="wrap", id=start % 4, cache=3)
    for s in ("s0", "s1"):
        t.read(s, T1 + 0x3800, beats=8, size=0, id=1, cache=3)
        t.read(s, T1 + 0x3900, beats=8, size=1, id=2, cache=3)
    # Beats narrower than the target's word stay apart: not packed (non-modifiable) they reach the
    # target narrow; a FIXED burst's beats name one word.
    t.read("s1", T1 + 0x3a00, beats=8, size=0, id=1, cache=0)
    t.read("s1", T1 + 0x3b00, beats=8, size=1, id=2, cache=0)
    t.read("s1", T1 + 0x3c00, beats=4, burst="fixed", id=3, cache=3)
    t.read("s0", T0 + 0x3a00, beats=8, size=2, id=1, cache=0)
    # Only the source beat containing the failed target word fails. Successful neighbours stand,
    # and the words gathered before the failed one leave without it.
    t.drain(limit=40000, label="settled")
    t.set("t1", "err_addr", 0x3d08)
    t.set("t1", "err_on", 1)
    t.read("s1", T1 + 0x3d00, beats=16, id=0, cache=3,
           beat_resp={str(i): 2 if i == 2 else 0
                       for i in range(16)})
    t.drain(limit=2000, label="drain")
    t.set("t1", "err_on", 0)
    t.end()
