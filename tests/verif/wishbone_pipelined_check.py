"""Check pipelined termination with CYC low and STB low independently."""

import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_engine


BENCH = r"""
module tb;
  reg clk = 0, rst_n = 0;
  reg cyc = 0, stb = 0, ack = 0, err = 0, rty = 0;
  integer mode;
  always #5 clk = !clk;
  task nlv_fail_report(input string rule, input string name, input integer detail);
    $display("OBSERVED:%s", rule);
  endtask
  nlv_wishbone_check #(.PIPELINED(1), .ALLOW_ABORT(1)) check_bus (
    .clk(clk), .rst_n(rst_n), .cyc(cyc), .stb(stb), .we(1'b0), .stall(1'b0),
    .adr(32'b0), .dat_w(32'b0), .dat_r(32'b0), .sel(4'hf),
    .ack(ack), .err(err), .rty(rty), .constant_ack(1'b0));
  initial begin
    if (!$value$plusargs("MODE=%d", mode)) $fatal(1, "missing mode");
    repeat (3) @(negedge clk);
    rst_n = 1;
    @(negedge clk);
    if (mode == 1) begin
      cyc = 1; stb = 1;
      repeat (2) @(negedge clk);
      stb = 0;
      @(negedge clk);
      ack = 1;
      @(negedge clk);
      ack = 0; stb = 1;
      @(negedge clk);
      stb = 0; ack = 1;
      repeat (2) @(negedge clk);
      ack = 0; cyc = 0;
    end
    if (mode >= 2 && mode <= 4) begin
      cyc = 1; stb = 1;
      @(negedge clk);
      stb = 0;
      @(negedge clk);
      cyc = 0;
      ack = mode == 2; err = mode == 3; rty = mode == 4;
      @(negedge clk);
      ack = 0; err = 0; rty = 0;
    end
    if (mode >= 5 && mode <= 7) begin
      ack = mode == 5; err = mode == 6; rty = mode == 7;
    end
    if (mode == 8) begin
      cyc = 1; stb = 1;
      @(negedge clk);
      cyc = 0; stb = 0; ack = 1;
    end
    if (mode == 9) begin
      cyc = 1;
      @(negedge clk);
      cyc = 0; ack = 1;
    end
    repeat (5) @(negedge clk);
    $display("CHECK_PASS");
    $finish;
  end
endmodule
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    (work / "tb.sv").write_text(BENCH)
    (work / "network.v").write_text(
        (Path(__file__).parent / "sv/nlv_wishbone_check.sv").read_text())
    sim = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                success=("CHECK_PASS",))
    rows = []
    for mode in range(10):
        expected = ["WB.RULE_3_30"] * (5 if mode in (5, 6, 7, 9) else 5 if mode == 8 else 1 if mode in (2, 3, 4) else 0)
        result = sim.run([f"+MODE={mode}"])
        observed = [line.removeprefix("OBSERVED:") for line in result.stdout.splitlines()
                    if line.startswith("OBSERVED:")]
        (work / f"mode-{mode:02d}.log").write_text(result.stdout)
        rows.append(dict(mode=mode, expected=expected, observed=observed,
                         passed=result.returncode == 0 and observed == expected))
        print(json.dumps(rows[-1]))
    (work / "results.json").write_text(json.dumps(rows, indent=2))
    return 0 if all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
