"""Count locally refused read beats while an idle response bus carries miss marks."""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile

from cross_hardware_sim import simulate


BENCH = r'''
`timescale 1ns/1ps
module tb;
@DECL@
@MODULE@ dut(@CONN@);
initial nl_clk=0;
always #5 nl_clk=~nl_clk;
integer cycle=0, received=0, expected=0, requests=0, serial=0, j;
reg held=0;
reg [@BITS@+2:0] saved;
reg [31:0] random_state=18;
task check;
 input ok;
 input [255:0] label;
 begin
  if (ok !== 1'b1) begin
   $display("ASSERT_FAIL:CLAMP_REFUSED.%0s cycle=%0d beat=%0d expected=%0d",
            label,cycle,received,expected);
   $fatal(1);
  end
 end
endtask
always @(posedge nl_clk) if(nl_reset_n) begin
 cycle=cycle+1;
 check(cycle<3000,"PROGRESS");
 if(nl_down_rreq_valid && nl_down_rreq_ready) requests=requests+1;
 if(held) check(nl_up_rrsp_valid &&
   {nl_up_rrsp_data,nl_up_rrsp_result,nl_up_rrsp_last}===saved,"STABLE");
 held=nl_up_rrsp_valid && !nl_up_rrsp_ready;
 saved={nl_up_rrsp_data,nl_up_rrsp_result,nl_up_rrsp_last};
 if(nl_up_rrsp_valid && nl_up_rrsp_ready) begin
  check(received<expected,"EXTRA_BEAT");
  check(nl_up_rrsp_last==(received==expected-1),"LAST");
  check(nl_up_rrsp_first==(received==0),"FIRST");
  check(nl_up_rrsp_result==2 && nl_up_rrsp_data==0,"SLVERR_ZERO");
  check(nl_up_rrsp_tag==serial%3,"TAG");
  received=received+1;
 end
end
// Payload on an invalid response has no owner. Change both its miss length and
// mark, including while the local response is under backpressure.
always @(negedge nl_clk) if(nl_reset_n) begin
 random_state=random_state*32'd1664525+32'd1013904223;
 nl_up_rrsp_ready=(cycle%5!=1 && cycle%5!=2);
 nl_down_rrsp_result=3;
 nl_down_rrsp_data=0;
 for(j=0;j<@BITS@/32;j=j+1)
  nl_down_rrsp_data[32*j+:32]=(cycle%3==0) ? 14 : random_state[7:0];
end
task refused;
 input integer beats;
 begin
  @(negedge nl_clk);
  received=0; expected=beats;
  nl_up_rreq_length=beats-1;
  nl_up_rreq_tag=serial%3;
  nl_up_rreq_valid=1;
  @(posedge nl_clk);
  while(!nl_up_rreq_ready) @(posedge nl_clk);
  @(negedge nl_clk); nl_up_rreq_valid=0;
  wait(received==expected);
  repeat(8) @(negedge nl_clk);
  check(requests==0,"NO_TARGET_REQUEST");
  serial=serial+1;
 end
endtask
initial begin
@INIT@
 nl_reset_n=0;
 repeat(5) @(negedge nl_clk);
 nl_up_rreq_first=1; nl_up_rreq_last=1;
 nl_up_rreq_size=@SIZE@; nl_up_rreq_burst=0;
 nl_up_rreq_count=@BITS@/8;
 nl_down_rreq_ready=1;
 nl_down_rrsp_valid=0;
 nl_reset_n=1;
 refused(2); refused(1); refused(16); refused(3);
 $display("NOCLOOM_PASS clamp refused reads transfers=%0d",serial);
 $finish;
end
endmodule
'''


