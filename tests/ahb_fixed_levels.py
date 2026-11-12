"""AHB fixed levels competing with AXI4, with per-transfer conservation checks."""
import json
import re
import subprocess
from pathlib import Path
from cross_hardware_sim import simulate
from sim_engine import generated_rtl


def configuration(protocol, high):
    return dict(schema_version=10, name='ahb_levels', address_width=32,
                reset_ports=[dict(name='reset_n')], test_ports=[dict(name='test')],
                clock_domains=[dict(name='main', clock_port='clk', reset_port='reset_n', test_port='test')],
                initiators=[dict(name='s0', clock_domain='main', protocol=protocol, data_width=32,
                                 max_outstanding=1, qos=dict(levels=dict(read=high, write=8-high))),
                            dict(name='s1', clock_domain='main', protocol='axi4', data_width=32,
                                 max_outstanding=4, qos=dict(levels=dict(read=4, write=4)))],
                targets=[dict(name='t0', clock_domain='main', protocol='axi4', data_width=32,
                              attributes='amba', mappings=[dict(base='0x0', size='0x10000')])],
                connectivity='all', arbitration=dict(policy='round_robin'),
                output=dict(backend='verilog', language='verilog_2001'))


TB = r'''`timescale 1ns/1ps
module tb;
@DECL@
@TOP@ dut(@PORTS@);
initial clk=0; always #5 clk=~clk;
integer cyc=0, awn=0, arn=0, wn=0, bn=0, rn=0, hd=0;
integer seenw[0:1], seenr[0:1];
reg busyw=0, busyr=0; reg [31:0] wa=0,ra=0; reg [15:0] wi=0,ri=0;
reg [3:0] done=0;
task check(input bit ok,input [255:0] msg);
begin if(!ok) begin $display("ASSERT_FAIL:AHB_LEVELS.%0s cycle=%0d",msg,cyc);$fatal(1);end end
endtask
initial begin
 @INIT@
 seenw[0]=0;seenw[1]=0;seenr[0]=0;seenr[1]=0;
 repeat(12) @(negedge clk);reset_n=1;
end
initial begin
 wait(reset_n);repeat(12) @(negedge clk);
 a_awsize=2;a_awburst=1;a_awcache=3;a_wstrb=15;a_wlast=1;a_bready=1;
 for(integer k=0;k<64;k=k+1) begin
  @(negedge clk);a_awaddr=32'h4000+4*k;a_awvalid=1;
  @(posedge clk);while(!a_awready) @(posedge clk);
  @(negedge clk);a_awvalid=0;a_wdata=32'h12344000+4*k;a_wvalid=1;
  @(posedge clk);while(!a_wready) @(posedge clk);
  @(negedge clk);a_wvalid=0;
 end
 done[0]=1;
end
initial begin
 wait(reset_n);repeat(12) @(negedge clk);
 a_arsize=2;a_arburst=1;a_arcache=3;a_rready=1;
 for(integer k=0;k<64;k=k+1) begin
  @(negedge clk);a_araddr=32'h6000+4*k;a_arvalid=1;
  @(posedge clk);while(!a_arready) @(posedge clk);
  @(negedge clk);a_arvalid=0;
 end
 done[1]=1;
end
initial begin
 wait(reset_n);repeat(12) @(negedge clk);
 h_hsize=2;h_hprot=7;
 for(integer k=0;k<128;k=k+1) begin
  @(negedge clk);h_hwrite=((k%2==0)==@WRITE_FIRST@);
  h_haddr=(h_hwrite ? 0 : 32'h2000)+4*(k/2);
  h_hsel=1;h_htrans=2;
  @(posedge clk);while(!h_hreadyout) @(posedge clk);
  @(negedge clk);h_htrans=0;h_hwdata=32'h12340000+4*(k/2);
  @(posedge clk);while(!h_hreadyout) @(posedge clk);
  check(!h_hresp,"AHB_RESPONSE");
  if(!h_hwrite) check(h_hrdata===(32'h87652000+4*(k/2)),"AHB_DATA");
  hd=hd+1;
 end
 @(negedge clk);h_hsel=0;done[2]=1;
end
always @(negedge clk) if(reset_n) begin
 t_awready=!busyw && cyc%19==0;t_wready=busyw && cyc%7==0;
 t_arready=!busyr && cyc%23==0;
 t_rvalid=busyr && cyc%11==0;t_rid=ri;t_rlast=1;t_rdata=32'h87650000+ra;
end
always @(posedge clk) if(reset_n) begin
 cyc=cyc+1;
 if(t_awvalid && t_awready) begin
  check(!busyw && t_awlen==0,"TARGET_AW");wa=t_awaddr;wi=t_awid;busyw=1;
  check(wa==((wa>=32'h4000 ? 32'h4000 : 0)+4*seenw[wa>=32'h4000]),"WRITE_ORDER");
  seenw[wa>=32'h4000]=seenw[wa>=32'h4000]+1;
 end
 if(t_wvalid && t_wready) begin
  check(busyw && t_wlast && t_wstrb==15 && t_wdata===(32'h12340000+wa),"TARGET_DATA");
  wn=wn+1;
 end
 if(t_bvalid && t_bready) busyw=0;
 if(t_arvalid && t_arready) begin
  check(!busyr && t_arlen==0,"TARGET_AR");ra=t_araddr;ri=t_arid;busyr=1;
  check(ra==((ra>=32'h4000 ? 32'h6000 : 32'h2000)+4*seenr[ra>=32'h4000]),"READ_ORDER");
  seenr[ra>=32'h4000]=seenr[ra>=32'h4000]+1;
 end
 if(t_rvalid && t_rready) busyr=0;
 if(a_bvalid && a_bready) begin check(a_bresp==0,"AXI_B");bn=bn+1;end
 if(a_rvalid && a_rready) begin
  check(a_rresp==0 && a_rlast && a_rdata===(32'h87656000+4*rn),"AXI_R");rn=rn+1;
 end
 if(done[2:0]==7 && bn>=64 && rn>=64 && wn>=128) begin
  check(hd==128 && wn==128 && bn==64 && rn==64 && seenw[0]==64 && seenw[1]==64 && seenr[0]==64 && seenr[1]==64,"CONSERVATION");
  done[3]=1;
 end
 if(cyc>30000) $fatal(1,"AHB_LEVELS_TIMEOUT");
end
// Responses start after the last write beat, independently of the request channel.
always @(negedge clk) if(reset_n) begin
 if(t_bvalid && !busyw) t_bvalid=0;
 else if(busyw && wn>seenw[0]+seenw[1]-1) begin t_bvalid=1;t_bid=wi;end
end
always @(posedge clk) if(reset_n) begin
 $display("Q %0d %0d %0d %0d %0d %0d",@V0@,@V1@,@W0@,@W1@,@DECIDE@,@WINNER@);
end
initial begin wait(done[3]);repeat(60) @(negedge clk);
 check(wn==128 && bn==64 && rn==64,"NO_DUPLICATES");
 $display("CHECK_PASS ahb_levels transfers=256");$finish;end
endmodule
'''


