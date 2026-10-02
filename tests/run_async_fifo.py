"""Generate asynchronous FIFO crossings and run them across clock ratios.

Cases:
  ratios      ordering, loss, duplication, stall, qualification, latency, a sender
              started before its stopped receiver, and mid-stream resets of both
              sides released in random order over fixed and seeded random clock
              ratios with per-edge jitter and the synchronizer sampling-skew model;
              with the reset handshake also one side reset alone mid-stream
  throughput  steady-state words per slow-clock period at the formula depth,
              and the bubble at one entry less with aligned equal clocks
  faults      designated faults must fail with their named check; the
              unmodified design passes the same stimulus; the same for the
              clock gate cell (clock_gate_bench.py). One side reset alone must
              deliver a stale or repeated word without the handshake and with
              the handshake's enable ignoring the other side
"""
import argparse
import hashlib
import json
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from async_fifo_bench import PASS, inline_synchronizers, instrument, parse, write_bench
import clock_gate_bench
from sim_engine import Simulation


def formula(n, ta, tb):
    return -(-((n + 1) * (ta + tb)) // max(ta, tb))


class Designs:
    def __init__(self, fixture, root):
        self.fixture = fixture
        self.root = root
        self.cache = {}

    def get(self, depth, n, fault="none", handshake=False):
        key = (depth, n, fault, handshake)
        if key in self.cache:
            return self.cache[key]
        work = self.root / "designs" / f"d{depth}_n{n}_{fault}{'_hs' if handshake else ''}"
        work.mkdir(parents=True)
        argv = [self.fixture, str(work), str(depth), str(n), fault] + (["handshake"] if handshake else [])
        generated = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        (work / "generate.log").write_text(generated.stdout + generated.stderr)
        if generated.returncode:
            raise AssertionError(f"fixture failed: {argv}: {generated.stderr}")
        meta = json.loads((work / "bindings.json").read_text())
        rtl = inline_synchronizers((work / "generated.v").read_text(), (work / "cells.v").read_text())
        (work / "network.v").write_text(instrument(rtl, meta))
        write_bench(work, meta)
        engine = Simulation(work, "network.v", compile_timeout=900, run_timeout=900)
        design = dict(work=work, engine=engine, meta=meta, argv=argv, runs=0,
                      generated_sha256=hashlib.sha256((work / "generated.v").read_bytes()).hexdigest(),
                      rtl_sha256=hashlib.sha256((work / "network.v").read_bytes()).hexdigest(),
                      tb_sha256=hashlib.sha256((work / "tb.sv").read_bytes()).hexdigest())
        self.cache[key] = design
        return design

    @staticmethod
    def run(design, name, args):
        flags = ("meta", "no_cdc_monitor")
        options = [f"+{k}={v}" for k, v in args.items() if k not in flags]
        options += [f"+{k}" for k in flags if args.get(k)]
        result = design["engine"].run(options)
        text = (design["work"] / "simulation.log").read_text(errors="replace")
        design["runs"] += 1
        log = design["work"] / f"run-{design['runs']:03d}-{name}.log"
        shutil.copyfile(design["work"] / "simulation.log", log)
        row = dict(name=name, design=design["work"].name, args=args, options=options,
                   exit=result.returncode, passed=result.returncode == 0 and PASS in text
                   and "ASSERT_FAIL" not in text, log=str(log),
                   generated_sha256=design["generated_sha256"], rtl_sha256=design["rtl_sha256"],
                   tb_sha256=design["tb_sha256"])
        row.update(parse(text))
        return row


# depth, synchronizer stages of every chain
SHAPES = [(6, 2), (2, 2), (3, 3), (4, 3), (5, 4), (7, 3), (8, 4), (9, 2), (10, 4)]
RATIOS = [("1to1", 10000, 10000, 0, 0), ("1to1_offset", 10000, 10000, 0, 3000),
          ("1to3", 10000, 30000, 0, 1000), ("3to1", 30000, 10000, 0, 1000),
          ("7to5", 14000, 10000, 0, 600), ("5to7", 10000, 14000, 0, 600),
          ("1to8", 5000, 40000, 0, 200), ("8to1", 40000, 5000, 0, 200),
          ("jitter", 10000, 10400, 1200, 2468)]


def stimulus(rng):
    return dict(pvalid=rng.choice([100, 90, 60, 30, 10]), pready=rng.choice([100, 90, 60, 30, 10]),
                resets=rng.randint(1, 3))


def case_ratios(designs, rng, seeds):
    rows = []
    for shape in SHAPES:
        design = designs.get(*shape)
        runs = [(name, ta, tb, jitter, phase) for name, ta, tb, jitter, phase in RATIOS]
        for i in range(seeds):
            # Seeded random ratio between 1:8 and 8:1, random jitter and phase.
            ta = rng.randrange(1250, 10001) * 4
            ratio = rng.uniform(1.0, 8.0)
            tb = int(ta * (ratio if rng.random() < 0.5 else 1 / ratio)) // 4 * 4
            tb = max(1000, tb)
            jitter = rng.randrange(0, min(ta, tb) // 8) // 2 * 2
            runs.append((f"random{i}", ta, tb, jitter, rng.randrange(0, ta // 2) * 2))
        for name, ta, tb, jitter, phase in runs:
            args = dict(ta=ta, tb=tb, jitter=jitter, phase=phase, words=200, meta=1,
                        seed=rng.randrange(1, 1 << 30), **stimulus(rng))
            row = designs.run(design, name, args)
            row["expected"] = "PASS"
            row["accepted"] = row["passed"] and row.get("delivered", 0) >= 200
            rows.append(row)
    # The reset handshake: both sides reset together, then one side alone mid-stream.
    for shape in HANDSHAKE_SHAPES:
        design = designs.get(*shape, handshake=True)
        for name, ta, tb, jitter, phase in RATIOS:
            for mode in ("together", "oneside"):
                args = dict(ta=ta, tb=tb, jitter=jitter, phase=phase, words=200, meta=1,
                            seed=rng.randrange(1, 1 << 30), **stimulus(rng))
                if mode == "oneside":
                    args.update(resets=0, oneside=rng.randint(2, 4))
                row = designs.run(design, f"{name}_hs_{mode}", args)
                row["expected"] = "PASS"
                row["accepted"] = row["passed"] and row.get("delivered", 0) >= (
                    1 if mode == "oneside" else 200)
                rows.append(row)
    return rows


HANDSHAKE_SHAPES = [(6, 2), (2, 2), (5, 4), (10, 3)]


THROUGHPUT = [
    # label, n, ta, tb, phase
    ("1to1_aligned_n2", 2, 10000, 10000, 0),
    ("1to1_aligned_n3", 3, 10000, 10000, 0),
    ("1to1_aligned_n4", 4, 10000, 10000, 0),
    ("1to1_offset_n2", 2, 10000, 10000, 3000),
    ("1to3_n2", 2, 10000, 30000, 1000),
    ("1to3_n4", 4, 10000, 30000, 1000),
    ("3to1_n2", 2, 30000, 10000, 1000),
    ("7to5_n2", 2, 14000, 10000, 600),
    ("5to7_n2", 2, 10000, 14000, 600),
    ("1to8_n2", 2, 5000, 40000, 200),
    ("8to1_n2", 2, 40000, 5000, 200),
]


def case_throughput(designs, rng):
    rows = []
    for label, n, ta, tb, phase in THROUGHPUT:
        depth = formula(n, ta, tb)
        for d in range(depth, 1, -1):
            # Full rate at the formula depth. One entry less must bubble when equal
            # clocks are edge-aligned, where the loop is exactly 2 (n + 1) periods.
            # Smaller depths are reported to show how conservative the formula is.
            want = 1 if d == depth else (2 if "aligned" in label else 0)
            args = dict(ta=ta, tb=tb, jitter=0, phase=phase, pvalid=100, pready=100,
                        words=450, resets=0, mode=1, measure=300, expect=want, seed=rng.randrange(1, 1 << 30))
            row = designs.run(designs.get(d, n), f"{label}_d{d}", args)
            row.update(label=label, depth=d, formula_depth=depth,
                       expected={1: "full", 2: "bubble", 0: "report"}[want])
            row["per_slow"] = row["words"] / row["slow_cycles"] if row.get("slow_cycles") else None
            row["accepted"] = row["passed"]
            if "aligned" in label:
                # With aligned equal clocks the slot loop is exactly 2n + 2
                # cycles, so depth D passes D / (2n + 2) words per cycle.
                row["loop_rate"] = d / (2 * n + 2)
                row["accepted"] = row["accepted"] and row["per_slow"] is not None and \
                    abs(row["per_slow"] - row["loop_rate"]) <= 0.01
            rows.append(row)
    return rows


FAULTS = [
    # fault, accepted failure labels, stimulus
    ("binary_count", {"CDC_ONE_BIT"},
     dict(ta=14000, tb=10000, jitter=1200, phase=600, words=400, resets=0, pvalid=90, pready=60, meta=1)),
    ("full_late", {"ORDER", "STOPPED_RECEIVER_BOUND"},
     dict(ta=10000, tb=30000, jitter=0, phase=1000, words=200, resets=0, pvalid=100, pready=20, meta=1)),
    ("no_receive_register", {"RECEIVE_LATENCY"},
     dict(ta=10000, tb=10000, jitter=0, phase=3000, words=200, resets=1, pvalid=60, pready=60, meta=1)),
    ("early_nonempty", {"RECEIVE_LATENCY"},
     dict(ta=10000, tb=10000, jitter=0, phase=3000, words=200, resets=1, pvalid=60, pready=60, meta=1)),
    ("full_early", {"THROUGHPUT", "STOPPED_RECEIVER_BOUND"},
     dict(ta=10000, tb=10000, jitter=0, phase=0, words=450, resets=0, pvalid=100, pready=100,
          mode=1, measure=300, expect=1)),
]
# One side reset alone: the restored design is the reset handshake; the faults are the
# design without it and the handshake whose enable ignores the other side.
ONE_SIDE = dict(ta=14000, tb=10000, jitter=1200, phase=600, words=300, resets=0, pvalid=80,
                pready=70, oneside=4, meta=1)
ONE_SIDE_LABELS = {"FALSE_DATA", "PHANTOM", "RECOVERY_LOSS", "STALL", "EXACT_COUNT", "TIMEOUT"}
# The diagnostic may find an end-to-end violation. Missing verdicts and bench
# failures do not establish either a completed run or a counterexample.
END_TO_END_FAILURES = {
    "ORDER", "FALSE_DATA", "PHANTOM", "RECOVERY_LOSS", "STALL", "EXACT_COUNT",
    "RECEIVE_LATENCY", "RESET_VALID", "RESET_CLEAR", "INVALID_VISIBILITY",
    "X_READY", "X_VALID", "STOPPED_RECEIVER_BOUND",
}


def case_faults(designs, rng):
    rows = []
    for fault, labels, stimulus_ in FAULTS:
        args = dict(stimulus_, seed=rng.randrange(1, 1 << 30))
        for variant in ("none", fault):
            design = designs.get(6, 2, variant)
            row = designs.run(design, f"{fault}_{'restored' if variant == 'none' else 'fault'}", args)
            row["fault"] = variant
            if variant == "none":
                row["expected"] = "PASS"
                row["accepted"] = row["passed"]
            else:
                row["expected"] = sorted(labels)
                row["accepted"] = not row["passed"] and row.get("failure") in labels
            rows.append(row)
        if fault == "binary_count":
            # Recorded finding, not a pass criterion for the fault: with one transfer
            # per cycle and the old/new skew model, a binary count is not seen end to
            # end; the one-bit monitor (and the structural verifier) catch it.
            quiet = dict(args, no_cdc_monitor=1)
            row = designs.run(designs.get(6, 2, fault), "binary_count_end_to_end", quiet)
            row["fault"] = fault
            row["expected"] = "report"
            row["accepted"] = (row["passed"] and row.get("delivered") == quiet["words"]
                               and row.get("lost") == 0) or row.get("failure") in END_TO_END_FAILURES
            rows.append(row)
    for seed in range(3):
        args = dict(ONE_SIDE, seed=rng.randrange(1, 1 << 30))
        for variant, fault, handshake in (("handshake", "none", True), ("no_handshake", "none", False),
                                          ("peer_unmasked", "peer_unmasked", True)):
            row = designs.run(designs.get(6, 2, fault, handshake), f"oneside{seed}_{variant}", args)
            row["fault"] = variant
            if variant == "handshake":
                row["expected"] = "PASS"
                row["accepted"] = row["passed"]
            else:
                row["expected"] = sorted(ONE_SIDE_LABELS)
                row["accepted"] = not row["passed"] and row.get("failure") in ONE_SIDE_LABELS
            rows.append(row)
    return rows


def report(rows):
    lines = ["| case | depth | formula depth | words | slow cycles | words per slow cycle | expected | result |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        per = f"{r['per_slow']:.3f}" if r.get("per_slow") else "-"
        result = "PASS" if r["accepted"] else "FAIL " + str(r.get("failure"))
        lines.append(f"| {r['label']} | {r['depth']} | {r['formula_depth']} | {r.get('words', '-')} | "
                     f"{r.get('slow_cycles', '-')} | {per} | {r['expected']} | {result} |")
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fixture", required=True)
    p.add_argument("--work", required=True)
    p.add_argument("--case", choices=("ratios", "throughput", "faults"), required=True)
    p.add_argument("--seeds", type=int, default=3, help="random ratios per design shape")
    p.add_argument("--rng", type=int, default=20261001)
    args = p.parse_args()
    root = Path(args.work).resolve()
    root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    rng = random.Random(f"{args.case}:{args.rng}")
    designs = Designs(args.fixture, root)
    rows = {"ratios": lambda: case_ratios(designs, rng, args.seeds),
            "throughput": lambda: case_throughput(designs, rng),
            "faults": lambda: case_faults(designs, rng)
            + clock_gate_bench.case_rows(args.fixture, root, rng)}[args.case]()
    (root / "results.json").write_text(json.dumps(rows, indent=2) + "\n")
    if args.case == "throughput":
        (root / "throughput.md").write_text(report(rows))
        print(report(rows))
    bad = [r for r in rows if not r["accepted"]]
    for r in bad:
        print("REJECTED", r["name"], r["design"], r.get("failure"), r["log"], file=sys.stderr)
    print(f"results: {root / 'results.json'}")
    if bad:
        return 1
    events = sum(r.get("meta_events", 0) for r in rows)
    print(f"ASYNC_FIFO_SUITE_PASS case={args.case} runs={len(rows)} designs={len(designs.cache)} "
          f"skew_samples={events}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
