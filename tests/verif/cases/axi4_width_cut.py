"""Wide AXI4 initiator -> fixed-width cutter -> 32-bit AXI4 target outlet.
"""

from nlverif import model

PARAMS = globals().get("PARAMS", {})
DATA_BITS = int(PARAMS.get("data_bits", "128"))
if DATA_BITS not in (64, 128, 256, 512, 1024):
    raise ValueError(f"data_bits {DATA_BITS} not in 64..1024")

CASE = dict(
    name=f"axi4_width_cut_{DATA_BITS}_32",
    contracts=["AXI.WIDTH.DOWNSIZE.PIECES", "AXI.WIDTH.FIXED_REFUSE", "AXI.MERGE.SEVERITY",
               "AXI.RESET.CANCEL", "AXI.W.UNSTROBED_ZERO"],
    dut=dict(source="fixture", recipe="axi4_width_cut", params=dict(data_bits=str(DATA_BITS))),
    nets={"front": [("nl_axi_", "s_"), ("nl_", "u_")],
          "bridge": [("nl_up_", "u_"), ("nl_down_", "d_")],
          "target": [("nl_axi_", "t_"), ("nl_", "d_")]},
    bfms=[dict(name="s0", kind="axi_master", component="front", endpoint="axi"),
          dict(name="t0", kind="axi_slave", component="target", endpoint="axi", salt=5)],
    streams=[dict(name="u_wrsp", component="front", endpoint="wrsp"),
             dict(name="u_rrsp", component="front", endpoint="rrsp")],
    model=dict(routes={"s0": [("t0", 0, 1 << 32)]},
               targets={"t0": dict(kind="downsize", bus_bytes=4, error_data_zero=False,
                                   fixed_reject=True, cache=True, ids=True)}),
    max_cycles=3000000,
    run_timeout=600,
    faults=[
        dict(id="sub_hold", component="bridge", op="set_next", at={"binding": "w.sub_address"},
             **{"with": {"self": True}},
             expect=["TARGET_WRITE_BYTES", "TARGET_FOOTPRINT:w", "TARGET_UNATTRIBUTED:w"],
             contract="AXI.WIDTH.DOWNSIZE.PIECES"),
        dict(id="piece_every_word", component="bridge", op="set_operand", operand=1,
             at={"binding": "w.piece_last", "where": {"op": "Equal"}},
             **{"with": {"operand": 0}},
             expect=["TARGET_DESCRIPTOR", "TAPE.WAIT_TIMEOUT:PROGRESS"],
             contract="AXI.WIDTH.DOWNSIZE.PIECES"),
        dict(id="next_piece_lost", component="bridge", op="replace", at={"binding": "r.next_piece"},
             **{"with": {"const": 0}}, expect=["TAPE.WAIT_TIMEOUT:PROGRESS"],
             contract="AXI.WIDTH.DOWNSIZE.PIECES"),
        dict(id="merge_lost", component="bridge", op="set_next", at={"binding": "w.result"},
             **{"with": {"const": 0}}, expect=["SOURCE_B_RESP"],
             contract="AXI.MERGE.SEVERITY"),
        dict(id="read_early", component="bridge", op="replace",
             at={"binding": "r.beat_end"}, **{"with": {"const": 1}},
             expect=["SOURCE_R_RESP", "STREAM_FIELD", "SOURCE_R_DATA", "SOURCE_R_COUNT", "AXI.R.LAST"],
             contract="AXI.WIDTH.DOWNSIZE.PIECES"),
        dict(id="strobe_lost", component="bridge", op="set_driver", port="nl_down_wreq_be",
             **{"with": {"const": 0}}, expect=["TARGET_WRITE_BYTES"],
             contract="AXI.WIDTH.DOWNSIZE.PIECES"),
        dict(id="restart_lost", component="bridge", op="set_next", at={"binding": "r.restart"},
             **{"with": {"self": True}},
             expect=["TARGET_DESCRIPTOR", "TAPE.WAIT_TIMEOUT:PROGRESS", "SOURCE_R_DATA"],
             contract="AXI.WIDTH.DOWNSIZE.PIECES"),
        dict(id="fixed_cut", component="bridge", op="variant", params={"fixed_reject": "off"},
             expect=["CONTRACT.QUIET.AW", "CONTRACT.QUIET.W", "CONTRACT.QUIET.AR", "TARGET_ACCESS"],
             contract="AXI.WIDTH.FIXED_REFUSE",
             note="parameter-level variant: fixed_reject window removed"),
    ],
)


