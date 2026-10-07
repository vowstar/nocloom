"""Burst exchange compositions under sixteen directed phases, one
registration per DUT (--param dut=...).

Every phase sends one write and one read from each source at once: INCR,
FIXED and WRAP, narrow single beats, the longest burst, unaligned starts,
target errors (B SLVERR, one R beat SLVERR, the remaining beats complete),
decode misses (DECERR, no target traffic), AxLOCK without exclusive support
(a normal access, OKAY, AxLOCK 0 at the target), B and R consumers held for
350 cycles, AxCACHE
and AxPROT from the phase number. Destinations alternate so both targets
serve both sources; a common reset ends every phase. graph_axi also stalls
one source's W stream inside a burst and cancels a write in the middle of
its packet: nothing replays after the reset. The checker judges routes,
target-local addresses, descriptors, attributes (amba cache mapping),
IDs, bytes, read data, responses and error data."""

PARAMS = globals().get("PARAMS", {})
DUT = PARAMS.get("dut", "exchange32")
if DUT not in ("exchange32", "exchange64", "graph_axi"):
    raise ValueError(f"unknown exchange DUT {DUT}")
GRAPH = DUT == "graph_axi"
DATA = 64 if DUT == "exchange64" else 32
IDS = DATA == 32
LEN_BITS = 8 if DATA == 32 else 4
SOURCES = ["s0", "s1", "s2"] if GRAPH else ["s0", "s1"]
TARGETS = ["t0", "t1", "t2"] if GRAPH else ["t0", "t1"]
WINDOWS = [(65536 * i, 65536) for i in range(len(TARGETS))]
MISS = 0x80000000
LADDER = "a=8,b=8,m=32,x=8,y=8,n=16"


def recipe(ladder=""):
    if GRAPH:
        return dict(source="fixture", recipe="graph_axi")
    params = dict(data_bits=str(DATA), id_bits="4" if IDS else "0",
                  len_bits=str(LEN_BITS))
    if ladder:
        params["ladder"] = ladder
    return dict(source="fixture", recipe="exchange", params=params)


def faults():
    if GRAPH:
        return [
            dict(id="target_address", component="network", op="set_driver",
                 port="nl_t_t2_awaddr",
                 **{"with": {"op": "Subtract", "args": [{"self": True}, {"const": 1}]}},
                 expect=["TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED", "TARGET_WRITE_BYTES"],
                 contract="AXI.ROUTE.LOCAL_OFFSET"),
            dict(id="return_identity", component="network", op="set_driver",
                 port="nl_i_s2_rid", **{"with": {"const": 0}},
                 expect=["SOURCE_EXTRA_R", "AXI.R.UNEXPECTED", "SOURCE_R_DATA"],
                 contract="AXI.ROUTE.LOCAL_OFFSET"),
            dict(id="packet_tail", component="network", op="set_driver",
                 port="nl_t_t0_wlast", **{"with": {"const": 0}},
                 expect=["AXI.W.LAST"],
                 contract="AXI.ROUTE.LOCAL_OFFSET"),
            # The merge > branch output: switches are numbered in name order (branch is
            # req_node0, merge req_node4). Interleaved packets also break the runs a wider
            # link gathers, so a write may stay unanswered.
            dict(id="packet_owner", component="network", op="set_next",
                 at={"binding": "graph/req/req_node4>node/req_node0.held"},
                 **{"with": {"const": 0}},
                 expect=["TARGET_WRITE_BYTES", "AXI.W.LAST", "TARGET_FOOTPRINT",
                         "TARGET_UNATTRIBUTED", "SOURCE_B_RESP", "SOURCE_UNANSWERED"],
                 contract="AXI.ROUTE.LOCAL_OFFSET"),
        ]
    return [
        dict(id="raw_write", component="network", op="set_driver", port="nl_t_t0_awaddr",
             **{"with": {"select": {"binding": "burst_service/t0/w.descriptor.address"}}},
             expect=["TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED", "TARGET_WRITE_BYTES",
                     "TARGET_DESCRIPTOR"],
             contract="AXI.ROUTE.LOCAL_OFFSET"),
        dict(id="raw_read", component="network", op="set_driver", port="nl_t_t0_araddr",
             **{"with": {"select": {"binding": "burst_service/t0/r.slot0.address"}}},
             expect=["TARGET_DESCRIPTOR", "TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED", "SOURCE_R_DATA"],
             contract="AXI.ROUTE.LOCAL_OFFSET"),
        dict(id="translation", component="network", op="set_driver", port="nl_t_t1_awaddr",
             **{"with": {"select": {"binding": "burst_service/t1/w.descriptor.start_address"}}},
             expect=["TARGET_FOOTPRINT", "TARGET_UNATTRIBUTED", "TARGET_WRITE_BYTES"],
             contract="AXI.ROUTE.LOCAL_OFFSET"),
    ]


ROUTES = {s: [(t, base, size) for t, (base, size) in zip(TARGETS, WINDOWS)] for s in SOURCES}
TARGET_MODEL = {t: dict(kind="axi", bus_bytes=DATA // 8, cache="amba", prot=True, lock=True,
                        ids=IDS, error_data_zero=True, raw_address=True) for t in TARGETS}
def runs(word):
    """No map from the fixture: the response run rule of these builds' routes (message.hpp point 18)."""
    return {f"{s}|{t}": dict(word=word, interleave=True) for s in SOURCES for t in TARGETS}


