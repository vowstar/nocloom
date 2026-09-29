"""Tag FIFO and serial-class transactions on an internal bounded-packet face.

Packets of one tag intentionally have different lengths and widths. The public
packetizer supplies such packets in order; its integration has separate tests.
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
nocloom_axi4_width_clamp_probe dut(@CONN@);
initial nl_clk=0;
always #5 nl_clk=~nl_clk;
integer cycle=0, wp=0, rp=0, wb=0, rb=0, waccepted=0, raccepted=0;
integer rt[0:511], ra[0:511], rn[0:511], rs[0:511];
integer wt[0:511], wa[0:511], wn[0:511], ws[0:511];
integer rtail[0:63], rhead[0:63], wtail[0:63], whead[0:63];
integer ea[0:1023], en[0:1023], es[0:1023], ei[0:1023], ek[0:1023];
integer ba[0:1023], bs[0:1023], bk[0:1023];
integer rbeats=0, wbeats=0, idx, addr, j;
reg [63:0] expected_data;
function [63:0] pattern;
 input integer address,tag;
 integer k;
 begin
  pattern=0;
  for(k=0;k<8;k=k+1) pattern[k*8+:8]=((address&(-8))+k+tag*17)&255;
 end
endfunction
task check; input ok; input [8*64-1:0] name; begin
 if(ok !== 1'b1) begin
  $display("ASSERT_FAIL:WIDTH_CUT_MULTI.%0s cycle=%0d wp=%0d rp=%0d wb=%0d rb=%0d",name,cycle,wp,rp,wb,rb); $fatal(1);
 end
end endtask
always @(posedge nl_clk) begin
 cycle=cycle+1;
 if(cycle>20000) begin $display("ASSERT_FAIL:WIDTH_CUT_MULTI.TIMEOUT"); $fatal(1); end
 if(nl_reset_n) begin
  if(nl_up_wreq_valid && nl_up_wreq_ready && nl_up_wreq_first) waccepted=waccepted+1;
  if(nl_up_rreq_valid && nl_up_rreq_ready) raccepted=raccepted+1;
  if(nl_down_wreq_valid && nl_down_wreq_ready) begin
   check(nl_down_wreq_id==(nl_down_wreq_tag%8),"W_ID");
   check(nl_down_wreq_source==2,"W_SOURCE");
   check(nl_down_wreq_data==pattern(nl_down_wreq_address,nl_down_wreq_tag),"W_DATA");
   if(nl_down_wreq_first) begin
    wt[wp]=nl_down_wreq_tag; wa[wp]=nl_down_wreq_start_address;
    wn[wp]=nl_down_wreq_length+1; ws[wp]=nl_down_wreq_size; wbeats=0;
   end
   // A transfer carries a run of whole target words of its source beat.
   check(nl_down_wreq_address==wa[wp]+(wbeats<<ws[wp]),"W_ADDRESS");
   check(nl_down_wreq_count>0 && nl_down_wreq_count%(1<<ws[wp])==0,"W_RUN");
   check(nl_down_wreq_last==(wbeats+(nl_down_wreq_count>>ws[wp])==wn[wp]),"W_LAST");
   wbeats=wbeats+(nl_down_wreq_count>>ws[wp]);
   if(nl_down_wreq_last) wp=wp+1;
  end
  if(nl_down_rreq_valid && nl_down_rreq_ready) begin
   check(nl_down_rreq_id==(nl_down_rreq_tag%8),"AR_ID");
   check(nl_down_rreq_source==3,"AR_SOURCE");
   rt[rp]=nl_down_rreq_tag; ra[rp]=nl_down_rreq_start_address;
   rn[rp]=nl_down_rreq_length+1; rs[rp]=nl_down_rreq_size; rp=rp+1;
  end
  if(nl_up_wrsp_valid && nl_up_wrsp_ready) begin
   idx=nl_up_wrsp_tag*16+whead[nl_up_wrsp_tag];
   check(whead[nl_up_wrsp_tag]<wtail[nl_up_wrsp_tag],"B_RESERVED");
   check(nl_up_wrsp_address==ba[idx] && nl_up_wrsp_count==(1<<bs[idx]),"B_FIFO_METADATA");
   check(nl_up_wrsp_id==nl_up_wrsp_tag%8 && nl_up_wrsp_source==2,"B_ID_SOURCE");
   check(nl_up_wrsp_result==(bk[idx]==0?2:0),"B_RESULT");
   whead[nl_up_wrsp_tag]=whead[nl_up_wrsp_tag]+1; wb=wb+1;
  end
  if(nl_up_rrsp_valid && nl_up_rrsp_ready) begin
   idx=nl_up_rrsp_tag*16+rhead[nl_up_rrsp_tag];
   check(rhead[nl_up_rrsp_tag]<rtail[nl_up_rrsp_tag],"R_RESERVED");
   addr=ea[idx]+(ei[idx]<<es[idx]);
   check(nl_up_rrsp_address==addr && nl_up_rrsp_count==(1<<es[idx]),"R_FIFO_METADATA");
   check(nl_up_rrsp_id==nl_up_rrsp_tag%8 && nl_up_rrsp_source==3,"R_ID_SOURCE");
   check(nl_up_rrsp_first==(ei[idx]==0) && nl_up_rrsp_last==(ei[idx]==en[idx]-1),"R_FIFO_BOUNDARY");
   expected_data=pattern(addr,nl_up_rrsp_tag);
   // Only requested lanes are defined for an unconverted narrow beat.
   if(ek[idx]==0) begin
    check(nl_up_rrsp_data==0 && nl_up_rrsp_result==2,"R_LOCAL_ERROR");
   end else begin
    for(j=0;j<(1<<es[idx]);j=j+1)
     check(nl_up_rrsp_data[((addr%8)+j)*8+:8]==expected_data[((addr%8)+j)*8+:8],"R_DATA");
    check(nl_up_rrsp_result==0,"R_RESULT");
   end
   ei[idx]=ei[idx]+1;
   if(nl_up_rrsp_last) begin rhead[nl_up_rrsp_tag]=rhead[nl_up_rrsp_tag]+1; rb=rb+1; end
  end
 end
end
always @(negedge nl_clk) begin
 nl_down_wreq_ready=(cycle%4)!=1;
 nl_down_rreq_ready=(cycle%3)!=1;
end
task pause_cycles; input integer n; begin repeat(n) @(negedge nl_clk); end endtask
task wait_wp; input integer n; begin while(wp<n) @(negedge nl_clk); end endtask
task wait_rp; input integer n; begin while(rp<n) @(negedge nl_clk); end endtask
task write_packet;
 input integer tag,address,n,size,kind;
 integer i,q;
 begin
  q=tag*16+wtail[tag]; ba[q]=address; bs[q]=size; bk[q]=kind; wtail[tag]=wtail[tag]+1;
  for(i=0;i<n;i=i+1) begin
   @(negedge nl_clk); nl_up_wreq_valid=1; nl_up_wreq_write=1;
   nl_up_wreq_tag=tag; nl_up_wreq_id=tag%8; nl_up_wreq_source=2;
   nl_up_wreq_address=address+(kind==0?0:(i<<size)); nl_up_wreq_start_address=address;
   nl_up_wreq_length=n-1; nl_up_wreq_size=size; nl_up_wreq_count=1<<size;
   nl_up_wreq_burst=kind; nl_up_wreq_first=(i==0); nl_up_wreq_last=(i==n-1);
   nl_up_wreq_data=pattern(address+(i<<size),tag); nl_up_wreq_be=255;
   @(posedge nl_clk); while(!nl_up_wreq_ready) @(posedge nl_clk);
  end
  @(negedge nl_clk); nl_up_wreq_valid=0;
 end
endtask
task read_packet;
 input integer tag,address,n,size,kind;
 integer q;
 begin
  q=tag*16+rtail[tag]; ea[q]=address; en[q]=n; es[q]=size; ei[q]=0; ek[q]=kind; rtail[tag]=rtail[tag]+1;
  @(negedge nl_clk); nl_up_rreq_valid=1;
  nl_up_rreq_tag=tag; nl_up_rreq_id=tag%8; nl_up_rreq_source=3;
  nl_up_rreq_address=address; nl_up_rreq_start_address=address;
  nl_up_rreq_length=n-1; nl_up_rreq_size=size; nl_up_rreq_count=1<<size;
  nl_up_rreq_burst=kind; nl_up_rreq_first=1; nl_up_rreq_last=1;
  @(posedge nl_clk); while(!nl_up_rreq_ready) @(posedge nl_clk);
  @(negedge nl_clk); nl_up_rreq_valid=0;
 end
endtask
task b_response;
 input integer packet;
 begin
  @(negedge nl_clk); nl_down_wrsp_valid=1;
  nl_down_wrsp_tag=wt[packet]; nl_down_wrsp_id=wt[packet]%8; nl_down_wrsp_source=2;
  // Deliberately unrelated fields: the saved packet must supply these.
  nl_down_wrsp_address=32'hdead0000; nl_down_wrsp_count=4;
  nl_down_wrsp_first=1; nl_down_wrsp_last=1; nl_down_wrsp_result=0;
  nl_up_wrsp_ready=0;
  @(posedge nl_clk);
  // Intermediate serial Bs are absorbed even under source backpressure.
  // Retire valid after that handshake rather than sending the B again.
  if(!nl_down_wrsp_ready) begin
   pause_cycles(3); nl_up_wrsp_ready=1;
   @(posedge nl_clk); while(!nl_down_wrsp_ready) @(posedge nl_clk);
  end
  @(negedge nl_clk); nl_down_wrsp_valid=0; nl_up_wrsp_ready=1;
 end
endtask
task r_response;
 input integer packet;
 integer i,address;
 begin
  for(i=0;i<rn[packet];i=i+1) begin
   @(negedge nl_clk); nl_down_rrsp_valid=1;
   address=ra[packet]+(i<<rs[packet]);
   nl_down_rrsp_tag=rt[packet]; nl_down_rrsp_id=rt[packet]%8; nl_down_rrsp_source=3;
   nl_down_rrsp_address=address; nl_down_rrsp_count=1<<rs[packet];
   nl_down_rrsp_first=(i==0); nl_down_rrsp_last=(i==rn[packet]-1);
   nl_down_rrsp_data=pattern(address,rt[packet]); nl_down_rrsp_result=0;
   @(posedge nl_clk); while(!nl_down_rrsp_ready) @(posedge nl_clk);
  end
  @(negedge nl_clk); nl_down_rrsp_valid=0;
 end
endtask
integer k,mark,old;
initial begin
@INIT@
 for(k=0;k<64;k=k+1) begin rtail[k]=0; rhead[k]=0; wtail[k]=0; whead[k]=0; end
 nl_reset_n=0; pause_cycles(3); nl_reset_n=1; nl_up_rrsp_ready=1; nl_up_wrsp_ready=1;
 // Four packets of tag 63 coexist; differing descriptors expose overwrite.
 write_packet(63,256,2,3,1); write_packet(63,272,3,2,1);
 write_packet(63,288,2,3,1); write_packet(63,304,1,2,1);
 write_packet(2,512,1,3,1); wait_wp(5);
 check(wb==0 && wn[0]==4 && wn[1]==3 && wn[2]==4 && wn[3]==1,"W_FOUR_SAME_TAG");
 b_response(4);
 for(k=0;k<4;k=k+1) b_response(k);
 read_packet(63,256,2,3,1); read_packet(63,272,3,2,1);
 read_packet(63,288,2,3,1); read_packet(63,304,1,2,1);
 read_packet(2,512,1,3,1); wait_rp(5);
 check(rb==0 && rn[0]==4 && rn[1]==3 && rn[2]==4 && rn[3]==1,"R_FOUR_SAME_TAG");
 r_response(4); for(k=0;k<4;k=k+1) r_response(k);
 pause_cycles(4); check(rb==5 && wb==5,"FIFO_RETIRE");
 // A serial width cut cannot overtake a prior single packet, and its two
 // pieces issue before any response. A following packet waits for delivery.
 write_packet(3,768,1,3,1); wait_wp(6);
 fork
  write_packet(4,1024,16,3,1);
  begin pause_cycles(5); check(wp==6 && waccepted==6,"W_SERIAL_DRAIN"); b_response(5); end
 join
 wait_wp(8); check(wb==6 && wn[6]==16 && wn[7]==16,"W_SERIAL_PIPELINE");
 fork
  write_packet(5,1280,1,3,1);
  begin pause_cycles(5); check(wp==8,"W_SERIAL_EXCLUDE"); b_response(6); b_response(7); end
 join
 wait_wp(9); b_response(8);
 read_packet(3,768,1,3,1); wait_rp(6);
 fork
  read_packet(4,1024,16,3,1);
  begin pause_cycles(5); check(rp==6 && raccepted==6,"R_SERIAL_DRAIN"); r_response(5); end
 join
 wait_rp(8); check(rb==6 && rn[6]==16 && rn[7]==16,"R_SERIAL_PIPELINE");
 fork
  read_packet(5,1280,1,3,1);
  begin pause_cycles(5); check(rp==8,"R_SERIAL_EXCLUDE"); r_response(6); r_response(7); end
 join
 wait_rp(9); r_response(8);
 // Local refusal is serial too, and follows all earlier network responses.
 write_packet(6,1536,1,3,1); wait_wp(10);
 fork
  write_packet(7,1792,1,3,0);
  begin pause_cycles(5); check(wb==8,"W_LOCAL_DRAIN"); b_response(9); end
 join
 pause_cycles(5); check(wp==10 && wb==10,"W_LOCAL_COMPLETE");
 read_packet(6,1536,1,3,1); wait_rp(10);
 fork
  read_packet(7,1792,1,3,0);
  begin pause_cycles(5); check(rb==8,"R_LOCAL_DRAIN"); r_response(9); end
 join
 pause_cycles(5); check(rp==10 && rb==10,"R_LOCAL_COMPLETE");
 // Hold the final converted beat while another request changes the issue
 // descriptor. A third response cannot take ownership before final delivery.
 // The word completing the beat waits on the down face for the up face's ready.
 read_packet(8,2048,1,3,1); wait_rp(11); nl_up_rrsp_ready=0;
 fork r_response(10); join_none
 pause_cycles(3); check(nl_up_rrsp_valid && nl_up_rrsp_tag==8,"R_HELD_OWNER");
 read_packet(9,2304,3,2,1); wait_rp(12);
 repeat(4) begin @(negedge nl_clk); check(nl_up_rrsp_tag==8 && nl_up_rrsp_count==8,"R_OWNER_STABLE"); end
 nl_up_rrsp_ready=1; while(rb<11) @(negedge nl_clk); pause_cycles(1);
 r_response(11); pause_cycles(3); check(rb==12,"R_OWNER_RETIRE");
 // Reset cancels populated same-tag queues, then reuses their highest tag.
 for(k=0;k<4;k=k+1) begin write_packet(62,2560+k*8,1,3,1); read_packet(62,2560+k*8,1,3,1); end
 wait_wp(14); wait_rp(16);
 nl_reset_n=0; pause_cycles(3);
 for(k=0;k<64;k=k+1) begin rtail[k]=0; rhead[k]=0; wtail[k]=0; whead[k]=0; end
 nl_reset_n=1;
 write_packet(62,2816,1,3,1); read_packet(62,2816,1,3,1);
 wait_wp(15); wait_rp(17); b_response(14); r_response(16); pause_cycles(3);
 $display("AXI4_TARGET_PASS width_cut_multi same_tag_depth=4 highest_tag=63 serial_pieces=2 reset_full=1");
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
    subprocess.run([args.fixture, str(work)], check=True, timeout=120)
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
    passed = rc == 0 and 'AXI4_TARGET_PASS width_cut_multi' in log and 'ASSERT_FAIL' not in log
    result = {'status': 'PASS' if passed else 'FAIL', 'returncode': rc,
              'rtl_sha256': hashlib.sha256((work / 'network.v').read_bytes()).hexdigest(),
              'tb_sha256': hashlib.sha256((work / 'tb.sv').read_bytes()).hexdigest()}
    (work / 'results.json').write_text(json.dumps(result, indent=1) + '\n')
    if not passed:
        raise RuntimeError(log[-3000:])
    print('AXI4_TARGET_PASS width_cut_multi')


if __name__ == '__main__':
    main()