def cases_for(data_bits):
    """The cases (beats, size, burst, start, mode, abort) of one data width."""
    lw = (data_bits // 8).bit_length() - 1
    out = [(4, 2, 1, 0x100c, 4, 0), (8, 1, 1, 0x1001, 0, 0), (16, 0, 1, 0x1003, 0, 0),
           (3, 1, 0, 0x1102, 0, 0), (4, 2, 2, 0x114c, 0, 0), (4, 2, 1, 0x1004, 3, 0)]
    if data_bits > 256:
        out.append((4, 2, 1, 4096 + data_bits // 8 - 4, 4, 0))
    for sz in range(3, lw + 1):
        step = 1 << sz
        limit = 256 * 4 // step
        page = 4096 // step
        out += [(4, sz, 1, 0x1000, 0, 0), (limit, sz, 1, 0x1000, 4, 0),
                (min(limit + 1, page), sz, 1, 0x1000, 0, 0),
                (min(256, page), sz, 1, 0x1000, 3, 0), (min(256, page), sz, 1, 0x1000, 5, 0),
                (3, sz, 1, 0x1000 + step + 6, 4, 0), (min(256, page) - 1, sz, 1, 0x1004, 0, 0),
                (1, sz, 1, 0x2000 - step, 0, 0), (2, sz, 1, 0x2000 - step, 0, 0),
                (2, sz, 0, 0x1100, 0, 0), (3, sz, 0, 0x1100 + 6, 4, 0), (16, sz, 0, 0x1100, 5, 0),
                (4, sz, 1, 0x1000, 1, 0), (4, sz, 1, 0x1000, 2, 0),
                (min(256, page), sz, 1, 0x1000, 6, 0), (min(256, page), sz, 1, 0x1000, 7, 0),
                (8, sz, 0, 0x1100, 7, 0)]
        for n in (2, 4, 8, 16):
            span = n * step
            out += [(n, sz, 2, 0x1000 + span + span // 2, 0, 0),
                    (n, sz, 2, 0x1000 + 2 * span - step, 3 if n >= 8 else 0, 0)]
        out.append((16, sz, 2, 0x1000 + 16 * step + 4 * step, 5, 0))
        long = limit + 1 if limit + 1 <= page else 4
        out += [(long, sz, 1, 0x1000, 0, 1), (long, sz, 1, 0x1000, 0, 2),
                (16, sz, 2, 0x1000 + 16 * step + 8 * step, 0, 2),
                (long, sz, 1, 0x1000, 0, 3), (long, sz, 1, 0x1000, 0, 4),
                (16, sz, 0, 0x1100, 3, 5),
                (16, sz, 0, 0x1100, 5, 0), (long, sz, 1, 0x1000, 0, -1),
                (16, sz, 2, 0x1000 + 16 * step + 4 * step, 0, -1), (3, sz, 1, 0x1000 + 6, 4, -1)]
    return out


def scenario(t, seed):
    s0, t0 = "s0", "t0"
    t.ready(s0, "b", "modulo", 7, 0)
    t.ready(s0, "r", "modulo", 5, 0)
    t.ready(t0, "aw", "modulo", 3, 0)
    t.ready(t0, "w", "modulo", 4, 0)
    t.ready(t0, "ar", "modulo", 3, 0)
    for index, (n, sz, burst, start, mode, abort) in enumerate(cases_for(DATA_BITS)):
        if abort >= 0:
            t.reset(4)
            t.delay(3)
        t.mark("case", index)
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
        # Target response program for this case (indices since the base mark).
        t.set(t0, "base", 1)
        for k in range(4):
            t.set(t0, f"b_ovr_i{k}", 0)
            t.set(t0, f"r_ovr_i{k}", 0)
        t.set(t0, "b_default", {1: 2, 2: 3, 6: 2}.get(mode, 0))
        if mode == 3:
            t.set(t0, "r_ovr_i0", ea + 1)
            t.set(t0, "r_ovr_c0", 2)
            t.set(t0, "r_ovr_i1", eb + 1)
            t.set(t0, "r_ovr_c1", 3)
        elif mode == 5:
            t.set(t0, "b_ovr_i0", err_piece + 1)
            t.set(t0, "b_ovr_c0", 2)
        elif mode == 6:
            t.set(t0, "b_ovr_i0", 1)
            t.set(t0, "b_ovr_c0", 3)
        elif mode == 7:
            t.set(t0, "b_ovr_i0", 1)
            t.set(t0, "b_ovr_c0", 2)
            if np_ > 1:
                t.set(t0, "b_ovr_i1", np_)
                t.set(t0, "b_ovr_c1", 3)
        if refused:
            for ch in ("aw", "w", "ar"):
                t.quiet(t0, ch, True)
        snaps = {(p, k): t.snap(p, k) for p, k in ((s0, "b"), (s0, "rlast"), (s0, "w"), (t0, "w"),
                                               (t0, "r"), (t0, "b"), (t0, "rlast"))}
        cache = 0 if (index + 1) % 2 else 2
        t.write(s0, start, beats=n, size=sz, burst=("fixed", "incr", "wrap")[burst], id=5,
                cache=cache, prot=5, strobe="sparse" if mode == 4 else "full")
        t.read(s0, start + 0x4000, beats=n, size=sz, burst=("fixed", "incr", "wrap")[burst],
               id=6, cache=cache, prot=3)
        if abort > 0:
            port, counter = {1: (t0, "w"), 2: (t0, "r"), 3: (t0, "b"), 4: (t0, "rlast"),
                             5: (s0, "w")}[abort]
            t.wait(port, counter, ">=", 1, since=snaps[(port, counter)], limit=20000,
                   label="RESET_REACHED")
        else:
            t.wait(s0, "b", "==", 1, since=snaps[(s0, "b")], limit=60000, label="PROGRESS")
            t.wait(s0, "rlast", "==", 1, since=snaps[(s0, "rlast")], limit=60000, label="PROGRESS")
        if refused:
            for ch in ("aw", "w", "ar"):
                t.quiet(t0, ch, False)
    t.drain()
    t.end()
    t.check(kind="stream_beats", stream="u_rrsp", port="s0", dir="r")
    t.check(kind="stream_beats", stream="u_wrsp", port="s0", dir="w", per="burst")
