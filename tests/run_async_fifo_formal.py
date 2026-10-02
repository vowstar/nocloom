"""Prove the asynchronous FIFO crossing properties and check designated faults.

control   mode prove (abc pdr): the control members together as FIFO_ALL,
          unbounded on every shape. The unbounded SLOT_STABLE
          checks are part of that invariant. ORDER remains separate.
slot      SLOT_STABLE is included in control on every shape except (10,4),
          where it remains bounded (abc bmc3, 20)
order     ORDER unbounded (abc pdr) on depths 2, 3 and 6; bounded (abc bmc3, 18
          ticks) on (7,3) and (10,2), where abc pdr does not converge and the
          bmc cost roughly doubles with each frame.
          On every shape with ORDER, the last_slot_wrong fault (reading the last
          slot returns slot 0) must fail ORDER within the same bound, which shows
          the bound reaches the index wrap. (10,4) has no ORDER task.
cover     mode cover: full, count wrap, drain and a marked word that waited
          behind a full FIFO must all be reachable
faults    mode bmc: each fault must violate its named property. The binary count
          must also pass the end-to-end proof with the one-bit properties omitted
oneside   the reset handshake under resets of either side alone (oneside_model):
          every property unbounded (abc pdr) on the HANDSHAKE_SHAPES, the covers
          reachable, and in bmc the design without the handshake and the
          handshake whose enable ignores the other side must each violate a
          data property, and the handshake whose counts carry async resets must
          violate a one-bit Gray property
gate      a switch region behind its clock gate (gate_model), per arbiter policy:
          GATE_SAFE unbounded (abc pdr), the gate closing and a wake while closed
          reachable, and an enable without the wake term or an output slice that never
          reads busy must violate GATE_SAFE (--shapes gate runs these alone); and every other
          gated component kind (GATED: the crossing sides, the initiator protocol zones, the
          source and target transport components, the target outlets with timers and exclusive
          monitors) gated as composition gates it (endpoint_model): GATE_EQUAL unbounded with
          its helper invariants STATE_EQUAL and GATE_REST (AHB adds BURST_STATE), all
          asserted together as GATE_ALL, the covers reachable, an enable
          without the wake term or a region idle while it holds a word must violate GATE_EQUAL
          (a crossing's receiver side also: the wake one edge late)
levels    confirmed queue levels and slot ownership, joint unbounded induction on
          (7,3) and (10,4). retain_max must violate the combined assertion FIFO_LEVEL_ALL
cells     the clock gate and synchronizer wrappers bound to library cells
          (generate_async_fifo cells, tests/cell_library.v) equal the behavioral cell
          models (cell_equivalence, multiclock bmc, 24 steps): a flip-flop chained to 2, 3
          and 4 stages with an ICG with a test pin, and a 2-stage synchronizer with an
          active-high clear with an ICG without one, each with the hand-written and with the
          generated library models; the covers reachable; a flip-flop set by its reset, a
          2-stage synchronizer of one stage, an ICG without its latch and the 2-stage
          synchronizer serving a 3-stage chain must each violate their equality
          (--shapes cells runs these alone)
product   the generated two-domain outlet and merge, with actual composition wakes:
          see product_gate_formal.py for the inductive decomposition and fault checks
          (--shapes product runs these alone)
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from async_fifo_bench import inline_synchronizers
from async_fifo_formal import (CELL_COVERS, CELLS, DATA, ENDPOINT, ENDPOINT_COVERS, ENDPOINT_HELPERS, GATE,
                               GATE_COVERS, HANDSHAKE, PROPERTIES, cell_equivalence, endpoint_model,
                               gate_model, model, oneside_model)
from product_gate_formal import product_jobs

# depth, synchronizer stages of every chain, ORDER task ("prove", a bmc bound, or
# None), SLOT_STABLE task ("prove" or a bmc bound)
SHAPES = [(2, 2, "prove", "prove"), (3, 3, "prove", "prove"), (6, 2, "prove", "prove"),
          (7, 3, 18, "prove"), (10, 2, 18, "prove"), (10, 4, None, 20)]
WRAP_BOUND = 18
CONTROL = tuple(p for p in PROPERTIES if p not in ("ORDER", "SLOT_STABLE"))
# depth, synchronizer stages with the reset handshake; every property is proven.
HANDSHAKE_SHAPES = [(2, 2), (3, 3), (6, 2)]
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
# Gated components: label, generate_async_fifo arguments (a .yaml is an examples/ file, or GATING
# written to the work directory). Each kind a gated domain has: the switch crossing sides (the
# arbiters and pipes are the gate tasks), the initiator protocol zone (APB4, AXI4-Lite, AXI4 with
# one and with several bursts in flight), the source transport zone (packet splitter, width
# clamp, fixed-width cut), the target protocol zone (APB4, AHB and AXI4 outlets with timers, APB4 and
# AXI4 outlets with exclusive monitors) and the target transport zone (upsize merge, both
# single-outstanding and queued). The AXI4-Lite
# outlet with a timer or an exclusive monitor and the AHB outlet with an exclusive monitor are not
# listed. These outlets are not in the unbounded set.
GATED = [
    ("crossing_joint", ["crossing_formal", "joint"]),
    ("crossing_handshake", ["crossing_formal", "handshake"]),
    ("initiator_apb4", ["endpoint_formal", "apb_simple.yaml", "initiator:control"]),
    ("initiator_axi4_lite", ["endpoint_formal", "axi_lite_apb.yaml", "initiator:cpu"]),
    ("initiator_axi4", ["endpoint_formal", "axi4_target_timeout.yaml", "initiator:cpu0"]),
    ("initiator_axi4_outstanding", ["endpoint_formal", "axi4_outstanding.yaml", "initiator:cpu0"]),
    ("packets", ["endpoint_formal", "axi4_apb_targets.yaml", "packets:cpu0"]),
    ("width_cut", ["endpoint_formal", "axi4_downsize.yaml", "conversion:s0"]),
    ("clamp", ["endpoint_formal", "axi4_endpoint_widths.yaml", "conversion:wide"]),
    ("outlet_apb4", ["endpoint_formal", "axi4_target_timeout.yaml", "outlet:timer0"]),
    ("outlet_apb4_timer", ["endpoint_formal", "axi4_target_timeout.yaml", "outlet:uart0"]),
    ("outlet_ahb_timer", ["endpoint_formal", "axi4_target_timeout.yaml", "outlet:rom0"]),
    ("outlet_axi4_timer", ["endpoint_formal", "axi4_target_timeout.yaml", "outlet:mem0"]),
    ("outlet_apb4_exclusive", ["endpoint_formal", "gating.yaml", "outlet:regs"]),
    ("outlet_axi4_exclusive", ["endpoint_formal", "gating.yaml", "outlet:mem"]),
    ("upsize", ["endpoint_formal", "gating.yaml", "merge:mem"]),
    ("upsize_queued", ["endpoint_formal", "gating2.yaml", "merge:mem"]),
]
# Targets with a timer and an exclusive monitor each, the AXI4 one wider than the network word.
GATING = """schema_version: 10
name: gating
address_width: 16
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu, clock_domain: main, protocol: axi4, data_width: 32, id_width: 2, len_width: 8, user_flags: {owner: 0}}
targets:
  - {name: mem, clock_domain: main, protocol: axi4, data_width: 64, len_width: 8, attributes: amba,
     exclusive: network, exclusive_id_flags: [owner], mappings: [{base: '0x0', size: '0x1000'}], timeout: {cycles: 1024, completion: true}}
  - {name: regs, clock_domain: main, protocol: apb4, data_width: 32, attributes: amba, exclusive: network, exclusive_id_flags: [owner],
     mappings: [{base: '0x1000', size: '0x100'}], timeout: {cycles: 1024}}
  - {name: port, clock_domain: main, protocol: axi4_lite, data_width: 32, attributes: amba,
     exclusive: network, exclusive_id_flags: [owner], mappings: [{base: '0x2000', size: '0x100'}], timeout: {cycles: 1024}}
  - {name: bus, clock_domain: main, protocol: ahb_v3, data_width: 32, attributes: amba, exclusive: network, exclusive_id_flags: [owner],
     mappings: [{base: '0x3000', size: '0x100'}], timeout: {cycles: 1024}}
