"""Exercise the tag-consuming Wishbone peer without a generated network."""
import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_engine

BENCH = r"""
module tb;
  reg clk = 0, rst_n = 0, cyc = 0, stb = 0, we = 0;
  reg [31:0] adr = 0, dat_w = 0;
  reg [3:0] sel = 15;
  reg [2:0] cti = 0;
  reg [1:0] bte = 0;
  wire [31:0] dat_r;
  wire ack, err, rty, stall;
  integer nlv_fd, mode, start, k, b, n, completed = 0, failures = 0, guard;
  always #5 clk = !clk;
  task nlv_fail_report(input string rule, input string name, input integer detail);
    if (mode == 1) begin
      $display("OBSERVED:%s", rule); failures = failures + 1;
    end else $fatal(1, "CHECK_FAIL:%s", rule);
  endtask
  nlv_wishbone_slave #(.NAME("peer"), .REGISTERED_FEEDBACK(1), .RETRY(1)) peer (
      .clk(clk), .rst_n(rst_n), .cyc(cyc), .stb(stb), .we(we), .adr(adr),
      .dat_w(dat_w), .sel(sel), .cti(cti), .bte(bte), .dat_r(dat_r),
      .ack(ack), .err(err), .rty(rty), .stall(stall), .sideband(1'b0));
  task transfer(input integer a, c, bt, write);
    begin
      @(negedge clk);
      cyc = 1; stb = 1; adr = a; cti = c; bte = bt; we = write;
      dat_w = 32'hbabe0000 ^ a;
      guard = 0;
      @(posedge clk);
      while (!(ack || err || rty)) begin
        guard = guard + 1;
        if (guard > 12) $fatal(1, "CHECK_FAIL:peer_progress");
        @(posedge clk);
      end
      if (!write && !err && !rty && mode == 0 && dat_r !== (32'hbabe0000 ^ a))
        $fatal(1, "CHECK_FAIL:peer_prediction:%h", a);
      completed = completed + 1;
      @(negedge clk); stb = 0;
    end
  endtask
  initial begin
    nlv_fd = $fopen("events.jsonl", "w");
    if (!$value$plusargs("MODE=%d", mode)) mode = 0;
    repeat (4) @(negedge clk);
    rst_n = 1; repeat (2) @(negedge clk);
    peer.set_ctl(62, 4);
    if (mode == 1) begin
      transfer(0, 2, 0, 1);
      transfer(8, 7, 0, 1);
    end else begin
      for (k = 0; k < 16; k = k + 1) begin
        transfer(k * 4, 0, 0, 1);
        cyc = 0; @(negedge clk);
      end
      for (b = 0; b < 4; b = b + 1) begin
        n = b == 0 ? 7 : 2 << b;
        for (start = 0; start < n; start = start + 1) begin
          for (k = 0; k < n; k = k + 1)
            transfer((b == 0 ? k : (start + k) % n) * 4, k == n - 1 ? 7 : 2, b, 0);
          cyc = 0; @(negedge clk);
        end
      end
      for (k = 0; k < 4; k = k + 1) transfer(0, k == 3 ? 7 : 1, 0, 0);
      cyc = 0; @(negedge clk);
      peer.set_ctl(43, 8); peer.set_ctl(44, 12); peer.set_ctl(45, 1);
      for (k = 0; k < 4; k = k + 1) transfer(k * 4, k == 3 ? 7 : 2, 0, 0);
    end
    cyc = 0; stb = 0; repeat (3) @(negedge clk);
    if (mode == 0 && (completed != 409 || peer.counter(0) != 409))
      $fatal(1, "CHECK_FAIL:peer_word_count");
    if (mode == 1 && failures == 0) $fatal(1, "CHECK_FAIL:peer_control_survived");
    $display("CHECK_PASS:%0d", completed); $finish;
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
    sv = Path(__file__).parent / "sv"
    (work / "tb.sv").write_text(BENCH)
    (work / "network.v").write_text((sv / "nlv_wishbone_check.sv").read_text() +
                                    (sv / "nlv_wishbone_slave.sv").read_text())
    (work / "nlv_common.svh").write_text((sv / "nlv_common.svh").read_text())
    sim = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                success=("CHECK_PASS",))
    rows = []
    for mode in (0, 1):
        result = sim.run([f"+MODE={mode}"])
        (work / f"mode-{mode}.log").write_text(result.stdout)
        (work / f"mode-{mode}-events.jsonl").write_bytes((work / "events.jsonl").read_bytes())
        observed = {line.removeprefix("OBSERVED:") for line in result.stdout.splitlines()
                    if line.startswith("OBSERVED:")}
        passed = result.returncode == 0 and "CHECK_PASS" in result.stdout
        passed = passed and observed == ({"WB.RULE_4_40"} if mode else set())
        rows.append(dict(mode=mode, observed=sorted(observed), passed=passed))
        print(json.dumps(rows[-1]))
    (work / "results.json").write_text(json.dumps(rows, indent=2))
    return 0 if all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
