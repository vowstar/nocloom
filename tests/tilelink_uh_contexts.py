"""Packet capacity and source lifetime at the generated TileLink target."""
import argparse
from pathlib import Path
import subprocess
import tempfile

import sim_engine


def capacity_body(capacity, source_bits):
    drive_source = "nl_tl_d_source=sources[index];" if source_bits else ""
    implicit = "" if source_bits else "wire nl_tl_a_source=0, nl_tl_d_source=0;"
    return f"""
{implicit}
localparam K={capacity};
integer cycle=0, issued=0, returned=0;
integer sources[0:16], request_source[0:16], live[0:15], answered[0:16];
integer current, logical_id;
function [31:0] value_at(input integer index);
  value_at=32'ha5000000+index;
endfunction
always #5 nl_clk=~nl_clk;
task nlv_fail_report(input [255:0] rule_name, input [255:0] who, input [63:0] value);
  begin $display("ASSERT_FAIL:%0s %0s value=%0d cycle=%0d",rule_name,who,value,cycle); $fatal(1); end
endtask
task check(input ok, input [255:0] label);
  if(!ok) nlv_fail_report(label,"contexts",0);
endtask
always @(posedge nl_clk) if(nl_reset_n) begin
  cycle=cycle+1; check(cycle<20000,"PROGRESS");
  if(nl_tl_a_valid && nl_tl_a_ready) begin
    check(nl_tl_a_opcode==4 && nl_tl_a_size==2,"GET_SHAPE");
    check(nl_tl_a_source<K,"SOURCE_RANGE");
    current=(nl_tl_a_address-4096)/256;
    check(current>=0 && current<=K && nl_tl_a_address==4096+current*256,"READ_ADDRESS");
    check(!live[nl_tl_a_source],"SOURCE_PACKET_LIFETIME");
    live[nl_tl_a_source]=1;
    sources[issued]=nl_tl_a_source;
    request_source[current]=nl_tl_a_source;
    issued=issued+1;
  end
  if(nl_rrsp_valid && nl_rrsp_ready) begin
    logical_id=nl_rrsp_source*16+nl_rrsp_id;
    check(logical_id>=0 && logical_id<=K,"RESPONSE_ID");
    check(!answered[logical_id],"RESPONSE_ONCE");
    check(nl_rrsp_tag==logical_id%16,"RESPONSE_TAG");
    check(nl_rrsp_first && nl_rrsp_last && nl_rrsp_count==4,"RESPONSE_FRAMING");
    check(nl_rrsp_address==4096+logical_id*256,"RESPONSE_ADDRESS");
    check(nl_rrsp_result==0 && nl_rrsp_data==value_at(logical_id),"RESPONSE_DATA");
    live[request_source[logical_id]]=0;
    answered[logical_id]=1; returned=returned+1;
  end
  check(!nl_wrsp_valid,"NO_WRITE_RESPONSE");
end
task offer_read(input integer index);
  begin
    nl_rreq_address=4096+index*256; nl_rreq_start_address=nl_rreq_address;
    nl_rreq_id=index%16; nl_rreq_source=index/16; nl_rreq_tag=index%16;
    nl_rreq_valid=1;
  end
endtask
task send_read(input integer index);
  begin
    @(negedge nl_clk); offer_read(index);
    @(posedge nl_clk); while(!nl_rreq_ready) @(posedge nl_clk);
    @(negedge nl_clk); nl_rreq_valid=0;
  end
endtask
task respond(input integer index, input integer packet_index);
  begin
    @(negedge nl_clk); {drive_source}
    nl_tl_d_opcode=1; nl_tl_d_size=2;
    nl_tl_d_data=value_at(packet_index); nl_tl_d_valid=1;
    @(posedge nl_clk); while(!nl_tl_d_ready) @(posedge nl_clk);
    @(negedge nl_clk); nl_tl_d_valid=0;
  end
endtask
nlv_tl_checker #(.NAME("target"),.SOURCE_W({max(1, source_bits)}),.SIZE_W(4),.UH(1),.SOURCE_LIMIT(K)) checker_t (
  .clk(nl_clk),.rst_n(nl_reset_n),.allow_reject(1'b0),
  .a_opcode(nl_tl_a_opcode),.a_param(nl_tl_a_param),.a_size(nl_tl_a_size),.a_source(nl_tl_a_source),
  .a_address(nl_tl_a_address),.a_mask(nl_tl_a_mask),.a_data(nl_tl_a_data),.a_corrupt(nl_tl_a_corrupt),
  .a_valid(nl_tl_a_valid),.a_ready(nl_tl_a_ready),
  .d_opcode(nl_tl_d_opcode),.d_param(nl_tl_d_param),.d_size(nl_tl_d_size),.d_source(nl_tl_d_source),
  .d_data(nl_tl_d_data),.d_corrupt(nl_tl_d_corrupt),.d_denied(nl_tl_d_denied),
  .d_valid(nl_tl_d_valid),.d_ready(nl_tl_d_ready));
initial begin
  for(integer k=0;k<16;k=k+1) live[k]=0;
  for(integer k=0;k<17;k=k+1) begin answered[k]=0; request_source[k]=-1; end
  repeat(160) @(negedge nl_clk); nl_reset_n=1;
  nl_tl_a_ready=1; nl_rreq_size=2; nl_rreq_burst=1;
  nl_rreq_count=4; nl_rreq_be=15; nl_rreq_first=1; nl_rreq_last=1;
  for(integer k=0;k<K;k=k+1) send_read(k);
  wait(issued==K);
  for(integer k=K-1;k>=0;k=k-1) respond(k,k);
  repeat(8) @(negedge nl_clk);
  offer_read(K);
  repeat(8) begin @(posedge nl_clk); check(!nl_rreq_ready,"CAPACITY_HOLDS_THROUGH_RESPONSE"); end
  @(negedge nl_clk); nl_rrsp_ready=1;
  @(posedge nl_clk); while(!nl_rreq_ready) @(posedge nl_clk);
  @(negedge nl_clk); nl_rreq_valid=0;
  wait(issued==K+1); respond(K,K);
  wait(returned==K+1);
  repeat(8) @(negedge nl_clk);
  check(issued==K+1 && returned==K+1,"CAPACITY_REACHED");
  $display("UH_CONTEXTS_PASS capacity=%0d issued=%0d returned=%0d",K,issued,returned);
  $finish;
end
initial begin #250000; nlv_fail_report("PROGRESS","timeout",0); end
"""



