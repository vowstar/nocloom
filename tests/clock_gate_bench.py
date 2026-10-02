"""Clock gate cell bench, run by run_async_fifo.py --case faults.

The fixture (generate_async_fifo OUT clock_gate FAULT) emits a 16-bit counter
clocked through one clock gate, with the cell model ahead of the module. The
bench drives the domain clock (rising at k*T, falling at k*T + T/2), changes
enable at seeded offsets inside both clock phases and checks:

  GLITCH     every gated clock edge falls on a clock edge: the gated clock only
             passes whole high phases
  ENABLE     with test low, the gated clock pulses in cycle k exactly when
             enable was high just before the rising edge
  TEST_MODE  with test high, the gated clock pulses every cycle whatever enable
  COUNT      the counter on the gated clock counts exactly those pulses

Faults (cell model edits): no_latch gates with the live enable and must fail
GLITCH; no_test ignores test and must fail TEST_MODE. The unmodified cell
passes the same stimulus.
"""

import json
import random
import re
import subprocess
from pathlib import Path

from sim_engine import Simulation

PERIOD = 10000
FAULTS = {"no_latch": {"GLITCH"}, "no_test": {"TEST_MODE"}}


def bench(gated, offsets, enables, tests):
    n = len(offsets)
    table = "\n".join(f"    offset[{i}] = {o}; level[{i}] = {e}; mode[{i}] = {t};"
                      for i, (o, e, t) in enumerate(zip(offsets, enables, tests)))
    half = PERIOD // 2
    return f"""`timescale 1ps/1ps
module tb;
  // Assigned at time zero, not initialized, so the cell latch sees its first low phase.
  reg clk, reset_n, enable, test;
  initial {{ clk, reset_n, enable, test }} = 4'b0;
  wire [15:0] count;
  integer offset [0:{n - 1}];
  reg level [0:{n - 1}];
  reg mode [0:{n - 1}];
  integer k, j, pulses = 0, failures = 0;
  reg expected;
  nocloom_clock_gate_probe dut(.nl_clk(clk), .nl_reset_n(reset_n), .test(test),
                               .nl_enable(enable), .nl_count(count));
  wire gated = dut.{gated};
  task fail(input [8*16-1:0] label);
    begin
      $display("CHECK_FAIL %0s at %0t", label, $time);
      failures = failures + 1;
    end
  endtask
  always @(gated)
    if ($time > 0 && ($time % {PERIOD}) != (gated ? 0 : {half})) fail("GLITCH");
  initial begin
{table}
  end
  // Clock rising edges at (k + 1) * T; enable and test change once per cycle at a seeded
  // offset in either phase, never on an edge.
  initial begin
    #({PERIOD});
    for (k = 0; k < {n}; k = k + 1) begin
      #(offset[k]);
      enable = level[k];
      test = mode[k];
      #({PERIOD} - offset[k]);
    end
  end
  initial #({PERIOD} * 2 + {half} + 2000) reset_n = 1;
  initial begin
    #({PERIOD});
    for (j = 0; j < {n}; j = j + 1) begin
      expected = enable | test;
      clk = 1;
      #1 if (gated !== expected) fail(test ? "TEST_MODE" : "ENABLE");
      if (expected && reset_n) pulses = pulses + 1;
      #({half} - 1) clk = 0;
      #({half});
    end
    #({PERIOD});
    if (count !== pulses[15:0]) fail("COUNT");
    if (failures == 0) $display("CHECK_PASS clock_gate pulses=%0d", pulses);
    $finish;
  end
endmodule
"""


def write_bench(work, meta, rng, cycles=400):
    # Offsets avoid the clock edges by more than the checks' one picosecond.
    offsets = [rng.choice([rng.randrange(10, PERIOD // 2 - 10),
                           rng.randrange(PERIOD // 2 + 10, PERIOD - 10)]) for _ in range(cycles)]
    enables = [rng.randrange(2) for _ in range(cycles)]
    tests = [1 if cycles // 2 <= i < cycles // 2 + 40 else 0 for i in range(cycles)]
    (work / "tb.sv").write_text(bench(meta["gated"], offsets, enables, tests))


def case_rows(fixture, root, rng):
    """One run of the unmodified cell and one per fault, on the same stimulus."""
    seed = rng.randrange(1, 1 << 30)
    rows = []
    for fault in ("none", *FAULTS):
        work = Path(root) / "designs" / f"clock_gate_{fault}"
        work.mkdir(parents=True)
        generated = subprocess.run([fixture, str(work), "clock_gate", fault], capture_output=True,
                                   text=True, timeout=60, check=False)
        if generated.returncode:
            raise AssertionError(f"fixture failed: clock_gate {fault}: {generated.stderr}")
        write_bench(work, json.loads((work / "bindings.json").read_text()), random.Random(seed))
        result = Simulation(work, "network.v").run()
        text = (work / "simulation.log").read_text(errors="replace")
        failed = sorted({m.group(1) for m in re.finditer(r"CHECK_FAIL\W*([A-Z_]+)", text)})
        passed = result.returncode == 0 and "CHECK_PASS clock_gate" in text and not failed
        row = dict(name=f"clock_gate_{fault}", design=work.name, fault=fault, seed=seed,
                   passed=passed, failure=failed[0] if failed else None, failures=failed,
                   log=str(work / "simulation.log"))
        if fault == "none":
            row.update(expected="PASS", accepted=passed)
        else:
            row.update(expected=sorted(FAULTS[fault]),
                       accepted=not passed and bool(FAULTS[fault] & set(failed)))
        rows.append(row)
    return rows
