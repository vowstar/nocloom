"""Connectivity oracle over three sources and three targets.

The external protocol oracle (graph_bench, the three-source extension of
axi4_master_exchange_bench) drives every source to every
target in the first three phases and to rotating and shared targets after
them. A connectivity matrix (allowed {(source, target)} pairs, source s0 must
reach t0 for the reset phase) decides the expectation: a connected pair is a
normal access, any other pair answers DECERR (from the source's default
target, without a target handshake). A target that sees
a handshake of a pair outside the matrix fails FORBIDDEN_WRITE/READ, a denied
burst that does not complete fails DENIED_*_COMPLETION, and the allowed and
denied pairs exercised in the first three phases must cover the whole matrix
and its complement. The addresses, responses and completion masks are
external observations only."""
import re

from axi4_master_exchange_bench import write_bench


def graph_bench(work, decerr=3):
    """Reuse the external protocol oracle with explicitly indexed third endpoints."""
    write_bench(work, decerr)
    p = work / "tb.sv"
    text = p.read_text()
    text = text.replace("[0:1]", "[0:2]")
    text = re.sub(r"(\w+)\[0\]=0; \1\[1\]=0;", r"\1[0]=0; \1[1]=0; \1[2]=0;", text)
    for variable in ("s", "t"):
        text = text.replace(f"{variable}<2;", f"{variable}<3;")
    for name in ("bseen", "rseen"):
        text = text.replace(f"{name}[0]+{name}[1]", f"{name}[0]+{name}[1]+{name}[2]")
    text = text.replace("beats_for(0)+beats_for(1)", "beats_for(0)+beats_for(1)+beats_for(2)")
    text = text.replace("bseen[0]+bseen[1]+bseen[2]!=2", "bseen[0]+bseen[1]+bseen[2]!=3")
    text = text.replace("bseen[0]+bseen[1]+bseen[2]==2", "bseen[0]+bseen[1]+bseen[2]==3")
    text = text.replace("target_of[s]=(phase%2==0 ? s : 0);",
                        "target_of[s]=(phase<9 ? (s+phase)%3 : 0);")
    text = text.replace("(wsent[s]<beats_for(s))",
                        "(wsent[s]<beats_for(s)) && !(phase==15 && s==0 && wsent[s]>=8 && cycle<200)")
    old = "target_base=(target==0 ? 32'h0 : 32'h10000);"
    assert text.count(old) == 1
    text = text.replace(old, "target_base=target*65536;")
    for direction, owner in (("aw", "wowner"), ("ar", "rowner")):
        old = f"{owner}[t]=(target_of[0]!=t ? 1 : (target_of[1]!=t ? 0 : (t_{direction}addr[t]===target_address(1) ? 1 : 0)));"
        assert text.count(old) == 1
        # The stimulus assigns distinct 16 KiB regions to the three sources.
        # This oracle uses external addresses, never a DUT identity binding.
        text = text.replace(old, f'{owner}[t]=t_{direction}addr[t]/16384;\n     check({owner}[t]<3,"SOURCE_REGION");')
    assert "[0:1]" not in text and "s<2;" not in text and "t<2;" not in text
    marker = ' $display("AXI4_TARGET_PASS real_exchange'
    assert text.count(marker) == 1
    cancellation = r''' reset;
 @(negedge clk_main);
 i_awaddr[0]=256;i_awlen[0]=15;i_awsize[0]=2;i_awburst[0]=1;i_awid[0]=1;
 t_awready[0]=1;t_wready[0]=1;
 awsent[0]=0;wsent[0]=0;cycle=0;k=0;
 while(k==0) begin
  @(negedge clk_main);
  i_awvalid[0]=!awsent[0];i_wvalid[0]=wsent[0]<16;
  i_wdata[0]=wsent[0]+1;i_wstrb[0]=15;i_wlast[0]=wsent[0]==15;
  @(posedge clk_main);
  if(i_awvalid[0] && i_awready[0]) awsent[0]=1;
  if(i_wvalid[0] && i_wready[0]) wsent[0]=wsent[0]+1;
  if(t_wvalid[0] && t_wready[0]) begin
   check(!t_wlast[0],"RESET_REACHED_MID_PACKET");k=1;
  end
  cycle=cycle+1;check(cycle<1000,"RESET_ARRIVAL");
 end
 reset;
 for(t=0;t<3;t=t+1) begin t_awready[t]=1;t_wready[t]=1;t_arready[t]=1;end
 for(s=0;s<3;s=s+1) begin i_bready[s]=1;i_rready[s]=1;end
 repeat(40) begin
  @(posedge clk_main);#1;
  for(t=0;t<3;t=t+1)
   check(!t_awvalid[t] && !t_wvalid[t] && !t_arvalid[t],"RESET_NO_REPLAY");
  for(s=0;s<3;s=s+1)
   check(i_awready[s] && i_arready[s] && !i_bvalid[s] && !i_rvalid[s],"RESET_REARM");
 end
 $display("GRAPH_OBSERVATION routes=9 reset_mid_packet=1");
'''
    text = text.replace(marker, cancellation + marker)
    p.write_text(text)

