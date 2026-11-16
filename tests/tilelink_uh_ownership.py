"""Target ownership, write ordering and completion under read backpressure."""
import argparse
from pathlib import Path

import sim_engine
from tilelink_uh_contexts import run, segments_body


def ownership_body(capacity, pressure):
    setup = r'''
  send_read(12288,1,1); send_read(16384,1,2);
  wait(messages==2); respond(0,0); respond(1,0);
  repeat(6) @(negedge nl_clk);
  check(nl_probe_tl_x_context0_x_tl_x_rfull && nl_probe_tl_x_context0_x_tl_x_dfull &&
        nl_probe_tl_x_context1_x_tl_x_rfull && nl_probe_tl_x_context1_x_tl_x_dfull,"READ_BUFFERS_FULL");
  prepare_write(4100,2,3);
  fork
    begin
      send_word(0,0); send_word(1,0);
      wait(words==2); repeat(8) @(negedge nl_clk);
      nl_tl_a_ready=0;
      repeat(8) @(negedge nl_clk);
      check(early_ack==1 && !nl_wrsp_valid,"EARLY_ACK_BEFORE_TAIL");
      nl_tl_a_ready=1; send_word(2,1);
    end
    begin
      wait(nl_tl_a_valid && nl_tl_a_address==4100); same_ack(1);
      wait(words==2); @(negedge nl_clk); nl_tl_a_ready=0;
      respond(3,1); early_ack=early_ack+1;
    end
  join
  wait(words==3); check(same_count==1,"A_COMPLETION_AFTER_ACK");
  nl_rrsp_ready=1; nl_wrsp_ready=1;
  wait(rwords==4 && bwords==1);
  check(denied_b==1,"DENIED_PACKET_AGGREGATE");
'''
    ticket = r'''
  prepare_write(4100,2,1);
  fork
    begin
      send_word(0,0); send_word(1,0); send_word(2,1);
      prepare_write(8192,0,2); send_word(3,1);
      send_read(12288,0,3);
    end
    begin
      wait(nl_tl_a_valid && nl_tl_a_address==4100); same_ack(0);
      wait(words==3); wait(messages==3);
      check(kinds[2]==4,"UNRELATED_GET_PROGRESS");
      respond(2,0); nl_rrsp_ready=1;
      wait(rwords==1);
      repeat(16) @(negedge nl_clk);
      check(words==3 && messages==3,"WRITE_TICKET_FINAL_ACK");
      respond(1,0); final_ack=1;
      wait(messages==4); respond(3,0);
    end
  join
  nl_wrsp_ready=1; wait(bwords==2);
  check(words==4 && same_count==1 && denied_b==0,"WRITE_TICKET_COMPLETE");
'''
    common = r'''
localparam K=CAPACITY;
integer cycle=0, messages=0, words=0, rwords=0, bwords=0;
integer sources[0:7], sizes[0:7], kinds[0:7], addresses[0:7];
integer put_left=0, put_address=0, early_ack=0, same_count=0, denied_b=0;
reg final_ack=0;
reg [3:0] b_seen=0;
integer read_counts[0:3];
always #5 nl_clk=~nl_clk;
task nlv_fail_report(input [255:0] rule_name, input [255:0] who, input [63:0] value);
  begin $display("ASSERT_FAIL:%0s %0s value=%0d cycle=%0d",rule_name,who,value,cycle); $fatal(1); end
endtask
task check(input ok, input [255:0] label);
  if(!ok) nlv_fail_report(label,"ownership",0);
endtask
always @(posedge nl_clk) if(nl_reset_n) begin
  cycle=cycle+1; check(cycle<20000,"PROGRESS");
  if(nl_tl_a_valid && nl_tl_a_ready) begin
    if(put_left==0) begin
      sources[messages]=nl_tl_a_source; sizes[messages]=nl_tl_a_size;
      kinds[messages]=nl_tl_a_opcode; addresses[messages]=nl_tl_a_address;
      messages=messages+1;
      if(nl_tl_a_address==4104) check(nl_tl_a_source==sources[PRESSURE ? 2 : 0],"SAME_PACKET_SOURCE");
      if(nl_tl_a_address==8192) check(nl_tl_a_source!=sources[0],"SOURCE_HELD_UNTIL_PACKET_RETIRE");
      if(nl_tl_a_opcode!=4) begin
        put_left=nl_tl_a_size<=2 ? 1 : (1<<nl_tl_a_size)/4;
        put_address=nl_tl_a_address;
        if(put_address==8192) check(final_ack,"WRITE_TICKET");
      end
    end
    if(nl_tl_a_opcode!=4) begin
      check(nl_tl_a_mask==15,"WRITE_MASK");
      check(put_address==(words<3 ? 4100+4*words : 8192),"WRITE_BYTE_ORDER");
      check(nl_tl_a_data==32'h33000000+words,"WRITE_BYTE_DATA");
      words=words+1; put_left=put_left-1; put_address=put_address+4;
    end
  end
  check(!(nl_rrsp_valid && nl_wrsp_valid),"ONE_RESPONSE_FACE");
  if(nl_rrsp_valid && nl_rrsp_ready) begin
    check(nl_rrsp_result==0 && nl_rrsp_data==32'hbb000000+nl_rrsp_address,"READ_BYTE_DATA");
    check(nl_rrsp_id>=1 && nl_rrsp_id<=3 && nl_rrsp_source==nl_rrsp_id && nl_rrsp_tag==nl_rrsp_id,"READ_IDENTITY");
    check(nl_rrsp_first==(read_counts[nl_rrsp_id]==0),"READ_FIRST");
    check(nl_rrsp_last==(read_counts[nl_rrsp_id]==(PRESSURE ? 1 : 0)),"READ_LAST");
    read_counts[nl_rrsp_id]=read_counts[nl_rrsp_id]+1;
    rwords=rwords+1;
  end
  if(nl_wrsp_valid && nl_wrsp_ready) begin
    check(nl_wrsp_first && nl_wrsp_last,"WRITE_RESPONSE_FRAME");
    check(nl_wrsp_result==(PRESSURE ? 2 : 0),"WRITE_DENIED_RESULT");
    check((PRESSURE ? nl_wrsp_id==3 : nl_wrsp_id==1 || nl_wrsp_id==2) &&
      nl_wrsp_source==nl_wrsp_id && nl_wrsp_tag==nl_wrsp_id,"WRITE_IDENTITY");
    check(!b_seen[nl_wrsp_id],"WRITE_RESPONSE_ONCE"); b_seen[nl_wrsp_id]=1;
    bwords=bwords+1;
    if(nl_wrsp_result==2) denied_b=denied_b+1;
  end
end
task send_read(input integer address, input integer length, input integer id);
  begin
    @(negedge nl_clk); nl_rreq_address=address; nl_rreq_start_address=address;
    nl_rreq_length=length; nl_rreq_id=id; nl_rreq_source=id; nl_rreq_tag=id;
    nl_rreq_size=2; nl_rreq_burst=1; nl_rreq_count=4; nl_rreq_be=15;
    nl_rreq_first=1; nl_rreq_last=1; nl_rreq_valid=1;
    @(posedge nl_clk); while(!nl_rreq_ready) @(posedge nl_clk);
    @(negedge nl_clk); nl_rreq_valid=0;
  end
endtask
task prepare_write(input integer address, input integer length, input integer id);
  begin
    nl_wreq_address=address; nl_wreq_start_address=address; nl_wreq_length=length;
    nl_wreq_id=id; nl_wreq_source=id; nl_wreq_tag=id;
    nl_wreq_size=2; nl_wreq_burst=1; nl_wreq_count=4; nl_wreq_be=15;
  end
endtask
task send_word(input integer index, input last);
  begin
    @(negedge nl_clk); nl_wreq_data=32'h33000000+index;
    nl_wreq_last=last; nl_wreq_first=index==0 || index==3; nl_wreq_valid=1;
    @(posedge nl_clk); while(!nl_wreq_ready) @(posedge nl_clk);
    @(negedge nl_clk); nl_wreq_valid=0;
  end
endtask
task respond(input integer index, input denied);
  integer n;
  begin
    n=kinds[index]==4 && sizes[index]>2 ? (1<<sizes[index])/4 : 1;
    for(integer k=0;k<n;k=k+1) begin
      @(negedge nl_clk); nl_tl_d_source=sources[index]; nl_tl_d_size=sizes[index];
      nl_tl_d_opcode=kinds[index]==4 ? 1 : 0;
      nl_tl_d_denied=denied; nl_tl_d_data=32'hbb000000+addresses[index]+4*k;
      nl_tl_d_valid=1;
      @(posedge nl_clk); while(!nl_tl_d_ready) @(posedge nl_clk);
      @(negedge nl_clk); nl_tl_d_valid=0;
    end
  end
endtask
task same_ack(input denied);
  begin
    @(negedge nl_clk); check(nl_tl_a_valid && nl_tl_a_address==4100,"FIRST_A_VISIBLE");
    nl_tl_d_source=nl_tl_a_source; nl_tl_d_size=nl_tl_a_size;
    nl_tl_d_opcode=0; nl_tl_d_denied=denied; nl_tl_d_valid=1;
    @(posedge nl_clk); check(nl_tl_a_ready && nl_tl_d_ready,"SAME_CYCLE_ACK");
    same_count=same_count+1;
    @(negedge nl_clk); nl_tl_d_valid=0;
  end
endtask
nlv_tl_checker #(.NAME("target"),.SOURCE_W(SOURCE_BITS),.SIZE_W(4),.UH(1),.SOURCE_LIMIT(K)) checker_t (
  .clk(nl_clk),.rst_n(nl_reset_n),.allow_reject(1'b0),
  .a_opcode(nl_tl_a_opcode),.a_param(nl_tl_a_param),.a_size(nl_tl_a_size),.a_source(nl_tl_a_source),
  .a_address(nl_tl_a_address),.a_mask(nl_tl_a_mask),.a_data(nl_tl_a_data),.a_corrupt(nl_tl_a_corrupt),
  .a_valid(nl_tl_a_valid),.a_ready(nl_tl_a_ready),
  .d_opcode(nl_tl_d_opcode),.d_param(nl_tl_d_param),.d_size(nl_tl_d_size),.d_source(nl_tl_d_source),
  .d_data(nl_tl_d_data),.d_corrupt(nl_tl_d_corrupt),.d_denied(nl_tl_d_denied),
  .d_valid(nl_tl_d_valid),.d_ready(nl_tl_d_ready));
initial begin
  for(integer k=0;k<4;k=k+1) read_counts[k]=0;
  repeat(160) @(negedge nl_clk); nl_reset_n=1; nl_tl_a_ready=1;
  SETUP
  repeat(8) @(negedge nl_clk);
  $display("UH_CONTEXTS_PASS capacity=%0d pressure=%0d writes=%0d responses=%0d",K,PRESSURE,words,rwords+bwords);
  $finish;
end
initial begin #250000; nlv_fail_report("PROGRESS","timeout",0); end
'''
    return common.replace('CAPACITY',str(capacity)).replace('SOURCE_BITS',str((capacity-1).bit_length())).replace('PRESSURE',str(int(pressure))).replace('SETUP',setup if pressure else ticket)


