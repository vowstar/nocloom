"""Random-backpressure stress of the AXI4 width converters and the bounded
packet splitter through the public CLI, one registration per mode
(--param mode=...).

Sources and target t0 drive and accept every channel with random valid and
ready (per-segment rates of 10 to 100 percent, every fourth segment at full
rate; W may lead AW). Bursts are INCR, WRAP and FIXED, narrow or full width,
aligned or not, 1 to 64 beats, AxCACHE 0, 2 or 3, random IDs and AxPROT,
with full, sparse, half or empty strobes per beat (unstrobed bytes carry
data). Address classes: the target error page (SLVERR from t0), unrouted
(DECERR from the source's default target, without target access), a FIXED
burst wider than t0 (refused, no target access) and, in the exclusive
configuration (t0 exclusive: target), admitted exclusive bursts (EXOKAY,
one target burst). About one segment in three takes the common reset at a
random cycle. t1, where present, selects
per-target widths and is never addressed. In the uniform-width modes whose
source is converted (down64, down128, mo_down128) the configuration
"target2" gives t0 a pool of two bursts, which the converted source's
bursts and pieces must reach and never exceed (case oracle). In the modes of
EXACT the configuration "exact" gives t0 read side effects: every
Non-modifiable INCR burst that reaches it must make one target access per
naturally aligned power-of-two block of its bytes (case oracle).

The multi-burst modes (MULTI) keep several bursts per source and direction
in flight; each such source must reach min(3, capacity) bursts in flight
per direction. A random decode miss travels to the source's default
target, behind the source's older bursts when that is t0, so the random
traffic need not overtake. A source whose default target is t1 (the
smaller window) therefore ends with a directed pair per direction: with
t0's B (R) held, a burst to t0 and then a miss of another ID, which must
complete at t1 first; it must overtake in both directions.

Judged by the monitors and the offline checker: holds, WLAST, reset quiet,
every strobed byte at the target exactly once with the source value, every
unstrobed target byte zero, no source answer before its target accesses,
response codes per class, zero data on failed read beats, read data equal
to the target's, R beats of one ID in order at a source, legal target
bursts."""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402
from nlverif import model as nlmodel  # noqa: E402

