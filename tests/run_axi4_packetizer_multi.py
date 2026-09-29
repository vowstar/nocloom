"""Directed multi-burst packetizer checks, including context admission and tags.

The standalone window has a sixteen-byte packet limit. This exercises several
packets of one burst independently of public target-derived packet limits.
Public network integration is covered by the mo_bounded/mo_bsplit stress modes.
"""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from cross_hardware_sim import simulate

BENCH = r'''
`timescale 1ns/1ps
module tb;
@DECL@
nocloom_axi4_packetizer_probe dut(@CONN@);
initial nl_clk=0;
always #5 nl_clk=~nl_clk;
integer cycle=0, wp=0, rp=0, wb=0, rb=0, ra=0, wa=0;
integer wt[0:255], wl[0:255], wz[0:255], wf[0:255], wlast[0:255];
integer rt[0:255], rl[0:255], rz[0:255];
integer wn=0, rn=0;
task check; input ok; input [8*48-1:0] name; begin
 if(ok !== 1'b1) begin
  $display("ASSERT_FAIL:PACKETIZER_MULTI.%0s cycle=%0d wp=%0d rp=%0d",name,cycle,wp,rp); $fatal(1);
 end
end endtask
always @(posedge nl_clk) begin
 cycle=cycle+1;
 if(cycle>10000) begin $display("ASSERT_FAIL:PACKETIZER_MULTI.TIMEOUT"); $fatal(1); end
 if(nl_reset_n) begin
  if(nl_down_wreq_valid && nl_down_wreq_ready) begin
   wt[wn]=nl_down_wreq_tag; wl[wn]=nl_down_wreq_length; wz[wn]=nl_down_wreq_size;
   wf[wn]=nl_down_wreq_first; wlast[wn]=nl_down_wreq_last;
   check(nl_down_wreq_id==(nl_down_wreq_tag%4),"WRITE_ID_TAG");
   wn=wn+1;
   if(nl_down_wreq_last) wp=wp+1;
  end
  if(nl_down_rreq_valid && nl_down_rreq_ready) begin
   rt[rn]=nl_down_rreq_tag; rl[rn]=nl_down_rreq_length; rz[rn]=nl_down_rreq_size;
   check(nl_down_rreq_id==(nl_down_rreq_tag%4),"READ_ID_TAG");
   rn=rn+1; rp=rp+1;
  end
  if(nl_up_wrsp_valid && nl_up_wrsp_ready) wb=wb+1;
  if(nl_up_rrsp_valid && nl_up_rrsp_ready) rb=rb+1;
  if(nl_up_rreq_valid && nl_up_rreq_ready) ra=ra+1;
  if(nl_up_wreq_valid && nl_up_wreq_ready && nl_up_wreq_first) wa=wa+1;
 end
end
// Ready modulation is independent of request valid and exercises held fields.
always @(negedge nl_clk) begin
 nl_down_wreq_ready=(cycle%4)!=1;
 nl_down_rreq_ready=(cycle%3)!=1;
end
task pause_cycles; input integer n; begin repeat(n) @(negedge nl_clk); end endtask
task wait_wp; input integer n; begin while(wp<n) @(negedge nl_clk); end endtask
task wait_rp; input integer n; begin while(rp<n) @(negedge nl_clk); end endtask
task write_burst;
 input integer tag,n,size,kind,locked;
 integer j;
 begin
  for(j=0;j<n;j=j+1) begin
   @(negedge nl_clk);
   nl_up_wreq_valid=1; nl_up_wreq_write=1; nl_up_wreq_tag=tag; nl_up_wreq_id=tag%4;
   nl_up_wreq_address=256+((kind==0)?0:(j*(1<<size)));
   nl_up_wreq_start_address=256; nl_up_wreq_length=n-1;
   nl_up_wreq_size=size; nl_up_wreq_count=1<<size; nl_up_wreq_burst=kind;
   nl_up_wreq_cache=0; nl_hint_write_lock=locked;
   nl_up_wreq_first=(j==0); nl_up_wreq_last=(j==n-1);
   nl_up_wreq_data=32'h12345678; nl_up_wreq_be=15;
   @(posedge nl_clk); while(!nl_up_wreq_ready) @(posedge nl_clk);
  end
  @(negedge nl_clk); nl_up_wreq_valid=0;
 end
endtask
task b_response;
 input integer tag,code,final_b,merged;
 begin
  @(negedge nl_clk);
  nl_down_wrsp_valid=1; nl_down_wrsp_tag=tag; nl_down_wrsp_id=tag%4;
  nl_down_wrsp_result=code; nl_down_wrsp_first=1; nl_down_wrsp_last=1;
  nl_up_wrsp_ready=!final_b; // an intermediate B waits for the up ready only
  if(final_b) repeat(3) begin @(posedge nl_clk);
   check(!nl_down_wrsp_ready,"B_BACKPRESSURE");
  end
  if(final_b) begin @(negedge nl_clk); nl_up_wrsp_ready=1; end
  @(posedge nl_clk);
  while(!nl_down_wrsp_ready) @(posedge nl_clk);
  check(nl_up_wrsp_valid==final_b,"ONE_B_PER_BURST");
  if(final_b) begin
   check(nl_up_wrsp_tag==tag && nl_up_wrsp_id==tag%4,"B_TAG");
   check(nl_up_wrsp_result==merged,"B_MERGE");
  end
  @(negedge nl_clk); nl_down_wrsp_valid=0;
 end
endtask
task read_burst;
 input integer tag,n,size,kind,locked;
 begin
  @(negedge nl_clk); nl_up_rreq_valid=1;
  nl_up_rreq_tag=tag; nl_up_rreq_id=tag%4; nl_up_rreq_length=n-1;
  nl_up_rreq_address=256; nl_up_rreq_start_address=256;
  nl_up_rreq_size=size; nl_up_rreq_count=1<<size; nl_up_rreq_burst=kind;
  nl_up_rreq_cache=0; nl_hint_read_lock=locked;
  nl_up_rreq_first=1; nl_up_rreq_last=1;
  @(posedge nl_clk); while(!nl_up_rreq_ready) @(posedge nl_clk);
  @(negedge nl_clk); nl_up_rreq_valid=0;
 end
endtask
// Words from..to-1 of a packed read: each answers per source beats, the
// first from the down face and the rest from the replay register.
task r_packed;
 input integer tag,from,to,words,per;
 integer j,m;
 begin
  for(j=from;j<to;j=j+1) begin
   @(negedge nl_clk); nl_down_rrsp_valid=1;
   nl_down_rrsp_tag=tag; nl_down_rrsp_id=tag%4; nl_down_rrsp_result=0;
   nl_down_rrsp_data=tag*256+j; nl_down_rrsp_first=(j==0); nl_down_rrsp_last=(j==words-1);
   nl_up_rrsp_ready=1;
   for(m=0;m<per;m=m+1) begin
    @(posedge nl_clk);
    check(nl_up_rrsp_valid && nl_up_rrsp_tag==tag && nl_up_rrsp_data==tag*256+j,"R_PACKED");
    check(nl_up_rrsp_first==(j*per+m==0) && nl_up_rrsp_last==(j*per+m==words*per-1),
          "R_PACKED_BOUNDARY");
    check(nl_down_rrsp_ready==(m==0),"R_PACKED_REGISTER");
   end
   @(negedge nl_clk); nl_down_rrsp_valid=0;
  end
 end
endtask
// A folded WRAP word answering four beats.
task r_fold;
 input integer tag;
 integer m;
 begin
  @(negedge nl_clk); nl_down_rrsp_valid=1; nl_down_rrsp_tag=tag; nl_down_rrsp_id=tag%4;
  nl_down_rrsp_data=32'habcdef01; nl_down_rrsp_first=1; nl_down_rrsp_last=1;
  nl_up_rrsp_ready=1;
  for(m=0;m<4;m=m+1) begin
   @(posedge nl_clk); check(nl_up_rrsp_valid && nl_up_rrsp_tag==tag,"FOLD_R_TAG");
   check(nl_up_rrsp_first==(m==0) && nl_up_rrsp_last==(m==3),"FOLD_R_BOUNDARY");
   check(nl_down_rrsp_ready==(m==0),"FOLD_R_REGISTER");
  end
  @(negedge nl_clk); nl_down_rrsp_valid=0;
 end
endtask
task r_response;
 input integer tag,n,offset,total;
 integer j;
 begin
  for(j=0;j<n;j=j+1) begin
   @(negedge nl_clk); nl_down_rrsp_valid=1;
   nl_down_rrsp_tag=tag; nl_down_rrsp_id=tag%4; nl_down_rrsp_result=0;
   nl_down_rrsp_data=tag*256+offset+j;
   nl_down_rrsp_first=(j==0); nl_down_rrsp_last=(j==n-1);
   nl_up_rrsp_ready=0;
   repeat(2) begin @(posedge nl_clk); check(!nl_down_rrsp_ready,"R_BACKPRESSURE"); end
   @(negedge nl_clk); nl_up_rrsp_ready=1;
   @(posedge nl_clk); while(!nl_down_rrsp_ready) @(posedge nl_clk);
   check(nl_up_rrsp_valid && nl_up_rrsp_tag==tag && nl_up_rrsp_id==tag%4,"R_TAG");
   check(nl_up_rrsp_data==tag*256+offset+j,"R_DATA");
   check(nl_up_rrsp_first==(offset+j==0) && nl_up_rrsp_last==(offset+j==total-1),"R_BOUNDARY");
  end
  @(negedge nl_clk); nl_down_rrsp_valid=0;
 end
endtask
integer k,mark,done_before;
initial begin
@INIT@
 nl_reset_n=0; pause_cycles(3); nl_reset_n=1;
 // Four passthrough writes genuinely leave before any B. A split write needs
 // only a reassembly context: it does not drain them, and a later write is
 // not held out; a B of another tag passes between its B, which merge.
 for(k=0;k<4;k=k+1) write_burst(k,1,2,1,0);
 wait_wp(4); check(wb==0,"WRITE_FOUR_IN_FLIGHT");
 fork
  write_burst(4,12,2,1,0);
  begin
   pause_cycles(5); check(wn>4 && wb==0,"SPLIT_W_NO_DRAIN");
   b_response(3,0,1,0); b_response(2,2,1,2); b_response(1,0,1,0); b_response(0,0,1,0);
  end
 join
 wait_wp(7); check(wb==4,"SPLIT_W_PIPELINE");
 fork
  write_burst(5,1,2,1,0);
  begin
   pause_cycles(5); check(wp==8 && wb==4,"SPLIT_W_NO_EXCLUDE");
   b_response(4,2,0,0); b_response(5,0,1,0); b_response(4,0,0,0); b_response(4,0,1,2);
  end
 join
 // A second split write takes its own context at once; their B interleave,
 // each merged on its own context.
 write_burst(20,8,2,1,0); write_burst(21,8,2,1,0);
 wait_wp(12); check(wb==6,"SPLIT_W_TWO_CONTEXTS");
 b_response(20,0,0,0); b_response(21,2,0,0); b_response(20,0,1,0); b_response(21,0,1,2);
 // Narrow splitting and the original AxLOCK hint differ even with no Weft
 // exclusive field. Folded WRAP takes a context despite producing one packet.
 mark=wn; write_burst(6,4,1,1,0); wait_wp(16);
 for(k=mark;k<wn;k=k+1) check(wl[k]==0 && wt[k]==6,"NARROW_SPLIT");
 for(k=0;k<4;k=k+1) b_response(6,0,k==3,0);
 mark=wn; write_burst(7,4,1,1,1); wait_wp(17);
 // A locked narrow INCR is not split but packed: four halfwords, two words.
 check(wn-mark==2,"SOURCE_LOCK_PACKED");
 for(k=mark;k<wn;k=k+1) check(wl[k]==1 && wt[k]==7,"SOURCE_LOCK_WRITE");
 b_response(7,0,1,0);
 write_burst(8,1,2,1,0); wait_wp(18);
 fork
  write_burst(9,4,0,2,0);
  begin pause_cycles(5); check(wp==19,"FOLD_W_NO_DRAIN"); b_response(8,0,1,0); end
 join
 check(wl[wn-1]==0 && wz[wn-1]==2,"FOLD_W_REQUEST"); b_response(9,0,1,0);
 // Four folded writes hold every context; a fifth is not accepted until one
 // frees, and the others return in any order.
 for(k=12;k<16;k=k+1) write_burst(k,4,0,2,0);
 wait_wp(23); mark=wa;
 fork
  write_burst(16,4,0,2,0);
  begin pause_cycles(8); check(wa==mark,"W_CONTEXT_VACANT_WAIT"); b_response(14,0,1,0); end
 join
 wait_wp(24); b_response(16,0,1,0); b_response(12,0,1,0); b_response(15,0,1,0);
 b_response(13,0,1,0);
 // A split write with four packets in flight leaves contexts vacant but no
 // piece room: the next folded write waits for one of its B.
 write_burst(22,16,2,1,0); wait_wp(28); mark=wa;
 fork
  write_burst(23,4,0,2,0);
  begin pause_cycles(8); check(wa==mark,"W_PIECE_ROOM"); b_response(22,0,0,0); end
 join
 wait_wp(29); b_response(22,0,0,0); b_response(23,0,1,0); b_response(22,2,0,0);
 b_response(22,0,1,2);
 // A narrow-split write's pieces take no piece room (the source's pool of request
 // packets bounds them, one packet each): a following folded write is accepted
 // while four of its pieces are still in flight.
 write_burst(24,4,1,1,0); wait_wp(33); mark=wa;
 fork
  write_burst(25,4,0,2,0);
  begin pause_cycles(8); check(wa==mark+1,"NARROW_W_NO_PIECE_ROOM"); end
 join
 for(k=0;k<4;k=k+1) b_response(24,0,k==3,0);
 b_response(25,0,1,0);
 // A split read needs only the reassembly context: it does not drain the reads
 // in flight, and a later read follows once its requests are out. Responses
 // of other tags pass between its packets with their own first and last, in
 // any order; descriptor lengths come from each response's own burst.
 for(k=0;k<2;k=k+1) read_burst(k,k+1,2,1,0);
 wait_rp(2); check(rb==0,"READ_TWO_IN_FLIGHT");
 fork
  read_burst(4,12,2,1,0);
  begin pause_cycles(12); check(rp==5 && rb==0,"SPLIT_R_NO_DRAIN"); end
 join
 fork
  read_burst(5,1,2,1,0);
  begin pause_cycles(8); check(rp==6 && rb==0,"SPLIT_R_NO_EXCLUDE"); end
 join
 r_response(4,4,0,12); r_response(1,2,0,2); r_response(4,4,4,12);
 r_response(5,1,0,1); r_response(0,1,0,1);
 // A second split read takes its own context at once; the packets of the two
 // bursts return interleaved, each counted on its own context.
 fork
  read_burst(6,8,2,1,0);
  begin pause_cycles(8); check(rp==8,"SPLIT_R_TWO_CONTEXTS"); end
 join
 r_response(6,4,0,8); r_response(4,4,8,12); r_response(6,4,4,8);
 // The lock hint can change after acceptance while later pieces are issued.
 read_burst(6,6,1,1,0); nl_hint_read_lock=1;
 wait_rp(10); check(rl[8]==0 && rl[9]==0,"SOURCE_LOCK_EARLY_SNAPSHOT");
 wait_rp(12);
 for(k=8;k<12;k=k+1) check(rl[k]==0 && rt[k]==6,"SOURCE_LOCK_SNAPSHOT");
 for(k=0;k<6;k=k+1) begin
  if(k>=4) wait_rp(9+k);
  r_response(6,1,k,6);
 end
 read_burst(7,4,1,1,1); nl_hint_read_lock=0; wait_rp(15);
 check(rl[14]==1 && rt[14]==7,"SOURCE_LOCK_READ");
 // A packed read holds no later read out; their responses pass between its
 // words, and a folded WRAP does not wait for older reads.
 fork
  read_burst(8,1,2,1,0);
  begin pause_cycles(8); check(rp==16,"PACKED_R_NO_EXCLUDE"); end
 join
 r_packed(7,0,1,2,2); r_response(8,1,0,1); r_packed(7,1,2,2,2);
 read_burst(8,1,2,1,0); wait_rp(17);
 fork
  read_burst(9,4,0,2,0);
  begin pause_cycles(8); check(rp==18,"FOLD_R_NO_DRAIN"); end
 join
 check(rl[17]==0 && rz[17]==2,"FOLD_R_REQUEST");
 r_fold(9); r_response(8,1,0,1);
 // A narrow-split read's pieces take no piece room (the source's pool of request
 // packets bounds them, one packet each): all of them issue without a word of
 // them answered.
 read_burst(10,8,1,1,0); pause_cycles(16); check(rp==26,"NARROW_R_NO_PIECE_ROOM");
 for(k=0;k<8;k=k+1) r_response(10,1,k,8);
 // Four folded reads hold every context; a fifth is not accepted until one
 // frees, and the others return in any order.
 for(k=12;k<16;k=k+1) read_burst(k,4,0,2,0);
 wait_rp(22); mark=ra;
 fork
  read_burst(16,4,0,2,0);
  begin pause_cycles(8); check(ra==mark,"CONTEXT_VACANT_WAIT"); r_fold(14); end
 join
 wait_rp(23); r_fold(16); r_fold(12); r_fold(15); r_fold(13);
 // Reset with all four serial piece credits occupied. Cancellation resets
 // both burst admission and piece bookkeeping before the next ordinary tag.
 read_burst(10,24,2,1,0); write_burst(10,16,2,1,0);
 wait_rp(27); wait_wp(33); pause_cycles(3);
 nl_reset_n=0; nl_up_wreq_valid=0; nl_up_rreq_valid=0; pause_cycles(3); nl_reset_n=1;
 read_burst(31,1,2,1,0); write_burst(31,1,2,1,0);
 wait_rp(28); wait_wp(34); r_response(31,1,0,1); b_response(31,0,1,0);
 $display("AXI4_TARGET_PASS packetizer_multi passthrough=4 serial_piece_limit=4 reset_full=1");
 $finish;
end
endmodule
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--work', required=True)
    args = parser.parse_args()
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run([args.fixture, str(work), 'multi'], check=True, timeout=120)
    ports = [(n, int(w), i == '1') for n, w, i in
             (line.split() for line in (work / 'ports.tsv').read_text().splitlines())]
    text = BENCH
    for key, value in {
        'DECL': '\n'.join(f"{'reg' if i else 'wire'} [{w-1}:0] {n};" for n, w, i in ports),
        'CONN': ', '.join(f'.{n}({n})' for n, _, _ in ports),
        'INIT': '\n'.join(f' {n}=0;' for n, _, i in ports if i and n != 'nl_clk'),
    }.items():
        text = text.replace(f'@{key}@', value)
    (work / 'tb.sv').write_text(text)
    rc = simulate(work, work / 'compiled', ())
    log = (work / 'simulation.log').read_text()
    passed = rc == 0 and 'AXI4_TARGET_PASS packetizer_multi' in log and 'ASSERT_FAIL' not in log
    result = {'status': 'PASS' if passed else 'FAIL', 'returncode': rc,
              'rtl_sha256': hashlib.sha256((work / 'network.v').read_bytes()).hexdigest(),
              'tb_sha256': hashlib.sha256((work / 'tb.sv').read_bytes()).hexdigest()}
    (work / 'results.json').write_text(json.dumps(result, indent=1) + '\n')
    if not passed:
        raise RuntimeError(log[-3000:])
    print('AXI4_TARGET_PASS packetizer_multi')


if __name__ == '__main__':
    main()