CASE = dict(
    name=f"exchange_phases_{DUT}",
    contracts=["AXI.ROUTE.LOCAL_OFFSET", "AXI.ERR.UNMAPPED_DECERR", "AXI.ERR.READ_ZERO",
               "AXI.MERGE.SEVERITY", "AXI.RESET.CANCEL"],
    dut=recipe(),
    bfms=[dict(name=s, kind="axi_master", endpoint=f"initiator:{s}") for s in SOURCES]
    + [dict(name=t, kind="axi_slave", endpoint=f"target:{t}", salt=i)
       for i, t in enumerate(TARGETS)],
    model=dict(routes=ROUTES, error_codes=True, targets=TARGET_MODEL,
               response_runs=runs(32) if GRAPH else {}),
    # The exchange runs flat node words and a ladder of words, one build each.
    configs=[dict(label="")] + ([] if GRAPH else [dict(
        label="ladder", dut=recipe(LADDER),
        model=dict(routes=ROUTES, error_codes=True, targets=TARGET_MODEL, response_runs=runs(32)))]),
    negative_runs_all=True,
    max_cycles=400000,
    run_timeout=600,
    faults=faults(),
)


def phase_shape(phase):
    """(beats, size, burst, mode) of a phase: mode 1 target errors, 2 decode
    miss, 3 AxLOCK without exclusive support, 4 and 5 B or R held."""
    full = (DATA // 8).bit_length() - 1
    n, size, burst, mode = 4, full, 1, 0
    if phase == 2:
        n, burst = 16, 0
    elif phase == 3:
        burst = 2
    elif phase == 4:
        n, size = 1, 0
    elif phase == 5:
        n, size = 1, 1
    elif phase == 6:
        n = 1 << LEN_BITS
    elif phase in (7, 8, 9, 10, 11):
        mode = phase - 6
    elif phase == 12:
        size, burst = 1, 0
    elif phase == 13:
        n = 1
    elif phase == 14:
        n, burst = 16, 2
    elif phase == 15:
        n = 16
    return n, size, burst, mode


def scenario(t, seed):
    for s in SOURCES:
        t.ready(s, "b", "modulo", 5, 0)
        t.ready(s, "r", "modulo", 7, 0)
    for tg in TARGETS:
        t.ready(tg, "aw", "modulo", 3, 0)
        t.ready(tg, "ar", "modulo", 4, 0)
        t.ready(tg, "w", "modulo", 5, 0)
    t.reset(4)
    t.delay(2)
    for phase in range(16):
        n, size, burst, mode = phase_shape(phase)
        t.mark("phase", phase)
        for tg in TARGETS:
            # Target errors: every B SLVERR, the second R beat of each read.
            t.set(tg, "base", 1)
            t.set(tg, "b_default", 2 if mode == 1 else 0)
            for k in range(4):
                t.set(tg, f"r_ovr_i{k}", 0)
            if mode == 1:
                for k in range(len(SOURCES)):
                    t.set(tg, f"r_ovr_i{k}", 2 + n * k)
                    t.set(tg, f"r_ovr_c{k}", 2)
        if mode in (4, 5):
            for s in SOURCES:
                t.ready(s, "b" if mode == 4 else "r", "never")
        w_snap = t.snap("s0", "w")
        for si, s in enumerate(SOURCES):
            if GRAPH:
                target = (si + phase) % 3 if phase < 9 else 0
            else:
                target = si if phase % 2 == 0 else 0
            start = WINDOWS[target][0] + si * 16384 + phase * 128 + 256
            if phase in (1, 12, 13):
                start += 1
            if phase in (3, 14):
                start += 1 << size
            if mode == 2:
                start = MISS + si * 16384
            # Without exclusive support AxLOCK makes a normal access: OKAY,
            # AxLOCK 0 at the target.
            expect = dict(target_lock=0) if mode == 3 else {}
            common = dict(beats=n, size=size, burst=("fixed", "incr", "wrap")[burst],
                          cache=phase % 16, prot=phase % 8, lock=int(mode == 3), **expect)
            t.write(s, start, id=(si + 1) if IDS else 0, **common)
            t.read(s, start, id=(si + 5) % 16 if IDS else 0, **common)
        if GRAPH and phase == 15:
            # s0's W stream pauses inside its 16-beat burst.
            t.wait("s0", "w", ">=", 8, since=w_snap, limit=2000, label="W_PAUSE")
            t.gate("s0", "w", False)
            t.delay(150)
            t.gate("s0", "w", True)
        if mode in (4, 5):
            t.delay(350)
            for s in SOURCES:
                t.ready(s, "b" if mode == 4 else "r", "modulo", 5 if mode == 4 else 7, 0)
        t.drain(limit=20000, label=f"PHASE_{phase}")
        # A reset at every phase boundary also cancels stale state.
        t.reset(4)
        t.delay(2)
    if GRAPH:
        cancel_mid_packet(t)
    t.end()


def cancel_mid_packet(t):
    """A 16-beat write reaches t0 and the common reset lands inside its
    packet: after it nothing replays at any target or source."""
    for tg in TARGETS:
        for ch in ("aw", "w", "ar"):
            t.ready(tg, ch, "always")
    w = t.snap("t0", "w")
    wlast = t.snap("t0", "wlast")
    t.write("s0", 256, beats=16, id=1 if IDS else 0)
    t.wait("t0", "w", ">=", 1, since=w, limit=1000, label="RESET_ARRIVAL")
    t.expect_now("t0", "wlast", "==", 0, "RESET_REACHED_MID_PACKET", since=wlast)
    t.reset(4)
    for tg in TARGETS:
        for ch in ("aw", "w", "ar"):
            t.quiet(tg, ch, True)
    for s in SOURCES:
        t.quiet(s, "b", True)
        t.quiet(s, "r", True)
    t.delay(40)
    for tg in TARGETS:
        for ch in ("aw", "w", "ar"):
            t.quiet(tg, ch, False)
    for s in SOURCES:
        t.quiet(s, "b", False)
        t.quiet(s, "r", False)
