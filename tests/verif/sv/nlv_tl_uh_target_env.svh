// Environment of a TL-UH target behind a full network. AXI4 masters drive
// constrained random bursts. A TileLink slave model answers them from its own
// memory. The scoreboard keeps an expected memory and one record for each
// transaction. It reads no network state. The clock gate monitor reads only the
// control nets that the map names.
// The driver supplies NS, DW, LANES, LSZ, K, MAXB, SRCW, TIMEOUT, CDC, NG, IG, TG,
// TGP, the port glue, the gate probes, g_kind and the pool probes.
localparam GT=64, MEM=65536;
integer SEED=1, PM=10000, PF=14000, GOAL=0, LONG=48, BURSTS=24, PHASE_M=0, PHASE_F=0;
reg ready_go=0, started=0;
reg [31:0] rs_g=1, rs_r=1, rs_s=1, rs_c=1;
function [31:0] xs(input [31:0] v);
  reg [31:0] t;
  begin t=v; t=t^(t<<13); t=t^(t>>17); t=t^(t<<5); xs=t; end
endfunction
function integer rg(input integer n); begin rs_g=xs(rs_g); rg=rs_g[30:0]%n; end endfunction
function integer rr(input integer n); begin rs_r=xs(rs_r); rr=rs_r[30:0]%n; end endfunction
function integer rsl(input integer n); begin rs_s=xs(rs_s); rsl=rs_s[30:0]%n; end endfunction
function integer rc(input integer n); begin rs_c=xs(rs_c); rc=rs_c[30:0]%n; end endfunction
initial begin
  if(!$value$plusargs("SEED=%d",SEED)) SEED=1;
  if(!$value$plusargs("PM=%d",PM)) PM=10000;
  if(!$value$plusargs("PF=%d",PF)) PF=14000;
  if(!$value$plusargs("GOAL=%d",GOAL)) GOAL=0;
  if(!$value$plusargs("LONG=%d",LONG)) LONG=48;
  if(!$value$plusargs("BURSTS=%d",BURSTS)) BURSTS=24;
  rs_c=SEED*32'h9e3779b1+32'h7f4a7c15; rs_g=rs_c^32'h51ed270b; rs_r=rs_c^32'h2545f491;
  rs_s=rs_c^32'h6c078965;
  if(rs_c==0) rs_c=1; if(rs_g==0) rs_g=2; if(rs_r==0) rs_r=3; if(rs_s==0) rs_s=4;
  // Each seed gets its own phase offset on both clocks.
  if(!$value$plusargs("PHASE_M=%d",PHASE_M)) PHASE_M=1+rc(PM);
  if(!$value$plusargs("PHASE_F=%d",PHASE_F)) PHASE_F=1+rc(PF);
  if(!CDC) PF=PM;
  ready_go=1;
end
initial begin wait(ready_go); #(PHASE_M); forever begin clk=1; #(PM/2); clk=0; #(PM-PM/2); end end
initial begin wait(ready_go); #(PHASE_F); forever begin clk_far=1; #(PF/2); clk_far=0; #(PF-PF/2); end end
wire fclk=CDC ? clk_far : clk;
wire [63:0] slow_ps=PM>PF ? PM : PF;

// ---- failure reports ----
integer mcycle=0, fcycle=0;
task nlv_fail_report(input [255:0] rule_name, input [255:0] who, input [63:0] value);
  begin
    $display("ASSERT_FAIL:%0s %0s value=%0d main_cycle=%0d target_cycle=%0d time=%0t",
             rule_name,who,value,mcycle,fcycle,$time);
    $fatal(1);
  end
endtask
task fail(input [255:0] label, input integer value);
  nlv_fail_report(label,"uh_target",value);
endtask
task check(input ok, input [255:0] label);
  if(!ok) nlv_fail_report(label,"uh_target",0);
endtask

// ---- expected state ----
reg [7:0] mem[0:MEM-1], exp_mem[0:MEM-1];
integer owner[0:MEM-1], rmark[0:MEM-1], wmark[0:MEM-1];
reg tx_live[0:GT-1];
integer tx_m[0:GT-1], tx_kind[0:GT-1], tx_id[0:GT-1], tx_addr[0:GT-1], tx_len[0:GT-1];
integer tx_size[0:GT-1], tx_uid[0:GT-1], tx_lo[0:GT-1], tx_hi[0:GT-1], tx_smode[0:GT-1];
integer tx_state[0:GT-1], tx_beat[0:GT-1], tx_gseq[0:GT-1], tx_seq[0:GT-1], tx_dly[0:GT-1];
integer tx_rbytes[0:GT-1], tx_wbytes[0:GT-1], tx_wexp[0:GT-1], tx_err[0:GT-1];
integer tx_msgs[0:GT-1], tx_mexp[0:GT-1], tx_order[0:GT-1];
integer tx_bypass[0:GT-1], tx_chain_m[0:GT-1], tx_chain_w[0:GT-1], tx_young[0:GT-1];
integer tx_sf[0:GT-1], tx_sm[0:GT-1];
time tx_tacc[0:GT-1], tx_budget[0:GT-1];
integer uid_next=1, pending=0, accepted_now=0, order_next=0, outstanding=0;
integer live_count[0:NS-1], gen_seq[0:NS-1], aseq[0:NS-1], budget[0:NS-1], gen_dly[0:NS-1];
integer ar_cur[0:NS-1], aw_cur[0:NS-1], w_cur[0:NS-1], ar_dly[0:NS-1], aw_dly[0:NS-1];
integer w_dly[0:NS-1], r_stall[0:NS-1], b_stall[0:NS-1], r_wait[0:NS-1], b_wait[0:NS-1];
integer w_wait[0:NS-1];
reg ar_fired[0:NS-1], aw_fired[0:NS-1], w_fired[0:NS-1];
// Stimulus and backpressure controls. The scenario sets them.
integer k_wr_pct=40, k_len_max=8, k_long_pct=0, k_gap=3, k_inflight=24, k_ids=16;
integer k_narrow_pct=25, k_unaligned_pct=25, k_sparse_pct=40;
integer SA=2, SD=2, SR=2, SB=2, SW=2, p_stall=64, F=4, d_policy=0, early_pct=30;
reg hold_d=0, hold_a=0, poison_on=1, mon_fair=0, wd_pause=0, chk_msgs=1;
// Largest stall in force during this run. The fairness bounds use it.
integer SA_top=2, SD_top=2, SR_top=2, SB_top=2, SW_top=2;
time last_progress=0;
integer n_reads=0, n_writes=0, n_rbeats=0, n_wbeats=0, n_msgs=0, n_gets=0, n_puts=0;
integer n_err_reads=0, n_err_writes=0, n_resets=0;
time max_latency=0;

function poison(input integer a);
  poison=poison_on && ((a>>8)&15)==14 && ((a>>2)%5)==(SEED%5);
endfunction
function [7:0] wbyte(input integer uid, input integer a);
  wbyte=(a*37+uid*101+(a>>8)*11+93)&255;
endfunction
function strobe(input integer g, input integer a);
  begin
    if(a<tx_lo[g] || a>=tx_hi[g]) strobe=0;
    else if(tx_smode[g]==1) strobe=((a+tx_uid[g])%3)!=0;
    // Mode 2 leaves whole words without a strobe.
    else if(tx_smode[g]==2) strobe=(((a>>2)+tx_uid[g])%2)==0 && (a%5)!=1;
    else strobe=1;
  end