transport: {response_codes: distinct, user_flags: [owner]}
connectivity: all
topology:
  request:
    nodes: [{name: q}]
    links: []
    entries: [{endpoint: cpu, node: q}]
    exits: [{endpoint: mem, node: q}, {endpoint: regs, node: q}, {endpoint: port, node: q}, {endpoint: bus, node: q}]
  response:
    nodes: [{name: p}]
    links: []
    entries: [{endpoint: mem, node: p}, {endpoint: regs, node: p}, {endpoint: port, node: p}, {endpoint: bus, node: p}]
    exits: [{endpoint: cpu, node: p}]
output: {backend: verilog, language: verilog_2001}
"""
# A 128-bit target holding four merge contexts behind a 32-bit network word: the queued
# read-merge path of the target-side upsize, where a queued merge waiting on the downstream
# request channel must keep the gate open.
GATING2 = """schema_version: 10
name: gating2
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains: [{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}]
initiators:
  - {name: cpu, clock_domain: main, protocol: axi4, data_width: 32, id_width: 16, len_width: 8, max_outstanding: 4}
  - {name: dma, clock_domain: main, protocol: axi4, data_width: 64, id_width: 16, len_width: 8, max_outstanding: 2}
targets:
  - {name: mem, clock_domain: main, protocol: axi4, data_width: 128, len_width: 8, attributes: amba,
     max_outstanding: 4, mappings: [{base: '0x0', size: '0x100000'}]}
  - {name: aux, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8, attributes: amba,
     mappings: [{base: '0x100000', size: '0x1000'}]}