def basic_body():
    return r"""
localparam K=3;
integer cycle=0, messages=0, words=0, responses=0;
integer sources[0:3], sizes[0:3], kinds[0:3];
integer put_left=0, put_address=0, p_tail=0, early_ack=0;
reg ack_two=0;
always #5 nl_clk=~nl_clk;
task nlv_fail_report(input [255:0] rule_name, input [255:0] who, input [63:0] value);
  begin $display("ASSERT_FAIL:%0s %0s value=%0d cycle=%0d",rule_name,who,value,cycle); $fatal(1); end
endtask
task check(input ok, input [255:0] label);
  if(!ok) nlv_fail_report(label,"basic",0);
endtask
always @(posedge nl_clk) if(nl_reset_n) begin
  cycle=cycle+1; check(cycle<20000,"PROGRESS");
  if(nl_tl_a_valid && nl_tl_a_ready) begin
    if(put_left==0) begin
      sources[messages]=nl_tl_a_source; sizes[messages]=nl_tl_a_size;
      kinds[messages]=nl_tl_a_opcode; messages=messages+1;
      if(nl_tl_a_opcode!=4) begin
        put_left=nl_tl_a_size<=2 ? 1 : (1<<nl_tl_a_size)/4;
        put_address=nl_tl_a_address;
        if(put_address==8192) check(p_tail && ack_two,"WRITE_TICKET");
      end
    end
    if(nl_tl_a_opcode!=4) begin
      check(nl_tl_a_mask==15,"WRITE_MASK");
      check(put_address==(words<3 ? 4100+4*words : 8192),"WRITE_ORDER");
      check(nl_tl_a_data==32'h11000000+words,"WRITE_DATA");
      words=words+1; put_left=put_left-1; put_address=put_address+4;
      if(words==3) p_tail=1;
    end
  end
  check(!(nl_rrsp_valid && nl_wrsp_valid),"ONE_RESPONSE_FACE");
  if(nl_rrsp_valid && nl_rrsp_ready) begin
    check(nl_rrsp_id==3 && nl_rrsp_source==1 && nl_rrsp_tag==3,"READ_IDENTITY");
    check(nl_rrsp_first && nl_rrsp_last && nl_rrsp_data==32'h8899aabb,"READ_DATA");
    responses=responses+1;
  end
  if(nl_wrsp_valid && nl_wrsp_ready) begin
    check(nl_wrsp_result==0 && (nl_wrsp_id==1 || nl_wrsp_id==2),"WRITE_RESPONSE");
    responses=responses+1;
  end
end
task send_word(input integer index, input last);
  begin
    @(negedge nl_clk);
    nl_wreq_data=32'h11000000+index; nl_wreq_last=last; nl_wreq_first=index==0 || index==3;
    nl_wreq_valid=1;
    @(posedge nl_clk); while(!nl_wreq_ready) @(posedge nl_clk);
    @(negedge nl_clk); nl_wreq_valid=0;
  end
endtask
task respond(input integer index);
  begin
    @(negedge nl_clk); nl_tl_d_source=sources[index]; nl_tl_d_size=sizes[index];
    nl_tl_d_opcode=kinds[index]==4 ? 1 : 0;
    nl_tl_d_data=kinds[index]==4 ? 32'h8899aabb : 0;
    nl_tl_d_valid=1;
    @(posedge nl_clk); while(!nl_tl_d_ready) @(posedge nl_clk);
    if(index==1) begin check(words<3,"EARLY_ACK"); early_ack=early_ack+1; ack_two=1; end
    @(negedge nl_clk); nl_tl_d_valid=0;
  end
endtask
nlv_tl_checker #(.NAME("target"),.SOURCE_W(2),.SIZE_W(4),.UH(1),.SOURCE_LIMIT(K)) checker_t (
  .clk(nl_clk),.rst_n(nl_reset_n),.allow_reject(1'b0),
  .a_opcode(nl_tl_a_opcode),.a_param(nl_tl_a_param),.a_size(nl_tl_a_size),.a_source(nl_tl_a_source),
  .a_address(nl_tl_a_address),.a_mask(nl_tl_a_mask),.a_data(nl_tl_a_data),.a_corrupt(nl_tl_a_corrupt),
  .a_valid(nl_tl_a_valid),.a_ready(nl_tl_a_ready),
  .d_opcode(nl_tl_d_opcode),.d_param(nl_tl_d_param),.d_size(nl_tl_d_size),.d_source(nl_tl_d_source),
  .d_data(nl_tl_d_data),.d_corrupt(nl_tl_d_corrupt),.d_denied(nl_tl_d_denied),
  .d_valid(nl_tl_d_valid),.d_ready(nl_tl_d_ready));
initial begin
  repeat(160) @(negedge nl_clk); nl_reset_n=1; nl_tl_a_ready=1;
  nl_wreq_address=4100; nl_wreq_start_address=4100; nl_wreq_length=2;
  nl_wreq_size=2; nl_wreq_burst=1; nl_wreq_count=4; nl_wreq_be=15;
  nl_wreq_id=1; nl_wreq_tag=1;
  fork
    begin
      send_word(0,0); send_word(1,0);
      wait(messages==2); repeat(20) @(negedge nl_clk);
      send_word(2,1);
      nl_wreq_address=8192; nl_wreq_start_address=8192; nl_wreq_length=0;
      nl_wreq_id=2; nl_wreq_tag=2; send_word(3,1);
    end
    begin
      wait(messages==1); respond(0);
      wait(messages==2); respond(1);
      @(negedge nl_clk);
      nl_rreq_address=12288; nl_rreq_start_address=12288; nl_rreq_size=2; nl_rreq_burst=1;
      nl_rreq_count=4; nl_rreq_be=15; nl_rreq_first=1; nl_rreq_last=1;
      nl_rreq_id=3; nl_rreq_source=1; nl_rreq_tag=3; nl_rreq_valid=1;
      @(posedge nl_clk); while(!nl_rreq_ready) @(posedge nl_clk);
      @(negedge nl_clk); nl_rreq_valid=0;
      wait(messages>=3); check(kinds[2]==4,"GET_AFTER_PUT_TAIL"); respond(2);
      repeat(8) @(negedge nl_clk);
      wait(messages==4); respond(3);
    end
  join
  repeat(8) @(negedge nl_clk); nl_rrsp_ready=1; nl_wrsp_ready=1;
  wait(responses==3); repeat(8) @(negedge nl_clk);
  check(words==4 && early_ack==1 && messages==4,"WRITE_COMPLETE");
  $display("UH_CONTEXTS_PASS writes=%0d responses=%0d",words,responses); $finish;
end
initial begin #250000; nlv_fail_report("PROGRESS","timeout",0); end
"""