# mode: sources [(name, bits)], t0 bits, t1 bits or None, network bits or None
MODES = {
    'pw32': ([('s0', 32)], 32, 64, None),
    'pw64': ([('s0', 64)], 64, 32, 64),
    'up128': ([('s0', 32)], 128, 64, None),
    'cut128': ([('s0', 128)], 32, 64, None),
    'cut1024': ([('s0', 1024)], 32, 1024, None),
    'down64': ([('s0', 64)], 32, None, None),
    'down128': ([('s0', 128)], 32, None, None),
    'bounded32': ([('s0', 32)], 32, None, None),
    'bounded_pw': ([('s0', 32)], 32, 64, None),
    'bounded_up': ([('s0', 32)], 128, 64, None),
    'bounded_cut': ([('s0', 128)], 32, 64, None),
    'merge2': ([('s0', 32), ('s1', 64)], 128, None, None),
    'merge2b': ([('s0', 32), ('s1', 64)], 128, None, None),
    'mix2': ([('s0', 128), ('s1', 32)], 64, 32, None),
    'bsplit32': ([('s0', 32)], 32, None, None),
    'bsplit_pw': ([('s0', 64)], 64, 32, 64),
    'bsplit_up': ([('s0', 32)], 128, 64, None),
    'bsplit_cut': ([('s0', 128)], 32, 64, None),
    'merge2auto': ([('s0', 32), ('s1', 64)], 128, None, None),
    'merge2s': ([('s0', 32), ('s1', 64)], 128, None, None),
    'mo_up128': ([('s0', 32)], 128, 64, None),
    'mo_pw32': ([('s0', 32)], 32, 64, None),
    'mo_merge2': ([('s0', 32), ('s1', 64)], 128, None, None),
    'mo_wide': ([('s0', 128), ('s1', 32)], 128, 32, None),
    'mo_bounded': ([('s0', 32), ('s1', 32)], 32, None, None),
    'mo_bsplit': ([('s0', 32), ('s1', 32)], 32, None, None),
    'mo_bsplit_up': ([('s0', 32), ('s1', 32)], 128, 64, None),
    'mo_down128': ([('s0', 128)], 32, None, None),
    'mo_cut1024': ([('s0', 1024)], 32, 1024, None),
    'mo_bsplit_cut': ([('s0', 128)], 32, 64, None),
}
# Shared burst capacity of the multi-burst modes' sources; mo_wide keeps the
# 128-bit s0 away from the 32-bit t1 so no clamp converts it.
MULTI = {
    'mo_up128': {'s0': 8}, 'mo_pw32': {'s0': 4}, 'mo_merge2': {'s0': 4, 's1': 2},
    'mo_wide': {'s0': 4, 's1': 3}, 'mo_bounded': {'s0': 4, 's1': 2},
    'mo_bsplit': {'s0': 4, 's1': 2}, 'mo_bsplit_up': {'s0': 4, 's1': 2},
    'mo_down128': {'s0': 4}, 'mo_cut1024': {'s0': 3}, 'mo_bsplit_cut': {'s0': 4},
}
CONNECT = {'mo_wide': {'s0': ['t0'], 's1': ['t0', 't1']}}
SPLIT = ('bsplit', 'merge2s', 'mo_bsplit')
# Uniform-width modes whose source is converted to the target width: the
# companion target2 gives t0 a pool of two bursts.
CONVERTED_UNIFORM = ('down64', 'down128', 'mo_down128')
# Modes whose companion "exact" gives t0 read side effects: Non-modifiable INCR bursts reach it
# as exact blocks (case oracle), the packet limit of its 8-bit AxLEN capping them.
EXACT = ('bounded32', 'bounded_up', 'bounded_cut', 'mo_bounded', 'down128', 'mo_down128', 'mo_up128')
# Wide AXI IDs: 16-bit
# source IDs whose values share their low byte, through source conversion
# (mo_merge2) and through a target that keeps four bursts and merges them
# into its words (target_merge: s0 32, s1 64, t0 128 with max_outstanding 4,
# t1 32, a 32-bit network). One seed, six segments, no exclusive pass.
WIDE_ID = {
    'mo_merge2_id16': dict(base='mo_merge2'),
    'target_merge_id16': dict(base='mo_merge2', spec=([('s0', 32), ('s1', 64)], 128, 32, 32),
                              target_outstanding=4),
}
WIDE_VALUES = (0x00ff, 0x80ff, 0xffff, 0x40ff)

PARAMS = globals().get("PARAMS", {})
MODE = PARAMS.get("mode", "pw32")
if MODE not in MODES and MODE not in WIDE_ID:
    raise ValueError(f"unknown width stress mode {MODE}")
WIDE = WIDE_ID.get(MODE)
BASE = WIDE["base"] if WIDE else MODE
SOURCES, T0, T1, NET = WIDE.get("spec", MODES[BASE]) if WIDE else MODES[MODE]
MO = BASE in MULTI
SEGMENTS = 6 if WIDE else 10
# Multi-burst sources that reach t1: their default target is t1.
OVERTAKE = [n for n in MULTI[BASE] if T1
            and 't1' in CONNECT.get(BASE, {}).get(n, ['t1'])] if MO else []


def configuration(exclusive, target_outstanding=None, exact=False):
    transport = {}
    if NET:
        transport['data_width'] = NET
    targets = [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': T0,
                'mappings': [{'base': '0x0', 'size': '0x100000'}]}]
    if exclusive:
        targets[0]['exclusive'] = 'target'
        targets[0]['exclusive_id_flags'] = ['owner']
        transport['user_flags'] = ['owner']
    if exact:
        targets[0]['read_side_effects'] = True
    if T1:
        targets.append({'name': 't1', 'clock_domain': 'main', 'protocol': 'axi4',
                        'data_width': T1, 'mappings': [{'base': '0x100000', 'size': '0x1000'}]})
    connect = CONNECT.get(BASE, 'all')
    cfg = {'schema_version': 10, 'name': 'stress', 'address_width': 32,
           'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
           'initiators': [dict({'name': n, 'clock_domain': 'main', 'protocol': 'axi4',
                                'data_width': b},
                               **({'narrow_bursts': 'split_non_modifiable'}
                                  if BASE.startswith(SPLIT) else {}),
                               **({'max_outstanding': MULTI[BASE][n]}
                                  if n in MULTI.get(BASE, {}) else {}),
                               **({'id_width': 16} if WIDE else {}),
                               **({'user_flags': {'owner': i % 2}} if exclusive else {}))
                          for i, (n, b) in enumerate(SOURCES)],
           'targets': targets, 'transport': transport, 'connectivity': connect,
           'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    if len(SOURCES) > 1 and not MODE.endswith('auto'):
        cfg['arbitration'] = {'policy': 'round_robin'}
    if WIDE and WIDE.get("target_outstanding"):
        targets[0]['max_outstanding'] = WIDE["target_outstanding"]
    if target_outstanding:
        targets[0]['max_outstanding'] = target_outstanding
    return endpoint_ports(cfg, id_width=2, len_width=8)


def model(exclusive, target_outstanding=None, exact=False):
    cfg = configuration(exclusive, target_outstanding, exact)
    targets = nlconfig.targets(cfg, error_data_zero=True, cache="amba", prot=True)
    targets["t0"]["lock"] = exclusive
    return dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg), targets=targets)


