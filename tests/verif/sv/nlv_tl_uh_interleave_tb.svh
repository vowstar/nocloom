// Tape-driven AXI4 source, TL-UH slave model with one message per source, an
// optional AXI4 slave model and a scoreboard that uses only port activity.
//
// The tape is a list of 160-bit rows: a header with the seed and the required
// coverage, then batches. A batch is one control row and its requests. Reads
// and writes of a batch start in tape order on their own channels. The next
// batch starts when every response has arrived, after a whole-memory check.
// A request can wait for a full pool (1), for a target read between its R
// beats (2) or for two Gets in flight (3). Each wait has a cycle limit.
// Rows after the control row name target words that answer every Get beat
// with corrupt set during that batch.
localparam LANES=DATA_WIDTH/8;
localparam NID=1<<ID_WIDTH;
localparam MEM=65536, QMEM=4096, MAXT=8192, QD=128, WF=262144;
localparam STALL_LIMIT=30000, WAIT_LIMIT=6000, HOLD_LIMIT=8000, CYCLE_LIMIT=2000000;
localparam K_UH=0, K_Q=1, K_MISS=2;
always #5 clk=~clk;
integer cycle=0, last_progress=0;
reg [159:0] tape[0:MAXT-1];
reg [1023:0] tape_name;
reg [159:0] header;
integer rows=0;
reg trace=0;

task nlv_fail_report(input [255:0] rule_name, input [255:0] who, input [63:0] value);
  begin
    $display("ASSERT_FAIL:%0s %0s value=%0d cycle=%0d", rule_name, who, value, cycle);
    $fatal(1);
  end
endtask
task check(input ok, input [255:0] label);
  if(!ok) nlv_fail_report(label, "interleave", 0);
endtask

function [31:0] xs(input [31:0] v);
  reg [31:0] x;
  begin x=v ^ (v<<13); x=x ^ (x>>17); xs=x ^ (x<<5); end
endfunction
function [7:0] initial_byte(input [31:0] a);
  reg [31:0] x;
  begin x=a*32'h9e3779b1; initial_byte=x[31:24] ^ x[15:8] ^ 8'h5a; end