transport: {response_codes: distinct, data_width: 32}
connectivity: all
arbitration: {policy: round_robin}
output: {backend: verilog, language: verilog_2001}
"""
# Measured long tasks, longest first, relative to the run directory.
# Start them first to keep them out of the pool's tail.
HEAVY = [
    'product_64_k4_distinct_axi3_none/cover_decode',
    'product_128_k4_distinct_none/prove',
    'product_64_k2_distinct_none/cover_decode',
    'product_128_k3_distinct_none/prove',
    'side_upsize_queued_none_prove/sby',
    'd6_n2_none_hs/oneside',
    'product_128_k2_distinct_none/prove',
    'product_64_k4_distinct_none/cover_decode',
    'product_64_k4_distinct_none/prove',
    'product_128_k4_distinct_none/cover',
    'product_64_k1_distinct_none/hold',
    'product_64_k4_distinct_axi3_none/prove',
    'product_64_k4_distinct_write_only_none/prove',
    'product_64_k4_uniform_none/prove',
    'product_64_k3_distinct_none/prove',
    'product_64_k3_distinct_write_only_none/cover',
    'product_64_k4_distinct_write_only_face_narrow/equivalent',
    'product_64_k4_distinct_write_only_none/cover',
    'product_64_k3_distinct_none/cover_decode',
    'product_128_k4_distinct_none/cover_decode',
    'product_64_k4_distinct_axi3_none/cover',
    'product_128_k2_distinct_none/cover',
    'product_128_k1_distinct_none/cover',
    'product_64_k4_distinct_none/cover',
    'product_64_k3_distinct_none/cover',
    'd10_n2_none/control',
    'levels_d10_n4_none/proof',
    'product_64_k3_distinct_write_only_none/prove',
    'product_64_k3_distinct_write_only_face_narrow/equivalent',
    'product_128_k1_distinct_none/prove',
    'product_128_k1_distinct_none/hold',
    'product_128_k3_distinct_none/cover',
    'product_64_k2_distinct_none/prove',
    'side_packets_none_prove/sby',
    'product_128_k2_distinct_none/cover_decode',
    'd10_n4_none/control',
    'd10_n2_none/order',
    'product_128_k3_distinct_none/cover_decode',
    'd7_n3_none/control',
]
FAULTS = [("binary_count", ("GRAY_ONE_BIT_W", "GRAY_ONE_BIT_R")),
          ("full_late", ("NO_OVERFLOW", "SLOT_STABLE", "ORDER"))]


def split_cover_budgets(rows, limit):
    groups = {}
    for row in rows:
        if row["stage"] == "product_cover" and row["fault"] == "none":
            groups.setdefault(row["shape"][0], []).append(row)
    budgets = []
    for shape, members in groups.items():
        if len(members) != 2:
            continue
        seconds = sum(row["seconds"] for row in members)
        accepted = seconds <= limit and all(row["accepted"] for row in members)
        budgets.append(dict(task=shape + "/cover", seconds=seconds, limit=limit,
                            status="PASS" if accepted else "FAIL"))
    return budgets


def sby(directory, text, mode, engine, depth, timeout, top="nocloom_async_fifo"):
    directory.mkdir()
    (directory / "formal.v").write_text(text)
    trace = "aigsmt z3\n" if engine.startswith("abc") else ""
    trace += "multiclock on\n" if top == "nl_cells_equal" else ""
    (directory / "task.sby").write_text(
        f"[options]\nmode {mode}\ndepth {depth}\ntimeout {timeout}\n{trace}"
        f"[engines]\n{engine}\n[script]\nread -formal formal.v\nprep -top {top}\n"
        "[files]\nformal.v\n")
    env = dict(os.environ)
    # sby helpers use `#!/usr/bin/env python3`; bind it to this interpreter.
    shim = directory / "python-shim"
    shim.mkdir()
    (shim / "python3").symlink_to(sys.executable)
    env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
    try:
        run = subprocess.run(["sby", "-f", "task.sby"], cwd=directory, env=env, capture_output=True,
                             text=True, timeout=timeout + 120)
        log, code = run.stdout + run.stderr, run.returncode
    except subprocess.TimeoutExpired:
        log, code = "", "timeout"
    (directory / "run.log").write_text(log)
    status_file = directory / "task/status"
    status = status_file.read_text().split()[0] if status_file.exists() else "ERROR"
    return status, code, log


def violated(log):
    """Failed property names, from smtbmc or abc engine summaries."""
    names = set(re.findall(r"Assert failed in \w+: (\w+)", log))
    names |= set(re.findall(r"failed assertion \w+\.(\w+)", log))
    return sorted(names)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fixture", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--timeout", type=int, default=1800, help="seconds per sby task")
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--check-driver", action="store_true", help="check result rejection before proving")
    p.add_argument("--self-test", action="store_true", help="check the driver without running proofs")
    p.add_argument("--shapes", default=os.environ.get("NOCLOOM_ASYNC_FORMAL_SHAPES", "all"),
                   help="comma separated indexes into SHAPES, all, gate, cells, levels or product")
    p.add_argument("--only", default=os.environ.get("NOCLOOM_ASYNC_FORMAL_ONLY", ""),
                   help="comma separated GATED labels to run alone (for focused reruns)")
    p.add_argument("--stages", default=os.environ.get("NOCLOOM_ASYNC_FORMAL_STAGES", ""),
                   help="comma separated task stages to run alone")
    args = p.parse_args()
    root = Path(args.work).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.check_driver or args.self_test:
        from async_fifo_formal_driver_test import check_driver
        check_driver(sys.modules[__name__], args.fixture, root / "driver-controls")
        if args.self_test:
            return 0
    root = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    shapes = (SHAPES if args.shapes == "all" else [] if args.shapes in ("gate", "cells", "product", "levels")
              else [SHAPES[int(i)] for i in args.shapes.split(",")])

    def generate(depth, n, fault, handshake=False, tag=""):
        work = root / f"d{depth}_n{n}_{fault}{'_hs' if handshake else ''}{tag}"
        work.mkdir()
        argv = [args.fixture, str(work), str(depth), str(n), fault, "narrow"] + (
            ["handshake"] if handshake else [])
        subprocess.run(argv, check=True, timeout=60)
        rtl = inline_synchronizers((work / "generated.v").read_text(), (work / "cells.v").read_text())
        return work, json.loads((work / "bindings.json").read_text()), rtl

    def passed(status, log):
        return status == "PASS"

    jobs = []  # (row, directory, text, mode, engine, depth, accept)
    if args.shapes in ("all", "levels") and not args.only:
        from fifo_levels_formal import level_jobs
        jobs.extend(level_jobs(root, args.fixture, passed, violated))
    if args.shapes in ("all", "product") and (not args.only or args.only.startswith("product_")):
        jobs.extend(product_jobs(root, args.fixture, violated, only=args.only))
    for depth, n, order, slot in shapes:
        work, meta, rtl = generate(depth, n, "none")
        shape = [depth, n]
        only_order = model(rtl, meta, CONTROL + ("SLOT_STABLE",))
        members = CONTROL + (("SLOT_STABLE",) if slot == "prove" else ())
        omitted = tuple(p for p in PROPERTIES if p not in members)
        jobs.append((dict(shape=shape, fault="none", stage="control_prove", properties=list(members)),
                     work / "control", model(rtl, meta, omitted, combine=True), "prove", "abc pdr -l",
                     20, passed))
        if slot != "prove":
            jobs.append((dict(shape=shape, fault="none", stage=f"slot_bmc{slot}",
                              properties=["SLOT_STABLE"]),
                         work / "slot", model(rtl, meta, CONTROL + ("ORDER",)),
                         "bmc", "abc bmc3", slot, passed))
        if order == "prove":
            jobs.append((dict(shape=shape, fault="none", stage="order_prove", properties=["ORDER"]),
                         work / "order", only_order, "prove", "abc pdr", 20, passed))
        elif order:
            jobs.append((dict(shape=shape, fault="none", stage=f"order_bmc{order}",
                              properties=["ORDER"]),
                         work / "order", only_order, "bmc", "abc bmc3", order, passed))
        if order:
            bound = order if order != "prove" else WRAP_BOUND
            fwork, fmeta, frtl = generate(depth, n, "last_slot_wrong")
            jobs.append((dict(shape=shape, fault="last_slot_wrong", stage=f"order_bmc{bound}",
                              expected=["ORDER"]),
                         fwork / "bmc", model(frtl, fmeta, CONTROL + ("SLOT_STABLE",)), "bmc",
                         "abc bmc3", bound,
                         lambda status, log: status == "FAIL" and violated(log) == ["ORDER"]))
        jobs.append((dict(shape=shape, fault="none", stage="cover",
                          properties=["COVER_FULL", "COVER_WRAP", "COVER_DRAIN", "COVER_MARKED"]),
                     work / "cover", model(rtl, meta), "cover", "smtbmc z3", 24 + 2 * depth, passed))
        for fault, labels in FAULTS:
            if fault == "full_late" and depth + 1 == 1 << meta["count_bits"]:
                continue  # depth + 1 wraps to zero: that fault deadlocks instead
            fwork, fmeta, frtl = generate(depth, n, fault)

            def caught(status, log, labels=labels):
                return status == "FAIL" and bool(set(violated(log)) & set(labels))
            jobs.append((dict(shape=shape, fault=fault, stage="bmc", expected=list(labels)),
                         fwork / "bmc", model(frtl, fmeta), "bmc", "smtbmc z3", 30, caught))
            if fault == "binary_count":
                jobs.append((dict(shape=shape, fault=fault, stage="control_negative",
                                  expected=["FIFO_ALL"], members=list(members)),
                             fwork / "joint_control", model(frtl, fmeta, omitted, combine=True),
                             "bmc", "abc bmc3", 30,
                             lambda status, log: status == "FAIL" and violated(log) == ["FIFO_ALL"]))
            if fault == "binary_count" and order == "prove" and depth <= 3:
                jobs.append((dict(shape=shape, fault=fault, stage="end_to_end_prove", expected="PASS"),
                             fwork / "end_to_end", model(frtl, fmeta, labels), "prove", "abc pdr", 20,
                             passed))

    def caught_data(status, log):
        return status == "FAIL" and bool(set(violated(log)) & set(DATA))

    def caught_gray(status, log):
        return status == "FAIL" and bool(set(violated(log)) & {"GRAY_ENABLED_W", "GRAY_ENABLED_R"})
    for depth, n in (HANDSHAKE_SHAPES if args.shapes == "all" else []):
        shape = [depth, n]
        work, meta, rtl = generate(depth, n, "none", True)
        jobs.append((dict(shape=shape, fault="none", stage="oneside_prove",
                          properties=list(DATA + HANDSHAKE)),
                     work / "oneside", oneside_model(rtl, meta), "prove", "abc pdr", 20, passed))
        jobs.append((dict(shape=shape, fault="none", stage="oneside_cover",
                          properties=["COVER_MARK_AFTER_RESET", "COVER_LOSS"]),
                     work / "oneside_cover", oneside_model(rtl, meta), "cover", "smtbmc z3", 40,
                     passed))
        for fault, handshake in (("none", False), ("peer_unmasked", True)):
            fwork, fmeta, frtl = generate(depth, n, fault, handshake, "_oneside")
            jobs.append((dict(shape=shape, fault=fault if handshake else "no_handshake",
                              stage="oneside_bmc", expected=list(DATA)),
                         fwork / ("oneside_" + fault), oneside_model(frtl, fmeta, HANDSHAKE), "bmc",
                         "smtbmc z3", 40, caught_data))
        # The design keeps the handshake counts without reset pins exactly so that this
        # control fails: with async-reset counts a one-side reset moves several count bits
        # while the other side is still enabled.
        fwork, fmeta, frtl = generate(depth, n, "count_reset", True, "_oneside")
        jobs.append((dict(shape=shape, fault="count_reset", stage="oneside_bmc",
                          expected=["GRAY_ENABLED_W", "GRAY_ENABLED_R"]),
                     fwork / "oneside_count_reset", oneside_model(frtl, fmeta), "bmc",
                     "smtbmc z3", 40, caught_gray))

    # Clock gate safety of a switch region (generate_async_fifo gate_formal), per arbiter
    # policy: proven, the covers reachable, and each fault violates GATE_SAFE.
    def caught_gate(status, log):
        return status == "FAIL" and violated(log) == list(GATE)
    for policy in (("round_robin", "fifo") if args.shapes in ("all", "gate") else ()):
        for fault, mode, engine, depth, accept, labels in (
                ("none", "prove", "abc pdr", 20, passed, GATE),
                ("none", "cover", "smtbmc z3", 30, passed, GATE_COVERS),
                ("wake_registered", "bmc", "smtbmc z3", 20, caught_gate, GATE),
                ("idle_ignores_output", "bmc", "smtbmc z3", 20, caught_gate, GATE)):
            work = root / f"gate_{policy}_{fault}_{mode}"
            subprocess.run([args.fixture, str(work), "gate_formal", policy, fault], check=True,
                           timeout=60)
            key = "properties" if fault == "none" else "expected"
            jobs.append((dict(shape=[policy], fault=fault, stage="gate_" + mode, top="nocloom_gate_formal",
                              **{key: list(labels)}),
                         work / "sby", gate_model((work / "generated.v").read_text()), mode, engine,
                         depth, accept))
    # Gated components (generate_async_fifo endpoint_formal, crossing_formal): the gated copy equals
    # the ungated one, unbounded with its helper invariants; the covers reachable; an enable
    # without the wake term and a region idle while it holds a word violate GATE_EQUAL.
    def caught_side(status, log):
        return status == "FAIL" and violated(log) == list(ENDPOINT)
    if args.shapes in ("all", "gate"):
        (root / "gating.yaml").write_text(GATING)
        (root / "gating2.yaml").write_text(GATING2)
    only = set(args.only.split(",")) if args.only else None
    for side, argv in (GATED if args.shapes in ("all", "gate") else ()):
        if only is not None and side not in only:
            continue
        argv = [str(root / a) if a in ("gating.yaml", "gating2.yaml")
                else str(EXAMPLES / a) if a.endswith(".yaml") else a for a in argv]
        # A crossing's receiver gate also faces its wake one edge late.
        side_faults = ("wake_registered", "idle_held") + (
            ("wake_delayed",) if side.startswith("crossing_") else ())
        # The mutation BMCs use abc bmc3 (aigsmt z3 replays the trace and names the failed
        # assertion).
        for fault, mode, engine, depth, accept, labels in (
                ("none", "prove", "abc pdr", 20, passed, ENDPOINT + ENDPOINT_HELPERS),
                ("none", "cover", "smtbmc z3", 30, passed, ENDPOINT_COVERS),) + tuple(
                (f, "bmc", "abc bmc3", 20, caught_side, ENDPOINT) for f in side_faults):
            work = root / f"side_{side}_{fault}_{mode}"
            subprocess.run([args.fixture, str(work)] + argv + [fault], check=True, timeout=60)
            text = (work / "generated.v").read_text()
            if mode == "prove" and "output wire nl_burst_invariant," in text:
                labels += ("BURST_STATE",)
            key = "properties" if fault == "none" else "expected"
            jobs.append((dict(shape=[side], fault=fault, stage="side_" + mode,
                              top="nocloom_gate_formal", **{key: list(labels)}),
                         work / "sby", endpoint_model(text, mode == "prove", side == "crossing_joint"),
                         mode, engine, depth, accept))

    # Cell bindings (generate_async_fifo cells): bound wrappers equal the behavioral models.
    def caught_cell(labels):
        return lambda status, log: status == "FAIL" and violated(log) == list(labels)
    if args.shapes in ("all", "cells"):
        work = root / "cells"
        subprocess.run([args.fixture, str(work), "cells"], check=True, timeout=60)
        text = lambda name: (work / name).read_text()  # noqa: E731
        library = (Path(__file__).resolve().parent / "cell_library.v").read_text()
        broken = {"dff_set": ("    Q <= 1'b0;", "    Q <= 1'b1;"),
                  "sync2_one_stage": ("stage <= {stage[0], D};", "stage <= {D, D};"),
                  "icg_no_latch": ("assign Q = CP & open;", "assign Q = CP & (E | TE);")}
        for binding, stages, models, fault, mode, labels in (
                [("te_dff", n, "library", "none", "bmc", CELLS) for n in (2, 3, 4)]
                + [("te_dff", 2, "generated", "none", "bmc", CELLS),
                   ("rn_sync2", 2, "library", "none", "bmc", CELLS),
                   ("rn_sync2", 2, "generated", "none", "bmc", CELLS),
                   ("te_dff", 3, "library", "none", "cover", CELL_COVERS),
                   ("te_dff", 2, "library", "dff_set", "bmc", ("SYNC_EQUAL",)),
                   ("rn_sync2", 2, "library", "sync2_one_stage", "bmc", ("SYNC_EQUAL",)),
                   ("te_dff", 2, "library", "icg_no_latch", "bmc", ("CLOCK_EQUAL",)),
                   ("rn_sync2", 3, "library", "none", "bmc", ("SYNC_EQUAL",))]):
            cells = text(f"models_{binding}.v") if models == "generated" else library
            if fault != "none":
                old, new = broken[fault]
                if cells.count(old) != 1:
                    raise AssertionError(f"fault {fault} not found once")
                cells = cells.replace(old, new)
            wrong = fault != "none" or (binding == "rn_sync2" and stages != 2)
            key = "expected" if wrong else "properties"
            jobs.append((dict(shape=[binding, stages, models], fault=fault, stage="cells_" + mode,
                              top="nl_cells_equal", **{key: list(labels)}),
                         work / f"{binding}_{stages}_{models}_{fault}_{mode}",
                         cell_equivalence(text("default.v"), text(f"bound_{binding}.v"), cells, stages),
                         mode, "smtbmc z3", 24, caught_cell(labels) if wrong else passed))

    def execute(job):
        row, directory, text, mode, engine, depth, accept = job
        assertions = re.findall(r"^\s*(\w+):\s*assert\s*\(", text, flags=re.M)
        covers = re.findall(r"^\s*(\w+):\s*cover\s*\(", text, flags=re.M)
        row = dict(row)
        if (row["stage"].startswith("product_") and row["fault"] == "none"
                and row["stage"] != "product_hold"):
            row["properties"] = covers if mode == "cover" else assertions
        started = time.monotonic()
        status, code, log = sby(directory, text, mode, engine, depth, args.timeout,
                                row.get("top", "nocloom_async_fifo"))
        return dict(row, mode=mode, engine=engine, depth=depth, status=status, exit=code,
                    seconds=time.monotonic() - started,
                    assertions=assertions, covers=covers,
                    violated=violated(log), accepted=bool(accept(status, log))
                    and (status, code) in (("PASS", 0), ("FAIL", 2)),
                    formal_sha256=hashlib.sha256(text.encode()).hexdigest(), log=str(directory / "run.log"))

    # Longest first: the known heavy tasks start in HEAVY order so none of them lands in the
    # pool's tail, the rest follow in build order; rows are reported in the original order.
    rank = {name: i for i, name in enumerate(HEAVY)}
    built = {job[1].relative_to(root).as_posix() for job in jobs}
    if args.shapes == "all" and not args.only and not set(HEAVY) <= built:
        raise AssertionError(f"HEAVY names no task: {sorted(set(HEAVY) - built)}")
    if args.stages:
        selected = set(args.stages.split(","))
        if selected & {"product_prove", "product_or_prove", "product_equivalent"}:
            selected.add("product_hold")
        jobs = [job for job in jobs if job[0]["stage"] in selected]
    if not jobs:
        raise AssertionError("no formal tasks selected")
    order = sorted(range(len(jobs)),
                   key=lambda i: rank.get(jobs[i][1].relative_to(root).as_posix(), len(HEAVY)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        done = dict(zip(order, pool.map(execute, [jobs[i] for i in order])))
    rows = [done[i] for i in range(len(jobs))]
    for row in rows:
        if row["stage"] in ("product_prove", "product_or_prove", "product_equivalent"):
            row["hold_discharged"] = any(
                r["stage"] == "product_hold" and r["shape"] == row["shape"] and r["accepted"]
                and r["configuration_sha256"] == row["configuration_sha256"]
                and r["product_sha256"] == row["product_sha256"]
                for r in rows)
            row["accepted"] &= row["hold_discharged"]
        if row["stage"] == "product_functional":
            row["observation"] = {"PASS": "bounded equality", "FAIL": "difference"}.get(
                row["status"], "undetermined")
    # Splitting witnesses must not increase their original cumulative budget.
    budgets = split_cover_budgets(rows, args.timeout)
    (root / "budgets.json").write_text(json.dumps(budgets, indent=2) + "\n")
    (root / "results.json").write_text(json.dumps(rows, indent=2) + "\n")
    for r in rows:
        print("FORMAL", r["shape"], r["fault"], r["stage"], r["status"], ",".join(r["violated"]),
              "ok" if r["accepted"] else "REJECTED")
    print(f"results: {root / 'results.json'}")
    for budget in budgets:
        print("COVER_BUDGET", budget["task"], budget["seconds"], budget["limit"], budget["status"])
    if any(not r["accepted"] for r in rows) or any(b["status"] != "PASS" for b in budgets):
        return 1
    print(f"ASYNC_FIFO_FORMAL_PASS shapes={len(shapes)} tasks={len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
