"""Independent external AXI scoreboards for burst-preserving exchange outlets."""
import json

from target_id_model import id_function


def write_bench(work, decerr=3, run_word=32):
    """decerr: the code of an unmapped burst, 3 with error codes, else 2. run_word: the widest
    response word on the routes: a failed beat fails the beats of its word."""
    meta = json.loads((work / "bindings.json").read_text())
    db, ids, lb = (meta[k] for k in ("data_bits", "id_bits", "len_bits"))
    declarations, connections, initials = [], [], []
    seen = set()
    bus_widths = {}
    for p in meta["ports"]:
        if p["name"].startswith(("nl_i_", "nl_t_")):
            side, _, signal = p["name"].split("_", 3)[1:]
            key = f"{side}_{signal}"
            bus_widths[key] = max(bus_widths.get(key, 0), p["width"])
    for p in meta["ports"]:
        name, width, inp = p["name"], p["width"], p["input"]
        if name.startswith(("nl_i_", "nl_t_")):
            side, endpoint, signal = name.split("_", 3)[1:]
            key = f"{side}_{signal}"
            index = int(endpoint[1:])
            array_width = bus_widths[key]
            if key not in seen:
                seen.add(key)
                declarations.append(f"{'reg' if inp else 'wire'} [{array_width-1}:0] {key}[0:1];")
                if inp:
                    initials += [f"{key}[0]=0; {key}[1]=0;"]
            selection = f"{key}[{index}]"
            if width < array_width:
                selection += f"[{width-1}:0]"
                if not inp:
                    declarations.append(f"assign {key}[{index}][{array_width-1}:{width}] = {array_width-width}'b0;")
            connections.append(f".{name}({selection})")
        else:
            declarations.append(f"reg [{width-1}:0] {name};")
            connections.append(f".{name}({name})")
    mixed = meta.get("mixed", False)
    if mixed:
        declarations.append("assign i_rlast[1] = 1'b1;")
        if ids:
            declarations += ["assign i_bid[1] = 0;", "assign i_rid[1] = 0;"]
    text = r'''
`timescale 1ns/1ps
module tb;
@DECL@
@MODULE@ dut(@CONNECT@);
initial clk_main=0;
always #5 clk_main=~clk_main;
integer phase,cycle,s,t,k,n,sz,kind,mode,target_of[0:1];
integer awsent[0:1],arsent[0:1],wsent[0:1],bseen[0:1],rseen[0:1];
integer wbusy[0:1],rbusy[0:1],wowner[0:1],rowner[0:1],wc[0:1],rc[0:1];
integer awtotal=0,artotal=0,wtotal=0,rtotal=0,completed=0;
integer expected_aw,expected_ar,expected_w,expected_r;
reg [@ABM@:0] start[0:1];
reg [@DBM@:0] mask;
reg [7:0] previous_source_w[0:1];
function [@ABM@:0] target_base;
 input integer target;
 begin target_base=(target==0 ? @AB@'h@BASE0@ : @AB@'h@BASE1@);end
endfunction
// Target AxCACHE under the default AMBA attribute mapping: ARCACHE[3] and
// AWCACHE[2] are lookup, AxCACHE[2] | AxCACHE[3].
function [3:0] amba_r; input [3:0] c; amba_r = {c[3] | c[2], c[2:0]}; endfunction
function [3:0] amba_w; input [3:0] c; amba_w = {c[3], c[3] | c[2], c[1:0]}; endfunction
@TARGETIDFN@
function integer lite;
 input integer owner;
 begin lite=@MIXED@ && owner==1;end
endfunction
function integer beats_for;
 input integer owner;
 begin beats_for=lite(owner) ? 1 : n;end
endfunction
function integer size_for;
 input integer owner;
 begin size_for=lite(owner) ? @FULLSIZE@ : sz;end
endfunction
function integer kind_for;
 input integer owner;
 begin kind_for=lite(owner) ? 1 : kind;end
endfunction
function integer mode_for;
 input integer owner;
 begin mode_for=mode==3 ? 0 : mode;end
endfunction
// A Lite access uses the full bus width: the target sees the address with
// the bits below the bus width cleared, while WSTRB passes unchanged.
function [31:0] target_address;
 input integer owner;
 begin target_address=lite(owner) ? (start[owner]%65536)/@BYTES@*@BYTES@ : start[owner]%65536;end
endfunction
function integer error_beat;
 input integer owner;
 begin error_beat=lite(owner) ? 0 : 1;end
endfunction
// A response run answers as one: the other beats of the failed beat's word may fail with it.
// FIXED, narrow and WRAP top beats end a run.
function integer in_error_run;
 input integer owner,beat;
 begin
  in_error_run=kind_for(owner)==1 && size_for(owner)==@FULLSIZE@ &&
               local_beat(owner,beat)/@RUNW@==local_beat(owner,error_beat(owner))/@RUNW@;
 end
endfunction
function [31:0] local_beat;
 input integer owner,beat;
 integer aligned,boundary;
 begin
  aligned=(start[owner]%65536)/(1<<size_for(owner))*(1<<size_for(owner));
  boundary=beats_for(owner)*(1<<size_for(owner));
  if(kind_for(owner)==0) local_beat=aligned;
  else if(kind_for(owner)==2) local_beat=(aligned/boundary)*boundary+(aligned+beat*(1<<size_for(owner)))%boundary;
  else local_beat=aligned+beat*(1<<size_for(owner));
 end
endfunction
function [@DBM@:0] data_word;
 input integer owner,beat,read_side;
 integer lane;
 begin
  for(lane=0;lane<@BYTES@;lane=lane+1)
   data_word[8*lane+:8]=(owner*47+phase*13+beat*7+lane*19+read_side*97)%256;
 end
endfunction
function [@BEM@:0] strobes;
 input integer owner,beat;
 integer offset,low,high,lane;
 begin
  offset=local_beat(owner,beat)%@BYTES@;
  low=offset;
  if(beat==0 || kind_for(owner)==0) low=low+start[owner]%(1<<size_for(owner));
  high=offset+(1<<size_for(owner));
  strobes=0;
  for(lane=0;lane<@BYTES@;lane=lane+1)
   if(lane>=low && lane<high) strobes[lane]=1;
  if(lite(owner) && phase==17) strobes=0;
  if(lite(owner) && phase==18) strobes=strobes & @BYTES@'h55;
  if(lite(owner) && phase==19) strobes=1; // below the addressed byte
 end
endfunction
function [@DBM@:0] read_mask;
 input integer owner,beat;
 integer lane;
 reg [@BEM@:0] be;
 begin
  be=lite(owner) ? {@BYTES@{1'b1}} : strobes(owner,beat);
  read_mask=0;
  for(lane=0;lane<@BYTES@;lane=lane+1)
   if(be[lane]) read_mask[8*lane+:8]=255;
 end
endfunction
task check;
 input condition;
 input [8*40-1:0] label;
 begin
  if(condition !== 1'b1) begin
   $display("ASSERT_FAIL:AXI4_MASTER.%0s phase=%0d cycle=%0d s=%0d t=%0d",label,phase,cycle,s,t);
   $fatal(1);
  end
 end
endtask
task reset;
 begin
  @(negedge clk_main); reset_n=0;
  @INITIAL@
  for(t=0;t<2;t=t+1) begin wbusy[t]=0;rbusy[t]=0;wc[t]=0;rc[t]=0;end
  repeat(3) begin @(posedge clk_main); #1;
   for(t=0;t<2;t=t+1) check({t_awvalid[t],t_wvalid[t],t_arvalid[t],t_bready[t],t_rready[t]}===0,"RESET");
  end
  @(negedge clk_main);reset_n=1;
  // The domain leaves reset on the second clock edge after the port deasserts.
  repeat(2) @(posedge clk_main); @(negedge clk_main);
 end
endtask
initial begin
 @INITIAL@
 reset_n=0;
 phase=0;cycle=0;
 reset;
 for(phase=0;phase<@PHASES@;phase=phase+1) begin
  n=4; sz=@FULLSIZE@; kind=1; mode=0;
  if(phase==2) begin n=16; kind=0; end
  if(phase==3) begin n=4; kind=2;end
  // Narrow single beats: a multi-beat narrow INCR is packed into words at
  // the source (covered by the per-target width and stress benches).
  if(phase==4) begin n=1; sz=0; end
  if(phase==5) begin n=1; sz=1; end
  if(phase==6) n=@MAXBEATS@;
  if(phase==7) mode=1; // target errors, remaining beats still complete
  if(phase==8) mode=2; // decode miss, no target traffic
  if(phase==9) mode=3; // exclusive without exclusive support: a normal access, OKAY, AxLOCK 0
  // Hold the B (mode 4) or R (mode 5) consumers for 350 cycles. Reads and
  // writes share the response network, so a held response may hold the
  // other direction's responses behind it; every response completes once
  // the consumer resumes.
  if(phase==10) mode=4;
  if(phase==11) mode=5;
  if(phase==12) begin sz=1; kind=0;end
  if(phase==13) begin n=1;sz=@FULLSIZE@;end
  if(phase==14) begin n=16;kind=2;end
  if(phase==15) begin n=16;kind=1;end
  for(s=0;s<2;s=s+1) begin
   target_of[s]=@DESTINATION@;
   start[s]=target_base(target_of[s])+s*16384+phase*128+256;
   if(!lite(s) && (phase==1 || phase==12 || phase==13)) start[s]=start[s]+1;
   if(lite(s) && phase==16) start[s]=start[s]+1;
   if(lite(s) && phase==19) start[s]=start[s]+@BYTES@-1;
   if(phase==3 || phase==14) start[s]=start[s]+(1<<size_for(s));
   if(mode==2) start[s]=@AB@'h@MISS@+s*16384;
   @TOP_BOUNDARY@
   awsent[s]=0;arsent[s]=0;wsent[s]=0;bseen[s]=0;rseen[s]=0;
  end
  expected_aw=awtotal;expected_ar=artotal;expected_w=wtotal;expected_r=rtotal;
  for(s=0;s<2;s=s+1) if(mode_for(s)!=2 && mode_for(s)!=3) begin
   expected_aw=expected_aw+1;expected_ar=expected_ar+1;
   expected_w=expected_w+beats_for(s);expected_r=expected_r+beats_for(s);
  end
  cycle=0;
  while(bseen[0]+bseen[1]!=2 || rseen[0]+rseen[1]!=beats_for(0)+beats_for(1)) begin
   @(negedge clk_main);
   for(s=0;s<2;s=s+1) begin
    i_awaddr[s]=start[s];i_araddr[s]=start[s];
    i_awlen[s]=n-1;i_arlen[s]=n-1;i_awsize[s]=sz;i_arsize[s]=sz;
    i_awburst[s]=kind;i_arburst[s]=kind;i_awcache[s]=phase%16;i_arcache[s]=phase%16;
    i_awprot[s]=phase%8;i_arprot[s]=phase%8;
    i_awlock[s]=(mode==3);i_arlock[s]=(mode==3);
    @SOURCEID@
    i_awvalid[s]=!awsent[s] && !(lite(s) && phase==0 && cycle<9);i_arvalid[s]=!arsent[s];
    i_wvalid[s]=(wsent[s]<beats_for(s)) && !(lite(s) && phase==1 && cycle<9);i_wdata[s]=data_word(s,wsent[s],0);
    i_wstrb[s]=strobes(s,wsent[s]);i_wlast[s]=(wsent[s]==n-1);
    i_bready[s]=(mode!=4 || cycle>350) && cycle%5!=0;
    i_rready[s]=(mode!=5 || cycle>350) && cycle%7!=0;
   end
   for(t=0;t<2;t=t+1) begin
    t_awready[t]=!wbusy[t] && cycle%3!=0;
    t_arready[t]=!rbusy[t] && cycle%4!=0;
    t_wready[t]=wbusy[t] && wc[t]<beats_for(wowner[t]) && cycle%5!=0;
    t_bvalid[t]=wbusy[t] && wc[t]==beats_for(wowner[t]);
    t_bresp[t]=(mode_for(wowner[t])==1 ? 2:0);
    t_rvalid[t]=rbusy[t];t_rdata[t]=data_word(rowner[t],rc[t],1);
    t_rresp[t]=(mode_for(rowner[t])==1 && rc[t]==error_beat(rowner[t]) ? 2:0);t_rlast[t]=(rc[t]==beats_for(rowner[t])-1);
    @TARGETID@
   end
   @(posedge clk_main);
   for(s=0;s<2;s=s+1) begin
    if(i_awvalid[s] && i_awready[s]) awsent[s]=1;
    if(i_arvalid[s] && i_arready[s]) arsent[s]=1;
    if(i_wvalid[s] && i_wready[s]) wsent[s]=wsent[s]+1;
    if(i_bvalid[s] && i_bready[s]) begin
     check(!bseen[s],"DUPLICATE_B");
     check(i_bresp[s]==(mode_for(s)==2 ? @DECERR@ : (mode_for(s)==1 || mode_for(s)==3 ? 2:0)),"B_RESULT");
     @BID@
     bseen[s]=1;
    end
    if(i_rvalid[s] && i_rready[s]) begin
     check(rseen[s]<beats_for(s),"DUPLICATE_R");
     check(i_rlast[s]==(rseen[s]==beats_for(s)-1),"R_LAST");
     check(i_rresp[s]==(mode_for(s)==2 ? @DECERR@ : (mode_for(s)==3 || (mode_for(s)==1 && rseen[s]==error_beat(s)) ? 2:0))
           || (mode_for(s)==1 && in_error_run(s,rseen[s]) && i_rresp[s]==2),"R_RESULT");
     if(i_rresp[s]==0) begin
      mask=read_mask(s,rseen[s]);
      check(((i_rdata[s]^data_word(s,rseen[s],1))&mask)===0,"R_DATA");
     end else check(i_rdata[s]===0,"ERROR_READ_ZERO");
     @RID@
     rseen[s]=rseen[s]+1;
    end
   end
   for(t=0;t<2;t=t+1) begin
    if(t_awvalid[t] && t_awready[t]) begin
     wowner[t]=(target_of[0]!=t ? 1 : (target_of[1]!=t ? 0 : (t_awaddr[t]===target_address(1) ? 1 : 0)));
     check(t==target_of[wowner[t]],"WRITE_ROUTE");
     check(t_awaddr[t]===target_address(wowner[t]),"RAW_WRITE_ADDRESS");
     check(t_awlen[t]==beats_for(wowner[t])-1 && t_awsize[t]==size_for(wowner[t]) && t_awburst[t]==kind_for(wowner[t]),"WRITE_DESCRIPTOR");
     check(t_awcache[t]==(lite(wowner[t]) ? 0 : amba_w(phase%16)) && t_awprot[t]==phase%8 && t_awlock[t]==0,"WRITE_ATTRIBUTES");
     @AWID@
     wbusy[t]=1;wc[t]=0;awtotal=awtotal+1;
    end
    if(t_arvalid[t] && t_arready[t]) begin
     rowner[t]=(target_of[0]!=t ? 1 : (target_of[1]!=t ? 0 : (t_araddr[t]===target_address(1) ? 1 : 0)));
     check(t==target_of[rowner[t]],"READ_ROUTE");
     check(t_araddr[t]===target_address(rowner[t]),"RAW_READ_ADDRESS");
     check(t_arlen[t]==beats_for(rowner[t])-1 && t_arsize[t]==size_for(rowner[t]) && t_arburst[t]==kind_for(rowner[t]),"READ_DESCRIPTOR");
     check(t_arcache[t]==(lite(rowner[t]) ? 0 : amba_r(phase%16)) && t_arprot[t]==phase%8 && t_arlock[t]==0,"READ_ATTRIBUTES");
     @ARID@
     rbusy[t]=1;rc[t]=0;artotal=artotal+1;
    end
    if(t_wvalid[t] && t_wready[t]) begin
     check(t_wlast[t]==(wc[t]==beats_for(wowner[t])-1),"W_LAST");
     check(t_wstrb[t]===strobes(wowner[t],wc[t]),"W_STROBES");
     mask=0;
     for(k=0;k<@BYTES@;k=k+1) if((strobes(wowner[t],wc[t])>>k)&1) mask[8*k+:8]=255;
     check(((t_wdata[t]^data_word(wowner[t],wc[t],0))&mask)===0,"W_DATA");
     wc[t]=wc[t]+1;wtotal=wtotal+1;
    end
    if(t_bvalid[t] && t_bready[t]) wbusy[t]=0;
    if(t_rvalid[t] && t_rready[t]) begin
     rc[t]=rc[t]+1;rtotal=rtotal+1;
     if(rc[t]==beats_for(rowner[t])) rbusy[t]=0;
    end
   end
   if(cycle==300 && (mode==4 || mode==5))
    $display("BACKPRESSURE_OBSERVATION mode=%0d w=%0d expected_w=%0d b=%0d r=%0d",mode,wtotal,expected_w,bseen[0]+bseen[1],rseen[0]+rseen[1]);
   if(@MIXED@ && phase==0 && cycle==8) check(wsent[1]==1 && awsent[1]==0,"LITE_W_BEFORE_AW");
   if(@MIXED@ && phase==1 && cycle==8) check(awsent[1]==1 && wsent[1]==0,"LITE_AW_BEFORE_W");
   if(@MIXED@ && phase==2 && cycle==0) check(awsent[1]==1 && wsent[1]==1,"LITE_SAME_EDGE");
   cycle=cycle+1;check(cycle<20000,"TIMEOUT");
  end
  check(awtotal==expected_aw && artotal==expected_ar,"NO_FALSE_TARGET");
  check(wtotal==expected_w && rtotal==expected_r,"CONSERVATION");
  completed=completed+1;
  // Reset at phase boundaries also cancels stale driver assertions.
  reset;
 end
 if(@MIXED@) for(k=0;k<2;k=k+1) begin
  reset;
  @(negedge clk_main);
  i_awaddr[1]=target_base(0)+16384;i_wdata[1]=123;i_wstrb[1]=1;
  i_awvalid[1]=(k==0);i_wvalid[1]=(k==1);
  @(posedge clk_main);
  check(k==0 ? i_awready[1] : i_wready[1],"LITE_PARTIAL_CAPTURE");
  @(negedge clk_main);i_awvalid[1]=0;i_wvalid[1]=0;
  repeat(4) begin @(posedge clk_main);#1;
   check(k==0 ? !i_awready[1] : !i_wready[1],"LITE_PARTIAL_HELD");
   check(!t_awvalid[0] && !t_awvalid[1] && !t_wvalid[0] && !t_wvalid[1],"LITE_PARTIAL_NO_EFFECT");
  end
  reset;
  for(t=0;t<2;t=t+1) begin t_awready[t]=1;t_wready[t]=1;t_arready[t]=1;end
  repeat(15) begin @(posedge clk_main);#1;
   check(i_awready[1] && i_wready[1],"LITE_RESET_REARM");
   check(!i_bvalid[1] && !i_rvalid[1] && !t_awvalid[0] && !t_awvalid[1] &&
         !t_wvalid[0] && !t_wvalid[1],"LITE_RESET_NO_REPLAY");
  end
 end
 $display("AXI4_TARGET_PASS real_exchange cases=%0d aw=%0d ar=%0d w=%0d r=%0d",completed,awtotal,artotal,wtotal,rtotal);
 $finish;
end
initial begin #20000000;$display("ASSERT_FAIL:AXI4_MASTER.GLOBAL_TIMEOUT");$fatal(1);end
endmodule
'''
    ab = meta.get("address_bits", 32)
    bases = [t["base"] for t in meta["targets"]]
    heterogeneous = meta.get("heterogeneous", False)
    miss = 65536 if heterogeneous else 0 if ab > 32 else 32768 if ab < 32 else 0x80000000
    boundary = "if(phase==15) begin target_of[s]=1; start[s]=target_base(1)+(s==0 ? 65536-n*(1<<sz) : 32768);end" if ab > 32 and db == 64 and not heterogeneous else ""
    replacements = {"RUNW": str(run_word), "DECERR": str(decerr), "MIXED": "1" if mixed else "0", "PHASES": "20" if mixed else "16", "DESTINATION": "(s==0 && phase%2==0 ? 1 : 0)" if heterogeneous else "(phase%2==0 ? s : 0)", "ABM": str(max(ab, 32)-1), "AB": str(max(ab, 32)), "BASE0": f"{bases[0]:x}", "BASE1": f"{bases[1]:x}", "TOP_BOUNDARY": boundary, "MISS": f"{miss:08x}", "DECL": "\n".join(declarations), "CONNECT": ",\n".join(connections),
                    "MODULE": meta["module"], "INITIAL": "\n".join(initials),
                    "DBM": str(db-1), "BYTES": str(db//8), "BEM": str(db//8-1),
                    "FULLSIZE": "2" if db == 32 else "3", "MAXBEATS": "256" if lb == 8 else "16",
                    "SOURCEID": "i_awid[s]=s+1;i_arid[s]=s+5;" if ids else "",
                    "TARGETID": "t_bid[t]=target_id(t,wowner[t],0);t_rid[t]=target_id(t,rowner[t],1);" if ids else "",
                    "BID": 'check(i_bid[s]==(lite(s) ? 0 : s+1),"B_ID");' if ids else "",
                    "RID": 'check(i_rid[s]==(lite(s) ? 0 : s+5),"R_ID");' if ids else "",
                    "AWID": 'check(t_awid[t]==target_id(t,wowner[t],0),"AW_ID");' if ids else "",
                    "ARID": 'check(t_arid[t]==target_id(t,rowner[t],1),"AR_ID");' if ids else "",
                    "TARGETIDFN": id_function("target_id", meta["target_ids"], meta["ports"],
                                              lambda s, read: 0 if mixed and s == 1 else s + (5 if read else 1))
                    if ids else ""}
    for key, value in replacements.items():
        text = text.replace("@"+key+"@", value)
    (work / "tb.sv").write_text(text)
