"""Watchdog counts at a TileLink UH target under network stalls and slave stalls."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

import sim_engine
from tilelink_uh_target_ordering import bench, configuration

# Name, least capacity and plusargs. {g} is a network stall longer than the watchdog, {l} a
# slave hold longer than it and {s} a shorter one. EXPECT names the handshake before the expiry.
QUIET = (
    ("write_gap_first", 1, "MODE=0 WGAP_AT=0 WGAP={g}"),
    ("write_gap_middle", 1, "MODE=0 WGAP_AT=7 WGAP={g}"),
    ("read_stall", 1, "MODE=1 RLOW={g}"),
    ("read_stall_pair", 2, "MODE=2 RLOW={g}"),
    ("read_stall_queue", 3, "MODE=3 RLOW={g}"),
    ("write_behind_read", 4, "MODE=4 RLOW={g}"),
    ("read_behind_message", 2, "MODE=2"),
    ("slave_short_a", 1, "MODE=1 AHOLD_AT=0 AHOLD={s}"),
    ("slave_short_d", 1, "MODE=0 DHOLD_AT=0 DHOLD={s}"),
    ("mixed_write_short", 1, "MODE=0 WGAP_AT=0 WGAP={g} DHOLD_AT=0 DHOLD={s}"),
    ("mixed_read_short", 2, "MODE=2 RLOW={g} DHOLD_AT=16 DHOLD={s}"),
    ("mixed_read_restart", 2, "MODE=2 RLOW={g} DHOLD_AT=0 DHOLD={s} DHOLD2_AT=16 DHOLD2={s}"),
)
EXPIRY = (
    ("slave_a_read", 1, "MODE=1 AHOLD_AT=0 AHOLD={l} EXPECT=1 EXPECT_AT=0"),
    ("slave_a_write", 1, "MODE=0 AHOLD_AT=5 AHOLD={l} EXPECT=1 EXPECT_AT=5"),
    ("slave_d_read", 1, "MODE=1 DHOLD_AT=0 DHOLD={l} EXPECT=2 EXPECT_AT=0"),
    ("slave_d_write", 1, "MODE=0 DHOLD_AT=0 DHOLD={l} EXPECT=2 EXPECT_AT=15"),
    ("slave_d_middle", 1, "MODE=1 DHOLD_AT=5 DHOLD={l} EXPECT=3 EXPECT_AT=4"),
    ("slave_d_pair", 2, "MODE=2 DHOLD_AT=0 DHOLD={l} EXPECT=2 EXPECT_AT=0"),
    ("slave_d_second", 2, "MODE=2 DHOLD_AT=16 DHOLD={l} EXPECT=4 EXPECT_AT=15"),
    ("slave_d_eighth", 8, "MODE=5 DHOLD_AT=14 DHOLD={l} EXPECT=4 EXPECT_AT=13"),
    ("mixed_write", 1, "MODE=0 WGAP_AT=0 WGAP={g} DHOLD_AT=0 DHOLD={l} EXPECT=2 EXPECT_AT=15"),
    ("mixed_read_single", 1, "MODE=1 RLOW={g} DHOLD_AT=8 DHOLD={l} EXPECT=3 EXPECT_AT=7"),
    ("mixed_read", 2, "MODE=2 RLOW={g} DHOLD_AT=16 DHOLD={l} EXPECT=4 EXPECT_AT=15"),
)
REPRODUCTIONS = ("write_gap_first", "read_stall_pair", "read_stall_queue")
# Fault selector, name and the cases that must report a wrong expiry.
FAULTS = (
    (1, "d_wait_before_last_a", ("write_gap_first", "mixed_write")),
    (2, "d_wait_ignores_channel", ("read_stall_pair", "read_stall_queue", "mixed_read")),
    (3, "count_stopped", tuple(name for name, _, _ in EXPIRY)),
)


def faulted(rtl, bindings, capacity):
    """Each selector removes one term of every context watchdog."""
    assigns = dict(re.findall(r"^assign (\w+) = (.+);$", rtl, re.M))

    def select(net, selector, value):
        nonlocal rtl
        line = f"assign {net} = {assigns[net]};"
        if rtl.count(line) != 1:
            raise RuntimeError("fault anchor is not unique: " + line)
        rtl = rtl.replace(line, f"assign {net} = nlv_fault=={selector} ? {value} : {assigns[net]};")

    for k in range(capacity):
        scope = "burst_service/t/" + (f"tl.context{k}." if capacity > 1 else "")
        net = lambda name: "nl_internal_v" + str(bindings[scope + name]["index"])
        # D wait: every A beat left, D is not complete, and the context can take the beat.
        owed = [(name, m[1]) for name, value in assigns.items()
                if (m := re.fullmatch(rf"\({net('tl.a_done')} & (\w+)\)", value))
                and (inner := re.fullmatch(r"\((\w+) & \w+\)", assigns.get(m[1], "")))
                and assigns.get(inner[1]) == f"(~{net('tl.d_done')})"]
        if len(owed) != 1:
            raise RuntimeError(f"context {k} has {len(owed)} D wait terms")
        select(owed[0][0], 1, owed[0][1])
        if capacity > 1:
            gated = [name for name, value in assigns.items()
                     if re.fullmatch(rf"\({owed[0][0]} & \w+\)", value)]
            if len(gated) != 1:
                raise RuntimeError(f"context {k} has {len(gated)} D channel gates")
            select(gated[0], 2, owed[0][0])
        select(net("timeout.waiting"), 3, "1'b0")
    end = rtl.index("\n", rtl.index(");", rtl.index("module nocloom_uh_target")))
    return (rtl[:end] + '\ninteger nlv_fault;\ninitial if(!$value$plusargs("FAULT=%d",nlv_fault)) '
            "nlv_fault=0;" + rtl[end:])


def run(args, capacity):
    work = Path(tempfile.mkdtemp(prefix=f"k{capacity}-", dir=args.work)).resolve()
    cfg = configuration(capacity)
    cfg["targets"][0]["timeout"] = dict(cycles=args.cycles, completion=False)
    (work / "input.yaml").write_text(json.dumps(cfg))
    generated = work / "generated"
    subprocess.run([str(args.cli.resolve()), "generate", str(work / "input.yaml"),
                    "--out-dir", str(generated)], check=True, timeout=60)
    mapping = json.loads((generated / "nocloom_uh_target.map.json").read_text())
    rtl = sim_engine.generated_rtl(generated / "nocloom_uh_target.v")
    if args.negative:
        subprocess.run([str(args.fixture.resolve()), "tilelink_uh_probe", str(work / "probe"),
                        "--param", f"config={work / 'input.yaml'}", "--list-bindings"],
                       check=True, timeout=60)
        if (work / "probe/network.v").read_text() != (generated / "nocloom_uh_target.v").read_text():
            raise RuntimeError("fixture differs from the CLI RTL")
        rtl = faulted(rtl, json.loads((work / "probe/bindings.json").read_text())["dut"], capacity)
    body = f"localparam CYCLES={args.cycles};\n" + Path(__file__).with_suffix(".sv").read_text()
    (work / "tb.sv").write_text(bench(mapping, body))
    (work / "network.v").write_text(rtl)
    simulation = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                       compile_timeout=300, run_timeout=120, success=("UH_PASS",))
    values = dict(g=args.stall, l=args.cycles + 20, s=args.cycles - 4)
    cases = {name: ["+" + item.format(**values) for item in text.split()]
             for name, least, text in QUIET + EXPIRY
             if least <= capacity and (not args.reproductions or name in REPRODUCTIONS)}
    failed = 0
    for name, plusargs in cases.items():
        result = simulation.run(plusargs)
        print(f"WATCHDOG_CASE K={capacity} {name} {'PASS' if result.returncode == 0 else 'FAIL'}",
              flush=True)
        if result.returncode:
            print(result.stdout, work, flush=True)
            failed += 1
    if failed or not args.negative:
        return failed
    for selector, fault, catching in FAULTS:
        if selector == 2 and capacity == 1:
            continue
        for name in catching:
            if name not in cases:
                continue
            result = simulation.run(cases[name] + [f"+FAULT={selector}"])
            caught = result.returncode != 0 and "CHECK_FAIL WATCHDOG fired=" in result.stdout
            print(f"WATCHDOG_FAULT K={capacity} {fault} {name} "
                  f"{'CAUGHT' if caught else 'SURVIVED'}", flush=True)
            if not caught:
                print(result.stdout, work, flush=True)
                failed += 1
    return failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True, type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--capacity", required=True, type=int, nargs="+")
    parser.add_argument("--cycles", type=int, default=16)
    parser.add_argument("--stall", type=int, default=100)
    parser.add_argument("--reproductions", action="store_true")
    parser.add_argument("--negative", action="store_true")
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    if args.negative and not args.fixture:
        parser.error("--negative requires --fixture")
    args.work.mkdir(parents=True, exist_ok=True)
    return 1 if sum(run(args, capacity) for capacity in args.capacity) else 0


if __name__ == "__main__":
    raise SystemExit(main())