endfunction
// Aligned power-of-two blocks that cover [lo, hi), each within the limit.
function integer blocks(input integer lo, input integer hi);
  integer a, s, n;
  begin
    n=0; a=lo;
    while(a<hi) begin
      s=MAXB;
      while((a%s)!=0 || a+s>hi) s=s/2;
      a=a+s; n=n+1;
    end
    blocks=n;
  end
endfunction
function integer words_of(input integer g);
  words_of=((tx_hi[g]+3)/4)-(tx_lo[g]/4);
endfunction
// Latency bound. One word of a transaction needs a handshake on A or D at
// the target and a handshake on R, B or W at its master. A handshake waits
// for the longest stall of its channel and one more cycle, so a word costs at
// most SA+SD+2 target cycles and SR+SB+SW+3 master cycles. Each message
// costs one more A and D turn. C_WORD, C_MSG and C_TXN cycles of both clocks
// are allowances of this bench for the pipeline, the message turn and the
// two clock crossings with their gate wakes. A transaction can wait for
// every transaction that is in flight with it. Its bound is the sum of these
// costs over itself, the transactions accepted before it, and the younger
// ones that the starvation bound lets pass. See on_accept.
localparam C_WORD=4, C_MSG=8, C_TXN=96;
function [63:0] cost(input integer g);
  reg [63:0] word_ps, msg_ps;
  integer messages;
  begin
    word_ps=(SA_top+SD_top+2)*PF+(SR_top+SB_top+SW_top+3)*PM+C_WORD*(PM+PF);
    msg_ps=(SA_top+SD_top+2)*PF+C_MSG*(PM+PF);
    messages=tx_size[g]<2 ? tx_hi[g]-tx_lo[g] : words_of(g);
    cost=words_of(g)*word_ps+messages*msg_ps+C_TXN*(PM+PF);
  end
endfunction

task make_txn(input integer m, input integer kind, input integer id, input integer addr,
              input integer len, input integer size, input integer smode, input integer dly,
              output integer gout);
  integer g, b, sz, lo, hi, hlo, hhi, okk;
  begin
    gout=-1; g=-1;
    for(b=0;b<GT;b=b+1) if(g<0 && !tx_live[b]) g=b;
    sz=1<<size; lo=addr; hi=(addr & ~(sz-1))+len*sz; hlo=lo & ~3; hhi=(hi+3) & ~3;
    okk=g>=0 && hi<=MEM && hi<=(addr & ~4095)+4096;
    if(okk) for(b=hlo;b<hhi;b=b+1) if(owner[b]!=-1) okk=0;
    if(okk) begin
      tx_live[g]=1; tx_m[g]=m; tx_kind[g]=kind; tx_id[g]=id; tx_addr[g]=addr; tx_len[g]=len;
      tx_size[g]=size; tx_uid[g]=uid_next; uid_next=uid_next+1; tx_lo[g]=lo; tx_hi[g]=hi;
      tx_smode[g]=kind ? smode : 0; tx_state[g]=0; tx_beat[g]=0; tx_dly[g]=dly;
      tx_gseq[g]=gen_seq[m]; gen_seq[m]=gen_seq[m]+1; tx_seq[g]=-1;
      tx_rbytes[g]=0; tx_wbytes[g]=0; tx_wexp[g]=0; tx_err[g]=0; tx_msgs[g]=0;
      tx_mexp[g]=blocks(lo,hi); tx_bypass[g]=0; tx_young[g]=0;
      for(b=hlo;b<hhi;b=b+1) owner[b]=g;
      if(kind) for(b=lo;b<hi;b=b+1) if(strobe(g,b)) begin
        tx_wexp[g]=tx_wexp[g]+1;
        // A poisoned word keeps its old content and fails the write.
        if(poison(b)) tx_err[g]=1; else exp_mem[b]=wbyte(tx_uid[g],b);
      end
      live_count[m]=live_count[m]+1; pending=pending+1; gout=g;
    end
  end
endtask
task random_txn(input integer m);
  integer kind, size, len, addr, sz, id, smode, g, tries, page, room;
  reg [15:0] busy_ids;
  begin
    kind=rg(100)<k_wr_pct;
    size=rg(100)<k_narrow_pct ? rg(LSZ+1) : LSZ;
    sz=1<<size;
    len=rg(100)<k_long_pct ? 1+rg(256) : 1+rg(k_len_max);
    id=rg(k_ids);
    // With all 16 IDs in use, a read takes an ID that no pending read of its
    // master holds, so that the reads do not wait for each other.
    if(!kind && k_ids>=16) begin
      busy_ids=0;
      for(g=0;g<GT;g=g+1) if(tx_live[g] && tx_m[g]==m && tx_kind[g]==0) busy_ids[tx_id[g]]=1;
      for(tries=0;tries<16 && busy_ids[id];tries=tries+1) id=(id+1)%16;
    end
    smode=rg(100)<k_sparse_pct ? 1+rg(2) : 0;
    g=-1;
    for(tries=0;tries<6 && g<0;tries=tries+1) begin
      // Each master writes only its own share of the 16 pages. Reads go anywhere.
      page=kind ? m*(16/NS)+rg(16/NS) : rg(16);
      room=4096-len*sz;
      if(room<0) begin len=4096/sz; room=0; end
      addr=page*4096+(room==0 ? 0 : (rg(room+1) & ~(sz-1)));
      if(rg(100)<k_unaligned_pct && sz>1 && len>1) addr=addr+1+rg(sz-1);
      make_txn(m,kind,id,addr,len,size,smode,rg(k_gap+1),g);
    end
    if(g>=0 && budget[m]>0) budget[m]=budget[m]-1;
  end
endtask
// A directed transaction. The master offers it on the next falling edge.
task force_txn(input integer m, input integer kind, input integer id, input integer addr,
               input integer len, input integer size, output integer g);
  begin
    make_txn(m,kind,id,addr,len,size,0,0,g);
    check(g>=0,"DIRECTED_RANGE_BUSY");
  end
endtask

// ---- fairness accounting ----
// Starvation bound for transaction g, in completions of younger transactions.
//   chain_m = messages that g and the older transactions it must follow can
//             send. Each message waits for at most K-1 other A grants under
//             round robin and for at most F+K-1 other D messages at the slave.
//   chain_w = words of the same chain. One far cycle carries at most one
//             message, so each stalled cycle lets at most one other pass.
//   Both request paths can hold DUT_OUTSTANDING accepted transactions.
function integer starve_limit(input integer g);
  integer stall;
  begin
    stall=(tx_sf[g]+2)+(tx_sm[g]+3)*((PM+PF-1)/PF);
    starve_limit=2*tx_chain_m[g]*(K-1+F)+tx_chain_w[g]*stall+NS*DUT_OUTSTANDING+16;
  end