BFMS = ([dict(name=n, kind="axi_master", endpoint=f"initiator:{n}") for n, _ in SOURCES]
        + [dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=3)]
        + ([dict(name="t1", kind="axi_slave", endpoint="target:t1", salt=4)] if T1 else []))

CASE = dict(
    name=f"width_stress_{MODE}",
    contracts=["AXI.WIDTH.DOWNSIZE.PIECES", "AXI.WIDTH.FIXED_REFUSE", "AXI.MERGE.SEVERITY",
               "AXI.RESET.CANCEL", "AXI.W.UNSTROBED_ZERO", "AXI.ERR.READ_ZERO",
               "AXI.ERR.UNMAPPED_DECERR", "AXI.ROUTE.LOCAL_OFFSET"]
    + (["AXI.OUTSTANDING.REACH"] if MO else []),
    dut=dict(source="cli", config=configuration(False)),
    bfms=BFMS,
    model=model(False),
    seeds=[17] if WIDE else list(range(11, 27)) if MODE == "merge2b" else list(range(11, 17)),
    configs=[dict(label="")] + ([] if WIDE else [
        dict(label="exclusive", dut=dict(source="cli", config=configuration(True)),
             model=model(True), scenario_args=dict(exclusive=True), seeds=[21, 22])]) + (
        # A uniform-width network whose converted source reaches a target of four.
        [dict(label="target2", dut=dict(source="cli", config=configuration(False, 2)),
              model=model(False, 2), seeds=[31, 32],
              oracle=nlmodel.target_pool("t0", 2))]
        if MODE in CONVERTED_UNIFORM else []) + (
        [dict(label="exact", dut=dict(source="cli", config=configuration(False, exact=True)),
              model=model(False, exact=True), seeds=[41, 42],
              oracle=nlmodel.exact_access("t0", min(4096, 256 * T0 // 8)))]
        if MODE in EXACT else []),
    max_cycles=2000000,
    run_timeout=600,
    # Designated faults on port assignments of the generated network (the
    # fault cases check that the stress assertions reject incorrect RTL).
    faults=[
        dict(id="t0_strobes_all", op="text", contract="AXI.W.UNSTROBED_ZERO",
             subs=[(r"(assign nl_t_t0_wstrb = )[^;]+;", r"\g<1>{%d{1'b1}};" % (T0 // 8), 1)],
             expect=["TARGET_UNSTROBED_DATA", "TARGET_WRITE_BYTES"]),
        dict(id="t0_address_bit8", op="text", contract="AXI.ROUTE.LOCAL_OFFSET",
             subs=[(r"(assign nl_t_t0_awaddr = )([^;]+);", r"\g<1>(\g<2>) ^ 32'h100;", 1)],
             expect=["TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED", "TARGET_WRITE_BYTES",
                     "SOURCE_UNANSWERED"]),
        dict(id="s0_rdata_shift", op="text", contract="AXI.WIDTH.DOWNSIZE.PIECES",
             subs=[(r"(assign nl_i_s0_rdata = )([^;]+);", r"\g<1>(\g<2>) >> 8;", 1)],
             expect=["SOURCE_R_DATA"]),
        dict(id="s0_bresp_okay", op="text", contract="AXI.MERGE.SEVERITY",
             subs=[(r"(assign nl_i_s0_bresp = )[^;]+;", r"\g<1>2'd0;", 1)],
             expect=["SOURCE_B_RESP"]),
    ],
)


def scenario(t, seed, exclusive=False):
    rnd = random.Random(seed)
    names = [n for n, _ in SOURCES]
    tbytes = T0 // 8
    for ch in ("aw", "w", "ar") if T1 else ():
        t.quiet("t1", ch, True)
    # Target error page: local addresses at and above 0xF000 answer SLVERR.
    t.set("t0", "reg0_lo", 0xF000)
    t.set("t0", "reg0_hi", 0x10000)
    t.set("t0", "reg0_w", 2)
    t.set("t0", "reg0_r", 2)
    if exclusive:
        t.set("t0", "lock_ok", 1)
    for k, n in enumerate(names):
        t.set(n, "seed", 1000 * seed + 17 * k + 1)
    t.set("t0", "seed", 1000 * seed + 99)
    t.reset(4)
    t.delay(2)
    role = {n: 0 for n in names}
    serial = 0
    for seg in range(SEGMENTS):
        t.mark("segment", seg)
        full = seg % 4 == 3
        rate = (lambda: 100) if full else (lambda: 10 + rnd.randrange(91))
        for n in names:
            for reg in ("vrate_aw", "vrate_w", "vrate_ar"):
                t.set(n, reg, 0 if full else rate())
            for ch in ("b", "r"):
                t.ready(n, ch, "random", rate())
        for ch in ("aw", "w", "ar"):
            t.ready("t0", ch, "random", rate())
        t.set("t0", "b_rate", 0 if full else rate())
        t.set("t0", "r_rate", 0 if full else rate())
        plan = []
        for ix, (n, bits) in enumerate(SOURCES):
            for direction, count in (("w", 4 + rnd.randrange(8)), ("r", 4 + rnd.randrange(8))):
                for k in range(count):
                    plan.append((k, ix, n, bits, direction))
        plan.sort(key=lambda p: (p[0], p[1], p[4]))
        for k, ix, n, bits, direction in plan:
            serial += 1
            burst(t, rnd, n, ix, bits, direction, k, role[n], tbytes, exclusive)
        reset_at = 20 + rnd.randrange(1500) if rnd.randrange(3) == 0 else None
        if reset_at is None:
            t.drain(limit=200000, label="SEGMENT")
        else:
            t.settle(reset_at, reset=3)
            t.drain(limit=200000, label="SEGMENT")
        for n in names:
            role[n] = 1 - role[n]
    for ix, (n, _) in enumerate(SOURCES):
        if n in OVERTAKE:
            overtake(t, n, ix)
        if MO and n in MULTI[BASE]:
            hold_open(t, n, ix, open_bound(n))
    t.end()
    for ix, (n, _) in enumerate(SOURCES):
        if MO and n in MULTI[BASE]:
            for d in ("w", "r"):
                t.check(kind="reach", port=n, dir=d, inflight=open_bound(n),
                        overtakes=int(n in OVERTAKE))


def open_bound(n):
    """Bursts of one source open at its port with t0's responses held: a source keeps AW and
    AR in its port pipe until the network takes the burst, so beyond the pipe only what t0
    admits is open (its pool, one burst unless stated): at most min(3, capacity, pool + 1)."""
    pool = WIDE.get("target_outstanding", 1) if WIDE else 1
    return min(3, MULTI[BASE][n], pool + 1)


def hold_open(t, n, ix, count):
    """With t0's B (R) held, `count` single-beat bursts of one source are open together
    (open_bound): the random segments alone, with slow random W beats holding the request
    merger to a write's last beat, need not reach the bound."""
    t.mark("hold_open", ix)
    value = WIDE_VALUES[0] if WIDE else 0
    for d, ch, port in (("w", "b", "aw"), ("r", "r", "ar")):
        t.gate("t0", ch, False)
        since = t.snap(n, port)
        for k in range(count):
            if d == "w":
                t.write(n, ix * 0x7000 + 0x400 + 0x40 * k, id=value)
            else:
                t.read(n, ix * 0x7000 + 0x400 + 0x40 * k, id=value)
        t.wait(n, port, "==", count, since=since, limit=20000, label="HOLD_OPEN")
        t.gate("t0", ch, True)
        t.drain(limit=20000, label="HOLD_OPEN_DRAIN")


def overtake(t, n, ix):
    """With t0's B (R) held, a burst of one ID to t0 stays open while a
    younger decode miss of another ID completes at the default target t1."""
    t.mark("overtake", ix)
    old, young = (WIDE_VALUES[0], WIDE_VALUES[1]) if WIDE else (0, 1)
    for d, ch, done in (("w", "b", "b"), ("r", "r", "rlast")):
        t.gate("t0", ch, False)
        since = t.snap(n, done)
        if d == "w":
            t.write(n, ix * 0x7000, id=old)
            t.write(n, 0x200000 + ix * 0x10000, id=young)
        else:
            t.read(n, ix * 0x7000, id=old)
            t.read(n, 0x200000 + ix * 0x10000, id=young)
        t.wait(n, done, "==", 1, since=since, limit=20000, label="OVERTAKE")
        t.gate("t0", ch, True)
        t.drain(limit=20000, label="OVERTAKE_DRAIN")


def burst(t, rnd, n, ix, bits, direction, k, role, tbytes, exclusive):
    """One random burst of the scenario's distribution."""
    sbytes = bits // 8
    sz_max = sbytes.bit_length() - 1
    x = rnd.randrange(100)
    if MO:
        # Multi-burst modes miss more often: misses share t0 with the bursts in flight.
        x = x * 6 // 20 if x < 20 else x - 14 if x < 40 else x
    cls = 2 if x < 6 else 1 if x < 14 else 3 if exclusive and x < 26 else 0
    x = rnd.randrange(100)
    kind = 1 if x < 60 else 2 if x < 80 else 0
    size = sz_max if rnd.randrange(2) == 0 else rnd.randrange(sz_max + 1)
    if kind == 2:
        length = (2, 4, 8, 16)[rnd.randrange(4)]
    elif kind == 1 and rnd.randrange(8) == 0:
        length = rnd.randrange(64) + 1
    else:
        length = rnd.randrange(16) + 1
    if cls == 3:
        # Admitted exclusive: INCR of a power-of-two beat count aligned to its
        # total, at most min(128, 16 target words) bytes.
        kind, length = 1, 1 << rnd.randrange(5)
        while (length << size) > 128 or (length << size) > 16 * tbytes:
            if length > 1:
                length //= 2
            else:
                size -= 1
    if cls < 2 and kind == 0 and (1 << size) > tbytes:
        cls = 4
    total = length << size

    def shrink():
        nonlocal length, total
        length = length // 2 if kind == 2 else max(1, length // 2)
        total = length << size
    while total > 2048:
        shrink()
    badwin = 256 if MO else 1024
    if cls == 1:
        while total > badwin:
            shrink()
    win = badwin if cls == 1 else 2048
    if cls == 2:
        lo = 0x200000 + ix * 0x10000 + rnd.randrange(16) * 4096
    elif cls == 1:
        lo = 0xF000 + ix * 0x800 + ((k % (6 if MO else 2)) * badwin if direction == "w" else 0)
    elif MO:
        lo = ix * 0x7000 + (role if direction == "w" else 1 - role) * 0x3800 + (
            (k % 6) if direction == "w" else rnd.randrange(7)) * 0x800
    else:
        lo = ix * 0x7000 + (role if direction == "w" else 1 - role) * 0x3800 + (
            (k % 2) if direction == "w" else rnd.randrange(2)) * 0x1800 + rnd.randrange(3) * 0x800
    if kind == 2 or cls == 3:
        addr = lo + rnd.randrange(win // total) * total + (
            rnd.randrange(length) << size if kind == 2 else 0)
    elif kind == 1:
        addr = lo + rnd.randrange(win - total + 1)
        if rnd.randrange(2):
            addr = addr >> size << size
    else:
        addr = lo + rnd.randrange(win - (1 << size) + 1)
        if rnd.randrange(2):
            addr = addr >> size << size
    cache = (0, 2, 3, 3)[rnd.randrange(4)]
    expect = {}
    if cls == 3:
        expect = dict(resp=1, one_target=True)
        # An exclusive burst is the only one of its direction in flight.
        t.wait(n, "w_out" if direction == "w" else "r_out", "==", 0, limit=200000,
               label="EXCLUSIVE_ALONE")
    elif cls == 1:
        expect = dict(resp=2)
    axid = rnd.randrange(4)
    common = dict(beats=length, size=size, burst=("fixed", "incr", "wrap")[kind],
                  id=WIDE_VALUES[axid] if WIDE else axid, lock=int(cls == 3), cache=cache,
                  prot=rnd.randrange(8), **expect)
    if direction == "w":
        t.write(n, addr, strobe="mix", **common)
    else:
        t.read(n, addr, **common)