# The band matrix of examples/axi4_graph_selective.yaml.
BAND = {(s, t) for s in range(3) for t in range(3) if t in (s, (s + 1) % 3)}


def matrix_bench(work, allowed, decerr=3):
    allowed = set(allowed)
    assert (0, 0) in allowed and allowed <= {(s, t) for s in range(3) for t in range(3)}
    mask = sum(1 << (3 * s + t) for s, t in allowed)
    graph_bench(work, decerr)
    path = work / 'tb.sv'
    text = path.read_text()

    def replace(old, new):
        nonlocal text
        assert text.count(old) == 1, old
        text = text.replace(old, new)

    replace('integer expected_aw,expected_ar,expected_w,expected_r;', f'''integer expected_aw,expected_ar,expected_w,expected_r;
reg [8:0] allowed_w=0,denied_w=0,allowed_r=0,denied_r=0;
localparam [8:0] CONNECTED=9'h{mask:03x};
function integer permitted;
 input integer source,target;
 begin permitted=CONNECTED[3*source+target];end
endfunction''')
    # An exclusive burst without exclusive support is a normal access, so an
    # unconnected pair answers DECERR in every mode.
    replace('begin mode_for=mode==3 ? 0 : mode;end', '''begin mode_for=mode==3 ? 0 : mode;
  if(!permitted(owner,target_of[owner])) mode_for=2;end''')
    replace('bseen[s]=1;', '''bseen[s]=1;
     if(phase<3) begin
      if(permitted(s,target_of[s])) allowed_w[3*s+target_of[s]]=1;
      else denied_w[3*s+target_of[s]]=1;
     end''')
    replace('rseen[s]=rseen[s]+1;', '''rseen[s]=rseen[s]+1;
     if(phase<3 && rseen[s]==beats_for(s)) begin
      if(permitted(s,target_of[s])) allowed_r[3*s+target_of[s]]=1;
      else denied_r[3*s+target_of[s]]=1;
     end''')
    replace('check(t==target_of[wowner[t]],"WRITE_ROUTE");', '''check(t==target_of[wowner[t]],"WRITE_ROUTE");
     check(permitted(wowner[t],t),"FORBIDDEN_WRITE");''')
    replace('check(t==target_of[rowner[t]],"READ_ROUTE");', '''check(t==target_of[rowner[t]],"READ_ROUTE");
     check(permitted(rowner[t],t),"FORBIDDEN_READ");''')
    replace('cycle=cycle+1;check(cycle<20000,"TIMEOUT");', '''if(phase<3 && cycle==300) for(s=0;s<3;s=s+1) if(mode_for(s)==2) begin
    check(bseen[s]==1,"DENIED_WRITE_COMPLETION");
    check(rseen[s]==beats_for(s),"DENIED_READ_COMPLETION");
   end
   cycle=cycle+1;check(cycle<20000,"TIMEOUT");''')
    replace('$display("GRAPH_OBSERVATION routes=9 reset_mid_packet=1");', f'''check(allowed_w==CONNECTED && allowed_r==CONNECTED,"ALLOWED_COVERAGE");
 check(denied_w==~CONNECTED && denied_r==~CONNECTED,"DENIED_COVERAGE");
 $display("SELECTIVE_OBSERVATION admitted_pairs={len(allowed)} denied_pairs={9 - len(allowed)} allowed_w=%h allowed_r=%h denied_w=%h denied_r=%h",allowed_w,allowed_r,denied_w,denied_r);''')
    path.write_text(text)


def selective_bench(work, decerr=3):
    matrix_bench(work, BAND, decerr)
