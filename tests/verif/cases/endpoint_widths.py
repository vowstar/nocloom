"""Unequal and wide AXI4 source widths at native ports on a 32-bit two-by-two
crossbar through the public CLI.

Mode data: four configurations (sources 32/128, 128/256, 1024/512 and
256/32 AXI4-Lite). Mode downsizeN: both sources N bits. Each configuration
runs:

- contention, three phases (both sources to t0, both to t1, each its own
  target; phase 1 offers AW late, phase 2 W late): s0 holds BREADY and RREADY
  low past a checkpoint while s1 runs. s0 must take nothing while it holds;
  s1 must complete by then only when the sources use different targets
  (phase 2): on a shared target a held read may keep the target's shared
  response path (docs/protocols.md), so same-target progress is reported, not required.
  In mode data the held s0 burst is one beat, and two beats when s0 is 32 bits;
- every wide source to each target with the cut cases of the downsize
  converter (the target pieces of nlverif.model.downsize_pieces, response
  programs with SLVERR and DECERR words, common resets mid-burst), FIXED
  wider than 32 bits refused without access;
- (downsize modes) a write and a read of each source at an unmapped address
  (local DECERR, zero read data).

Designated faults are text faults on port assignments of the generated
network, with traffic fault controls."""

import importlib.util
from pathlib import Path

from nlverif import config as nlconfig
from nlverif import model

PARAMS = globals().get("PARAMS", {})
MODE = PARAMS.get("mode", "data")
DATA_SETS = (('narrow-wide', (32, 128), False), ('two-wide', (128, 256), False),
             ('large-wide', (1024, 512), False), ('wide-lite', (256, 32), True))
if MODE == "data":
    SETS = DATA_SETS
elif MODE.startswith("downsize") and MODE[8:] in ("64", "128", "256", "512", "1024"):
    SETS = ((MODE, (int(MODE[8:]),) * 2, False),)
else:
    raise ValueError(f"unknown endpoint widths mode {MODE}")


def configuration(widths, lite):
    """The two-by-two crossbar of the public AXI4 tests: 32-bit targets with
    4-bit IDs and 8-bit lengths, windows 0 and 0x10000 of 32 KiB."""
    protocols = ["axi4", "axi4_lite" if lite else "axi4"]
    return {
        'schema_version': 10, 'name': 'axi4_public', 'address_width': 32,
        'arbitration': {'policy': 'round_robin'},
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
        'initiators': [dict({'name': f's{i}', 'clock_domain': 'main', 'protocol': p,
                             'data_width': w},
                            **({'id_width': 4, 'len_width': 8} if p == 'axi4' else {}))
                       for i, (p, w) in enumerate(zip(protocols, widths))],
        'targets': [{'name': f't{i}', 'clock_domain': 'main', 'protocol': 'axi4',
                     'data_width': 32, 'attributes': 'amba', 'id_width': 4, 'len_width': 8,
                     'mappings': [{'base': hex(i * 65536), 'size': '0x8000'}]} for i in range(2)],
        'transport': {'max_outstanding': 1, 'transport_word_bytes': 32},
        'connectivity': 'all',
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}


def setting(label, widths, lite):
    cfg = configuration(widths, lite)
    # Exact pieces come from model.network_downsize_pieces per transaction;
    # the fixture's serial-piece rule does not hold behind a network source.
    targets = nlconfig.targets(cfg, error_data_zero=True, fixed_reject=True)
    return dict(label=label, dut=dict(source="cli", config=cfg),
                model=dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg), targets=targets),
                bfms=[dict(name=f"s{i}", kind="axi_master", endpoint=f"initiator:s{i}",
                           lite=lite and i == 1) for i in range(2)]
                + [dict(name=f"t{i}", kind="axi_slave", endpoint=f"target:t{i}", salt=i + 3)
                   for i in range(2)],
                scenario_args=dict(widths=list(widths), lite=lite))


def downsize_cases(bits):
    """The shapes of the fixed-width fixture case (cases/axi4_width_cut.py)."""
    path = Path(__file__).resolve().parent / "axi4_width_cut.py"
    spec = importlib.util.spec_from_file_location(f"nlv_endpoint_downsize_{bits}", path)
    module = importlib.util.module_from_spec(spec)
    module.PARAMS = dict(data_bits=str(bits))
    spec.loader.exec_module(module)
    return module.cases_for(bits)


def flip(port, bits):
    return [(r'^(assign ' + port + r' = )([^;]+);',
             r"\g<1>(\g<2>) ^ " + f"{bits}'h{1 << (bits - 32):x};", 1)]


def read_fault(widths):
    """A read data bit of the wide source flipped (high_read)."""
    wide, source = (widths[0], "s0") if widths[0] > 32 else (widths[1], "s1")
    return dict(id="high_read", op="text", subs=flip(f"nl_i_{source}_rdata", wide),
                contract="AXI.WIDTH.DOWNSIZE.PIECES", expect=["SOURCE_R_DATA"])


