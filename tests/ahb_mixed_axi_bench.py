"""Native AXI target remains live alongside a dedicated AHB target."""
from target_id_model import target_id


def write_bench(work, mapping, target, source):
    declarations, connections, init, quiet = [], [], [], []
    for p in mapping['ports']:
        name, field = p['name'], p['signal']
        if field == 'clock': net = 'clk'
        elif field == 'reset_n': net = 'rst'
        elif name.startswith(f'nl_i_s{source}_'): net = 'a_'+field
        elif name.startswith(f'nl_t_t{target}_'): net = 't_'+field
        else:
            net = name
            if p['direction'] == 'output' and field in ('hsel', 'awvalid', 'arvalid', 'wvalid'):
                quiet.append(net)
        declarations.append(f'{"reg" if p["direction"] == "input" else "wire"} [{p["width"]-1}:0] {net};')
        connections.append(f'.{name}({net})')
        if p['direction'] == 'input': init.append(net+'=0;')
    text = r'''
`timescale 1ns/1ps
module tb;
@DECL@
@TOP@ dut(@CONN@);
initial clk=0;always #5 clk=~clk;
integer c,aw,ar,w,r,b,taw,tar,tw,tr,mode;
task check;
 input v;input [8*40-1:0] msg;
 begin if(v!==1'b1) begin $display("ASSERT_FAIL:AHB_MIXED.%0s cycle=%0d",msg,c);$fatal(1);end end
endtask
initial begin
 @INIT@
 for(mode=0;mode<2;mode=mode+1) begin
  @(negedge clk);rst=0;repeat(3) @(posedge clk);@(negedge clk);rst=1;
  aw=0;ar=0;w=0;r=0;b=0;taw=0;tar=0;tw=0;tr=0;
  for(c=0;c<4000 && b+r<5;c=c+1) begin
   @(negedge clk);
   a_awvalid=!aw && c>3;a_awaddr=@BASE@+64;a_awlen=3;a_awsize=2;a_awburst=1;a_awid=5;a_awprot=5;a_awcache=2;
   a_wvalid=w<4 && c%3!=1;a_wdata=32'hac000100+w;a_wstrb=15;a_wlast=w==3;
   a_arvalid=!ar;a_araddr=@BASE@+256;a_arlen=3;a_arsize=2;a_arburst=1;a_arid=6;a_arprot=1;a_arcache=1;
   a_bready=c>100;a_rready=c>80 && c%4!=1;
   t_awready=!taw && c%3!=0;t_arready=!tar && c%5!=0;t_wready=taw && tw<4 && c%3!=1;
   t_bvalid=tw==4 && b==0;t_bid=@WID@;t_bresp=mode?2:0;
   t_rvalid=tar && tr<4;t_rid=@RID@;t_rresp=mode && tr==1?2:0;t_rdata=32'hb1002200+tr;t_rlast=tr==3;
   @(posedge clk);
   check({@QUIET@}===0,"NONSELECTED");
   if(a_awvalid&&a_awready) aw=1;
   if(a_arvalid&&a_arready) ar=1;
   if(a_wvalid&&a_wready) w=w+1;
   if(t_awvalid&&t_awready) begin
    check(!taw && t_awaddr==64 && t_awlen==3 && t_awsize==2 && t_awburst==1 && t_awid==@WID@ && t_awprot==5 && t_awcache==2,"AW");taw=1;
   end
   if(t_arvalid&&t_arready) begin
    check(!tar && t_araddr==256 && t_arlen==3 && t_arsize==2 && t_arburst==1 && t_arid==@RID@,"AR");tar=1;
   end
   if(t_wvalid&&t_wready) begin
    check(t_wdata===32'hac000100+tw && t_wstrb===15 && t_wlast===(tw==3),"WDATA");tw=tw+1;
   end
   if(t_rvalid&&t_rready) tr=tr+1;
   if(a_bvalid&&a_bready) begin check(a_bid===5 && a_bresp===(mode?2:0),"BRESP");b=b+1;end
   if(a_rvalid&&a_rready) begin
    check(a_rid===6 && a_rlast===(r==3) && a_rresp===(mode&&(r==1||a_rresp===2)?2:0),"RRESP"); // a response run answers as one
    check(a_rdata===(a_rresp===2?0:32'hb1002200+r),"RDATA");r=r+1;
   end
  end
  check(aw==1&&ar==1&&w==4&&r==4&&b==1&&tw==4&&tr==4,"CONSERVATION");
 end
 $display("NOCLOOM_PASS mixed AXI target cases=2");$finish;
end
endmodule
'''
    numbers = mapping['definition']['target_ids']
    width = next(p['width'] for p in mapping['ports'] if p['name'] == f'nl_t_t{target}_arid')
    ids = {'WID': target_id(numbers, f's{source}', f't{target}', width, 5, True),
           'RID': target_id(numbers, f's{source}', f't{target}', width, 6, False)}
    replacements = {**{k: str(v) for k, v in ids.items()}, 'DECL':'\n'.join(declarations),'TOP':mapping['module'],'CONN':','.join(connections),
                    'INIT':'\n'.join(init), 'QUIET':','.join(quiet),
                    'BASE':mapping['definition']['targets'][target]['begin']}
    for key,value in replacements.items():text=text.replace('@'+key+'@',value)
    (work/'tb.sv').write_text(text)
