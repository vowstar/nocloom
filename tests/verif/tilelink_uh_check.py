"""TileLink UH checker controls for ownership, source lifetime and denied data."""

import argparse
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_engine

BENCH = r"""
module tb;
  reg clk=0, reset_n=0;
  always #5 clk=~clk;
  integer mode, beat;
  reg av=0,dv=0;
  reg [2:0] ao=4,do_=1;
  reg [3:0] az=4,dz=4;
  reg source=0;
  reg denied=0,corrupt=0;
  task nlv_fail_report(input string rule,input string name,input integer detail);
    $fatal(1,"ASSERT_FAIL:%s",rule);
  endtask
  nlv_tl_checker #(.NAME("uh_control"),.SOURCE_W(1),.SIZE_W(4),.UH(1)) checker_uh (
    .clk(clk),.rst_n(reset_n),.allow_reject(1'b1),
    .a_valid(av),.a_ready(1'b1),.a_opcode(ao),.a_param(3'b0),.a_size(az),
    .a_source(source),.a_address(32'b0),.a_mask(4'hf),.a_data(32'b0),.a_corrupt(1'b0),
    .d_valid(dv),.d_ready(1'b1),.d_opcode(do_),.d_param(2'b0),.d_size(dz),
    .d_source(1'b0),.d_data(32'b0),.d_denied(denied),.d_corrupt(corrupt));
  initial begin
    if(!$value$plusargs("MODE=%d",mode)) $fatal(1,"missing mode");
    repeat(3) @(negedge clk); reset_n=1;
    if(mode==5 || mode==6) begin ao=2; denied=1; corrupt=1; end
    @(negedge clk); av=1;
    if(mode==7) dv=1;
    repeat(ao==2 ? 4:1) @(negedge clk);
    av=0; dv=0;
    for(beat=(mode==7 ? 1:0);beat<4;beat=beat+1) begin
      @(negedge clk); dv=1;
      if(beat==0 && mode==1) av=1;
      if(beat==1 && mode==2) do_=0;
      if(beat==1 && mode==3) dz=3;
      if(beat==1 && mode==4) begin denied=1; corrupt=1; end
      if(beat==1 && mode==5) corrupt=0;
      if(beat==1 && mode==8) begin av=1; ao=5; az=2; end
      @(negedge clk); dv=0; av=0;
      if(mode==5) corrupt=1;
    end
    if(mode==8) begin
      @(negedge clk); dv=1; do_=2; dz=2;
      @(negedge clk); dv=0;
    end
    $display("CHECK_PASS"); $finish;
  end
endmodule
"""
EXPECTED = {
    1: "TL.6.4.SOURCE",
    2: "TL.4.2.D_OWNER",
    3: "TL.4.2.D_OWNER",
    4: "TL.4.2.D_OWNER",
    5: "TL.4.4.CORRUPT",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    (work / "tb.sv").write_text(BENCH)
    (work / "network.v").write_text(
        (Path(__file__).parent / "sv/nlv_tl_checker.sv").read_text()
    )
    sim = sim_engine.Simulation(
        work,
        "network.v",
        engine=args.sim,
        config=args.sim_config,
        success=("CHECK_PASS",),
    )
    for mode in range(9):
        result = sim.run([f"+MODE={mode}"])
        (work / f"mode-{mode}.log").write_text(result.stdout)
        failures = [
            line.split("ASSERT_FAIL:", 1)[1].split()[0]
            for line in result.stdout.splitlines()
            if "ASSERT_FAIL:" in line
        ]
        if mode in EXPECTED:
            assert result.returncode != 0 and failures == [EXPECTED[mode]], (
                mode,
                result.stdout,
            )
        else:
            assert result.returncode == 0 and not failures, (mode, result.stdout)
        print(f"mode {mode}: {EXPECTED.get(mode, 'PASS')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