endfunction
integer cov_done_oldest=0, cov_done_newest=0, max_bypass=0, max_bypass_limit=0;
task on_accept(input integer g);
  integer h, messages;
  reg [63:0] sum;
  begin
    tx_tacc[g]=$time; tx_order[g]=order_next; order_next=order_next+1;
    accepted_now=accepted_now+1; last_progress=$time;
    tx_sf[g]=SA+SD; tx_sm[g]=SR+SB+SW;
    messages=tx_size[g]<2 ? tx_hi[g]-tx_lo[g] : words_of(g);
    tx_chain_m[g]=messages; tx_chain_w[g]=words_of(g);
    sum=cost(g);
    for(h=0;h<GT;h=h+1) if(tx_live[h] && h!=g && tx_seq[h]>=0) begin
      sum=sum+cost(h);
      // g follows older reads with its ID from its master and every older write.
      if(tx_kind[g] ? tx_kind[h]==1 : (tx_kind[h]==0 && tx_m[h]==tx_m[g] && tx_id[h]==tx_id[g]))
      begin
        tx_chain_m[g]=tx_chain_m[g]+(tx_size[h]<2 ? tx_hi[h]-tx_lo[h] : words_of(h));
        tx_chain_w[g]=tx_chain_w[g]+words_of(h);
      end
      // A younger transaction adds its cost to h while the starvation bound
      // still permits h to wait for it.
      if(tx_young[h]<starve_limit(h)+NS*DUT_OUTSTANDING) begin
        tx_budget[h]=tx_budget[h]+cost(g); tx_young[h]=tx_young[h]+1;
      end
    end
    tx_budget[g]=sum;
  end
endtask
task complete(input integer g);
  integer h, b, older, younger;
  begin
    older=0; younger=0;
    for(h=0;h<GT;h=h+1) if(tx_live[h] && h!=g && tx_seq[h]>=0) begin
      if(tx_order[h]<tx_order[g]) begin
        older=older+1; tx_bypass[h]=tx_bypass[h]+1;
        if(tx_bypass[h]>max_bypass) begin max_bypass=tx_bypass[h]; max_bypass_limit=starve_limit(h); end
        if(mon_fair && tx_bypass[h]>starve_limit(h)) begin
          $display("STARVED uid=%0d master=%0d kind=%0d id=%0d addr=%h len=%0d passed=%0d limit=%0d",
                   tx_uid[h],tx_m[h],tx_kind[h],tx_id[h],tx_addr[h],tx_len[h],tx_bypass[h],starve_limit(h));
          fail("STARVATION",tx_uid[h]);
        end
      end else younger=younger+1;
    end
    if(accepted_now>=K && accepted_now>=2) begin
      if(older==0) cov_done_oldest=cov_done_oldest+1;
      if(younger==0) cov_done_newest=cov_done_newest+1;
    end
    if($time-tx_tacc[g]>max_latency) max_latency=$time-tx_tacc[g];
    if(mon_fair && $time-tx_tacc[g]>tx_budget[g]) begin
      $display("LATE uid=%0d master=%0d kind=%0d id=%0d addr=%h len=%0d waited_ps=%0d bound_ps=%0d",
               tx_uid[g],tx_m[g],tx_kind[g],tx_id[g],tx_addr[g],tx_len[g],$time-tx_tacc[g],tx_budget[g]);
      fail("LATENCY_BOUND",tx_uid[g]);
    end
    for(b=(tx_lo[g] & ~3);b<((tx_hi[g]+3) & ~3);b=b+1) owner[b]=-1;
    tx_live[g]=0; live_count[tx_m[g]]=live_count[tx_m[g]]-1; pending=pending-1;
    accepted_now=accepted_now-1; last_progress=$time;
    if(tx_kind[g]) n_writes=n_writes+1; else n_reads=n_reads+1;
  end
endtask
task report_stuck(input [255:0] label);
  integer h, g;
  begin
    g=-1;
    for(h=0;h<GT;h=h+1) if(tx_live[h] && (g<0 || tx_uid[h]<tx_uid[g])) g=h;
    if(g>=0)
      $display("STUCK uid=%0d master=%0d kind=%0d id=%0d addr=%h len=%0d size=%0d state=%0d beats=%0d messages=%0d accepted_ps=%0d pending=%0d",
               tx_uid[g],tx_m[g],tx_kind[g],tx_id[g],tx_addr[g],tx_len[g],tx_size[g],tx_state[g],
               tx_beat[g],tx_msgs[g],tx_seq[g]>=0 ? tx_tacc[g] : 0,pending);
    $display("GATES enable=%b idle=%b wake=%b outstanding=%0d a_valid=%b d_valid=%b",
             g_en,g_idle,g_wake,outstanding,t_a_valid,t_d_valid);
    fail(label,g>=0 ? tx_uid[g] : 0);
  end
endtask

// ---- AXI masters ----
integer cov_long_r=0, cov_long_b=0, cov_long_w=0, long_mark=1000000;
task pick_oldest(input integer m, input integer kind, input integer want, output integer g);
  integer h;
  begin
    g=-1;
    for(h=0;h<GT;h=h+1)
      if(tx_live[h] && tx_m[h]==m && tx_kind[h]==kind && tx_state[h]==want &&
         (g<0 || tx_gseq[h]<tx_gseq[g])) g=h;
  end
endtask
task drive_w(input integer m, input integer g);
  integer k, sz, base, blo, bhi, b;
  begin
    k=tx_beat[g]; sz=1<<tx_size[g];
    base=(tx_addr[g] & ~(sz-1))+k*sz; blo=k==0 ? tx_addr[g] : base; bhi=base+sz;
    m_wdata[m*DW+:DW]=0; m_wstrb[m*LANES+:LANES]=0;
    for(b=blo;b<bhi;b=b+1) begin
      m_wdata[m*DW+(b%LANES)*8+:8]=wbyte(tx_uid[g],b);
      m_wstrb[m*LANES+(b%LANES)]=strobe(g,b);
    end
    m_wlast[m]=k==tx_len[g]-1; m_wvalid[m]=1;
  end