def bench(gen):
    m = json.loads((gen/'nocloom_ahb_levels.map.json').read_text())
    rtl = (gen/'nocloom_ahb_levels.v').read_text()
    decl, ports, init = [], [], []
    for p in m['ports']:
        name = p['name']
        if name == 'nl_i_s0_hready':
            ports.append('.nl_i_s0_hready(h_hreadyout)')
            continue
        net = ('h_'+p['signal'] if name.startswith('nl_i_s0_') else
               'a_'+p['signal'] if name.startswith('nl_i_s1_') else
               't_'+p['signal'] if name.startswith('nl_t_t0_') else 'clk' if p['signal']=='clock' else p['signal'])
        decl.append(f'{"reg" if p["direction"]=="input" else "wire"} [{p["width"]-1}:0] {net};')
        ports.append(f'.{name}({net})')
        if p['direction']=='input': init.append(net+'=0;')
    grant = next(s for s in m['network']['storage'] if s['resource']=='graph/req/req_node0>endpoint/t0')
    num = lambda ref: ref.removeprefix('value/')
    net = lambda ref: 'dut.nl_internal_v'+num(ref)
    owner, held = map(num, grant['control'][-2:])
    nxt = re.search(r'nl_internal_v%s <= nl_internal_v(\d+);' % owner, rtl).group(1)
    sel = re.search(r'assign nl_internal_v%s = \(nl_internal_v\d+ \? nl_internal_v(\d+) : nl_internal_v%s\);' % (nxt,owner), rtl).group(1)
    win = re.search(r'assign nl_internal_v%s = \(nl_internal_v%s \? nl_internal_v%s : nl_internal_v(\d+)\);' % (sel,held,owner), rtl).group(1)
    formal = m['network']['formal']['sources']
    read_level = m['definition']['contract']['arbitration']['qos']['sources']['s0']['levels']['read']
    replace = dict(WRITE_FIRST=str(int(read_level < 4)), DECL='\n'.join(decl), PORTS=','.join(ports), INIT='\n'.join(init), TOP=m['module'],
                   V0=net(formal['s0']['request_valid']), V1=net(formal['s1']['request_valid']),
                   W0=net(formal['s0']['request_write']), W1=net(formal['s1']['request_write']),
                   DECIDE=f"(({net(grant['acquire'])} | {net(grant['release'])}) & !dut.nl_internal_v{held})",
                   WINNER='dut.nl_internal_v'+win)
    text = TB
    for k,v in replace.items(): text=text.replace('@'+k+'@',v)
    return text


def run(cli, root, protocols=('ahb_v3',)):
    root.mkdir()
    results=[]
    for protocol in protocols:
        for high in (1,7):
            case=root/(protocol+str(high));case.mkdir()
            (case/'config.yaml').write_text(json.dumps(configuration(protocol,high)))
            q=subprocess.run([cli,'generate',str(case/'config.yaml'),'--out-dir',str(case/'gen')],capture_output=True,text=True,timeout=120)
            (case/'generate.log').write_text(q.stdout+q.stderr)
            assert q.returncode==0,q.stderr
            (case/'network.v').write_text(generated_rtl(case/'gen/nocloom_ahb_levels.v'))
            (case/'tb.sv').write_text(bench(case/'gen'))
            rc=simulate(case,case/'compiled');log=(case/'simulation.log').read_text()
            assert rc==0 and 'CHECK_PASS ahb_levels transfers=256' in log,log[-3000:]
            contests=[0,0]
            for line in log.splitlines():
                if not line.startswith('Q '): continue
                v0,v1,w0,w1,dec,win=map(int,line.split()[1:])
                if dec:
                    assert (v0,v1)[win],line
                    if v0 and v1:
                        levels=(8-high if w0 else high,4)
                        assert levels[win]==max(levels),line
                        contests[w0]+=1
            assert min(contests)>0,contests
            results.append(dict(protocol=protocol,read_level=high,write_level=8-high,contests=contests,transfers=256))
    return results
