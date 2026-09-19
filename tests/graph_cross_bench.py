"""External scoreboards for mixed native ports on a selective transport graph."""
from target_id_model import id_function


def write_bench(work, mapping, decerr=3):
    """decerr: the code of an unconnected pair, 3 with error codes, else 2."""
    declarations, connections, initials = [], [], []
    widths, seen = {}, set()
    for port in mapping['ports']:
        name = port['name']
        if name.startswith(('nl_i_', 'nl_t_')):
            side, _, field = name.split('_', 3)[1:]
            key = side+'_'+field
            widths[key] = max(widths.get(key, 0), port['width'])
    for port in mapping['ports']:
        name, width, inp = port['name'], port['width'], port['direction']=='input'
        if name.startswith(('nl_i_', 'nl_t_')):
            side, endpoint, field = name.split('_', 3)[1:]
            key, index = side+'_'+field, int(endpoint[1:])
            if key not in seen:
                seen.add(key)
                declarations.append(f'{"reg" if inp else "wire"} [{widths[key]-1}:0] {key}[0:2];')
                if inp:
                    initials.extend(f'{key}[{i}]=0;' for i in range(3))
            selection = f'{key}[{index}]'
            if width < widths[key]:
                selection += f'[{width-1}:0]'
                if not inp:
                    declarations.append(f"assign {key}[{index}][{widths[key]-1}:{width}] = {widths[key]-width}'b0;")
            connections.append(f'.{name}({selection})')
        else:
            declarations.append(f'reg [{width-1}:0] {name};')
            connections.append(f'.{name}({name})')
    declarations.extend(["assign i_bid[1]=0;", "assign i_rid[1]=0;", "assign i_rlast[1]=1;"])
    text = r'''
`timescale 1ns/1ps
module tb;
@DECL@
@TOP@ dut(@PORTS@);
initial clk_main=0;
always #5 clk_main=~clk_main;
integer phase,cycle,s,t,j;
integer asent[0:2],arsent[0:2],wsent[0:2],bseen[0:2],rseen[0:2],target_of[0:2];
integer wb[0:2],rb[0:2],wo[0:2],ro[0:2],wc[0:2],rc[0:2];
integer awcount,arcount,wcount,rcount,bdone,rdone,expected_requests,expected_beats;
integer sum_aw=0,sum_ar=0,sum_w=0,sum_r=0;
reg done;
reg [8:0] allowed_w=0,denied_w=0,allowed_r=0,denied_r=0;
@TARGETIDFN@
function integer permitted; input integer owner,target;
 begin permitted=(target==owner || target==(owner+1)%3);end
endfunction
function integer bus_bytes; input integer owner;
 begin bus_bytes=(owner==0?16:(owner==1?4:32));end
endfunction
function integer beats; input integer owner;
 begin beats=owner==1?1:(phase==8?16:(owner==0?1:2));end
endfunction
function integer factor; input integer owner;begin factor=bus_bytes(owner)/4;end endfunction
function integer low_beats; input integer owner;begin low_beats=beats(owner)*factor(owner);end endfunction
function integer size_for; input integer owner;begin size_for=owner==0?4:(owner==1?2:5);end endfunction
function integer id_for; input integer owner,read_side;
 begin id_for=owner==1?0:owner+(read_side?5:1);end
endfunction
function integer target_error; input integer target;begin target_error=(phase==4 && target==1);end endfunction
function [255:0] data_word;
 input integer owner,beat,read_side;integer byte_;
 begin
  data_word=0;
  for(byte_=0;byte_<bus_bytes(owner);byte_=byte_+1)
   data_word[8*byte_+:8]=(owner*43+beat*17+byte_*7+read_side*91+phase*13)%256;
 end
endfunction
// A failed beat returns zero data, a wide one with any failed 32-bit word
// as a whole.
function integer in_first_run;
 input integer owner,beat;
 begin in_first_run=beat*(1<<size_for(owner))<32;end
endfunction
function [255:0] read_word;
 input integer owner,beat,target;
 begin
  read_word=data_word(owner,beat,1);
  if(!permitted(owner,target) || (target_error(target) && beat==0)) read_word=0;
  // A response run answers as one (a word of up to 32 bytes on a response link).
  if(target_error(target) && in_first_run(owner,beat) && i_rresp[owner]==2) read_word=0;
 end
endfunction
function [31:0] strobe_word;
 input integer owner,beat;integer byte_;
 begin
  strobe_word=0;
  for(byte_=0;byte_<bus_bytes(owner);byte_=byte_+1)
   strobe_word[byte_]=(phase<3 || (byte_+beat)%3!=1) && !(phase==5 && owner==2);
 end
endfunction
function [31:0] mask_word;
 input [3:0] strobe;integer byte_;
 begin
  mask_word=0;
  for(byte_=0;byte_<4;byte_=byte_+1) if(strobe[byte_]) mask_word[byte_*8+:8]=255;
 end
endfunction
task check;
 input condition; input [8*48-1:0] name;
 begin
  if(condition!==1'b1) begin
   $display("ASSERT_FAIL:GRAPH_CROSS.%0s phase=%0d cycle=%0d s=%0d t=%0d",name,phase,cycle,s,t);$fatal(1);
  end
 end
endtask
task reset;
 begin
  @(negedge clk_main);reset_n=0;@INIT@
  for(s=0;s<3;s=s+1) begin
   asent[s]=0;arsent[s]=0;wsent[s]=0;bseen[s]=0;rseen[s]=0;
   wb[s]=0;rb[s]=0;wo[s]=0;ro[s]=0;wc[s]=0;rc[s]=0;
  end
  repeat(4) @(posedge clk_main);
  @(negedge clk_main);reset_n=1;
  // The domain leaves reset on the second clock edge after the port deasserts.
  repeat(2) @(posedge clk_main); @(negedge clk_main);
 end
endtask
initial begin
 @INIT@
 reset_n=0;
 for(phase=0;phase<10;phase=phase+1) begin
  if(phase==9) begin
   reset;
   i_awaddr[0]='h1000;i_awlen[0]=15;i_awsize[0]=4;i_awburst[0]=1;i_awid[0]=1;
   t_awready[0]=1;t_wready[0]=1;j=0;
   for(cycle=0;cycle<2000 && j==0;cycle=cycle+1) begin
    @(negedge clk_main);i_awvalid[0]=!asent[0];i_wvalid[0]=wsent[0]<16;
    i_wdata[0]=data_word(0,wsent[0],0);i_wstrb[0]='hffff;i_wlast[0]=wsent[0]==15;
    @(posedge clk_main);
    if(i_awvalid[0] && i_awready[0]) asent[0]=1;
    if(i_wvalid[0] && i_wready[0]) wsent[0]=wsent[0]+1;
    if(t_wvalid[0] && t_wready[0]) begin check(!t_wlast[0],"RESET_MID_PACKET");j=1;end
   end
   check(j==1,"RESET_ARRIVAL");reset;
   for(s=0;s<3;s=s+1) begin i_bready[s]=1;i_rready[s]=1;end
   for(t=0;t<3;t=t+1) begin t_awready[t]=1;t_wready[t]=1;t_arready[t]=1;end
   repeat(40) begin
    @(posedge clk_main);#1;
    for(t=0;t<3;t=t+1) check(!t_awvalid[t]&&!t_wvalid[t]&&!t_arvalid[t],"RESET_NO_REPLAY");
    for(s=0;s<3;s=s+1) check(i_awready[s]&&i_arready[s]&&!i_bvalid[s]&&!i_rvalid[s],"RESET_REARM");
   end
  end
  reset;
  expected_requests=0;expected_beats=0;
  for(s=0;s<3;s=s+1) begin
   target_of[s]=phase<3?(s+phase)%3:(phase<6?phase-3:(phase<8?0:s));
   if(permitted(s,target_of[s])) begin expected_requests=expected_requests+1;expected_beats=expected_beats+low_beats(s);end
  end
  awcount=0;arcount=0;wcount=0;rcount=0;bdone=0;rdone=0;done=0;
  for(cycle=0;cycle<20000&&!done;cycle=cycle+1) begin
   @(negedge clk_main);
   for(s=0;s<3;s=s+1) begin
    i_awvalid[s]=!asent[s] && !(s==1 && phase==0 && cycle<9);
    i_awaddr[s]=target_of[s]*65536+'h1000+s*256;
    i_awlen[s]=beats(s)-1;i_awsize[s]=size_for(s);i_awburst[s]=1;i_awid[s]=id_for(s,0);i_awprot[s]=s;i_awcache[s]=s==1?0:2;
    i_wvalid[s]=wsent[s]<beats(s) && !(s==1 && phase==1 && cycle<9);
    i_wdata[s]=data_word(s,wsent[s],0);i_wstrb[s]=strobe_word(s,wsent[s]);i_wlast[s]=wsent[s]==beats(s)-1;
    i_arvalid[s]=!arsent[s];i_araddr[s]=target_of[s]*65536+'h2000+s*256;
    i_arlen[s]=beats(s)-1;i_arsize[s]=size_for(s);i_arburst[s]=1;i_arid[s]=id_for(s,1);i_arprot[s]=s;i_arcache[s]=s==1?0:2;
    i_bready[s]=!(phase==7 && s==0 && cycle<2000);
    i_rready[s]=!(phase==6 && s==0 && cycle<2000) && cycle%5!=0;
   end
   for(t=0;t<3;t=t+1) begin
    t_awready[t]=!wb[t] && cycle%3!=0;t_wready[t]=wb[t]&&wc[t]<low_beats(wo[t])&&cycle%4!=0;
    t_bvalid[t]=wb[t]&&wc[t]==low_beats(wo[t]);t_bid[t]=target_id(t,wo[t],0);t_bresp[t]=target_error(t)?2:0;
    t_arready[t]=!rb[t]&&cycle%4!=0;t_rvalid[t]=rb[t]&&rc[t]<low_beats(ro[t]);
    t_rid[t]=target_id(t,ro[t],1);t_rresp[t]=target_error(t)&&rc[t]==factor(ro[t])-1?2:0;t_rlast[t]=rc[t]==low_beats(ro[t])-1;
    t_rdata[t]=data_word(ro[t],rc[t]/factor(ro[t]),1) >> ((rc[t]%factor(ro[t]))*32);
   end
   @(posedge clk_main);
   for(s=0;s<3;s=s+1) begin
    if(i_awvalid[s]&&i_awready[s]) asent[s]=asent[s]+1;
    if(i_arvalid[s]&&i_arready[s]) arsent[s]=arsent[s]+1;
    if(i_wvalid[s]&&i_wready[s]) wsent[s]=wsent[s]+1;
    if(i_bvalid[s]&&i_bready[s]) begin
     check(bseen[s]==0 && i_bid[s]===id_for(s,0),"B_ID_ONCE");
     check(i_bresp[s]===(permitted(s,target_of[s])?(target_error(target_of[s])?2:0):@DECERR@),"B_RESULT");
     bseen[s]=bseen[s]+1;
     if(phase<3) begin
      if(permitted(s,target_of[s])) allowed_w[3*s+target_of[s]]=1;
      else denied_w[3*s+target_of[s]]=1;
     end
    end
    if(i_rvalid[s]&&i_rready[s]) begin
     check(rseen[s]<beats(s) && i_rid[s]===id_for(s,1) && i_rlast[s]===(rseen[s]==beats(s)-1),"R_FRAME");
     check(i_rresp[s]===(permitted(s,target_of[s])?(target_error(target_of[s])&&(rseen[s]==0||(in_first_run(s,rseen[s])&&i_rresp[s]==2))?2:0):@DECERR@),"R_RESULT");
     check(i_rdata[s]===read_word(s,rseen[s],target_of[s]),"R_DATA");
     rseen[s]=rseen[s]+1;
     if(phase<3 && rseen[s]==beats(s)) begin
      if(permitted(s,target_of[s])) allowed_r[3*s+target_of[s]]=1;
      else denied_r[3*s+target_of[s]]=1;
     end
    end
   end
   for(t=0;t<3;t=t+1) begin
    if(t_awvalid[t]&&t_awready[t]) begin
     wo[t]=(t_awaddr[t]-'h1000)/256;
     check(wo[t]>=0 && wo[t]<3,"W_OWNER");
     check(permitted(wo[t],t),"FORBIDDEN_WRITE");
     check(target_of[wo[t]]==t && t_awaddr[t]==='h1000+wo[t]*256 && t_awlen[t]===low_beats(wo[t])-1 && t_awsize[t]===2 && t_awburst[t]===1 && t_awid[t]===target_id(t,wo[t],0),"AW_DESCRIPTOR");
     check(t_awprot[t]===wo[t] && t_awcache[t]===(wo[t]==1?0:2),"AW_ATTRIBUTES");
     wb[t]=1;wc[t]=0;awcount=awcount+1;
    end
    if(t_wvalid[t]&&t_wready[t]) begin
     check(wb[t] && wc[t]<low_beats(wo[t]) && t_wlast[t]===(wc[t]==low_beats(wo[t])-1),"W_FRAME");
     check(t_wstrb[t]===(((strobe_word(wo[t],wc[t]/factor(wo[t])) >> ((wc[t]%factor(wo[t]))*4))&15)),"W_STROBE");
     check(((t_wdata[t]^((data_word(wo[t],wc[t]/factor(wo[t]),0) >> ((wc[t]%factor(wo[t]))*32)) & 256'hffffffff))&mask_word(t_wstrb[t]))===0,"W_DATA");
     check((t_wdata[t]&~mask_word(t_wstrb[t]))===0,"W_UNSTROBED_BYTE");
     wc[t]=wc[t]+1;wcount=wcount+1;
    end
    if(t_bvalid[t]&&t_bready[t]) begin wb[t]=0;bdone=bdone+1;end
    if(t_arvalid[t]&&t_arready[t]) begin
     ro[t]=(t_araddr[t]-'h2000)/256;
     check(ro[t]>=0 && ro[t]<3,"R_OWNER");
     check(permitted(ro[t],t),"FORBIDDEN_READ");
     check(target_of[ro[t]]==t && t_araddr[t]==='h2000+ro[t]*256 && t_arlen[t]===low_beats(ro[t])-1 && t_arsize[t]===2 && t_arburst[t]===1 && t_arid[t]===target_id(t,ro[t],1),"AR_DESCRIPTOR");
     check(t_arprot[t]===ro[t] && t_arcache[t]===(ro[t]==1?0:2),"AR_ATTRIBUTES");
     rb[t]=1;rc[t]=0;arcount=arcount+1;
    end
    if(t_rvalid[t]&&t_rready[t]) begin
     rc[t]=rc[t]+1;rcount=rcount+1;
     if(rc[t]==low_beats(ro[t])) begin rb[t]=0;rdone=rdone+1;end
    end
   end
   if(phase==0 && cycle==8) check(wsent[1]==1 && asent[1]==0,"LITE_W_BEFORE_AW");
   if(phase==1 && cycle==8) check(asent[1]==1 && wsent[1]==0,"LITE_AW_BEFORE_W");
   // A stalled multibeat read may hold a shared target and the responses
   // behind it (docs/protocols.md): only the holding source is checked while it holds
   // (its response reached it and waits there, nothing taken); the others
   // are reported, and EXACT_COUNT requires all after release.
   if(cycle==1500 && phase==6) begin
    check(rseen[0]==0 && i_rvalid[0]===1,"R_HOLD_SOURCE");
    $display("HOLD_OBSERVATION phase=6 b=%0d%0d%0d r2=%0d/%0d",bseen[0],bseen[1],bseen[2],rseen[2],beats(2));
   end
   if(cycle==1500 && phase==7) begin
    check(bseen[0]==0 && i_bvalid[0]===1,"B_HOLD_SOURCE");
    $display("HOLD_OBSERVATION phase=7 b=%0d%0d%0d r0=%0d/%0d r2=%0d/%0d",bseen[0],bseen[1],bseen[2],rseen[0],beats(0),rseen[2],beats(2));
   end
   done=1;
   for(s=0;s<3;s=s+1) if(bseen[s]!=1 || rseen[s]!=beats(s)) done=0;
  end
  check(done && awcount==expected_requests && arcount==expected_requests && wcount==expected_beats && rcount==expected_beats && bdone==expected_requests && rdone==expected_requests,"EXACT_COUNT");
  sum_aw=sum_aw+awcount;sum_ar=sum_ar+arcount;sum_w=sum_w+wcount;sum_r=sum_r+rcount;
 end
 check(allowed_w==9'h173 && allowed_r==9'h173 && denied_w==9'h08c && denied_r==9'h08c,"PAIR_COVERAGE");
 $display("AXI4_TARGET_PASS graph_cross phases=10 admitted_pairs=6 denied_pairs=3 reset_mid_packet=1 aw=%0d ar=%0d w=%0d r=%0d",sum_aw,sum_ar,sum_w,sum_r);$finish;
end
initial begin #3000000;$fatal(1,"ASSERT_FAIL:GRAPH_CROSS.TIMEOUT");end
endmodule
'''
    text = text.replace('@DECL@', '\n'.join(declarations)).replace('@PORTS@', ','.join(connections))
    text = text.replace('@TOP@', mapping['module']).replace('@INIT@', '\n'.join(initials))
    text = text.replace('@DECERR@', str(decerr))
    text = text.replace('@TARGETIDFN@', id_function('target_id', mapping['definition']['target_ids'], mapping['ports'],
        lambda s, read: 0 if s == 1 else s + (5 if read else 1)))
    assert '@' not in text.replace('@(', '')
    (work/'tb.sv').write_text(text)
