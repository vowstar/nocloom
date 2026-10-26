"""Cycle-accurate QoS pipe equations and DUT negative controls."""
import argparse
import subprocess
import tempfile
from pathlib import Path
from cross_hardware_sim import simulate

TB = r'''
`timescale 1ns/1ps
module tb;
localparam MODE=@MODE@;
reg clk=0,reset_n=0,valid=0,ready=0,in_ready=0,out_valid=0,idle=0;
reg[2:0] head=0,level_hint=0,source_level=0;
wire[2:0] out_level_hint,out_source_level;
nocloom_qos_pipeline dut(.nl_clk(clk),.nl_reset_n(reset_n),.nl_valid(valid),.nl_ready(ready),
 .nl_in_ready(in_ready),.nl_out_valid(out_valid),.nl_idle(idle),
 .nl_head(head),.nl_level_hint(level_hint),.nl_source_level(source_level),
 .nl_out_level_hint(out_level_hint),.nl_out_source_level(out_source_level));
always #5 clk=~clk;
integer checks=0;
task check;input ok;input[8*32-1:0] label;begin
 checks=checks+1;
 if(ok!==1'b1) begin $display("ASSERT_FAIL:QOS_PIPELINE.%0s",label);$fatal(1);end
end endtask
task level_hint_step;input v,r;input[2:0] h,p,expected;begin
 @(negedge clk);valid=v;ready=r;head=h;level_hint=p;#1;
 $display("LEVEL_HINT_CYCLE time=%0t value=%0d expected=%0d",$time,out_level_hint,expected);
 check(out_level_hint===expected,"LEVEL_HINT");
end endtask
task source_level_step;input v,rr,e,tv,tr;input[2:0] h,expected;begin
 @(negedge clk);valid=v;in_ready=rr;idle=e;out_valid=tv;ready=tr;source_level=h;#1;
 check(out_source_level===expected,"SOURCE_LEVEL");
end endtask
task clear_inputs;begin
 valid=0;ready=0;in_ready=0;out_valid=0;idle=0;head=0;level_hint=0;source_level=0;
end endtask
task reset;begin
 clear_inputs();reset_n=0;repeat(2) @(negedge clk);reset_n=1;
end endtask
initial begin
 reset();
 // Forward level hint: the head level joins the level hint, and invalid clears the register.
 if(!MODE) begin
  level_hint_step(1,0,1,3,0);level_hint_step(1,0,7,1,3);level_hint_step(0,0,7,7,7);level_hint_step(0,0,0,0,0);
 end
 reset();
 // Forward and backward level hint: the live pressure, the kept head level and the output slot differ.
 if(MODE) begin
  level_hint_step(1,0,1,0,0);level_hint_step(1,0,7,0,1);level_hint_step(0,0,0,0,7);
  level_hint_step(0,1,0,0,7);level_hint_step(0,1,0,0,7);level_hint_step(0,1,0,0,0);
 end
 reset();
 // Two slots (and three in the other configuration): idle mask, live, retained and consumed.
 source_level_step(0,0,1,0,0,7,0);source_level_step(1,0,0,0,0,1,1);source_level_step(1,0,0,1,0,0,1);
 source_level_step(1,1,0,1,1,0,1);source_level_step(0,0,1,0,0,0,0);
 $display("CHECK_PASS qos_pipeline checks=%0d",checks);$finish;
end
initial begin #1000;$display("ASSERT_FAIL:QOS_PIPELINE.TIMEOUT");$fatal(1);end
endmodule
'''


def run_one(fixture, root, mode, mutant, expect_pass):
    case = root / (mode + (f'-{mutant}' if mutant else ''))
    case.mkdir()
    subprocess.run([fixture, str(case), mode] + ([mutant] if mutant else []), check=True)
    (case / 'tb.sv').write_text(TB.replace('@MODE@', '1' if mode == 'both' else '0'))
    rc = simulate(case, case / 'compiled')
    log = (case / 'simulation.log').read_text()
    passed = rc == 0 and 'CHECK_PASS qos_pipeline' in log
    if expect_pass:
        assert passed, (mode, log[-2000:])
    else:
        assert not passed and 'ASSERT_FAIL:QOS_PIPELINE.' in log, (mode, mutant, rc, log[-2000:])
    return dict(mode=mode, mutant=mutant, status='PASS' if passed else 'CAUGHT', returncode=rc)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--fixture', required=True)
    p.add_argument('--work', required=True)
    args = p.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    (base / 'latest.txt').write_text(str(work) + '\n')
    rows = []
    for mode in ('forward', 'both'):
        rows.append(run_one(args.fixture, work, mode, '', True))
    rows.append(run_one(args.fixture, work, 'forward', 'level_hint_no_head', False))
    rows.append(run_one(args.fixture, work, 'forward', 'source_level_live_only', False))
    print(rows)


if __name__ == '__main__':
    main()
