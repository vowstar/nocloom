"""Legal registered feedback traces and one-rule waveform controls."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_engine

EXPECTED = {1: ["burst:WB.RULE_4_00"], 2: ["burst:WB.RULE_4_20"],
            3: ["burst:WB.RULE_4_30"], 4: ["burst:WB.RULE_4_35"],
            5: ["burst:WB.RULE_4_40"], 6: ["burst:WB.RULE_4_30"],
            7: ["burst:WB.RULE_3_60"], 8: ["burst:WB.RULE_3_60"],
            15: ["burst:WB.RULE_4_35"], 16: ["burst:WB.RULE_4_40"],
            18: ["burst:WB.RULE_3_30"]}
BENCH = r"""
module tb;
  reg clk = 0, rst_n = 0, cyc = 0, stb = 0, ack = 0, err = 0, rty = 0;
  reg [31:0] adr = 0;
  reg [3:0] sel = 15;
  reg [2:0] cti = 0;
  reg [1:0] bte = 0;
  integer mode, k, n, start;
  always #5 clk = !clk;
  task nlv_fail_report(input string rule, input string name, input integer detail);
    $display("OBSERVED:%s:%s", name, rule);
  endtask
  nlv_wishbone_check #(.NAME("burst"), .REGISTERED_FEEDBACK(1), .MASTER(@MASTER@), .WORD(@WORD@)) check (
      .clk(clk), .rst_n(rst_n), .cyc(cyc), .stb(stb), .we(1'b1),
      .ack(ack), .err(err), .rty(rty), .constant_ack(1'b0), .stall(1'b0),
      .adr(adr), .dat_w(32'h12345678), .dat_r(32'b0), .sel(sel), .cti(cti), .bte(bte));
  task beat(input [31:0] a, input [2:0] c, input [1:0] b, input [3:0] s);
    @(negedge clk); cyc = 1; stb = 1; adr = @WORD@ ? a/4 : a; cti = c; bte = b; sel = s; ack = 1;
    @(negedge clk); stb = 0; ack = 0;
  endtask
  initial begin
    if (!$value$plusargs("MODE=%d", mode)) $fatal(1, "missing mode");
    repeat (3) @(negedge clk);
    rst_n = 1;
    repeat (2) @(negedge clk);
    case (mode)
      1: beat(0, 3, 0, 15);
      2: begin beat(0, 2, 0, 15); beat(4, 7, 1, 15); end
      3: begin beat(0, 2, 0, 15); beat(4, 0, 0, 15); end
      4: begin beat(0, 1, 0, 15); beat(4, 7, 0, 15); end
      5: begin beat(0, 2, 0, 15); beat(8, 7, 0, 15); end
      6: beat(0, 2, 0, 15);
      7, 8: begin
        cyc = 1; stb = 1; cti = 2; ack = 0;
        @(negedge clk);
        if (mode == 7) cti = 7; else bte = 1;
        ack = 1;
        @(negedge clk); stb = 0; ack = 0;
        beat(mode == 7 ? 4 : 4, 7, mode == 7 ? 0 : 1, 15);
      end
      9: begin
        beat(0, 2, 0, 15);
        ack = 1; repeat (3) @(negedge clk); ack = 0;
        beat(4, 7, 0, 15);
      end
      10: begin beat(0, 1, 0, 5); beat(0, 1, 0, 5); beat(0, 7, 0, 5); end
      11, 12, 13: begin
        n = mode == 11 ? 4 : mode == 12 ? 8 : 16;
        for (start = 0; start < n; start = start + 1) begin
          for (k = 0; k < n; k = k + 1)
            beat(((start + k) % n) * 4, k == n - 1 ? 7 : 2, mode - 10, 15);
          cyc = 0; @(negedge clk);
        end
      end
      14: begin
        beat(0, 2, 0, 15);
        @(negedge clk); stb = 1; adr = @WORD@ ? 1:4; cti = 2; err = 1;
        @(negedge clk); stb = 0; err = 0;
        beat(8, 7, 0, 15);
      end
      15: begin beat(0, 1, 0, 5); beat(0, 7, 0, 7); end
      16: begin beat(0, 2, 0, 15); beat(4, 7, 0, 7); end
      18: begin
        @(negedge clk); ack = 1;
        @(negedge clk); ack = 0;
      end
      21: begin beat(12,2,1,15); beat(0,7,1,15); end
      22: begin beat(12,2,1,15); beat(16,7,1,15); end
      19, 20: begin
        beat(0, 2, 0, 15);
        if (mode == 19) err = 1; else rty = 1;
        repeat (3) @(negedge clk);
        err = 0; rty = 0;
        beat(4, 7, 0, 15);
      end
      default: begin beat(0, 0, 0, 15); beat(4, 7, 0, 15); end
    endcase
    cyc = 0; stb = 0; ack = 0;
    @(negedge clk);
    $display("CHECK_PASS"); $finish;
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
    rows = []
    for master, word in ((1,0),(0,0),(1,1)):
        rows.extend(controls(args, work, master, word))
    (work / "results.json").write_text(json.dumps(rows, indent=2))
    return 0 if all(row["passed"] for row in rows) else 1


def controls(args, root, master, word):
    work = root / f"master-{master}-word-{word}"
    work.mkdir()
    (work / "tb.sv").write_text(BENCH.replace("@MASTER@",str(master)).replace("@WORD@",str(word)))
    (work / "network.v").write_text(
        (Path(__file__).parent / "sv/nlv_wishbone_check.sv").read_text())
    sim = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                success=("CHECK_PASS",))
    rows = []
    expected = dict(EXPECTED)
    expected[22] = ["burst:WB.RULE_4_40"]
    if master == 0:
        expected[1] = []
        for mode in (9,19,20):
            expected[mode] = ["burst:WB.RULE_3_50"]
        expected[18] = ["burst:WB.RULE_3_30","burst:WB.RULE_3_50"]
    for mode in range(23):
        result = sim.run([f"+MODE={mode}"])
        observed = sorted({line.removeprefix("OBSERVED:") for line in result.stdout.splitlines()
                           if line.startswith("OBSERVED:")})
        row = dict(master=master,word=word,mode=mode, expected=expected.get(mode, []), observed=observed,
                   passed=result.returncode == 0 and observed == expected.get(mode, []))
        rows.append(row)
        (work / f"mode-{mode}.log").write_text(result.stdout)
        print(json.dumps(row))
    return rows


if __name__ == "__main__":
    sys.exit(main())
