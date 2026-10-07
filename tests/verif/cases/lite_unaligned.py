"""Unaligned AXI4-Lite source addresses through the public CLI: the source
clears the address bits below its bus width before any check and WSTRB
alone selects the written bytes.

The case table and the oracle of the access count and response of every
case (expected()) are the scenario's, with the default response classes: misses and the
sub-page remainder answer DECERR, an APB3 partial word SLVERR without
access, a word without strobes no access at APB and AHB. The offline
checker judges bytes, read data, alignment and responses."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

# name: (protocol, base, size, local address bits)
TARGETS = {
    32: {'ta': ('axi4', 0x10000, 0x1000, None),
         'tp': ('apb4', 0x11000, 0x1000, 12),
         't3': ('apb3', 0x12000, 0x1000, 12),
         'tl': ('axi4_lite', 0x13000, 0x1000, 12),
         'th': ('ahb_v3', 0x14000, 0x1000, 12),
         'ts': ('apb4', 0x15000, 0x100, 8)},
    # A 64-bit Lite source reaches 64-bit AXI4 targets only.
    64: {'ta': ('axi4', 0x10000, 0x1000, None),
         'tb': ('axi4', 0x11000, 0x1000, None)},
}


def configuration(width, lite_bits=32):
    targets = []
    for name, (kind, base, size, bits) in TARGETS[width].items():
        entry = {'name': name, 'clock_domain': 'main', 'protocol': kind, 'data_width': width,
                 'mappings': [{'base': hex(base), 'size': hex(size)}]}
        if kind != 'axi4':
            entry.update(address_width=bits)
        targets.append(entry)
    transport = {'max_outstanding': 1, 'transport_word_bytes': 4}
    topology = {}
    if len(targets) != 2:
        sources, names = ['l0', 's1'], list(TARGETS[width])
        topology['topology'] = {
            'request': {'nodes': [{'name': 'q0'}], 'links': [],
                        'entries': [{'endpoint': s, 'node': 'q0'} for s in sources],
                        'exits': [{'endpoint': t, 'node': 'q0'} for t in names]},
            'response': {'nodes': [{'name': 'p0'}], 'links': [],
                         'entries': [{'endpoint': t, 'node': 'p0'} for t in names],
                         'exits': [{'endpoint': s, 'node': 'p0'} for s in sources]}}
    return endpoint_ports({
        'schema_version': 10, 'name': 'lite_unaligned', 'address_width': 32,
        'arbitration': {'policy': 'round_robin'},
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
        'initiators': [dict({'name': 'l0', 'clock_domain': 'main', 'protocol': 'axi4_lite',
                             'data_width': width},
                            **({'address_width': lite_bits} if lite_bits != 32 else {})),
                       {'name': 's1', 'clock_domain': 'main', 'protocol': 'axi4',
                        'data_width': width}],
        'targets': targets, 'transport': transport, 'connectivity': 'all', **topology,
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}},
        id_width=2 if width == 32 else 0, len_width=8 if width == 32 else 4)


def cases(width, lite_bits):
    nbytes = width // 8
    full = (1 << nbytes) - 1
    out = []
    for name, (kind, base, size, _) in TARGETS[width].items():
        if size < 0x1000:
            continue
        if width == 32:
            writes = [(0x41, 0xe), (0x83, 0x3), (0xc2, 0xf), (0x101, 0x0), (0xfff, 0x8)]
            reads = [0x41, 0x86, 0xfff]
        else:
            writes = [(0x45, 0xf0), (0x87, 0x03), (0xc4, full), (0x104, 0x0f), (0x141, 0x0),
                      (0xfff, 0x80)]
            reads = [0x43, 0x104, 0xffd]
        out += [dict(write=1, address=base + o, strobe=s) for o, s in writes]
        out += [dict(write=0, address=base + o, strobe=0) for o in reads]
    if width == 32:
        # Sub-page window edges: the last word is inside the 256 B window,
        # the next byte after clearing lies in the page remainder.
        base = TARGETS[32]['ts'][1]
        out += [dict(write=0, address=base + 0xff, strobe=0),
                dict(write=1, address=base + 0xfd, strobe=0x6),
                dict(write=0, address=base + 0x101, strobe=0),
                dict(write=1, address=base + 0x102, strobe=0xf)]
    # Just below the first window and an unmapped address with low bits set.
    out += [dict(write=0, address=0xffff, strobe=0), dict(write=1, address=0xfffe, strobe=full),
            dict(write=0, address=0x80000003 if lite_bits == 32 else 0x1f003, strobe=0)]
    return out


def expected(width, c):
    """(target accesses, response) of one case (the scenario's oracle)."""
    nbytes = width // 8
    aligned = c['address'] // nbytes * nbytes
    owner = None
    for name, (kind, base, size, _) in TARGETS[width].items():
        if base <= aligned < base + max(size, 0x1000):
            owner = (name, kind, base, size)
    if owner is None or not owner[2] <= aligned < owner[2] + owner[3]:
        return 0, 3  # unmapped, or the page remainder of a sub-page window: DECERR
    _, kind, _, _ = owner
    if not c['write']:
        return 1, 0
    if kind not in ('axi4', 'axi4_lite') and c['strobe'] == 0:
        return 0, 0  # APB and AHB targets skip a word without strobes
    if kind == 'ahb_v3':
        return None, 0  # AHB splits a word by its strobes into HSIZE transfers
    if kind == 'apb3' and c['strobe'] != 0xf:
        return 0, 2  # no PSTRB: a partial word answers SLVERR without access
    return 1, 0


def bfms(width):
    out = [dict(name="l0", kind="axi_master", endpoint="initiator:l0", lite=True),
           dict(name="s1", kind="axi_master", endpoint="initiator:s1")]
    for k, (name, (kind, _, _, _)) in enumerate(TARGETS[width].items()):
        bfm = {"axi4": "axi_slave", "axi4_lite": "axi_slave", "ahb_v3": "ahb_slave"}.get(
            kind, "apb_slave")
        entry = dict(name=name, kind=bfm, endpoint=f"target:{name}", salt=k + 1)
        if kind == "axi4_lite":
            entry["lite"] = True
        if kind == "apb3":
            entry["dialect"] = 3
        out.append(entry)
    return out


def model(cfg):
    return dict(routes=nlconfig.routes(cfg), error_codes=True,
                targets=nlconfig.targets(cfg, error_data_zero=True))


C32, C64, C17 = configuration(32), configuration(64), configuration(32, 17)
CASE = dict(
    name="lite_unaligned",
    contracts=["AXI.LITE.WORD_ACCESS", "AXI.ERR.UNMAPPED_DECERR", "AXI.ROUTE.LOCAL_OFFSET",
               "APB.TARGET.ACCESS", "APB.SUBPAGE.DECODE", "AXI.W.UNSTROBED_ZERO"],
    dut=dict(source="cli", config=C32),
    bfms=bfms(32),
    model=model(C32),
    scenario_args=dict(width=32, lite_bits=32),
    configs=[dict(label=""),
             dict(label="w64", dut=dict(source="cli", config=C64), bfms=bfms(64),
                  model=model(C64), scenario_args=dict(width=64, lite_bits=32)),
             dict(label="a17", dut=dict(source="cli", config=C17), bfms=bfms(32),
                  model=model(C17), scenario_args=dict(width=32, lite_bits=17))],
    max_cycles=60000,
    faults=[],
)


def scenario(t, seed, width=32, lite_bits=32):
    t.ready("l0", "r", "modulo", 3, 0)
    t.ready("l0", "b", "modulo", 4, 1)
    for name, (kind, _, _, _) in TARGETS[width].items():
        if kind in ("apb4", "apb3"):
            t.set(name, "wait", 1)
        elif kind == "ahb_v3":
            t.set(name, "wait", 1)
        else:
            t.ready(name, "aw", "modulo", 3, 1)
            t.ready(name, "w", "modulo", 4, 2)
    t.reset(10)
    t.delay(20)
    for k, c in enumerate(cases(width, lite_bits)):
        t.mark("case", k)
        accesses, code = expected(width, c)
        if c['write']:
            order = k % 3  # AW with W, AW first, W first
            if order:
                t.gate("l0", "w" if order == 1 else "aw", False)
            t.strobes("l0", [c['strobe']])
            counted = {} if accesses is None else dict(targets=accesses)
            t.write("l0", c['address'], strobe="table", prot=2, resp=code, **counted)
            if order:
                t.delay(4)
                t.gate("l0", "w" if order == 1 else "aw", True)
        else:
            t.read("l0", c['address'], prot=0, targets=accesses, resp=code)
        t.drain(limit=2000, label=f"CASE_{k}")
        t.delay(30)
    t.end()