endtask
always @(negedge clk) begin : master_drive
  integer m, g, h;
  if(!reset_n) begin
    for(m=0;m<NS;m=m+1) begin
      m_arvalid[m]=0; m_awvalid[m]=0; m_wvalid[m]=0; m_rready[m]=0; m_bready[m]=0;
      ar_fired[m]=0; aw_fired[m]=0; w_fired[m]=0; ar_cur[m]=-1; aw_cur[m]=-1; w_cur[m]=-1;
    end
  end else if(started) for(m=0;m<NS;m=m+1) begin
    if(budget[m]!=0 && live_count[m]<k_inflight && pending<GT-8) begin
      if(gen_dly[m]>0) gen_dly[m]=gen_dly[m]-1;
      else begin random_txn(m); gen_dly[m]=rg(k_gap+1); end
    end
    if(ar_fired[m]) begin m_arvalid[m]=0; ar_fired[m]=0; ar_cur[m]=-1; end
    if(!m_arvalid[m]) begin
      if(ar_cur[m]<0) begin
        pick_oldest(m,0,0,g); ar_cur[m]=g;
        if(g>=0) ar_dly[m]=tx_dly[g];
      end
      g=ar_cur[m];
      if(g>=0) begin
        if(ar_dly[m]>0) ar_dly[m]=ar_dly[m]-1;
        else begin
          m_arid[m*4+:4]=tx_id[g]; m_araddr[m*32+:32]=tx_addr[g]; m_arlen[m*8+:8]=tx_len[g]-1;
          m_arsize[m*3+:3]=tx_size[g]; m_arvalid[m]=1; tx_state[g]=1;
        end
      end
    end
    if(aw_fired[m]) begin m_awvalid[m]=0; aw_fired[m]=0; aw_cur[m]=-1; end
    if(!m_awvalid[m]) begin
      if(aw_cur[m]<0) begin
        pick_oldest(m,1,0,g); aw_cur[m]=g;
        if(g>=0) aw_dly[m]=tx_dly[g];
      end
      g=aw_cur[m];
      if(g>=0) begin
        if(aw_dly[m]>0) aw_dly[m]=aw_dly[m]-1;
        else begin
          m_awid[m*4+:4]=tx_id[g]; m_awaddr[m*32+:32]=tx_addr[g]; m_awlen[m*8+:8]=tx_len[g]-1;
          m_awsize[m*3+:3]=tx_size[g]; m_awvalid[m]=1; tx_state[g]=1;
        end
      end
    end
    if(w_fired[m]) begin
      m_wvalid[m]=0; w_fired[m]=0;
      if(tx_beat[w_cur[m]]==tx_len[w_cur[m]]) w_cur[m]=-1;
      w_dly[m]=rr(256)<p_stall ? rr(SW+1) : 0;
    end
    if(!m_wvalid[m]) begin
      if(w_cur[m]<0) begin
        // W beats follow the AW order. The write must be offered first.
        g=-1;
        for(h=0;h<GT;h=h+1)
          if(tx_live[h] && tx_m[h]==m && tx_kind[h]==1 && tx_state[h]>=1 && tx_beat[h]<tx_len[h] &&
             (g<0 || tx_gseq[h]<tx_gseq[g])) g=h;
        w_cur[m]=g;
      end
      if(w_cur[m]>=0) begin
        if(w_dly[m]>0) w_dly[m]=w_dly[m]-1; else drive_w(m,w_cur[m]);
      end
    end
    // A stall ends after at most SR or SB cycles. One ready cycle follows.
    if(r_stall[m]>0) begin r_stall[m]=r_stall[m]-1; m_rready[m]=0; end
    else begin m_rready[m]=1; if(SR>0 && rr(256)<p_stall) r_stall[m]=1+rr(SR); end
    if(b_stall[m]>0) begin b_stall[m]=b_stall[m]-1; m_bready[m]=0; end
    else begin m_bready[m]=1; if(SB>0 && rr(256)<p_stall) b_stall[m]=1+rr(SB); end
  end
