"""Sample Wishbone reset release and termination premises at clock edges."""

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
  reg cyc = 0, stb = 0, ack = 0, err = 0, rty = 0, constant_ack = 0;
  reg static_ack = 1, static_err = 0, static_rty = 0;
  integer mode;
  always #5 clk = !clk;
  task nlv_fail_report(input string rule, input string name, input integer detail);
    $display("OBSERVED:%s:%s", name, rule);
  endtask
  nlv_wishbone_check #(.NAME("dynamic"), .ALLOW_ABORT(1)) dynamic_check (
    .clk(clk), .rst_n(rst_n), .cyc(cyc), .stb(stb), .we(1'b0),
    .adr(32'b0), .dat_w(32'b0), .dat_r(32'b0), .sel(4'hf),
    .ack(ack), .err(err), .rty(rty), .constant_ack(constant_ack));
  nlv_wishbone_check #(.NAME("static"), .CONSTANT_ACK(1)) static_check (
    .clk(clk), .rst_n(rst_n), .cyc(1'b0), .stb(1'b0), .we(1'b0),
    .adr(32'b0), .dat_w(32'b0), .dat_r(32'b0), .sel(4'hf),
    .ack(static_ack), .err(static_err), .rty(static_rty), .constant_ack(1'b0));
  initial begin
    if (!$value$plusargs("MODE=%d", mode)) $fatal(1, "missing mode");
    repeat (3) @(negedge clk);
    rst_n = 1;
    if (mode == 1) cyc = 1;
    if (mode == 2) begin cyc = 1; stb = 1; end
    @(negedge clk);
    cyc = 0; stb = 0;
    case (mode)
      3: begin constant_ack = 1; ack = 1; end
      4: constant_ack = 1;
      5: begin constant_ack = 1; ack = 1; err = 1; end
      6: begin constant_ack = 1; ack = 1; rty = 1; end
      7, 8, 9, 10: begin cyc = 1; stb = 1; ack = 1; end
      11: static_ack = 0;
      12: static_err = 1;
      13: static_rty = 1;
      14: err = 1;
      15: rty = 1;
      16: begin cyc = 1; stb = 1; err = 1; end
      17: begin cyc = 1; stb = 1; rty = 1; end
      18: begin cyc = 1; ack = 1; end
      19: begin cyc = 1; err = 1; end
      20: begin cyc = 1; rty = 1; end
    endcase
    @(negedge clk);
    cyc = 0; stb = 0; ack = 0; err = 0; rty = 0;
    constant_ack = 0; static_ack = 1; static_err = 0; static_rty = 0;
    if (mode == 7 || mode == 9) err = 1;
    if (mode == 8 || mode == 10) rty = 1;
    @(negedge clk);
    if (mode != 9) err = 0;
    if (mode != 10) rty = 0;
    @(negedge clk);
    $display("CHECK_PASS");
    $finish;
  end
endmodule
"""


EXPECTED = {
    1: ["dynamic:WB.RULE_3_20"], 2: ["dynamic:WB.RULE_3_20"],
    4: ["dynamic:WB.RULE_3_35"],
    5: ["dynamic:WB.RULE_3_45", "dynamic:WB.RULE_3_30", "dynamic:WB.RULE_3_50"],
    6: ["dynamic:WB.RULE_3_45", "dynamic:WB.RULE_3_30", "dynamic:WB.RULE_3_50"],
    7: ["dynamic:WB.RULE_3_30", "dynamic:WB.RULE_3_50"],
    8: ["dynamic:WB.RULE_3_30", "dynamic:WB.RULE_3_50"],
    9: ["dynamic:WB.RULE_3_30", "dynamic:WB.RULE_3_50"] * 2,
    10: ["dynamic:WB.RULE_3_30", "dynamic:WB.RULE_3_50"] * 2,
    11: ["static:WB.RULE_3_35"],
    12: ["static:WB.RULE_3_45", "static:WB.RULE_3_30", "static:WB.RULE_3_50"],
    13: ["static:WB.RULE_3_45", "static:WB.RULE_3_30", "static:WB.RULE_3_50"],
    14: ["dynamic:WB.RULE_3_30", "dynamic:WB.RULE_3_50"],
    15: ["dynamic:WB.RULE_3_30", "dynamic:WB.RULE_3_50"],
}
for mode in (18, 19, 20):
    EXPECTED[mode] = ["dynamic:WB.RULE_3_50"]


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
    for mode in range(21):
        result = sim.run([f"+MODE={mode}"])
        observed = [line.removeprefix("OBSERVED:") for line in result.stdout.splitlines()
                    if line.startswith("OBSERVED:")]
        (work / f"mode-{mode:02d}.log").write_text(result.stdout)
        row = dict(mode=mode, expected=EXPECTED.get(mode, []), observed=observed,
                   passed=result.returncode == 0 and sorted(observed) == sorted(EXPECTED.get(mode, [])))
        rows.append(row)
        print(json.dumps(row))
    (work / "results.json").write_text(json.dumps(rows, indent=2))
    return 0 if all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