MAIN = setting(*SETS[0])
FAULTS = [read_fault(SETS[0][1])]
if MODE != "data":
    FAULTS += [
        dict(id="offset", op="text", contract="AXI.ROUTE.LOCAL_OFFSET",
             subs=[(r'^(assign nl_t_t0_awaddr = )([^;]+);', r"\g<1>(\g<2>)+4;", 1)],
             expect=["TARGET_DESCRIPTOR", "TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED",
                     "TARGET_WRITE_BYTES"]),
        dict(id="strobe", op="text", contract="AXI.WIDTH.DOWNSIZE.PIECES",
             subs=[(r'^(assign nl_t_t0_wstrb = )([^;]+);', r"\g<1>0;", 1)],
             expect=["TARGET_WRITE_BYTES", "TARGET_UNSTROBED_DATA"]),
    ]

CASE = dict(
    name=f"endpoint_widths_{MODE}",
    contracts=["AXI.WIDTH.DOWNSIZE.PIECES", "AXI.WIDTH.FIXED_REFUSE", "AXI.MERGE.SEVERITY",
               "AXI.RESET.CANCEL", "AXI.ROUTE.LOCAL_OFFSET", "AXI.ERR.READ_ZERO"],
    dut=MAIN["dut"],
    model=MAIN["model"],
    bfms=MAIN["bfms"],
    scenario_args=MAIN["scenario_args"],
    # high_read runs on each width set.
    configs=[dict(setting(*s), label=s[0] if k else "", faults=[read_fault(s[1])] + FAULTS[1:])
             for k, s in enumerate(SETS)],
    negative_faults_all=MODE == "data",
    max_cycles=6000000,
    # One faulted run simulates the core plus every fault variant at once.
    # 1024-bit runs use a longer per-run limit than the 600 s default.
    run_timeout=900,
    faults=FAULTS,
)