def segments_body():
    return r"""
localparam K=3;
integer cycle=0, messages=0, returned=0;
integer sources[0:3], sizes[0:3], addresses[0:3];
integer seen[0:95], counts[0:2], rcounts[0:2];
integer owner, offset, width;
reg drain=0;
always #5 nl_clk=~nl_clk;
always @(negedge nl_clk) nl_rrsp_ready=drain && cycle%5!=1;
function [31:0] value_at(input integer address);
  value_at=32'h5a000000+address;
endfunction
task nlv_fail_report(input [255:0] rule_name, input [255:0] who, input [63:0] value);
  begin $display("ASSERT_FAIL:%0s %0s value=%0d cycle=%0d",rule_name,who,value,cycle); $fatal(1); end
endtask
task check(input ok, input [255:0] label);
  if(!ok) nlv_fail_report(label,"segments",0);
endtask
always @(posedge nl_clk) if(nl_reset_n) begin
  cycle=cycle+1; check(cycle<20000,"PROGRESS");
  if(nl_tl_a_valid && nl_tl_a_ready) begin
    check(nl_tl_a_opcode==4,"GET_ONLY");
    sources[messages]=nl_tl_a_source; sizes[messages]=nl_tl_a_size;
    addresses[messages]=nl_tl_a_address; messages=messages+1;
    owner=nl_tl_a_address>=16384 ? 2 : nl_tl_a_address>=8192 ? 1 : 0;
    offset=nl_tl_a_address-(owner==0 ? 4100 : owner==1 ? 8192 : 16384);
    width=1<<nl_tl_a_size;
    check(offset>=0 && offset+width<=(owner==0 ? 12 : owner==1 ? 32 : 4),"EXACT_GET_RANGE");
    if(owner==2) check(rcounts[0]==3,"SAME_KEY_SIDE_EFFECT_ORDER");
    for(integer b=0;b<width;b=b+1) begin
      check(seen[owner*32+offset+b]==0,"READ_SIDE_EFFECT_ONCE");
      seen[owner*32+offset+b]=1; counts[owner]=counts[owner]+1;
    end
  end
  if(nl_rrsp_valid && nl_rrsp_ready) begin
    owner=nl_rrsp_tag==1 ? 0 : nl_rrsp_tag==2 ? 1 : 2;
    check(nl_rrsp_source==(owner==1 ? 1 : 0),"SOURCE_IDENTITY");
    check(nl_rrsp_id==(owner==1 ? 2 : 1),"AXI_IDENTITY");
    check(nl_rrsp_data==value_at(nl_rrsp_address) && nl_rrsp_result==0,"READ_DATA");
    check(nl_rrsp_first==(rcounts[owner]==0),"PACKET_FIRST");
    check(nl_rrsp_last==(rcounts[owner]==(owner==0 ? 2 : owner==1 ? 7 : 0)),"PACKET_LAST");
    check(nl_probe_cut==(owner==0 && rcounts[0]==0),"MESSAGE_CUT");
    if(owner==0 && rcounts[0]>0) check(rcounts[1]==8,"Q_DRAIN_BEFORE_P2");
    rcounts[owner]=rcounts[owner]+1; returned=returned+1;
  end
end
task send_read(input integer address, input integer length, input integer id, input integer source, input integer tag);
  begin
    @(negedge nl_clk); nl_rreq_address=address; nl_rreq_start_address=address;
    nl_rreq_length=length; nl_rreq_id=id; nl_rreq_source=source; nl_rreq_tag=tag;
    nl_rreq_size=2; nl_rreq_burst=1; nl_rreq_count=4; nl_rreq_be=15;
    nl_rreq_first=1; nl_rreq_last=1; nl_rreq_valid=1;
    @(posedge nl_clk); while(!nl_rreq_ready) @(posedge nl_clk);
    @(negedge nl_clk); nl_rreq_valid=0;
  end
endtask
task respond(input integer index);
  integer n;
  begin
    n=sizes[index]<=2 ? 1 : (1<<sizes[index])/4;
    for(integer k=0;k<n;k=k+1) begin
      @(negedge nl_clk); nl_tl_d_source=sources[index]; nl_tl_d_size=sizes[index];
      nl_tl_d_opcode=1; nl_tl_d_data=value_at(addresses[index]+4*k); nl_tl_d_valid=1;
      @(posedge nl_clk); while(!nl_tl_d_ready) @(posedge nl_clk);
      @(negedge nl_clk); nl_tl_d_valid=0;
    end
  end
endtask
nlv_tl_checker #(.NAME("target"),.SOURCE_W(2),.SIZE_W(4),.UH(1),.SOURCE_LIMIT(K)) checker_t (
  .clk(nl_clk),.rst_n(nl_reset_n),.allow_reject(1'b0),
  .a_opcode(nl_tl_a_opcode),.a_param(nl_tl_a_param),.a_size(nl_tl_a_size),.a_source(nl_tl_a_source),
  .a_address(nl_tl_a_address),.a_mask(nl_tl_a_mask),.a_data(nl_tl_a_data),.a_corrupt(nl_tl_a_corrupt),
  .a_valid(nl_tl_a_valid),.a_ready(nl_tl_a_ready),
  .d_opcode(nl_tl_d_opcode),.d_param(nl_tl_d_param),.d_size(nl_tl_d_size),.d_source(nl_tl_d_source),
  .d_data(nl_tl_d_data),.d_corrupt(nl_tl_d_corrupt),.d_denied(nl_tl_d_denied),
  .d_valid(nl_tl_d_valid),.d_ready(nl_tl_d_ready));
initial begin
  for(integer k=0;k<96;k=k+1) seen[k]=0;
  for(integer k=0;k<3;k=k+1) begin counts[k]=0; rcounts[k]=0; end
  repeat(160) @(negedge nl_clk); nl_reset_n=1; nl_tl_a_ready=1;
  send_read(4100,2,1,0,1); send_read(8192,7,2,1,2); send_read(16384,0,1,0,3);
  wait(messages==2); respond(0);
  repeat(8) @(negedge nl_clk); check(messages==2,"P2_WAIT_BUFFER"); drain=1;
  wait(messages==3); check(addresses[2]==4104,"P2_EXACT_START");
  respond(1); check(rcounts[1]>=6,"Q_PROGRESS");
  respond(2); wait(messages==4); check(addresses[3]==16384,"SAME_KEY_RELEASE");
  respond(3); wait(returned==12); repeat(8) @(negedge nl_clk);
  check(counts[0]==12 && counts[1]==32 && counts[2]==4,"BYTE_ORACLE");
  $display("UH_CONTEXTS_PASS segments=%0d responses=%0d",messages,returned); $finish;
end
initial begin #250000; nlv_fail_report("PROGRESS","timeout",0); end
"""

