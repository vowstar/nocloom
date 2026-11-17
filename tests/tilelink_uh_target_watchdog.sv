// The slave answers messages in arrival order. Plusargs place a stall on the network side
// (W gap, RREADY low) and on the slave side (A or D hold), and name the handshake that the
// expiry must follow.
always #5 clk=~clk;
integer mode=0, w_gap_at=-1, w_gap=0, r_low=0, a_hold_at=-1, a_hold=0;
integer d_hold_at=-1, d_hold=0, d_hold2_at=-1, d_hold2=0, expect_kind=0, expect_at=0;
integer cycle=0, fired=-1, wanted=-1, a_taken=0, d_taken=0, a_held=0, d_held=0;
integer a_beat=0, d_beat=0, head=0, tail=0, reads=0, writes=0, read_done=0, write_done=0;
integer n, id;
integer a_offer[0:255], a_take[0:255], d_take[0:255], r_beats[0:15], r_seen[0:15];
reg [31:0] r_address[0:15], m_address[0:63];
reg [3:0] m_size[0:63];
reg [15:0] m_source[0:63];
reg m_put[0:63];
function [31:0] word(input [31:0] address);
  word={16'hc0de, address[15:2], 2'b01};
endfunction
wire d_holding=(d_taken==d_hold_at && d_held<d_hold) || (d_taken==d_hold2_at && d_held<d_hold2);
always @(negedge clk) begin
  s_bready=1; s_rready=cycle>=r_low;
  t_a_ready=!(a_taken==a_hold_at && a_held<a_hold);
  t_d_valid=head<tail && !d_holding;
  if(head<tail) begin
    if(d_holding) d_held=d_held+1;
    t_d_source=m_source[head]; t_d_size=m_size[head]; t_d_opcode=m_put[head] ? 0 : 1;
    t_d_data=m_put[head] ? 0 : word(m_address[head]+4*d_beat);
  end