def contention(t, widths, lite, held):
    """Three phases of one burst per source and direction; s0 holds its
    response channels past a checkpoint."""
    beats = [held, 1 if lite else 2]
    words = [beats[s] * (widths[s] // 32) for s in range(2)]
    checkpoint = max(250, 128 + 2 * (words[0] + words[1]))
    for phase in range(3):
        t.reset(4)
        t.delay(2)
        t.mark("contention", phase)
        t.ready("s0", "b", "never")
        t.ready("s0", "r", "never")
        t.ready("s1", "r", "modulo", 5, 0)
        b0, r0 = t.snap("s0", "b"), t.snap("s0", "rlast")
        b1, r1 = t.snap("s1", "b"), t.snap("s1", "rlast")
        if phase == 1:
            t.gate("s0", "aw", False)
            t.gate("s1", "aw", False)
        if phase == 2:
            t.gate("s0", "w", False)
            t.gate("s1", "w", False)
        for s in range(2):
            target = phase if phase < 2 else s
            base = target * 65536
            size = (widths[s] // 8).bit_length() - 1
            ids = (0, 0) if lite and s == 1 else (s + 1, s + 5)
            cache = 0 if lite and s == 1 else 2
            t.write(f"s{s}", base + 0x1000 + s * 256, beats=beats[s], size=size, id=ids[0],
                    cache=cache, prot=s)
            t.read(f"s{s}", base + 0x2000 + s * 256, beats=beats[s], size=size, id=ids[1],
                   cache=cache, prot=s)
        if phase:
            t.delay(9 if phase == 2 else 8)
            for s in range(2):
                t.gate(f"s{s}", "w" if phase == 2 else "aw", True)
        t.delay(checkpoint)
        t.expect_now("s0", "b", "==", 0, "CONTENTION_HOLD_SOURCE", since=b0)
        t.expect_now("s0", "rlast", "==", 0, "CONTENTION_HOLD_SOURCE", since=r0)
        if phase == 2:
            t.expect_now("s1", "b", "==", 1, "CONTENTION_PEER", since=b1)
            t.expect_now("s1", "rlast", "==", 1, "CONTENTION_PEER", since=r1)
        t.ready("s0", "b", "always")
        t.delay(50)
        t.ready("s0", "r", "modulo", 5, 0)
        t.drain(limit=4000, label="CONTENTION_EXACT_COUNT")
    t.ready("s0", "r", "modulo", 5, 0)


def downsize_shapes(t, source, target, base, bits, number):
    """The fixture downsize cases on one route of the network."""
    for index, (n, sz, burst, start, mode, abort) in enumerate(downsize_cases(bits)):
        start += base
        if abort >= 0:
            t.reset(4)
            t.delay(3)
        t.mark("route", number * 1000 + index)
        refused = burst == 0 and sz > 2 or model.crosses_4k(start, n, sz, burst)
        pieces, beats = ([], []) if refused else model.downsize_pieces(n, sz, burst, start)
        np_ = len(pieces)
        if np_ >= 3:
            ends, total = [], 0
            for _, length, _, _ in pieces:
                total += length + 1
                ends.append(total)
            ea = ends[np_ // 2 - 1]
        else:
            ea = len(beats) // 2
        eb = ea + 1
        err_piece = np_ // 2 if np_ >= 3 else (1 if np_ == 2 else 0)
        t.set(target, "base", 1)
        for k in range(4):
            t.set(target, f"b_ovr_i{k}", 0)
            t.set(target, f"r_ovr_i{k}", 0)
        t.set(target, "b_default", {1: 2, 2: 3, 6: 2}.get(mode, 0))
        if mode == 3:
            t.set(target, "r_ovr_i0", ea + 1)
            t.set(target, "r_ovr_c0", 2)
            t.set(target, "r_ovr_i1", eb + 1)
            t.set(target, "r_ovr_c1", 3)
        elif mode == 5:
            t.set(target, "b_ovr_i0", err_piece + 1)
            t.set(target, "b_ovr_c0", 2)
        elif mode == 6:
            t.set(target, "b_ovr_i0", 1)
            t.set(target, "b_ovr_c0", 3)
        elif mode == 7:
            t.set(target, "b_ovr_i0", 1)
            t.set(target, "b_ovr_c0", 2)
            if np_ > 1:
                t.set(target, "b_ovr_i1", np_)
                t.set(target, "b_ovr_c1", 3)
        if refused:
            for ch in ("aw", "w", "ar"):
                t.quiet(target, ch, True)
        snaps = {(p, k): t.snap(p, k) for p, k in ((source, "b"), (source, "rlast"),
                                                   (source, "w"), (target, "w"), (target, "r"),
                                                   (target, "b"), (target, "rlast"))}
        cache = 0 if (index + 1) % 2 else 2
        sbytes = bits // 8
        exact = {}
        if not refused:
            exact = {d: dict(pieces=[[a - base, n_, s_, b_] for a, n_, s_, b_ in
                                     model.network_downsize_pieces(n, sz, burst, at, sbytes, write=d == "w")])
                     for d, at in (("w", start), ("r", start + 0x4000))}
            if model.folds(n, sz, burst, sbytes):
                exact["r"]["whole"] = True
        t.write(source, start, beats=n, size=sz, burst=("fixed", "incr", "wrap")[burst], id=5,
                cache=cache, prot=5, strobe="sparse" if mode == 4 else "full",
                **exact.get("w", {}))
        t.read(source, start + 0x4000, beats=n, size=sz, burst=("fixed", "incr", "wrap")[burst],
               id=6, cache=cache, prot=3, **exact.get("r", {}))
        if abort > 0:
            port, counter = {1: (target, "w"), 2: (target, "r"), 3: (target, "b"),
                             4: (target, "rlast"), 5: (source, "w")}[abort]
            t.wait(port, counter, ">=", 1, since=snaps[(port, counter)], limit=20000,
                   label="RESET_REACHED")
        else:
            t.wait(source, "b", "==", 1, since=snaps[(source, "b")], limit=60000, label="PROGRESS")
            t.wait(source, "rlast", "==", 1, since=snaps[(source, "rlast")], limit=60000,
                   label="PROGRESS")
        if refused:
            for ch in ("aw", "w", "ar"):
                t.quiet(target, ch, False)
    t.drain(limit=20000, label="ROUTE_DRAIN")


def scenario(t, seed, widths=(32, 128), lite=False):
    for target in ("t0", "t1"):
        t.ready(target, "aw", "modulo", 3, 0)
        t.ready(target, "w", "modulo", 4, 0)
        t.ready(target, "ar", "modulo", 4, 0)
    for s in ("s0", "s1"):
        t.ready(s, "b", "modulo", 7, 0)
        t.ready(s, "r", "modulo", 5, 0)
    held_options = (1, 2) if MODE == "data" and widths[0] == 32 else (1 if MODE == "data" else 2,)
    for held in held_options:
        contention(t, widths, lite, held)
    t.reset(4)
    t.delay(3)
    for s, bits in enumerate(widths):
        if bits == 32:
            continue
        for k, target in enumerate(("t0", "t1")):
            downsize_shapes(t, f"s{s}", target, k * 65536, bits, 2 * s + k)
    if MODE != "data":
        # An unmapped address of each source: local DECERR, zero read data.
        for s, bits in enumerate(widths):
            size = (bits // 8).bit_length() - 1
            t.write(f"s{s}", 0x8000 + 0x100, beats=2, size=size, id=3)
            t.read(f"s{s}", 0x8000 + 0x100, beats=2, size=size, id=4)
        t.drain(limit=4000, label="UNMAPPED")
    t.end()