endfunction
function [7:0] write_byte(input [31:0] salt, input [31:0] a);
  reg [31:0] x;
  begin x=(a ^ (salt*32'h2545f491))*32'h85ebca6b; x=x ^ (x>>13); write_byte=x[7:0] ^ x[23:16]; end
endfunction
function strobe_bit(input [1:0] mode, input [31:0] salt, input [31:0] a, input integer k);
  reg [31:0] x;
  begin
    x=(a+salt)*32'hc2b2ae35; x=x ^ (x>>15);
    strobe_bit=mode==0 ? 1'b1 : mode==1 ? x[2:0]!=0 : mode==2 ? x[0] : (k%3)!=1;
  end
endfunction
// First byte of beat k and one past the last byte of that beat.
function [31:0] beat_lo(input [31:0] start, input integer k, input integer size, input integer count, input integer burst);
  reg [31:0] a, span;
  begin
    span=count<<size; a=(start & ~((32'd1<<size)-1))+(k<<size);
    beat_lo=burst==0 ? start : burst==2 ? (start/span)*span+(a%span) : k==0 ? start : a;
  end
endfunction
function [31:0] beat_hi(input [31:0] lo, input integer size);
  beat_hi=(lo & ~((32'd1<<size)-1))+(32'd1<<size);
endfunction

// Tape entry fields.
function [3:0] e_op(input [159:0] e); e_op=e[3:0]; endfunction
function [7:0] e_id(input [159:0] e); e_id=e[11:4]; endfunction
function [31:0] e_addr(input [159:0] e); e_addr=e[43:12]; endfunction
function [7:0] e_len(input [159:0] e); e_len=e[51:44]; endfunction
function [2:0] e_size(input [159:0] e); e_size=e[54:52]; endfunction
function [1:0] e_burst(input [159:0] e); e_burst=e[56:55]; endfunction
function [1:0] e_kind(input [159:0] e); e_kind=e[58:57]; endfunction
function [7:0] e_delay(input [159:0] e); e_delay=e[70:63]; endfunction
function [1:0] e_strobe(input [159:0] e); e_strobe=e[72:71]; endfunction
function [3:0] e_wait(input [159:0] e); e_wait=e[76:73]; endfunction
function [15:0] e_low(input [159:0] e); e_low=e[92:77]; endfunction
function [31:0] e_salt(input [159:0] e); e_salt=e[125:94]; endfunction
function [15:0] e_high(input [159:0] e); e_high=e[141:126]; endfunction

// Batch controls from the tape.
integer a_density=255, d_density=255, d_latency=0, d_order=0, r_density=255, b_density=255;
integer w_density=255, hold_mode=0, early_ack=0, hold_extra=0, q_density=255;
integer batch_lo=0, batch_hi=0, batch_no=0, batch_start=0, hold_from=0;
reg batch_active=0, full_seen=0;

// Model memories, expected contents and the expected write byte stream.
reg [7:0] memory[0:MEM-1], expected_memory[0:MEM-1];
reg [7:0] q_memory[0:QMEM-1], q_expected[0:QMEM-1];
integer actual_reads[0:MEM-1], expected_reads[0:MEM-1];
reg [31:0] wf_addr[0:WF-1];
reg [7:0] wf_data[0:WF-1];
integer wf_head=0, wf_count=0;
integer uh_cover[0:15];
integer q_cover=0;
reg [31:0] failed_words[0:15];
integer failed_count=0;

// A word that the target answers with corrupt, and a byte range that uses one.
function failed_word(input [31:0] address);
  integer i;
  begin
    failed_word=0;
    for(i=0;i<failed_count;i=i+1) if(failed_words[i]==(address & ~32'd3)) failed_word=1;
  end
endfunction
function failed_range(input [31:0] lo, input [31:0] hi);
  reg [31:0] word;
  begin
    failed_range=0;
    for(word=lo & ~32'd3;word<hi;word=word+4) if(failed_word(word)) failed_range=1;
  end
endfunction

// Transaction state by tape index.
reg t_done[0:MAXT-1], t_wlast[0:MAXT-1];
integer t_issue[0:MAXT-1], t_beats[0:MAXT-1];
integer rq[0:NID*QD-1], rq_head[0:NID-1], rq_count[0:NID-1];
integer bq[0:NID*QD-1], bq_head[0:NID-1], bq_count[0:NID-1];
integer wq[0:1023], wq_head=0, wq_count=0;
integer uw[0:1023], uw_head=0, uw_count=0;
integer rd_order[0:MAXT-1], rd_total=0, rd_oldest=0;
integer accepted=0, completed=0;
integer uh_reads_out=0, uh_reads_mid=0, miss_out=0, writes_out=0, prev_r=-1;
integer last_aw_id=-1;
integer expected_gets=0, expected_puts_low=0, expected_puts_high=0;

// Coverage counters.
// c_switch: an R beat follows a beat of another read that is not complete.
// c_cross: the same, and both reads need at least two target messages.
// c_resume: the same, and the new beat is not the first beat of its read.
// c_later: a read ends while a read that was accepted earlier is not complete.
// c_pool_full: clock cycles in which every source has a message in flight.
integer c_reads=0, c_writes=0, c_r_beats=0, c_incr=0, c_wrap=0, c_unaligned=0, c_narrow=0, c_full=0;
integer c_switch=0, c_cross=0, c_resume=0, c_later=0, c_later_uh=0, c_pool_full=0, c_r_stall=0;
integer c_d_gap=0, c_a_stall=0, c_d_reorder=0, c_early_ack=0, c_aw_overlap=0, c_max_bytes=0;
// c_r_error: R beats that use a corrupt target word. c_error_switch: such a
// beat follows a beat of another read that is not complete.
integer c_r_error=0, c_error_switch=0;
// Misses are counted at their request handshake: with two target reads
// pending (c_miss_two), with two Gets in flight (c_miss_gets), with another
// miss pending (c_miss_pair), with a full pool (c_miss_full), and behind a
// pending target read of the same ID (c_miss_same). c_miss_w_between counts
// miss W beats that arrive between the R beats of one target read.
integer c_miss_r=0, c_miss_w=0, c_miss_two=0, c_miss_gets=0, c_miss_same=0, c_miss_pair=0;
integer c_miss_full=0, c_miss_w_between=0, c_miss_b_between=0, c_miss_r_between=0;
integer c_uh_after_miss=0, c_q_reads=0, c_q_writes=0;

// TL-UH slave model.
reg m_busy[0:15], m_get[0:15], m_owed[0:15];
reg [31:0] m_addr[0:15];
integer m_size[0:15], m_at[0:15], m_order[0:15];
integer messages=0, gets=0, puts=0, arrivals=0;
integer put_left=0, put_source=0;
reg [31:0] put_cursor=0, d_cursor=0;
reg put_acked=0;
integer d_source_now=-1, d_left=0, d_total=0;
reg d_fired=0;
integer tl_inflight=0, tl_gets=0;
reg [31:0] rnd_a=1, rnd_d=2, rnd_r=3, rnd_b=4, rnd_w=5, rnd_q=6, rnd_l=7;

// AXI4 slave model for the second target.
reg [31:0] qr_addr[0:15], qw_addr[0:15];
integer qr_len[0:15], qr_size[0:15], qr_burst[0:15], qr_id[0:15];
integer qw_len[0:15], qw_size[0:15], qw_burst[0:15], qw_id[0:15];
integer qb_id[0:15];
integer qr_head=0, qr_count=0, qr_beat=0, qw_head=0, qw_count=0, qw_beat=0, qb_head=0, qb_count=0;
reg qr_fired=0, qb_fired=0;

// Source sequencing.
integer ar_index=0, aw_index=0, ar_since=0, aw_since=0;
reg ar_fired=0, aw_fired=0, w_fired=0, w_busy=0;
integer w_index=0, w_beat=0, w_total=0;
reg [159:0] ar_entry, aw_entry, w_entry;

function integer next_entry(input integer from, input integer op);
  integer p;
  begin
    p=from;
    while(p<batch_hi && tape[p][3:0]!=op) p=p+1;
    next_entry=p;
  end
endfunction
function wait_met(input [3:0] code);
  wait_met=code==1 ? tl_inflight==CAPACITY : code==2 ? uh_reads_mid>=1 :
           code==3 ? tl_gets>=2 || (CAPACITY==1 && tl_gets>=1 && uh_reads_out>=2) : 1'b1;
endfunction

task report_stuck(input [255:0] rule_name);
  integer oldest, p;
  begin
    oldest=-1;
    for(p=batch_lo;p<batch_hi;p=p+1)
      if((tape[p][3:0]==1 || tape[p][3:0]==2) && t_issue[p]>=0 && !t_done[p] &&
         (oldest<0 || t_issue[p]<t_issue[oldest])) oldest=p;
    if(oldest>=0)
      $display("STUCK transaction=%0d write=%0d id=%0d address=%h beats=%0d kind=%0d beats_done=%0d accepted_cycle=%0d batch=%0d",
               oldest, tape[oldest][3:0]==2, e_id(tape[oldest]), e_addr(tape[oldest]),
               e_len(tape[oldest])+1, e_kind(tape[oldest]), t_beats[oldest], t_issue[oldest], batch_no);
    else if(s_arvalid)
      $display("STUCK transaction=%0d write=0 id=%0d address=%h request not accepted batch=%0d",
               ar_index, s_arid, s_araddr, batch_no);
    else if(s_awvalid)
      $display("STUCK transaction=%0d write=1 id=%0d address=%h request not accepted batch=%0d",
               aw_index, s_awid, s_awaddr, batch_no);
    else
      $display("STUCK no source transaction, target messages in flight=%0d batch=%0d", tl_inflight, batch_no);
    $display("STUCK state accepted=%0d completed=%0d target_inflight=%0d put_left=%0d write_bytes_pending=%0d",
             accepted, completed, tl_inflight, put_left, wf_count);
    nlv_fail_report(rule_name, "interleave", cycle-last_progress);
  end
endtask

// Source request channels.
always @(negedge clk) begin : drive_requests
  integer k, lane;
  reg [31:0] lo, hi, b;
  if(!reset_n) begin
    s_arvalid=0; s_awvalid=0; s_wvalid=0;
  end else begin
    if(ar_fired) begin s_arvalid=0; ar_fired=0; ar_index=next_entry(ar_index+1,1); ar_since=0; end
    if(batch_active && !s_arvalid && ar_index<batch_hi) begin
      ar_entry=tape[ar_index]; ar_since=ar_since+1;
      if(ar_since>e_delay(ar_entry) && (wait_met(e_wait(ar_entry)) || ar_since>WAIT_LIMIT)) begin
        s_arid=e_id(ar_entry); s_araddr=e_addr(ar_entry); s_arlen=e_len(ar_entry);
        s_arsize=e_size(ar_entry); s_arburst=e_burst(ar_entry); s_arcache=0; s_arprot=0; s_arlock=0;
        s_arvalid=1;
        if(e_kind(ar_entry)==K_UH) uh_cover[s_araddr[15:12]]=uh_cover[s_araddr[15:12]]+1;
        if(e_kind(ar_entry)==K_Q) q_cover=q_cover+1;
      end
    end
    if(aw_fired) begin s_awvalid=0; aw_fired=0; aw_index=next_entry(aw_index+1,2); aw_since=0; end
    if(batch_active && !s_awvalid && aw_index<batch_hi) begin
      aw_entry=tape[aw_index]; aw_since=aw_since+1;
      if(aw_since>e_delay(aw_entry) && (wait_met(e_wait(aw_entry)) || aw_since>WAIT_LIMIT)) begin
        s_awid=e_id(aw_entry); s_awaddr=e_addr(aw_entry); s_awlen=e_len(aw_entry);
        s_awsize=e_size(aw_entry); s_awburst=e_burst(aw_entry); s_awcache=0; s_awprot=0; s_awlock=0;
        s_awvalid=1;
        if(e_kind(aw_entry)==K_UH) uh_cover[s_awaddr[15:12]]=uh_cover[s_awaddr[15:12]]+1;
        if(e_kind(aw_entry)==K_Q) q_cover=q_cover+1;
      end
    end
    if(w_fired) begin
      s_wvalid=0; w_fired=0; w_beat=w_beat+1;
      if(w_beat==w_total) begin w_busy=0; wq_head=(wq_head+1)%1024; wq_count=wq_count-1; end
    end
    if(!w_busy && wq_count>0) begin
      w_index=wq[wq_head]; w_entry=tape[w_index]; w_beat=0; w_total=e_len(w_entry)+1; w_busy=1;
    end
    if(w_busy && !s_wvalid) begin
      rnd_w=xs(rnd_w);
      if(rnd_w[7:0]<=w_density) begin
        lo=beat_lo(e_addr(w_entry),w_beat,e_size(w_entry),w_total,e_burst(w_entry));
        hi=beat_hi(lo,e_size(w_entry));
        s_wdata=0; s_wstrb=0;
        for(b=lo;b!=hi;b=b+1) begin
          lane=b & (LANES-1);
          s_wdata[lane*8+:8]=write_byte(e_salt(w_entry),b);
          s_wstrb[lane]=strobe_bit(e_strobe(w_entry),e_salt(w_entry),b,w_beat);
        end
        s_wlast=w_beat==w_total-1; s_wvalid=1;
      end
    end
    rnd_r=xs(rnd_r);
    s_rready=rnd_r[7:0]<=r_density;
    rnd_b=xs(rnd_b); s_bready=rnd_b[7:0]<=b_density;
  end
end

// TL-UH slave: A ready and D messages. One D message owns the channel until
// its last beat. Bytes outside a narrow Get carry inverted data.
always @(negedge clk) begin : drive_target
  integer s, best, total, b;
  reg held, covered;
  if(!reset_n) begin
    t_a_ready=0; t_d_valid=0;
  end else begin
    rnd_a=xs(rnd_a); t_a_ready=rnd_a[7:0]<=a_density;
    if(d_fired) begin t_d_valid=0; d_fired=0; end
    held=hold_mode!=0 && !(full_seen && cycle-hold_from>=hold_extra);
    if(cycle-batch_start>HOLD_LIMIT || !batch_active) held=0;
    if(!t_d_valid) begin
      if(d_source_now<0 && !held) begin
        best=-1;
        rnd_d=xs(rnd_d);
        for(s=0;s<CAPACITY;s=s+1) if(m_owed[s] && m_at[s]<=cycle) begin
          if(best<0) best=s;
          else if(d_order==1 ? m_order[s]>m_order[best] : d_order==2 ? m_order[s]<m_order[best] :
                  ((rnd_d>>(s%16))&1)!=0) best=s;
        end
        if(best>=0) begin
          for(s=0;s<CAPACITY;s=s+1)
            if(s!=best && m_owed[s] && m_order[s]<m_order[best]) c_d_reorder=c_d_reorder+1;
          d_source_now=best; m_owed[best]=0;
          d_total=m_get[best] && m_size[best]>2 ? (1<<m_size[best])/4 : 1;
          d_left=d_total; d_cursor=m_addr[best] & ~32'd3;
          if(!m_get[best] && put_left!=0 && put_source==best) c_early_ack=c_early_ack+1;
        end
      end
      if(d_source_now>=0) begin
        rnd_d=xs(rnd_d);
        if(rnd_d[15:8]<=d_density) begin
          t_d_valid=1; t_d_opcode=m_get[d_source_now]; t_d_size=m_size[d_source_now];
          t_d_source=d_source_now; t_d_param=0; t_d_denied=0; t_d_data=0;
          t_d_corrupt=m_get[d_source_now] && failed_word(d_cursor);
          if(m_get[d_source_now]) for(b=0;b<4;b=b+1) begin
            covered=m_size[d_source_now]>=2 || (d_cursor+b>=m_addr[d_source_now] &&
                   d_cursor+b<m_addr[d_source_now]+(32'd1<<m_size[d_source_now]));
            t_d_data[b*8+:8]=covered ? memory[d_cursor[15:0]+b] : ~memory[d_cursor[15:0]+b];
          end
        end else if(d_left<d_total) c_d_gap=c_d_gap+1;
      end
    end
  end
end

// Second target: AXI4 slave with in-order responses.
always @(negedge clk) begin : drive_other
  reg [31:0] lo, hi, b;
  if(!reset_n) begin
    q_arready=0; q_awready=0; q_wready=0; q_rvalid=0; q_bvalid=0;
  end else begin
    rnd_q=xs(rnd_q);
    q_arready=qr_count<8 && rnd_q[7:0]<=q_density;
    q_awready=qw_count<8 && rnd_q[15:8]<=q_density;
    q_wready=qw_count>0 && rnd_q[23:16]<=q_density;
    if(qr_fired) begin q_rvalid=0; qr_fired=0; end
    if(!q_rvalid && qr_count>0 && rnd_q[31:24]<=q_density) begin
      lo=beat_lo(qr_addr[qr_head],qr_beat,qr_size[qr_head],qr_len[qr_head]+1,qr_burst[qr_head]);
      hi=beat_hi(lo,qr_size[qr_head]);
      q_rdata=0;
      for(b=lo;b!=hi;b=b+1) q_rdata[(b & 3)*8+:8]=q_memory[b[11:0]];
      q_rid=qr_id[qr_head]; q_rresp=0; q_rlast=qr_beat==qr_len[qr_head]; q_rvalid=1;
    end
    if(qb_fired) begin q_bvalid=0; qb_fired=0; end
    if(!q_bvalid && qb_count>0 && rnd_q[12:5]<=q_density) begin
      q_bid=qb_id[qb_head]; q_bresp=0; q_bvalid=1;
    end
  end
end

always @(posedge clk) if(reset_n) begin : monitor
  integer id, index, k, count, size, burst, kind, s, lane, p, resp;
  reg [31:0] lo, hi, b, start;
  reg [159:0] e;
  reg last, bad, failed;
  cycle=cycle+1;
  if(tl_inflight==CAPACITY) begin c_pool_full=c_pool_full+1; if(!full_seen) hold_from=cycle; full_seen=1; end
  if(s_rvalid && !s_rready) c_r_stall=c_r_stall+1;
  if(t_a_valid && !t_a_ready) c_a_stall=c_a_stall+1;

  // Source requests.
  if(s_arvalid && s_arready) begin
    last_progress=cycle; ar_fired=1; index=ar_index; e=tape[index]; id=s_arid; kind=e_kind(e);
    if(trace) $display("TRACE AR cycle=%0d transaction=%0d id=%0d address=%h beats=%0d size=%0d kind=%0d",
                       cycle, index, id, s_araddr, s_arlen+1, s_arsize, kind);
    check(rq_count[id]<QD,"UH.BENCH.QUEUE");
    rq[id*QD+(rq_head[id]+rq_count[id])%QD]=index; rq_count[id]=rq_count[id]+1;
    rd_order[rd_total]=index; rd_total=rd_total+1;
    t_issue[index]=cycle; t_beats[index]=0; accepted=accepted+1;
    count=e_len(e)+1; size=e_size(e); burst=e_burst(e); start=e_addr(e);
    if(kind==K_UH) begin
      for(k=0;k<count;k=k+1) begin
        lo=beat_lo(start,k,size,count,burst); hi=beat_hi(lo,size);
        for(b=lo;b!=hi;b=b+1) expected_reads[b[15:0]]=expected_reads[b[15:0]]+1;
      end
      expected_gets=expected_gets+e_low(e);
      for(p=0;p<rq_count[id]-1;p=p+1)
        if(e_kind(tape[rq[id*QD+(rq_head[id]+p)%QD]])==K_MISS) c_uh_after_miss=c_uh_after_miss+1;
      uh_reads_out=uh_reads_out+1;
      if(burst==2) c_wrap=c_wrap+1; else c_incr=c_incr+1;
      if(burst==1 && (start & ((32'd1<<size)-1))!=0) c_unaligned=c_unaligned+1;
      if((1<<size)<LANES) c_narrow=c_narrow+1; else c_full=c_full+1;
      if((count<<size)>c_max_bytes) c_max_bytes=count<<size;
    end
    if(kind==K_MISS) begin
      c_miss_r=c_miss_r+1;
      if(uh_reads_out>=2) c_miss_two=c_miss_two+1;
      if(tl_gets>=2) c_miss_gets=c_miss_gets+1;
      if(miss_out>=1) c_miss_pair=c_miss_pair+1;
      if(tl_inflight==CAPACITY) c_miss_full=c_miss_full+1;
      bad=0;
      for(p=0;p<rq_count[id]-1;p=p+1)
        if(e_kind(tape[rq[id*QD+(rq_head[id]+p)%QD]])==K_UH) bad=1;
      if(bad) c_miss_same=c_miss_same+1;
      miss_out=miss_out+1;
    end
  end
  if(s_awvalid && s_awready) begin
    last_progress=cycle; aw_fired=1; index=aw_index; e=tape[index]; id=s_awid; kind=e_kind(e);
    check(bq_count[id]<QD && wq_count<1024 && uw_count<1024,"UH.BENCH.QUEUE");
    bq[id*QD+(bq_head[id]+bq_count[id])%QD]=index; bq_count[id]=bq_count[id]+1;
    wq[(wq_head+wq_count)%1024]=index; wq_count=wq_count+1;
    t_issue[index]=cycle; t_beats[index]=0; accepted=accepted+1;
    if(writes_out>0 && last_aw_id!=id) c_aw_overlap=c_aw_overlap+1;
    writes_out=writes_out+1; last_aw_id=id;
    if(kind==K_UH) begin
      uw[(uw_head+uw_count)%1024]=index; uw_count=uw_count+1;
      expected_puts_low=expected_puts_low+e_low(e); expected_puts_high=expected_puts_high+e_high(e);
    end
    if(kind==K_MISS) begin
      c_miss_w=c_miss_w+1;
      if(uh_reads_out>=2) c_miss_two=c_miss_two+1;
      if(tl_gets>=2) c_miss_gets=c_miss_gets+1;
      if(miss_out>=1) c_miss_pair=c_miss_pair+1;
      if(tl_inflight==CAPACITY) c_miss_full=c_miss_full+1;
      miss_out=miss_out+1;
    end
  end
  if(s_wvalid && s_wready) begin
    last_progress=cycle; w_fired=1; e=w_entry; kind=e_kind(e);
    lo=beat_lo(e_addr(e),w_beat,e_size(e),w_total,e_burst(e)); hi=beat_hi(lo,e_size(e));
    for(b=lo;b!=hi;b=b+1) begin
      lane=b & (LANES-1);
      if(s_wstrb[lane]) begin
        if(kind==K_UH) begin
          check(wf_count<WF,"UH.BENCH.QUEUE");
          wf_addr[(wf_head+wf_count)%WF]=b; wf_data[(wf_head+wf_count)%WF]=s_wdata[lane*8+:8];
          wf_count=wf_count+1; expected_memory[b[15:0]]=s_wdata[lane*8+:8];
        end
        if(kind==K_Q) q_expected[b[11:0]]=s_wdata[lane*8+:8];
      end
    end
    t_beats[w_index]=t_beats[w_index]+1;
    if(s_wlast) t_wlast[w_index]=1;
    if(kind==K_MISS && uh_reads_mid>=1) c_miss_w_between=c_miss_w_between+1;
  end

  // Source responses.
  if(s_rvalid && s_rready) begin
    last_progress=cycle; id=s_rid; c_r_beats=c_r_beats+1;
    if(rq_count[id]==0) begin
      $display("READ id=%0d has no pending read", id);
      nlv_fail_report("UH.R.ID","interleave",id);
    end
    index=rq[id*QD+rq_head[id]]; e=tape[index]; kind=e_kind(e); k=t_beats[index];
    if(trace && (k==0 || k==e_len(e))) $display("TRACE R cycle=%0d transaction=%0d id=%0d beat=%0d", cycle, index, id, k);
    count=e_len(e)+1; size=e_size(e); burst=e_burst(e); last=k==count-1;
    lo=beat_lo(e_addr(e),k,size,count,burst); hi=beat_hi(lo,size);
    failed=kind==K_UH && failed_range(lo,hi);
    bad=0;
    if(kind==K_MISS || failed) bad=s_rdata!=0;
    else for(b=lo;b!=hi;b=b+1) begin
      lane=b & (LANES-1);
      if(s_rdata[lane*8+:8]!=(kind==K_UH ? expected_memory[b[15:0]] : q_expected[b[11:0]])) bad=1;
    end
    resp=kind==K_MISS ? 3 : failed ? 2 : 0;
    if(bad || s_rresp!=resp || s_rlast!=last)
      $display("READ transaction=%0d id=%0d address=%h beat=%0d of %0d size=%0d burst=%0d kind=%0d data=%h resp=%0d expected_resp=%0d last=%0d",
               index, id, e_addr(e), k, count, size, burst, kind, s_rdata, s_rresp, resp, s_rlast);
    if(s_rresp!=resp) nlv_fail_report("UH.R.RESP","interleave",index);
    if(bad) nlv_fail_report(kind==K_MISS ? "UH.R.MISS_DATA" : "UH.R.DATA","interleave",index);
    if(s_rlast!=last) nlv_fail_report("UH.R.LAST","interleave",index);
    if(failed) begin
      c_r_error=c_r_error+1;
      if(prev_r>=0 && prev_r!=index) c_error_switch=c_error_switch+1;
    end
    if(prev_r>=0 && prev_r!=index) begin
      c_switch=c_switch+1;
      if(e_kind(tape[prev_r])==K_UH && kind==K_UH && e_low(tape[prev_r])>=2 && e_low(e)>=2) begin
        c_cross=c_cross+1;
        if(k>0) c_resume=c_resume+1;
      end
      if(e_kind(tape[prev_r])==K_UH && kind==K_MISS) c_miss_r_between=c_miss_r_between+1;
    end
    prev_r=last ? -1 : index;
    if(kind==K_UH && k==0 && !last) uh_reads_mid=uh_reads_mid+1;
    t_beats[index]=k+1;
    if(last) begin
      while(rd_oldest<rd_total && t_done[rd_order[rd_oldest]]) rd_oldest=rd_oldest+1;
      if(rd_order[rd_oldest]!=index) begin
        c_later=c_later+1;
        if(kind==K_UH && e_kind(tape[rd_order[rd_oldest]])==K_UH) c_later_uh=c_later_uh+1;
      end
      t_done[index]=1; completed=completed+1; c_reads=c_reads+1;
      rq_head[id]=(rq_head[id]+1)%QD; rq_count[id]=rq_count[id]-1;
      if(kind==K_UH) begin
        uh_reads_out=uh_reads_out-1; if(count>1) uh_reads_mid=uh_reads_mid-1;
        uh_cover[e[27:24]]=uh_cover[e[27:24]]-1;
      end
      if(kind==K_Q) q_cover=q_cover-1;
      if(kind==K_MISS) miss_out=miss_out-1;
    end
  end
  if(s_bvalid && s_bready) begin
    last_progress=cycle; id=s_bid;
    if(bq_count[id]==0) begin
      $display("WRITE response id=%0d has no pending write", id);
      nlv_fail_report("UH.B.ID","interleave",id);
    end
    index=bq[id*QD+bq_head[id]]; e=tape[index]; kind=e_kind(e);
    if(!t_wlast[index] || s_bresp!=(kind==K_MISS ? 2'd3 : 2'd0) || (kind==K_UH && uw[uw_head]!=index))
      $display("WRITE transaction=%0d id=%0d address=%h kind=%0d resp=%0d data_done=%0d oldest_target_write=%0d",
               index, id, e_addr(e), kind, s_bresp, t_wlast[index], uw[uw_head]);
    if(!t_wlast[index]) nlv_fail_report("UH.B.EARLY","interleave",index);
    if(s_bresp!=(kind==K_MISS ? 2'd3 : 2'd0)) nlv_fail_report("UH.B.RESP","interleave",index);
    if(kind==K_UH) begin
      if(uw[uw_head]!=index) nlv_fail_report("UH.B.ORDER","interleave",index);
      uw_head=(uw_head+1)%1024; uw_count=uw_count-1;
      uh_cover[e[27:24]]=uh_cover[e[27:24]]-1;
    end
    if(kind==K_Q) q_cover=q_cover-1;
    if(kind==K_MISS) begin
      miss_out=miss_out-1;
      if(uh_reads_mid>=1) c_miss_b_between=c_miss_b_between+1;
    end
    t_done[index]=1; completed=completed+1; c_writes=c_writes+1; writes_out=writes_out-1;
    bq_head[id]=(bq_head[id]+1)%QD; bq_count[id]=bq_count[id]-1;
  end

  // Target messages.
  if(t_d_valid && t_d_ready) begin
    last_progress=cycle; d_fired=1; d_left=d_left-1; d_cursor=d_cursor+4;
    if(d_left==0) begin
      if(trace) $display("TRACE D cycle=%0d source=%0d", cycle, d_source_now);
      m_busy[d_source_now]=0; tl_inflight=tl_inflight-1;
      if(m_get[d_source_now]) tl_gets=tl_gets-1;
      d_source_now=-1;
    end
  end
  if(t_a_valid && t_a_ready) begin
    last_progress=cycle;
    if(put_left==0) begin
      s=t_a_source; messages=messages+1; count=1<<t_a_size;
      if(trace) $display("TRACE A cycle=%0d opcode=%0d source=%0d address=%h size=%0d inflight=%0d",
                         cycle, t_a_opcode, s, t_a_address, t_a_size, tl_inflight);
      if(s>=CAPACITY || m_busy[s] || t_a_address>=MEM || t_a_address+count>MEM ||
         uh_cover[t_a_address[15:12]]==0 || (t_a_opcode!=4 && t_a_opcode!=0 && t_a_opcode!=1))
        $display("TARGET message opcode=%0d source=%0d address=%h size=%0d busy=%0d expected_page=%0d",
                 t_a_opcode, s, t_a_address, t_a_size, s<CAPACITY ? m_busy[s] : 1'b0,
                 t_a_address<MEM ? uh_cover[t_a_address[15:12]] : 0);
      if(s>=CAPACITY || m_busy[s]) nlv_fail_report("UH.TL.SOURCE","interleave",s);
      if(t_a_address>=MEM || t_a_address+count>MEM) nlv_fail_report("UH.TL.RANGE","interleave",t_a_address);
      if(uh_cover[t_a_address[15:12]]==0) nlv_fail_report("UH.TL.UNEXPECTED","interleave",t_a_address);
      if(t_a_opcode!=4 && t_a_opcode!=0 && t_a_opcode!=1) nlv_fail_report("UH.TL.OPCODE","interleave",t_a_opcode);
      if(count>MAXIMUM) nlv_fail_report("UH.TL.SIZE","interleave",count);
      m_busy[s]=1; m_addr[s]=t_a_address; m_size[s]=t_a_size; m_order[s]=arrivals; arrivals=arrivals+1;
      tl_inflight=tl_inflight+1; m_owed[s]=0;
      rnd_l=xs(rnd_l); m_at[s]=cycle+(d_latency==0 ? 0 : rnd_l[15:0]%(d_latency+1));
      if(t_a_opcode==4) begin
        gets=gets+1; m_get[s]=1; tl_gets=tl_gets+1; m_owed[s]=1;
        for(b=0;b<count;b=b+1) actual_reads[t_a_address[15:0]+b]=actual_reads[t_a_address[15:0]+b]+1;
      end else begin
        puts=puts+1; m_get[s]=0; put_source=s; put_cursor=t_a_address & ~32'd3;
        put_left=count<=4 ? 1 : count/4; put_acked=0;
        if(early_ack && put_left>1 && rnd_l[16]) begin m_owed[s]=1; put_acked=1; end
      end
    end
    if(t_a_opcode!=4) begin
      for(b=0;b<4;b=b+1) begin
        if(t_a_mask[b]) begin
          if(wf_count==0 || wf_addr[wf_head]!=put_cursor+b || wf_data[wf_head]!=t_a_data[b*8+:8])
            $display("TARGET write address=%h data=%h expected_address=%h expected_data=%h pending=%0d",
                     put_cursor+b, t_a_data[b*8+:8], wf_addr[wf_head], wf_data[wf_head], wf_count);
          if(wf_count==0) nlv_fail_report("UH.W.EXTRA","interleave",put_cursor+b);
          if(wf_addr[wf_head]!=put_cursor+b) nlv_fail_report("UH.W.ORDER","interleave",put_cursor+b);
          if(wf_data[wf_head]!=t_a_data[b*8+:8]) nlv_fail_report("UH.W.DATA","interleave",put_cursor+b);
          wf_head=(wf_head+1)%WF; wf_count=wf_count-1;
          memory[put_cursor[15:0]+b]=t_a_data[b*8+:8];
        end else if(t_a_data[b*8+:8]!=0) nlv_fail_report("UH.W.UNSELECTED","interleave",put_cursor+b);
      end
      put_cursor=put_cursor+4; put_left=put_left-1;
      if(put_left==0 && !put_acked) m_owed[put_source]=1;
    end
  end

  // Second target.
  if(q_arvalid && q_arready) begin
    last_progress=cycle; c_q_reads=c_q_reads+1;
    if(q_cover==0) nlv_fail_report("UH.Q.UNEXPECTED","interleave",q_araddr);
    p=(qr_head+qr_count)%16; qr_addr[p]=q_araddr; qr_len[p]=q_arlen; qr_size[p]=q_arsize;
    qr_burst[p]=q_arburst; qr_id[p]=q_arid; qr_count=qr_count+1;
  end
  if(q_rvalid && q_rready) begin
    last_progress=cycle; qr_fired=1;
    if(q_rlast) begin qr_beat=0; qr_head=(qr_head+1)%16; qr_count=qr_count-1; end
    else qr_beat=qr_beat+1;
  end
  if(q_awvalid && q_awready) begin
    last_progress=cycle; c_q_writes=c_q_writes+1;
    if(q_cover==0) nlv_fail_report("UH.Q.UNEXPECTED","interleave",q_awaddr);
    p=(qw_head+qw_count)%16; qw_addr[p]=q_awaddr; qw_len[p]=q_awlen; qw_size[p]=q_awsize;
    qw_burst[p]=q_awburst; qw_id[p]=q_awid; qw_count=qw_count+1;
  end
  if(q_wvalid && q_wready) begin
    last_progress=cycle;
    lo=beat_lo(qw_addr[qw_head],qw_beat,qw_size[qw_head],qw_len[qw_head]+1,qw_burst[qw_head]);
    hi=beat_hi(lo,qw_size[qw_head]);
    for(b=lo;b!=hi;b=b+1) if(q_wstrb[b & 3]) q_memory[b[11:0]]=q_wdata[(b & 3)*8+:8];
    if(q_wlast!=(qw_beat==qw_len[qw_head])) nlv_fail_report("UH.Q.WLAST","interleave",qw_beat);
    if(q_wlast) begin
      qb_id[(qb_head+qb_count)%16]=qw_id[qw_head]; qb_count=qb_count+1;
      qw_beat=0; qw_head=(qw_head+1)%16; qw_count=qw_count-1;
    end else qw_beat=qw_beat+1;
  end
  if(q_bvalid && q_bready) begin
    last_progress=cycle; qb_fired=1; qb_head=(qb_head+1)%16; qb_count=qb_count-1;
  end

  if(cycle-last_progress>STALL_LIMIT || cycle>CYCLE_LIMIT) report_stuck("UH.WATCHDOG");
end

task barrier;
  integer b;
  begin
    if(wf_count!=0)
      $display("TARGET write bytes missing=%0d next_address=%h", wf_count, wf_addr[wf_head]);
    check(wf_count==0,"UH.W.MISSING");
    for(b=0;b<MEM;b=b+1) begin
      if(memory[b]!=expected_memory[b] || actual_reads[b]!=expected_reads[b])
        $display("TARGET byte=%h data=%h expected=%h reads=%0d expected_reads=%0d batch=%0d",
                 b, memory[b], expected_memory[b], actual_reads[b], expected_reads[b], batch_no);
      check(memory[b]==expected_memory[b],"UH.MEMORY");
      check(actual_reads[b]==expected_reads[b],"UH.READ.FOOTPRINT");
    end
    for(b=0;b<QMEM;b=b+1) begin
      if(q_memory[b]!=q_expected[b])
        $display("OTHER byte=%h data=%h expected=%h batch=%0d", b, q_memory[b], q_expected[b], batch_no);
      check(q_memory[b]==q_expected[b],"UH.Q.MEMORY");
    end
    if(gets!=expected_gets || puts<expected_puts_low || puts>expected_puts_high)
      $display("TARGET messages gets=%0d expected=%0d puts=%0d expected=%0d..%0d batch=%0d",
               gets, expected_gets, puts, expected_puts_low, expected_puts_high, batch_no);
    check(gets==expected_gets,"UH.MSG.GETS");
    check(puts>=expected_puts_low && puts<=expected_puts_high,"UH.MSG.PUTS");
    check(!s_rvalid && !s_bvalid && !t_a_valid && !q_arvalid && !q_awvalid && !q_wvalid,"UH.IDLE");
    check(uh_reads_out==0 && miss_out==0 && writes_out==0 && q_cover==0,"UH.BENCH.COUNT");
  end
endtask

task need(input integer bit_index, input integer value, input [255:0] name);
  if(header[36+bit_index] && value==0) begin
    $display("COVERAGE %0s was not reached", name);
    nlv_fail_report("UH.COVER","interleave",bit_index);
  end
endtask

initial begin : main
  integer b, p, index;
  reg [159:0] e;
  if(!$value$plusargs("TAPE=%s",tape_name)) begin
    $display("ASSERT_FAIL:UH.BENCH.TAPE missing +TAPE"); $fatal(1);
  end
  if(!$value$plusargs("ROWS=%d",rows) || rows<3 || rows>MAXT) begin
    $display("ASSERT_FAIL:UH.BENCH.TAPE missing +ROWS"); $fatal(1);
  end
  trace=$test$plusargs("TRACE");
  $readmemh(tape_name,tape,0,rows-1);
  header=tape[0];
  if(header[3:0]!=4) begin $display("ASSERT_FAIL:UH.BENCH.TAPE bad header"); $fatal(1); end
  rnd_a=header[35:4] ^ 32'h1b873593; rnd_d=header[35:4] ^ 32'hcc9e2d51; rnd_r=header[35:4] ^ 32'h85ebca6b;
  rnd_b=header[35:4] ^ 32'hc2b2ae35; rnd_w=header[35:4] ^ 32'h27d4eb2f; rnd_q=header[35:4] ^ 32'h165667b1;
  rnd_l=header[35:4] ^ 32'h9e3779b9;
  for(b=0;b<MEM;b=b+1) begin
    memory[b]=initial_byte(b); expected_memory[b]=initial_byte(b); actual_reads[b]=0; expected_reads[b]=0;
  end
  for(b=0;b<QMEM;b=b+1) begin q_memory[b]=initial_byte(b+32'h20000)^8'h3c; q_expected[b]=q_memory[b]; end
  for(b=0;b<16;b=b+1) begin m_busy[b]=0; m_owed[b]=0; m_get[b]=0; uh_cover[b]=0; end
  for(b=0;b<NID;b=b+1) begin rq_head[b]=0; rq_count[b]=0; bq_head[b]=0; bq_count[b]=0; end
  for(b=0;b<MAXT;b=b+1) begin t_done[b]=0; t_wlast[b]=0; t_issue[b]=-1; t_beats[b]=0; end
  repeat(160) @(negedge clk);
  reset_n=1;
  repeat(20) @(negedge clk);
  index=1;
  while(e_op(tape[index])==3) begin
    e=tape[index];
    @(negedge clk);
    a_density=e[11:4]; d_density=e[19:12]; d_latency=e[27:20]; d_order=e[29:28];
    r_density=e[37:30]; b_density=e[45:38]; w_density=e[53:46]; hold_mode=e[55:54];
    early_ack=e[56]; hold_extra=e[64:57]*4; q_density=e[72:65];
    failed_count=0; batch_lo=index+1;
    while(e_op(tape[batch_lo])==5) begin
      failed_words[failed_count]=e_addr(tape[batch_lo]); failed_count=failed_count+1; batch_lo=batch_lo+1;
    end
    batch_hi=batch_lo;
    while(e_op(tape[batch_hi])==1 || e_op(tape[batch_hi])==2) batch_hi=batch_hi+1;
    ar_index=next_entry(batch_lo,1); aw_index=next_entry(batch_lo,2); ar_since=0; aw_since=0;
    full_seen=0; batch_start=cycle; hold_from=cycle; last_progress=cycle; batch_active=1;
    p=0;
    while(p<48) begin
      @(negedge clk);
      if(ar_index>=batch_hi && aw_index>=batch_hi && !s_arvalid && !s_awvalid && !ar_fired && !aw_fired &&
         accepted==completed && wq_count==0 && !w_busy && tl_inflight==0 && put_left==0 &&
         qr_count==0 && qw_count==0 && qb_count==0 && !t_d_valid && !q_rvalid && !q_bvalid) p=p+1;
      else p=0;
    end
    batch_active=0;
    barrier;
    $display("UH_BATCH %0d transactions=%0d cycle=%0d gets=%0d puts=%0d", batch_no, batch_hi-batch_lo, cycle, gets, puts);
    batch_no=batch_no+1; index=batch_hi;
  end
  check(e_op(tape[index])==0 && batch_no>0,"UH.BENCH.TAPE");
  $display("UH_COVER reads=%0d writes=%0d r_beats=%0d incr=%0d wrap=%0d unaligned=%0d narrow=%0d full=%0d max_bytes=%0d",
           c_reads, c_writes, c_r_beats, c_incr, c_wrap, c_unaligned, c_narrow, c_full, c_max_bytes);
  $display("UH_COVER switch=%0d cross_packet=%0d resume=%0d later_first=%0d later_first_target=%0d pool_full=%0d",
           c_switch, c_cross, c_resume, c_later, c_later_uh, c_pool_full);
  $display("UH_COVER r_stall=%0d d_gap=%0d a_stall=%0d d_reorder=%0d early_ack=%0d aw_overlap=%0d gets=%0d puts=%0d r_error=%0d error_switch=%0d",
           c_r_stall, c_d_gap, c_a_stall, c_d_reorder, c_early_ack, c_aw_overlap, gets, puts, c_r_error, c_error_switch);
  $display("UH_COVER miss_reads=%0d miss_writes=%0d miss_two_reads=%0d miss_two_gets=%0d miss_same_id=%0d miss_pair=%0d",
           c_miss_r, c_miss_w, c_miss_two, c_miss_gets, c_miss_same, c_miss_pair);
  $display("UH_COVER miss_pool_full=%0d miss_w_between=%0d miss_b_between=%0d miss_r_between=%0d target_after_miss=%0d other_reads=%0d other_writes=%0d",
           c_miss_full, c_miss_w_between, c_miss_b_between, c_miss_r_between, c_uh_after_miss,
           c_q_reads, c_q_writes);
  need(0,c_cross,"interleave across packets"); need(1,c_resume,"resumed interleave");
  need(2,c_later_uh,"later read first"); need(3,c_pool_full,"full pool");
  need(4,c_wrap,"wrap"); need(5,c_incr,"incr"); need(6,c_unaligned,"unaligned start");
  need(7,c_narrow,"narrow size"); need(8,c_full,"full size"); need(9,c_r_stall,"read backpressure");
  need(10,c_d_gap,"target response gap"); need(11,c_a_stall,"target request backpressure");
  need(12,c_aw_overlap,"writes of two ids in flight"); need(13,c_miss_two,"miss with two reads in flight");
  need(14,c_miss_w_between,"miss write between read beats"); need(15,c_miss_same,"miss with an in-flight id");
  need(16,c_miss_pair,"back-to-back misses"); need(17,c_miss_full,"miss with a full pool");
  need(18,c_d_reorder,"reordered target responses"); need(19,c_miss_gets,"miss with two target reads");
  need(20,c_writes,"writes"); need(21,c_q_reads+c_q_writes,"second target access");
  need(22,c_r_error,"corrupt target word");
  $display("UH_PASS interleave seed=%0d batches=%0d reads=%0d writes=%0d messages=%0d cycle=%0d",
           header[35:4], batch_no, c_reads, c_writes, messages, cycle);
  $finish;
end