def bench(work, bits):
    ports = [(n, int(w), int(i)) for n, w, i in
             (line.split() for line in (work / 'ports.tsv').read_text().splitlines())]
    values = {
        'DECL': '\n'.join(f"{'reg' if i else 'wire'} [{w-1}:0] {n};"
                         for n, w, i in ports),
        'CONN': ', '.join(f'.{n}({n})' for n, _, _ in ports),
        'MODULE': re.search(r'module (\w+)', (work / 'network.v').read_text())[1],
        'INIT': '\n'.join(f' {n}=0;' for n, _, i in ports if i and n != 'nl_clk'),
        'BITS': str(bits), 'SIZE': str((bits // 8).bit_length()-1),
    }
    text = BENCH
    for key, value in values.items():
        text = text.replace('@'+key+'@', value)
    return text


def axi_bench(work, bits):
    """Connect the AXI ingress BFM face to the clamp's packet face."""
    ports = {n: (int(w), int(i)) for n, w, i in
             (line.split() for line in (work / 'ports.tsv').read_text().splitlines())}
    source = [(n, int(w), int(i)) for n, w, i in
              (line.split() for line in (work / 'source_ports.tsv').read_text().splitlines())]
    declarations = {}
    connections = []
    driven = set()
    for name, width, incoming in source:
        packet = any(name.startswith('nl_'+face+'_')
                     for face in ('wreq', 'rreq', 'wrsp', 'rrsp'))
        net = name.replace('nl_', 'nl_up_', 1) if packet else name
        if packet:
            assert net in ports and ports[net] == (width, 1-incoming), (net, width)
            driven.add(net)
        declarations[net] = (width, incoming if not packet else 0)
        connections.append(f'.{name}({net})')
    ties = []
    for name, (width, incoming) in ports.items():
        if name.startswith('nl_up_'):
            declarations[name] = (width, 0)
            if incoming and name not in driven:
                assert name.endswith(('_ends', '_wrap_end')), name
                ties.append(f'assign {name}=0;')
        else:
            declarations[name] = (width, incoming)
    text = bench(work, bits)
    old = '\n'.join(f"{'reg' if i else 'wire'} [{w-1}:0] {n};"
                    for n, (w, i) in ports.items())
    new = '\n'.join(f"{'reg' if i else 'wire'} [{w-1}:0] {n};"
                    for n, (w, i) in declarations.items())
    old_init = '\n'.join(f' {n}=0;' for n, (_, i) in ports.items() if i and n != 'nl_clk')
    new_init = '\n'.join(f' {n}=0;' for n, (_, i) in declarations.items() if i and n != 'nl_clk')
    assert old in text and old_init in text
    text = text.replace(old, new+'\n'+'\n'.join(ties)).replace(old_init, new_init)
    module = (work / 'source_module.txt').read_text().strip()
    text = text.replace('initial nl_clk=0;',
                        f"{module} ingress({', '.join(connections)});\ninitial nl_clk=0;")
    prefix, text = text.split('integer cycle=0', 1)
    text = text.replace('  check(nl_up_rrsp_first==(received==0),"FIRST");\n', '')
    text = text.replace('  check(nl_up_rrsp_tag==serial%3,"TAG");\n', '')
    text = text.replace('  nl_up_rreq_tag=serial%3;\n', '')
    text = text.replace(' nl_up_rreq_first=1; nl_up_rreq_last=1;\n', '')
    text = text.replace(' nl_up_rreq_count=@BITS@/8;\n'.replace('@BITS@', str(bits)), '')
    for old_name, new_name in {
        'nl_up_rrsp_valid': 'nl_axi_rvalid', 'nl_up_rrsp_ready': 'nl_axi_rready',
        'nl_up_rrsp_data': 'nl_axi_rdata', 'nl_up_rrsp_result': 'nl_axi_rresp',
        'nl_up_rrsp_last': 'nl_axi_rlast', 'nl_up_rreq_valid': 'nl_axi_arvalid',
        'nl_up_rreq_ready': 'nl_axi_arready', 'nl_up_rreq_length': 'nl_axi_arlen',
        'nl_up_rreq_size': 'nl_axi_arsize', 'nl_up_rreq_burst': 'nl_axi_arburst',
    }.items():
        text = text.replace(old_name, new_name)
    return prefix+'integer cycle=0'+text


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--fixture', required=True)
    p.add_argument('--work', required=True)
    p.add_argument('--bits', type=int, choices=(64, 128), required=True)
    p.add_argument('--mode', type=int, choices=(1, 3, 4), required=True)
    args = p.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    (base / 'latest.txt').write_text(str(work)+'\n')
    subprocess.run([args.fixture, str(work), str(args.bits), str(args.mode)], check=True)
    axi = work / 'axi'
    axi.mkdir()
    (axi / 'network.v').write_bytes((work / 'network.v').read_bytes())
    (axi / 'tb.sv').write_text(axi_bench(work, args.bits))
    rc = simulate(axi, base / 'compiled')
    assert rc == 0, (axi, (axi / 'simulation.log').read_text()[-2000:])
    (work / 'tb.sv').write_text(bench(work, args.bits))
    rc = simulate(work, base / 'compiled')
    assert rc == 0, (work, (work / 'simulation.log').read_text()[-2000:])


if __name__ == '__main__':
    main()