def run(args, capacity, source_bits, basic=False, segments=False):
    work = Path(tempfile.mkdtemp(prefix="contexts-", dir=args.work)).resolve()
    subprocess.run([str(args.fixture.resolve()), str(work), "contexts", "64",
                    str(capacity), str(source_bits)], check=True, timeout=60)
    ports = [line.split() for line in (work / "ports.tsv").read_text().splitlines()]
    declarations, connections = [], []
    for name, width, incoming in ports:
        declarations.append(f"{'reg' if int(incoming) else 'wire'} [{int(width)-1}:0] {name}"
                            + ("=0;" if int(incoming) else ";"))
        connections.append(f".{name}({name})")
    body = segments_body() if segments else basic_body() if basic else capacity_body(capacity, source_bits)
    (work / "tb.sv").write_text("`timescale 1ns/1ps\nmodule tb;\n" + "\n".join(declarations)
                               + "\nnocloom_tl_uh_outlet_probe dut(" + ",".join(connections)
                               + ");\n" + body + "\nendmodule\n")
    checker = Path(__file__).parent / "verif/sv/nlv_tl_checker.sv"
    with (work / "network.v").open("a") as output:
        output.write(checker.read_text())
    result = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                   compile_timeout=180, run_timeout=60,
                                   success=("UH_CONTEXTS_PASS",)).run()
    print(result.stdout, work, flush=True)
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--capacity", type=int)
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    capacities = [args.capacity] if args.capacity else range(1, 17)
    for capacity in capacities:
        result = run(args, capacity, (capacity-1).bit_length())
        if result:
            return result
    result = run(args, 3, 16)
    result = result or run(args, 3, 2, basic=True)
    return result or run(args, 3, 2, segments=True)


if __name__ == "__main__":
    raise SystemExit(main())
