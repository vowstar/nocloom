"""Per-target data widths through the public CLI, one registration per mode
(--param mode=...).

The cases and the independent cut, merge, fold and packet model stay in
nlverif/widths.py. Every case runs alone (two when it has a partner): each
source transaction must reach its target as exactly the bursts that model
gives (address, length, size, type, and AxCACHE and AxPROT through the
target's attribute map), with every strobed byte once, unstrobed bytes
zero, the read data of the target words, a target error on a single beat
address (SLVERR on that beat or on the merged B) and a FIXED burst wider
than its target refused without access. A packets-mode partner lands
between the two packets of a long burst.

Designated faults: text faults on the generated port assignments, four per
family."""

import importlib.util

import nlverif.widths as widths_source
from nlverif import config as nlconfig

MODES = ('graph', 'clocked', 'net64', 'packets', 'attributes', 'outstanding', 'wide',
         'wide_packets', 'wide_clocked')
PARAMS = globals().get("PARAMS", {})
MODE = PARAMS.get("mode", "graph")
if MODE not in MODES:
    raise ValueError(f"unknown mixed widths mode {MODE}")

# A module object of its own: use() switches its tables for this mode.
_spec = importlib.util.spec_from_file_location("nlv_widths_" + MODE, widths_source.__file__)
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)
W.use(MODE)
WIDE = MODE in W.WIDE_MODES
CONFIG = W.configuration(MODE)
CLOCKED = MODE in ('clocked', 'wide_clocked')


def text_fault(name, pattern, replacement, expect):
    return dict(id=name, op="text", subs=[(pattern, replacement, 1)], expect=expect,
                contract="AXI.WIDTH.ENDPOINT")


FAULTS = [
    # Lose the upper half of the 256-bit target's write data.
    text_fault('wdata_high_zero', r'^(assign nl_t_t256_wdata = )([^;]+);',
               r"\g<1>{128'b0, \g<2>[127:0]};", ["TARGET_WRITE_BYTES"]),
    # Report a full-width size for every 128-bit target access.
    text_fault('awsize_full', r'^(assign nl_t_t128_awsize = )([^;]+);', r"\g<1>3'd4;",
               ["TARGET_DESCRIPTOR", "TARGET_ILLEGAL", "TARGET_WRITE_BYTES"]),
    # Copy byte 1 of lane 0 into byte 1 of lane 1 on the 128-bit target.
    text_fault('wdata_byte_copy', r'^(assign nl_t_t128_wdata = )([^;]+);',
               r"\g<1>{\g<2>[127:48], \g<2>[15:8], \g<2>[39:0]};",
               ["TARGET_WRITE_BYTES", "TARGET_UNSTROBED_DATA"]),
    # Return the 64-bit source's read data one byte lane off.
    text_fault('rdata_shift', r'^(assign nl_i_s64_rdata = )([^;]+);', r"\g<1>{(\g<2>) >> 8};",
               ["SOURCE_R_DATA"]),
]
WIDE_FAULTS = [
    # Lose the upper half of the 1024-bit target's write data.
    text_fault('wdata_high_zero', r'^(assign nl_t_t1024_wdata = )([^;]+);',
               r"\g<1>{512'b0, \g<2>[511:0]};", ["TARGET_WRITE_BYTES"]),
    # Report a full-width size for every 512-bit target access.
    text_fault('awsize_full', r'^(assign nl_t_t512_awsize = )([^;]+);', r"\g<1>3'd6;",
               ["TARGET_DESCRIPTOR", "TARGET_ILLEGAL", "TARGET_WRITE_BYTES"]),
    # Drop the top byte lane of the 512-bit target's write strobes.
    text_fault('wstrb_top_drop', r'^(assign nl_t_t512_wstrb = )([^;]+);',
               r"\g<1>{1'b0, \g<2>[62:0]};", ["TARGET_WRITE_BYTES"]),
    # Return the 1024-bit source's read data one byte lane off.
    text_fault('rdata_shift', r'^(assign nl_i_s1024_rdata = )([^;]+);',
               r"\g<1>{(\g<2>) >> 8};", ["SOURCE_R_DATA"]),
]

CASE = dict(
    name=f"mixed_widths_{MODE}",
    contracts=["AXI.WIDTH.ENDPOINT", "AXI.WIDTH.FIXED_REFUSE", "AXI.MERGE.SEVERITY",
               "AXI.ATTR.MAP", "AXI.W.UNSTROBED_ZERO", "AXI.ERR.READ_ZERO"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name=s, kind="axi_master", endpoint=f"initiator:{s}") for s in W.SOURCES]
    + [dict(name=t, kind="axi_slave", endpoint=f"target:{t}", salt=k,
            clock="memory" if CLOCKED else "main") for k, t in enumerate(W.TARGETS)],
    clocks=dict(main=10.0, fabric=14.0, memory=18.0) if CLOCKED else {},
    # Cut, merge and attribute maps are judged by the exact pieces of every
    # transaction; the generic attribute comparisons stay off.
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=True,
               targets=nlconfig.targets(CONFIG, error_data_zero=True, prot=False)),
    max_cycles=800000,
    run_timeout=600,
    faults=WIDE_FAULTS if WIDE else FAULTS,
)


def issue(t, c, number):
    """One case operation with its exact target bursts."""
    target = W.owner(c['address'])
    pieces = [list(e[2:]) for e in W.address_events(c)]
    common = dict(beats=c['beats'], size=c['size'], burst=("fixed", "incr", "wrap")[c['burst']],
                  id=(number * 5 + 1) % 8, cache=c.get('cache', 0),
                  prot=c.get('prot', 2 if c['write'] else 0))
    if target is not None:
        common['pieces'] = pieces
        if not c['write'] and W.folds(c):
            # The folded container is supplied by one physical target response.
            common['whole'] = True
    if c['write']:
        t.write(c['source'], c['address'], strobe="keep", keep=c['keep'], **common)
    else:
        t.read(c['source'], c['address'], **common)


def scenario(t, seed):
    for s in W.SOURCES:
        t.ready(s, "b", "modulo", 4, 1)
        t.ready(s, "r", "modulo", 3, 0)
    for target in W.TARGETS:
        t.ready(target, "aw", "modulo", 3, 1)
        t.ready(target, "w", "modulo", 4, 2)
        t.ready(target, "ar", "modulo", 3, 2)
        t.set(target, "r_rate", 80)
    t.reset(8)
    t.delay(20)
    for number, c in enumerate(W.cases(MODE)):
        t.mark("case", number)
        for target in W.TARGETS:
            t.set(target, "err_on", 0)
        if c['errors']:
            t.set(W.owner(c['address']), "err_addr", c['errors'][0][1])
            t.set(W.owner(c['address']), "err_on", 1)
        own = {k: v for k, v in c.items() if k not in ('partner', 'interleave')}
        issue(t, own, number)
        if 'partner' in c:
            partner = c['partner']
            if partner.get('delay'):
                t.delay(partner['delay'])
            issue(t, partner, number)
            if c.get('interleave'):
                # The partner lands between the first two packets of the long burst.
                target = W.owner(c['address'])
                base = W.TARGETS[target]['base']
                lo = c['address'] - base
                hi = lo + (c['beats'] << c['size'])
                t.check(kind="interpose", port=target, ch="aw", outer=(lo, hi),
                        inner=partner['address'] - base, after_mark="case", mark_value=number)
        t.drain(limit=40000, label=f"CASE_{number}")
    t.end()