end
always @(posedge clk) if(reset_n) begin
  cycle=cycle+1;
  if(t_timed_out && fired<0) fired=cycle;
  if(fired>=0 && !t_timed_out) $fatal(1,"CHECK_FAIL WATCHDOG_LATCH");
  if(t_a_valid) begin
    if(a_offer[a_taken]<0) a_offer[a_taken]=cycle;
    if(!t_a_ready) a_held=a_held+1;
    else begin
      if(a_beat==0) begin
        m_address[tail]=t_a_address; m_size[tail]=t_a_size; m_source[tail]=t_a_source;
        m_put[tail]=t_a_opcode!=4;
      end
      if(t_a_opcode!=4 && (t_a_mask!==4'hf || t_a_data!==word(m_address[tail]+4*a_beat)))
        $fatal(1,"CHECK_FAIL PUT_DATA beat=%0d",a_beat);
      a_take[a_taken]=cycle; a_taken=a_taken+1; a_held=0;
      if(t_a_opcode==4 || t_a_size<=2 || a_beat+1==(1<<(t_a_size-2))) begin
        tail=tail+1; a_beat=0;
      end else a_beat=a_beat+1;
    end
  end
  if(t_d_valid && t_d_ready) begin
    d_take[d_taken]=cycle; d_taken=d_taken+1; d_held=0;
    if(m_put[head] || m_size[head]<=2 || d_beat+1==(1<<(m_size[head]-2))) begin
      head=head+1; d_beat=0;
    end else d_beat=d_beat+1;
  end
  if(s_rvalid && s_rready) begin
    id=s_rid;
    if(s_rresp!==0 || s_rdata!==word(r_address[id]+4*r_seen[id]) ||
       s_rlast!==(r_seen[id]+1==r_beats[id]))
      $fatal(1,"CHECK_FAIL READ_DATA id=%0d beat=%0d",id,r_seen[id]);
    r_seen[id]=r_seen[id]+1;
    if(s_rlast) read_done=read_done+1;
  end
  if(s_bvalid && s_bready) begin
    if(s_bresp!==0) $fatal(1,"CHECK_FAIL WRITE_RESPONSE");
    write_done=write_done+1;
  end
end
task read_burst(input integer tag, input [31:0] address, input integer beats);
  begin
    r_address[tag]=address; r_beats[tag]=beats; r_seen[tag]=0; reads=reads+1;
    @(negedge clk); s_arid=tag; s_araddr=address; s_arlen=beats-1; s_arsize=2; s_arburst=1;
    s_arvalid=1;
    @(posedge clk); while(!s_arready) @(posedge clk);
    @(negedge clk); s_arvalid=0;
  end
endtask
task write_burst(input integer tag, input [31:0] address, input integer beats);
  integer beat;
  begin
    writes=writes+1;
    @(negedge clk); s_awid=tag; s_awaddr=address; s_awlen=beats-1; s_awsize=2; s_awburst=1;
    s_awvalid=1;
    @(posedge clk); while(!s_awready) @(posedge clk);
    @(negedge clk); s_awvalid=0;
    for(beat=0;beat<beats;beat=beat+1) begin
      s_wdata=word(address+4*beat); s_wstrb=4'hf; s_wlast=beat==beats-1; s_wvalid=1;
      @(posedge clk); while(!s_wready) @(posedge clk);
      @(negedge clk); s_wvalid=0;
      if(beat==w_gap_at) repeat(w_gap) @(negedge clk);
    end
  end
endtask
initial begin
  if(!$value$plusargs("MODE=%d",mode)) mode=0;
  if(!$value$plusargs("WGAP_AT=%d",w_gap_at)) w_gap_at=-1;
  if(!$value$plusargs("WGAP=%d",w_gap)) w_gap=0;
  if(!$value$plusargs("RLOW=%d",r_low)) r_low=0;
  if(!$value$plusargs("AHOLD_AT=%d",a_hold_at)) a_hold_at=-1;
  if(!$value$plusargs("AHOLD=%d",a_hold)) a_hold=0;
  if(!$value$plusargs("DHOLD_AT=%d",d_hold_at)) d_hold_at=-1;
  if(!$value$plusargs("DHOLD=%d",d_hold)) d_hold=0;
  if(!$value$plusargs("DHOLD2_AT=%d",d_hold2_at)) d_hold2_at=-1;
  if(!$value$plusargs("DHOLD2=%d",d_hold2)) d_hold2=0;
  if(!$value$plusargs("EXPECT=%d",expect_kind)) expect_kind=0;
  if(!$value$plusargs("EXPECT_AT=%d",expect_at)) expect_at=0;
  for(n=0;n<256;n=n+1) begin a_offer[n]=-1; a_take[n]=-1; d_take[n]=-1; end
  repeat(160) @(negedge clk); reset_n=1;
  case(mode)
    0: write_burst(1,32'h100,16);
    1: read_burst(1,32'h100,16);
    2: begin read_burst(1,32'h100,16); read_burst(2,32'h200,16); end
    3: begin
      read_burst(0,32'h0,1);
      for(n=1;n<5;n=n+1) read_burst(n,n*256,2);
    end
    4: begin
      read_burst(0,32'h0,1);
      for(n=1;n<5;n=n+1) read_burst(n,n*256,2);
      wait(a_taken==5); write_burst(5,32'h500,1);
    end
    default: for(n=0;n<8;n=n+1) read_burst(n,n*256,2);
  endcase
  wait(read_done==reads && write_done==writes && head==tail);
  repeat(CYCLES+8) @(negedge clk);
  // An expiry shows one cycle after the last counted cycle. A read word needs one more cycle
  // to leave the receive buffer before the next D beat is owed.
  case(expect_kind)
    1: wanted=a_offer[expect_at]+CYCLES;
    2: wanted=a_take[expect_at]+1+CYCLES;
    3: wanted=d_take[expect_at]+2+CYCLES;
    4: wanted=d_take[expect_at]+1+CYCLES;
    default: wanted=-1;
  endcase
  if(fired!==wanted) $fatal(1,"CHECK_FAIL WATCHDOG fired=%0d wanted=%0d",fired,wanted);
  $display("UH_PASS watchdog K=%0d mode=%0d fired=%0d reads=%0d writes=%0d",
           K,mode,fired,reads,writes);
  $finish;
end
initial begin repeat(8*CYCLES+3000) @(negedge clk); $fatal(1,"CHECK_FAIL CYCLE_LIMIT"); end