def d_owner_body(capacity, source_bits):
    return r'''
localparam K=CAPACITY;
integer cycle=0, messages=0, returned=0;
integer sources[0:1], addresses[0:1];
reg invalid_probe=0;
always #5 nl_clk=~nl_clk;
task nlv_fail_report(input [255:0] rule_name, input [255:0] who, input [63:0] value);
  begin $display("ASSERT_FAIL:%0s %0s value=%0d cycle=%0d",rule_name,who,value,cycle); $fatal(1); end
endtask
task check(input ok, input [255:0] label);
  if(!ok) nlv_fail_report(label,"d_owner",0);
endtask
always @(posedge nl_clk) if(nl_reset_n) begin
  cycle=cycle+1; check(cycle<20000,"PROGRESS");
  if(nl_tl_a_valid && nl_tl_a_ready) begin
    check(nl_tl_a_opcode==4 && nl_tl_a_size==3,"GET_SHAPE");
    sources[messages]=nl_tl_a_source; addresses[messages]=nl_tl_a_address;
    messages=messages+1;
  end
  if(nl_rrsp_valid && nl_rrsp_ready) begin
    check(nl_rrsp_data==32'hcc000000+nl_rrsp_address && nl_rrsp_result==0,"READ_DATA");
    returned=returned+1;
  end
end
task send_read(input integer index);
  begin
    @(negedge nl_clk); nl_rreq_address=4096+index*256;
    nl_rreq_start_address=nl_rreq_address; nl_rreq_length=1;
    nl_rreq_id=index; nl_rreq_source=index; nl_rreq_tag=index;
    nl_rreq_size=2; nl_rreq_burst=1; nl_rreq_count=4; nl_rreq_be=15;
    nl_rreq_first=1; nl_rreq_last=1; nl_rreq_valid=1;
    @(posedge nl_clk); while(!nl_rreq_ready) @(posedge nl_clk);
    @(negedge nl_clk); nl_rreq_valid=0;
  end
endtask
task respond_beat(input integer index, input integer beat);
  begin
    @(negedge nl_clk); nl_tl_d_source=sources[index]; nl_tl_d_size=3;
    nl_tl_d_opcode=1; nl_tl_d_data=32'hcc000000+addresses[index]+beat*4; nl_tl_d_valid=1;
    @(posedge nl_clk); while(!nl_tl_d_ready) @(posedge nl_clk);
    @(negedge nl_clk); nl_tl_d_valid=0;
  end
endtask
task reject_source(input integer source, input [255:0] rule_name);
  begin
    @(negedge nl_clk); invalid_probe=1;
    nl_tl_d_valid=1; nl_tl_d_source=source; nl_tl_d_size=3; nl_tl_d_opcode=1;
    repeat(4) begin @(posedge nl_clk); check(!nl_tl_d_ready,rule_name); end
    @(negedge nl_clk); nl_tl_d_valid=0; invalid_probe=0;
  end
endtask
nlv_tl_checker #(.NAME("target"),.SOURCE_W(SOURCE_BITS),.SIZE_W(4),.UH(1),.SOURCE_LIMIT(K)) checker_t (
  .clk(nl_clk),.rst_n(nl_reset_n),.allow_reject(1'b0),
  .a_opcode(nl_tl_a_opcode),.a_param(nl_tl_a_param),.a_size(nl_tl_a_size),.a_source(nl_tl_a_source),
  .a_address(nl_tl_a_address),.a_mask(nl_tl_a_mask),.a_data(nl_tl_a_data),.a_corrupt(nl_tl_a_corrupt),
  .a_valid(nl_tl_a_valid),.a_ready(nl_tl_a_ready),
  .d_opcode(nl_tl_d_opcode),.d_param(nl_tl_d_param),.d_size(nl_tl_d_size),.d_source(nl_tl_d_source),
  .d_data(nl_tl_d_data),.d_corrupt(nl_tl_d_corrupt),.d_denied(nl_tl_d_denied),
  .d_valid(nl_tl_d_valid && !invalid_probe),.d_ready(nl_tl_d_ready));
initial begin
  repeat(160) @(negedge nl_clk); nl_reset_n=1; nl_tl_a_ready=1;
  send_read(0); send_read(1); wait(messages==2);
  check(sources[0]!=sources[1],"DISTINCT_PACKET_SOURCES");
  reject_source(K,"UNUSED_SOURCE_REJECTED");
  respond_beat(0,0);
  reject_source(sources[1],"D_OWNER_LOCKED");
  respond_beat(0,1); respond_beat(1,0); respond_beat(1,1);
  repeat(8) @(negedge nl_clk); nl_rrsp_ready=1;
  wait(returned==4); repeat(8) @(negedge nl_clk);
  $display("UH_CONTEXTS_PASS d_owner capacity=%0d source_bits=%0d",K,SOURCE_BITS); $finish;
end
initial begin #250000; nlv_fail_report("PROGRESS","timeout",0); end
'''.replace('CAPACITY',str(capacity)).replace('SOURCE_BITS',str(source_bits))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--group", choices=("all", "ordering", "d-owner", "segments", "response"), default="all")
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    if args.group in ("all", "ordering"):
        for capacity in (3, 16):
            for pressure in (False, True):
                result = run(args, capacity, (capacity-1).bit_length(), body=ownership_body(capacity, pressure))
                if result:
                    return result
    if args.group in ("all", "d-owner"):
        for capacity, source_bits in ((3, 2), (16, 16)):
            result = run(args, capacity, source_bits, body=d_owner_body(capacity, source_bits))
            if result:
                return result
    if args.group in ("all", "segments"):
        for source, identity in ((0, 2), (1, 1)):
            result = run(args, 3, 2, body=segments_body(source, identity))
            if result:
                return result
    if args.group in ("all", "response"):
        return run(args, 3, 2, body=Path(__file__).with_name("tilelink_uh_response_owner.svh").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