end
task r_beat(input integer m);
  integer g, h, k, sz, base, blo, bhi, b, err, id;
  begin
    id=m_rid[m*4+:4]; g=-1;
    for(h=0;h<GT;h=h+1)
      if(tx_live[h] && tx_m[h]==m && tx_kind[h]==0 && tx_state[h]==2 && tx_id[h]==id &&
         (g<0 || tx_seq[h]<tx_seq[g])) g=h;
    if(g<0) fail("R_WITHOUT_REQUEST",id);
    k=tx_beat[g]; sz=1<<tx_size[g];
    base=(tx_addr[g] & ~(sz-1))+k*sz; blo=k==0 ? tx_addr[g] : base; bhi=base+sz;
    err=0;
    for(b=blo;b<bhi;b=b+1) if(poison(b)) err=1;
    if(m_rresp[m*2+:2]!=(err ? 2 : 0)) begin
      $display("RRESP uid=%0d master=%0d id=%0d addr=%h beat=%0d got=%0d", tx_uid[g],m,id,tx_addr[g],k,m_rresp[m*2+:2]);
      fail("RRESP",tx_uid[g]);
    end
    if(m_rlast[m]!=(k==tx_len[g]-1)) fail("RLAST",tx_uid[g]);
    // A failed beat must return zero in every byte.
    for(b=blo;b<bhi;b=b+1)
      if(m_rdata[m*DW+(b%LANES)*8+:8]!==(err ? 8'd0 : exp_mem[b])) begin
        $display("READ_DATA uid=%0d master=%0d id=%0d addr=%h len=%0d size=%0d beat=%0d byte=%h got=%h expected=%h error=%0d data=%h",
                 tx_uid[g],m,id,tx_addr[g],tx_len[g],tx_size[g],k,b,m_rdata[m*DW+(b%LANES)*8+:8],
                 err ? 8'd0 : exp_mem[b],err,m_rdata[m*DW+:DW]);
        fail("READ_DATA",tx_uid[g]);
      end
    if(err && k==0) n_err_reads=n_err_reads+1;
    tx_beat[g]=k+1; n_rbeats=n_rbeats+1; last_progress=$time;
    if(k==tx_len[g]-1) begin
      if(tx_rbytes[g]!=tx_hi[g]-tx_lo[g]) fail("READ_FOOTPRINT",tx_uid[g]);
      if(chk_msgs && tx_msgs[g]!=tx_mexp[g]) begin
        $display("GET_COUNT uid=%0d addr=%h len=%0d size=%0d got=%0d expected=%0d",
                 tx_uid[g],tx_addr[g],tx_len[g],tx_size[g],tx_msgs[g],tx_mexp[g]);
        fail("GET_COUNT",tx_uid[g]);
      end
      complete(g);
    end
  end
endtask
task b_resp(input integer m);
  integer g, h, b, id;
  begin
    id=m_bid[m*4+:4]; g=-1;
    for(h=0;h<GT;h=h+1)
      if(tx_live[h] && tx_m[h]==m && tx_kind[h]==1 && tx_state[h]==2 &&
         (g<0 || tx_seq[h]<tx_seq[g])) g=h;
    if(g<0) fail("B_WITHOUT_REQUEST",id);
    // Write responses keep the acceptance order of their master.
    if(tx_id[g]!=id) fail("WRITE_RESPONSE_ORDER",tx_uid[g]);
    if(tx_beat[g]!=tx_len[g]) fail("B_BEFORE_LAST_W",tx_uid[g]);
    if(m_bresp[m*2+:2]!=(tx_err[g] ? 2 : 0)) begin
      $display("BRESP uid=%0d master=%0d id=%0d addr=%h len=%0d size=%0d strobes=%0d got=%0d expected=%0d",
               tx_uid[g],m,id,tx_addr[g],tx_len[g],tx_size[g],tx_smode[g],m_bresp[m*2+:2],tx_err[g] ? 2 : 0);
      fail("BRESP",tx_uid[g]);
    end
    if(tx_wbytes[g]!=tx_wexp[g]) begin
      $display("WRITE_FOOTPRINT uid=%0d addr=%h len=%0d size=%0d got=%0d expected=%0d",
               tx_uid[g],tx_addr[g],tx_len[g],tx_size[g],tx_wbytes[g],tx_wexp[g]);
      fail("WRITE_FOOTPRINT",tx_uid[g]);
    end
    for(b=tx_lo[g];b<tx_hi[g];b=b+1)
      if(strobe(g,b) && !poison(b) && mem[b]!==wbyte(tx_uid[g],b)) fail("WRITE_MEMORY",b);
    if(chk_msgs && tx_smode[g]==0 && tx_size[g]>=2 && (tx_addr[g]%4)==0 && tx_msgs[g]!=tx_mexp[g]) begin
      $display("PUT_COUNT uid=%0d addr=%h len=%0d size=%0d got=%0d expected=%0d",
               tx_uid[g],tx_addr[g],tx_len[g],tx_size[g],tx_msgs[g],tx_mexp[g]);
      fail("PUT_COUNT",tx_uid[g]);
    end
    if(tx_err[g]) n_err_writes=n_err_writes+1;
    complete(g);
  end
endtask
always @(posedge clk) begin : master_sample
  integer m, g;
  if(!reset_n) begin
    for(m=0;m<NS;m=m+1) begin r_wait[m]=0; b_wait[m]=0; w_wait[m]=0; end
  end else begin
    mcycle=mcycle+1;
    for(m=0;m<NS;m=m+1) begin
      if(m_arvalid[m] && m_arready[m]) begin
        g=ar_cur[m]; tx_state[g]=2; tx_seq[g]=aseq[m]; aseq[m]=aseq[m]+1; ar_fired[m]=1;
        on_accept(g);
      end
      if(m_awvalid[m] && m_awready[m]) begin
        g=aw_cur[m]; tx_state[g]=2; tx_seq[g]=aseq[m]; aseq[m]=aseq[m]+1; aw_fired[m]=1;
        on_accept(g);
      end
      if(m_wvalid[m] && m_wready[m]) begin
        g=w_cur[m]; tx_beat[g]=tx_beat[g]+1; w_fired[m]=1; n_wbeats=n_wbeats+1;
        last_progress=$time;
      end
      if(m_rvalid[m] && m_rready[m]) r_beat(m);
      if(m_bvalid[m] && m_bready[m]) b_resp(m);
      // The environment must release every stall within its declared bound.
      if(m_rvalid[m] && !m_rready[m]) r_wait[m]=r_wait[m]+1;
      else begin if(r_wait[m]>=long_mark) cov_long_r=cov_long_r+1; r_wait[m]=0; end
      if(m_bvalid[m] && !m_bready[m]) b_wait[m]=b_wait[m]+1;
      else begin if(b_wait[m]>=long_mark) cov_long_b=cov_long_b+1; b_wait[m]=0; end
      if(w_cur[m]>=0 && !m_wvalid[m]) w_wait[m]=w_wait[m]+1;
      else begin if(w_wait[m]>=long_mark) cov_long_w=cov_long_w+1; w_wait[m]=0; end
      check(r_wait[m]<=SR_top && b_wait[m]<=SB_top && w_wait[m]<=SW_top+1,"ENVIRONMENT_STALL_BOUND");
    end
  end
end

// ---- TileLink slave model ----
reg s_busy[0:15], s_adone[0:15], s_early[0:15], s_denied[0:15];
integer s_g[0:15], s_op[0:15], s_addr[0:15], s_size[0:15], s_dleft[0:15], s_dbeat[0:15];
integer s_age[0:15], s_abeats[0:15], s_wlo[0:15], s_whi[0:15], src_msgs[0:15];
time s_t[0:15];
integer put_left=0, put_src=0, put_addr=0, put_g=0, d_cur=-1, d_gap=0;
integer a_stall=0, a_wait=0, d_wait=0, pool_peak=0;
integer cov_long_a=0, cov_long_d=0, cov_pool_full=0, cov_served_oldest=0, cov_served_newest=0;
integer cov_rr_wrap=0, cov_early_ack=0, last_a_source=-1, full_cycles=0, busy_cycles=0;
integer max_wait_lo=0, max_wait_hi=0, cov_timeout=0, max_message=0;
reg d_fired=0, timed_out_seen=0;
function eligible(input integer s);
  eligible=s_busy[s] && s_dleft[s]>0 && (s_op[s]==4 || s_adone[s] || (s_early[s] && s_abeats[s]>0));
endfunction
task present_d(input integer s);
  integer b, a, bytes;
  begin
    t_d_valid=1; t_d_source=s; t_d_size=s_size[s]; t_d_param=0;
    t_d_denied=0; t_d_corrupt=0; t_d_data=0;
    if(s_op[s]==4) begin
      t_d_opcode=1; bytes=1<<s_size[s];
      a=(s_addr[s] & ~3)+4*s_dbeat[s];
      // Lanes outside a narrow Get carry a marker that the network must ignore.
      for(b=0;b<4;b=b+1)
        t_d_data[b*8+:8]=(a+b>=s_addr[s] && a+b<s_addr[s]+bytes) ? mem[a+b] : 8'hee;
      if(poison(a)) begin t_d_corrupt=1; t_d_data=32'hbadbadba; end
    end else begin
      t_d_opcode=0; t_d_denied=s_denied[s];
      if(!s_adone[s]) cov_early_ack=cov_early_ack+1;
    end
  end
endtask
always @(negedge fclk) begin : slave_drive
  integer s, pick, n, oldest, newest, mode, first, last;
  if(!reset_n) begin
    t_a_ready=0; t_d_valid=0; d_fired=0; d_cur=-1; a_stall=0; d_gap=0;
  end else begin
    if(a_stall>0) begin a_stall=a_stall-1; t_a_ready=0; end
    else begin t_a_ready=!hold_a; if(SA>0 && rsl(256)<p_stall) a_stall=1+rsl(SA); end
    if(d_fired) begin t_d_valid=0; d_fired=0; end
    if(!t_d_valid) begin
      if(d_cur>=0) begin
        if(d_gap>0) d_gap=d_gap-1; else present_d(d_cur);
      end else if(!hold_d) begin
        n=0; pick=-1; oldest=-1; newest=-1;
        for(s=0;s<K;s=s+1) if(eligible(s)) begin
          n=n+1;
          if(oldest<0 || s_t[s]<s_t[oldest]) oldest=s;
          if(newest<0 || s_t[s]>s_t[newest]) newest=s;
        end
        if(n>0) begin
          if(d_gap>0) d_gap=d_gap-1;
          else begin
            // Policy 1 serves the oldest message, policy 2 the newest and
            // policy 0 any. A message passed F times goes next.
            mode=d_policy==0 ? rsl(3) : d_policy;
            pick=mode==1 ? oldest : mode==2 ? newest : -1;
            if(pick<0) begin
              n=rsl(n);
              for(s=0;s<K;s=s+1) if(eligible(s)) begin if(n==0) pick=s; n=n-1; end
            end
            for(s=0;s<K;s=s+1) if(eligible(s) && s_age[s]>=F && s_age[s]>s_age[pick]) pick=s;
            // Context age is the acceptance order of the owning request.
            first=1; last=1; n=0;
            for(s=0;s<K;s=s+1) if(s_busy[s] && s!=pick) begin
              n=n+1;
              if(tx_order[s_g[s]]<tx_order[s_g[pick]]) first=0; else last=0;
            end
            if(n>0 && first) cov_served_oldest=cov_served_oldest+1;
            if(n>0 && last) cov_served_newest=cov_served_newest+1;
            for(s=0;s<K;s=s+1) if(eligible(s) && s!=pick) s_age[s]=s_age[s]+1;
            d_cur=pick; present_d(pick);
          end
        end
      end
    end
  end
end
task a_beat;
  integer s, g, h, b, bytes, a, ba;
  begin
    s=t_a_source; a=t_a_address; bytes=1<<t_a_size;
    if(put_left==0) begin
      if(s>=K) fail("SOURCE_RANGE",s);
      if(s_busy[s]) fail("SOURCE_REUSE",s);
      if(bytes>MAXB) fail("MESSAGE_LIMIT",bytes);
      g=a<MEM ? owner[a] : -1;
      if(g<0) begin
        $display("A_WITHOUT_OWNER opcode=%0d address=%h size=%0d source=%0d",t_a_opcode,a,t_a_size,s);
        fail("A_WITHOUT_OWNER",a);
      end
      if(a+bytes>((tx_hi[g]+3) & ~3) || owner[a+bytes-1]!=g) fail("MESSAGE_RANGE",tx_uid[g]);
      n_msgs=n_msgs+1; src_msgs[s]=src_msgs[s]+1; tx_msgs[g]=tx_msgs[g]+1;
      if(bytes>max_message) max_message=bytes;
      // The A grant passes from the last context to an earlier one while
      // another message is still open.
      if(last_a_source==K-1 && s<last_a_source && outstanding>0) cov_rr_wrap=cov_rr_wrap+1;
      last_a_source=s;
      s_busy[s]=1; s_g[s]=g; s_op[s]=t_a_opcode; s_addr[s]=a; s_size[s]=t_a_size;
      s_adone[s]=0; s_denied[s]=0; s_age[s]=0; s_abeats[s]=0; s_dbeat[s]=0; s_t[s]=$time;
      s_early[s]=rsl(100)<early_pct; s_wlo[s]=0; s_whi[s]=0;
      outstanding=outstanding+1;
      if(outstanding>pool_peak) pool_peak=outstanding;
      if(outstanding==K) cov_pool_full=cov_pool_full+1;
      if(t_a_opcode==4) begin
        n_gets=n_gets+1;
        if(tx_kind[g]!=0) fail("GET_FOR_WRITE",tx_uid[g]);
        if(a<tx_lo[g] || a+bytes>tx_hi[g]) begin
          $display("GET_RANGE uid=%0d address=%h size=%0d interval=%h..%h",tx_uid[g],a,t_a_size,tx_lo[g],tx_hi[g]);
          fail("GET_RANGE",tx_uid[g]);
        end
        for(b=0;b<bytes;b=b+1) begin
          if(rmark[a+b]==tx_uid[g]) fail("READ_TWICE",a+b);
          rmark[a+b]=tx_uid[g];
        end
        tx_rbytes[g]=tx_rbytes[g]+bytes;
        s_dleft[s]=bytes<=4 ? 1 : bytes/4; s_adone[s]=1;
      end else begin
        n_puts=n_puts+1;
        if(t_a_opcode!=0 && t_a_opcode!=1) fail("A_OPCODE",t_a_opcode);
        if(tx_kind[g]!=1) fail("PUT_FOR_READ",tx_uid[g]);
        // Only the earliest unfinished write of a master sends Put messages.
        for(h=0;h<GT;h=h+1)
          if(tx_live[h] && tx_kind[h]==1 && tx_m[h]==tx_m[g] && tx_seq[h]>=0 && tx_seq[g]>=0 &&
             tx_seq[h]<tx_seq[g] && tx_wbytes[h]!=tx_wexp[h]) fail("WRITE_ORDER",tx_uid[g]);
        put_left=bytes<=4 ? 1 : bytes/4; put_src=s; put_addr=a & ~3; put_g=g;
        s_dleft[s]=1;
        // The result of a poisoned word depends on the masks of all beats.
        for(b=0;b<bytes;b=b+4) if(poison(a+b)) s_early[s]=0;
      end
    end else if(s!=put_src) fail("PUT_INTERLEAVED",s);
    if(t_a_opcode!=4) begin
      g=put_g;
      for(b=0;b<4;b=b+1) begin
        ba=put_addr+b;
        if(t_a_mask[b]) begin
          if(!strobe(g,ba)) fail("PUT_MASK",ba);
          if(t_a_data[b*8+:8]!==wbyte(tx_uid[g],ba)) begin
            $display("PUT_DATA uid=%0d byte=%h got=%h expected=%h",tx_uid[g],ba,t_a_data[b*8+:8],wbyte(tx_uid[g],ba));
            fail("PUT_DATA",tx_uid[g]);
          end
          if(wmark[ba]==tx_uid[g]) fail("WRITE_TWICE",ba);
          wmark[ba]=tx_uid[g]; tx_wbytes[g]=tx_wbytes[g]+1;
          if(poison(ba)) s_denied[put_src]=1; else mem[ba]=t_a_data[b*8+:8];
        end else if(t_a_data[b*8+:8]!==8'd0) fail("UNSELECTED_ZERO",ba);
      end
      put_addr=put_addr+4; put_left=put_left-1;
      s_abeats[put_src]=s_abeats[put_src]+1;
      if(put_left==0) s_adone[put_src]=1;
    end
    last_progress=$time;
  end
endtask
always @(posedge fclk) begin : slave_sample
  integer s, lo, hi;
  if(!reset_n) begin
    a_wait=0; d_wait=0;
  end else begin
    fcycle=fcycle+1;
    if(outstanding>0) busy_cycles=busy_cycles+1;
    if(outstanding==K) full_cycles=full_cycles+1;
    if(t_d_valid && t_d_ready) begin
      s=d_cur; d_fired=1; s_dleft[s]=s_dleft[s]-1; s_dbeat[s]=s_dbeat[s]+1; s_wlo[s]=0; s_whi[s]=0;
      last_progress=$time;
      if(s_dleft[s]==0) d_cur=-1;
      d_gap=(SD>0 && rsl(256)<p_stall) ? 1+rsl(SD) : 0;
    end
    if(t_a_valid && t_a_ready) begin
      a_beat;
      s_wlo[t_a_source]=0; s_whi[t_a_source]=0;
    end
    for(s=0;s<K;s=s+1) begin
      // A message ends after its last A beat and its last D beat.
      if(s_busy[s] && s_adone[s] && s_dleft[s]==0) begin s_busy[s]=0; outstanding=outstanding-1; end
      // Watchdog oracle. lo counts cycles in which the context certainly waits
      // for the slave. hi also counts cycles in which that reading is open.
      lo=0; hi=0;
      if(t_a_valid && !t_a_ready && t_a_source==s) begin lo=1; hi=1; end
      else if(s_busy[s] && !(t_a_valid && t_a_ready && t_a_source==s)) begin
        hi=1;
        if(s_adone[s] && s_dleft[s]>0 && !(t_d_valid && d_cur==s)) lo=1;
      end
      if(t_d_valid && t_d_ready && d_cur==s) begin lo=0; hi=0; end
      s_wlo[s]=lo ? s_wlo[s]+1 : 0;
      s_whi[s]=hi ? s_whi[s]+1 : 0;
      if(s_wlo[s]>max_wait_lo) max_wait_lo=s_wlo[s];
      if(s_whi[s]>max_wait_hi) max_wait_hi=s_whi[s];
    end
    if(TIMEOUT>0) begin
      if(t_timed_out && !timed_out_seen) begin
        if(max_wait_hi<TIMEOUT-2) fail("TIMEOUT_SPURIOUS",max_wait_hi);
        timed_out_seen=1; cov_timeout=cov_timeout+1;
      end
      if(!t_timed_out && timed_out_seen) fail("TIMEOUT_NOT_HELD",0);
      if(!t_timed_out && max_wait_lo>=TIMEOUT+4) fail("TIMEOUT_MISSED",max_wait_lo);
    end
    if(t_a_valid && !t_a_ready && !hold_a) a_wait=a_wait+1;
    else begin if(a_wait>=long_mark) cov_long_a=cov_long_a+1; a_wait=0; end
    if(outstanding>0 && !t_d_valid && !hold_d) d_wait=d_wait+1;
    else begin if(d_wait>=long_mark) cov_long_d=cov_long_d+1; d_wait=0; end
    check(a_wait<=SA_top,"ENVIRONMENT_STALL_BOUND");
  end
end

// ---- context pool occupancy ----
// The map names one net that takes a packet context and one that returns it.
// Their difference counts the contexts in use. The scoreboard does not use
// it. It measures coverage and proves that the pool empties.
integer pool_count=0, pool_full_cycles=0, pool_high_cycles=0, cov_contexts_full=0;
always @(posedge fclk) begin
  if(!reset_n) pool_count=0;
  else if(POOL_PROBE && (TGP==255 || g_en[TGP])) begin
    if(pool_acquire && !pool_release) begin
      pool_count=pool_count+1;
      if(pool_count==K) cov_contexts_full=cov_contexts_full+1;
    end else if(pool_release && !pool_acquire) pool_count=pool_count-1;
    if(pool_count<0 || pool_count>K) fail("POOL_COUNT",pool_count);
  end
  if(reset_n && pool_count==K) pool_full_cycles=pool_full_cycles+1;
  if(reset_n && 4*pool_count>=3*K) pool_high_cycles=pool_high_cycles+1;
end

// ---- clock gate monitor ----
// A gate closes only while its zones are idle and no wake is present. Two
// more edges pass after sampled activity. After three quiet edges the gate
// must close.
integer gq[0:NG-1], g_closed[0:NG-1], cov_wake[0:NG-1], cov_exact[0:NG-1], cov_one[0:NG-1];
reg g_full[0:NG-1];
integer cov_full_wake=0, cov_d_init_closed=0, cov_resp_wake=0, cov_sleep_pending=0;
integer cov_gate_after_timeout=0, ig_open_age=1000;
time g_tq1[0:NG-1], g_tact[0:NG-1];
task gate_edge(input integer i);
  reg e, idl, w, act;
  begin
    e=g_en[i]; idl=g_idle[i]; w=g_wake[i]; act=!idl || w;
    if(!e && !idl) fail("GATE_CLOSED_NOT_IDLE",i);
    if(!e && w) fail("GATE_CLOSED_WITH_WAKE",i);
    if(!e && gq[i]<2) fail("GATE_CLOSED_EARLY",i);
    if(!act && gq[i]>=3 && e) fail("GATE_NOT_SLEEPING",i);
    // A gate that slept while the pool was full wakes again.
    if(!e && (POOL_PROBE ? pool_count==K : outstanding==K)) g_full[i]=1;
    if(e && g_closed[i]>0) begin
      cov_wake[i]=cov_wake[i]+1;
      if(g_closed[i]==1) cov_one[i]=cov_one[i]+1;
      if(g_full[i]) cov_full_wake=cov_full_wake+1;
      g_full[i]=0;
    end
    // The gate would close on this edge. Only the wake keeps it open.
    if(e && w && g_rem[2*i+:2]==0 && g_closed[i]==0 && gq[i]>=2) cov_exact[i]=cov_exact[i]+1;
    g_closed[i]=e ? 0 : g_closed[i]+1;
    if(act) begin if(gq[i]>0) g_tact[i]=$time; gq[i]=0; end
    else begin if(gq[i]<1000) gq[i]=gq[i]+1; if(gq[i]==1) g_tq1[i]=$time; end
  end
endtask
// A domain leaves reset on the second clock edge after the reset port rises.
integer edges_main=0, edges_far=0;
always @(posedge clk) begin : gate_main
  integer i, m;
  if(!reset_n) edges_main=0;
  else if(edges_main<2) edges_main=edges_main+1;
  else if(GATED) begin
    for(i=0;i<NG;i=i+1) if(!G_FAR[i]) gate_edge(i);
    for(m=0;m<NS;m=m+1) if(IGP[m*8+:8]!=255) begin
      // A bus request or an offered response keeps the protocol clock running.
      if((m_arvalid[m] || m_awvalid[m] || m_wvalid[m] || m_rvalid[m] || m_bvalid[m]) &&
         !g_en[IGP[m*8+:8]]) fail("INITIATOR_GATED_WITH_BUS_WORK",m);
    end
    if(!g_en[IG]) begin
      ig_open_age=0;
      if(accepted_now>0) cov_sleep_pending=cov_sleep_pending+1;
    end else if(ig_open_age<1000) ig_open_age=ig_open_age+1;
    if((m_rvalid[0] || m_bvalid[0]) && ig_open_age>=1 && ig_open_age<=3) begin
      cov_resp_wake=cov_resp_wake+1; ig_open_age=1000;
    end
  end
  if(!reset_n || edges_main<2) for(i=0;i<NG;i=i+1) if(!G_FAR[i]) begin gq[i]=0; g_closed[i]=0; end
end
always @(posedge fclk) begin : gate_far
  integer i;
  if(!reset_n) edges_far=0;
  else if(edges_far<2) edges_far=edges_far+1;
  else if(GATED) begin
    for(i=0;i<NG;i=i+1) if(G_FAR[i]) gate_edge(i);
    if(TGP!=255 && (outstanding>0 || t_a_valid)) begin
      if(!g_en[TGP]) fail("TARGET_GATED_WITH_PENDING",outstanding);
      if(g_idle[TGP]) fail("TARGET_IDLE_WITH_PENDING",outstanding);
    end
    if(t_d_valid && t_d_ready && !g_en[IG]) cov_d_init_closed=cov_d_init_closed+1;
    if(TIMEOUT>0 && t_timed_out && !g_en[TG]) cov_gate_after_timeout=1;
  end
  if(!reset_n || edges_far<2) for(i=0;i<NG;i=i+1) if(G_FAR[i]) begin gq[i]=0; g_closed[i]=0; end
end

// ---- progress watchdog and periodic bound check ----
always @(posedge clk) if(reset_n && started) begin : watchdog
  integer h;
  if(pending>0 && !wd_pause && $time-last_progress>4096*slow_ps) report_stuck("STUCK_TRANSACTION");
  if(mon_fair && (mcycle%256)==0)
    for(h=0;h<GT;h=h+1)
      if(tx_live[h] && tx_seq[h]>=0 && $time-tx_tacc[h]>tx_budget[h]) begin
        $display("LATE uid=%0d master=%0d kind=%0d id=%0d addr=%h len=%0d waited_ps=%0d bound_ps=%0d",
                 tx_uid[h],tx_m[h],tx_kind[h],tx_id[h],tx_addr[h],tx_len[h],$time-tx_tacc[h],tx_budget[h]);
        report_stuck("LATENCY_BOUND");
      end
end

// ---- scenario helpers ----
task set_stalls(input integer a, input integer d, input integer r, input integer b, input integer w,
                input integer p);
  integer h;
  begin
    SA=a; SD=d; SR=r; SB=b; SW=w; p_stall=p;
    if(a>SA_top) SA_top=a; if(d>SD_top) SD_top=d; if(r>SR_top) SR_top=r;
    if(b>SB_top) SB_top=b; if(w>SW_top) SW_top=w;
    for(h=0;h<GT;h=h+1) if(tx_live[h]) begin
      if(a+d>tx_sf[h]) tx_sf[h]=a+d;
      if(r+b+w>tx_sm[h]) tx_sm[h]=r+b+w;
    end
  end
endtask
task clear_state;
  integer b, g, m, s;
  begin
    // A cut write leaves its range undefined. The model takes the content
    // that the slave holds. All other bytes must still match.
    for(g=0;g<GT;g=g+1) begin
      if(tx_live[g]) begin
        if(tx_kind[g]) for(b=tx_lo[g];b<tx_hi[g];b=b+1) exp_mem[b]=mem[b];
        for(b=(tx_lo[g] & ~3);b<((tx_hi[g]+3) & ~3);b=b+1) owner[b]=-1;
      end
      tx_live[g]=0;
    end
    for(m=0;m<NS;m=m+1) begin
      live_count[m]=0; budget[m]=0; gen_dly[m]=0; r_stall[m]=0; b_stall[m]=0; w_dly[m]=0;
      ar_cur[m]=-1; aw_cur[m]=-1; w_cur[m]=-1;
    end
    for(s=0;s<16;s=s+1) begin s_busy[s]=0; s_dleft[s]=0; s_adone[s]=0; s_wlo[s]=0; s_whi[s]=0; end
    pending=0; accepted_now=0; outstanding=0; put_left=0; d_cur=-1; last_a_source=-1;
    max_wait_lo=0; max_wait_hi=0; timed_out_seen=0; last_progress=$time;
  end
endtask
task do_reset;
  begin
    reset_n=0; started=0;
    #(1) clear_state;
    #(170*slow_ps+rc(slow_ps));
    clear_state;
    reset_n=1; n_resets=n_resets+1;
    #(24*slow_ps);
    check(m_rvalid==0 && m_bvalid==0 && !t_a_valid && !t_timed_out,"RESET_CLEARED");
    started=1; last_progress=$time;
  end
endtask
task drain;
  integer m, busy;
  begin
    busy=1;
    while(busy) begin
      @(posedge clk);
      busy=pending>0;
      for(m=0;m<NS;m=m+1) if(budget[m]!=0) busy=1;
    end
  end
endtask
// All transactions have retired. Every gate must close and report idle, the
// slave holds no message and the target memory equals the expected memory.
integer cov_quiet=0;
task quiet_check;
  integer i, b;
  begin
    #(64*slow_ps);
    if(GATED) for(i=0;i<NG;i=i+1) begin
      if(!g_idle[i]) fail("IDLE_AFTER_DRAIN",i);
      if(g_en[i] || g_wake[i]) fail("GATE_OPEN_AFTER_DRAIN",i);
    end
    check(pending==0 && outstanding==0 && !t_a_valid && m_rvalid==0 && m_bvalid==0,"WORK_AFTER_DRAIN");
    if(pool_count!=0) fail("POOL_NOT_EMPTY",pool_count);
    for(b=0;b<MEM;b=b+1) if(mem[b]!==exp_mem[b]) fail("MEMORY",b);
    cov_quiet=cov_quiet+1;
  end
endtask
// The pool must hold K packets again. K reads with K different IDs stay
// outstanding at the slave together.
task pool_probe;
  integer k, g, n;
  begin
    hold_d=1; wd_pause=1;
    for(k=0;k<K;k=k+1) begin @(posedge clk); force_txn(k%NS,0,k%16,(k%16)*4096+64*(k/16)+256,1,2,g); end
    n=0;
    while(outstanding<K && n<4096) begin @(posedge fclk); n=n+1; end
    if(outstanding<K) report_stuck("POOL_NOT_RESTORED");
    hold_d=0; wd_pause=0; last_progress=$time;
    drain;
  end
endtask
// One write and one read that the target can serve with a message of MAXB bytes.
task full_size;
  integer g, len;
  begin
    len=MAXB/LANES;
    if(len>=1 && len<=256) begin
      @(posedge clk); force_txn(0,1,1,61440,len,LSZ,g);
      drain;
      @(posedge clk); force_txn(0,0,2,61440,len,LSZ,g);
      drain;
      if(max_message!=MAXB) fail("LARGEST_MESSAGE",max_message);
    end
  end
endtask
task report_common;
  integer i, s, slots;
  begin
    slots=0;
    for(s=0;s<K;s=s+1) if(src_msgs[s]>0) slots=slots+1;
    $display("UHG_STAT seed=%0d pm=%0d pf=%0d main_cycles=%0d target_cycles=%0d reads=%0d writes=%0d read_beats=%0d write_beats=%0d messages=%0d gets=%0d puts=%0d error_reads=%0d error_writes=%0d resets=%0d",
             SEED,PM,PF,mcycle,fcycle,n_reads,n_writes,n_rbeats,n_wbeats,n_msgs,n_gets,n_puts,n_err_reads,n_err_writes,n_resets);
    $display("UHG_STAT contexts_full_events=%0d contexts_full_cycles=%0d contexts_three_quarters_cycles=%0d",
             cov_contexts_full,pool_full_cycles,pool_high_cycles);
    $display("UHG_STAT largest_message=%0d pool_peak=%0d pool_full_events=%0d messages_full_cycles=%0d busy_cycles=%0d slots_used=%0d max_latency_ps=%0d max_passed=%0d passed_limit=%0d early_acks=%0d quiet_checks=%0d",
             max_message,pool_peak,cov_pool_full,full_cycles,busy_cycles,slots,max_latency,max_bypass,
             max_bypass_limit,cov_early_ack,cov_quiet);
    for(i=0;i<NG;i=i+1)
      $display("UHG_GATE index=%0d kind=%0d far=%0d wakes=%0d exact_close=%0d one_edge_closed=%0d",
               i,g_kind[i],G_FAR[i],cov_wake[i],cov_exact[i],cov_one[i]);
  end
endtask
task need(input integer value, input [255:0] label);
  if(value<=0) nlv_fail_report("COVERAGE",label,0);
endtask
initial begin : init_model
  integer b;
  for(b=0;b<MEM;b=b+1) begin
    mem[b]=(b*13+7)^(b>>8); exp_mem[b]=(b*13+7)^(b>>8); owner[b]=-1; rmark[b]=-1; wmark[b]=-1;
  end
  for(b=0;b<GT;b=b+1) begin tx_live[b]=0; tx_seq[b]=-1; tx_state[b]=0; end
  for(b=0;b<NS;b=b+1) begin
    live_count[b]=0; gen_seq[b]=0; aseq[b]=0; budget[b]=0; gen_dly[b]=0; ar_cur[b]=-1; aw_cur[b]=-1;
    w_cur[b]=-1; ar_dly[b]=0; aw_dly[b]=0; w_dly[b]=0; r_stall[b]=0; b_stall[b]=0; r_wait[b]=0;
    b_wait[b]=0; w_wait[b]=0; ar_fired[b]=0; aw_fired[b]=0; w_fired[b]=0;
  end
  for(b=0;b<16;b=b+1) begin
    s_busy[b]=0; s_adone[b]=0; s_early[b]=0; s_denied[b]=0; s_dleft[b]=0; s_age[b]=0;
    s_wlo[b]=0; s_whi[b]=0; src_msgs[b]=0; s_abeats[b]=0; s_dbeat[b]=0;
  end
  for(b=0;b<NG;b=b+1) begin
    gq[b]=0; g_closed[b]=0; g_full[b]=0; cov_wake[b]=0; cov_exact[b]=0; cov_one[b]=0;
  end
end
