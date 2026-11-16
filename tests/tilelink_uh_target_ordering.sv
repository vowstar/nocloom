// Random AXI master and out-of-order multi-source TL-UH slave.
integer TRACE=0, RBASE=0, RIDS=1, MODE=0, DET=0, D=0, seed=1, NR=40, NW=40, IDS=1, GAP=6, DMAX=12, EARLY=30, RP=70, BP=70, STALL=0, DEN=1, LMAX=7;
integer cycle=0, last_progress=0, fails=0, seed0=0;
reg [7:0] mem[0:65535];
reg [7:0] expect_mem[0:65535];
always #5 clk=~clk;
task fail(input [8*40-1:0] what, input integer v);
  begin $display("FAIL %0s value=%0d cycle=%0d seed=%0d", what, v, cycle, seed0); fails=fails+1; $fatal(1,"CHECK_FAIL %0s", what); end
endtask
function [7:0] init_byte(input integer a);
  init_byte = (a*7+13) ^ (a>>8);
endfunction
// TL slave
integer pend[0:15], p_get[0:15], p_addr[0:15], p_size[0:15], p_den[0:15];
integer p_aleft[0:15], p_dbeats[0:15], p_time[0:15], p_early[0:15], p_acked[0:15];
reg d_fired=0;
integer put_src=-1, d_cur=-1, d_beat=0, msgs=0, gets=0, puts=0;
integer s, k, b, n, start;
function integer beats_of(input integer size);
  beats_of = size<=2 ? 1 : (1<<size)/4;
endfunction
function integer is_denied(input integer addr);
  is_denied = DEN && addr>=32'h8000 && ((addr-32'h8000)/64)%5==3;
endfunction
task write_beat(input integer src, input integer addr);
  begin
    if(!p_den[src]) for(b=0;b<4;b=b+1) if(t_a_mask[b]) mem[(addr & ~3)+b]=t_a_data[b*8+:8];
  end
endtask
always @(posedge clk) if(reset_n) begin
  if(t_a_valid && t_a_ready) begin
    last_progress=cycle;
    if(t_a_source>=K) fail("A_SOURCE_RANGE", t_a_source);
    if(put_src<0) begin
      s=t_a_source; msgs=msgs+1;
      if(pend[s]!=0) fail("SOURCE_REUSE_BEFORE_D", s);
      if(t_a_address % (1<<t_a_size)) fail("A_MISALIGNED", t_a_address);
      p_addr[s]=t_a_address; p_size[s]=t_a_size; p_get[s]=t_a_opcode==4;
      p_den[s]=is_denied(t_a_address); p_acked[s]=0;
      p_time[s]=cycle+($random(seed) & 32'h7fffffff)%(DMAX+1);
      p_early[s]=(($random(seed) & 32'h7fffffff)%100)<EARLY;
      if(t_a_opcode==4) begin
        gets=gets+1; pend[s]=2; p_dbeats[s]=beats_of(t_a_size);
        if(t_a_address<32'h8000 && t_a_address+(1<<t_a_size)>32'h8000) fail("GET_REGION", t_a_address);
      end else begin
        puts=puts+1;
        if(t_a_address<32'h8000) fail("PUT_IN_READ_REGION", t_a_address);
        p_aleft[s]=beats_of(t_a_size)-1;
        write_beat(s, t_a_address);
        if(p_aleft[s]==0) pend[s]=2; else begin pend[s]=1; put_src=s; end
      end
    end else begin
      s=put_src;
      if(t_a_source!=s) fail("PUT_SOURCE_CHANGED", t_a_source);
      write_beat(s, p_addr[s]+4*(beats_of(p_size[s])-p_aleft[s]));
      p_aleft[s]=p_aleft[s]-1;
      if(p_aleft[s]==0) begin
        put_src=-1;
        pend[s]=p_acked[s] ? 0 : 2;
      end
    end
  end
  if(t_d_valid && t_d_ready) begin
    last_progress=cycle; d_fired=1;
    s=t_d_source;
    if(t_d_opcode==1) begin
      d_beat=d_beat+1;
      if(d_beat==p_dbeats[s]) begin pend[s]=0; d_cur=-1; d_beat=0; end
    end else begin
      if(pend[s]==1) p_acked[s]=1; else pend[s]=0;
      d_cur=-1; d_beat=0;
    end
  end
end
always @(negedge clk) if(reset_n) begin
  t_a_ready=DET || (($random(seed) & 32'h7fffffff)%100)<80;
  if(!t_d_valid || d_fired) begin
    t_d_valid=0;
    if(d_cur<0) begin
      start=($random(seed) & 32'h7fffffff)%K;
      for(n=0;n<K && d_cur<0;n=n+1) begin
        k=(start+n)%K;
        if(cycle>=p_time[k] && (pend[k]==2 || (pend[k]==1 && p_early[k] && !p_acked[k] && !p_get[k])))
          d_cur=k;
      end
    end
    if(d_cur>=0 && (DET || (($random(seed) & 32'h7fffffff)%100)<75)) begin
      s=d_cur; t_d_valid=1; t_d_source=s; t_d_size=p_size[s]; t_d_param=0;
      t_d_denied=p_den[s]; t_d_corrupt=0; t_d_data=0;
      if(p_get[s]) begin
        t_d_opcode=1; t_d_corrupt=p_den[s];
        for(b=0;b<4;b=b+1) t_d_data[b*8+:8]=mem[((p_addr[s]+4*d_beat) & ~3)+b];
      end else t_d_opcode=0;
    end
  end
  d_fired=0;
end
// AXI master
integer rq_size[0:1023], rq_addr[0:1023], rq_len[0:1023], rhead[0:15], rtail[0:15], rbeat[0:15];
integer wq_idx[0:1023], whead[0:15], wtail[0:15];
integer w_err[0:255];
integer reads_issued=0, reads_done=0, writes_issued=0, writes_done=0;
integer ri, wi, bb, i, id, addr, len, o, w, strb, word;
reg [63:0] wd;
always @(posedge clk) if(reset_n) begin
  cycle=cycle+1;
  if(s_arvalid && s_arready) last_progress=cycle;
  if(s_awvalid && s_awready) last_progress=cycle;
  if(s_wvalid && s_wready) last_progress=cycle;
  if(s_rvalid && s_rready) begin
    last_progress=cycle;
    id=s_rid;
    if(rhead[id]==rtail[id]) fail("R_UNEXPECTED_ID", id);
    i=id*64+rhead[id]%64;
    addr=rq_addr[i]+(1<<rq_size[i])*rbeat[id];
    for(b=0;b<(1<<rq_size[i]);b=b+1) if(s_rdata[((addr+b)%DB)*8+:8]!==init_byte(addr+b)) begin
      $display("R data id=%0d addr=%h got=%h", id, addr, s_rdata); fail("R_DATA", addr);
    end
    if(s_rresp!=0) fail("R_RESP", s_rresp);
    if(s_rlast!=(rbeat[id]==rq_len[i])) fail("R_LAST", rbeat[id]);
    if(s_rlast && TRACE) $display("R done id=%0d addr=%h cycle=%0d", id, rq_addr[i], cycle);
    if(s_rlast) begin rhead[id]=rhead[id]+1; rbeat[id]=0; reads_done=reads_done+1; end
    else rbeat[id]=rbeat[id]+1;
  end
  if(s_bvalid && s_bready) begin
    last_progress=cycle;
    id=s_bid;
    if(whead[id]==wtail[id]) fail("B_UNEXPECTED_ID", id);
    w=wq_idx[id*64+whead[id]%64];
    if(s_bresp!=(w_err[w] ? 2 : 0)) begin
      $display("B order: id=%0d expected write %0d resp %0d got %0d", id, w, w_err[w]?2:0, s_bresp);
      fail("B_RESP_ORDER", w);
    end
    if(TRACE) $display("B id=%0d resp=%0d head_write=%0d cycle=%0d", id, s_bresp, w, cycle);
    whead[id]=whead[id]+1; writes_done=writes_done+1;
  end
  if(MODE==0 && cycle-last_progress>4000) begin
    $display("HANG reads %0d/%0d writes %0d/%0d msgs=%0d", reads_done, reads_issued, writes_done, writes_issued, msgs);
    for(k=0;k<K;k=k+1) $display("  slave source %0d pend=%0d", k, pend[k]);
    fail("HANG", cycle);
  end
end
reg stall_r=0;
integer stall_left=0;
task issue_read(input integer rid, input integer raddr, input integer rlen);
  begin
    s_arid=rid; s_araddr=raddr; s_arlen=rlen; s_arsize=2; s_arburst=1; s_arvalid=1;
    rq_size[rid*64+rtail[rid]%64]=2; rq_addr[rid*64+rtail[rid]%64]=raddr; rq_len[rid*64+rtail[rid]%64]=rlen; rtail[rid]=rtail[rid]+1;
    reads_issued=reads_issued+1;
    @(posedge clk); while(!s_arready) @(posedge clk);
    $display("AR id=%0d addr=%h accepted cycle=%0d", rid, raddr, cycle);
    @(negedge clk); s_arvalid=0;
  end
endtask
always @(posedge clk) if((MODE==1 || TRACE) && s_rvalid && s_rready && s_rlast)
  $display("R last id=%0d addr_beat_data=%h cycle=%0d", s_rid, s_rdata, cycle);
always @(posedge clk) if((MODE==1 || TRACE) && t_a_valid && t_a_ready)
  $display("TL A source=%0d addr=%h opcode=%0d cycle=%0d", t_a_source, t_a_address, t_a_opcode, cycle);
always @(posedge clk) if((MODE==1 || TRACE) && t_d_valid && t_d_ready)
  $display("TL D source=%0d cycle=%0d", t_d_source, cycle);
task directed;
  begin
    @(negedge clk);
    fork
      issue_read(0, 32'h100, 0);
      begin repeat(D) @(negedge clk); issue_read(0, 32'h200, 0); end
    join
    repeat(200) @(negedge clk);
    if(reads_done!=2) fail("READ_RELEASE_PROGRESS", reads_done);
    issue_read(0, 32'h300, 0);
    repeat(200) @(negedge clk);
    if(reads_done!=3) fail("READ_REUSE_PROGRESS", reads_done);
    if(!release_overlap) fail("READ_RELEASE_COVER", 0);
    $display("UH_PASS directed release overlap");
    $finish;
  end
endtask
always @(negedge clk) if(reset_n) begin
  if(stall_left>0) stall_left=stall_left-1;
  else if(STALL && (($random(seed) & 32'h7fffffff)%1000)<STALL) stall_left=($random(seed) & 32'h7fffffff)%300;
  s_rready=stall_left==0 && (($random(seed) & 32'h7fffffff)%100)<RP;
  s_bready=(($random(seed) & 32'h7fffffff)%100)<BP;
end
initial begin
  if($value$plusargs("seed=%d", seed)) ; seed0=seed;
  if($value$plusargs("NR=%d", NR)) ;
  if($value$plusargs("NW=%d", NW)) ;
  if($value$plusargs("IDS=%d", IDS)) ; RIDS=IDS; if($value$plusargs("RIDS=%d", RIDS)) ; if($value$plusargs("RBASE=%d", RBASE)) ;
  if($value$plusargs("GAP=%d", GAP)) ;
  if($value$plusargs("DMAX=%d", DMAX)) ;
  if($value$plusargs("EARLY=%d", EARLY)) ;
  if($value$plusargs("RP=%d", RP)) ;
  if($value$plusargs("BP=%d", BP)) ;
  if($value$plusargs("STALL=%d", STALL)) ;
  if($value$plusargs("DEN=%d", DEN)) ;
  if($value$plusargs("LMAX=%d", LMAX)) ;
  for(i=0;i<65536;i=i+1) begin mem[i]=init_byte(i); expect_mem[i]=init_byte(i); end
  for(i=0;i<16;i=i+1) begin pend[i]=0; rhead[i]=0; rtail[i]=0; rbeat[i]=0; whead[i]=0; wtail[i]=0; p_time[i]=0; end
  if($value$plusargs("MODE=%d", MODE)) ; if($value$plusargs("TRACE=%d", TRACE)) ;
  if($value$plusargs("DET=%d", DET)) ;
  if($value$plusargs("D=%d", D)) ;
  repeat(160) @(negedge clk); reset_n=1;
  if(MODE==1) directed;
  fork
    begin : reads
      for(ri=0;ri<NR;ri=ri+1) begin : one_read
        integer rid, raddr, rlen, rsz, rn, ro;
        repeat(($random(seed) & 32'h7fffffff)%(GAP+1)) @(negedge clk);
        rid=RBASE+($random(seed) & 32'h7fffffff)%RIDS;
        rsz=(LDB>2 && ($random(seed) & 1)) ? 2 : LDB;
        rn=64>>rsz; ro=($random(seed) & 32'h7fffffff)%rn;
        rlen=($random(seed) & 32'h7fffffff)%(rn-ro);
        if(rlen>LMAX) rlen=LMAX;
        raddr=((($random(seed) & 32'h7fffffff)%(32'h8000/64-1))*64) + (ro<<rsz);
        @(negedge clk);
        s_arid=rid; s_araddr=raddr; s_arlen=rlen; s_arsize=rsz; s_arburst=1; s_arvalid=1;
        rq_size[rid*64+rtail[rid]%64]=rsz; rq_addr[rid*64+rtail[rid]%64]=raddr; rq_len[rid*64+rtail[rid]%64]=rlen; rtail[rid]=rtail[rid]+1;
        reads_issued=reads_issued+1;
        @(posedge clk); while(!s_arready) @(posedge clk);
        if(TRACE) $display("AR id=%0d addr=%h len=%0d cycle=%0d", rid, raddr, rlen, cycle);
        @(negedge clk); s_arvalid=0;
      end
    end
    begin : writes
      for(wi=0;wi<NW;wi=wi+1) begin : one_write
        integer wid, waddr, wlen, wo, any, wsz, wn, ba;
        repeat(($random(seed) & 32'h7fffffff)%(GAP+1)) @(negedge clk);
        wid=($random(seed) & 32'h7fffffff)%IDS;
        wsz=(LDB>2 && ($random(seed) & 1)) ? 2 : LDB;
        wn=64>>wsz; wo=($random(seed) & 32'h7fffffff)%wn;
        wlen=($random(seed) & 32'h7fffffff)%(wn-wo);
        if(wlen>LMAX) wlen=LMAX;
        waddr=32'h8000+wi*64+(wo<<wsz);
        @(negedge clk);
        s_awid=wid; s_awaddr=waddr; s_awlen=wlen; s_awsize=wsz; s_awburst=1; s_awvalid=1;
        wq_idx[wid*64+wtail[wid]%64]=wi; wtail[wid]=wtail[wid]+1;
        writes_issued=writes_issued+1;
        any=0;
        @(posedge clk); while(!s_awready) @(posedge clk);
        if(TRACE) $display("AW write=%0d id=%0d addr=%h len=%0d denied=%0d cycle=%0d", wi, wid, waddr, wlen, is_denied(waddr), cycle);
        @(negedge clk); s_awvalid=0;
        for(o=0;o<=wlen;o=o+1) begin
          wd={$random(seed),$random(seed)};
          ba=waddr+(o<<wsz);
          strb=0;
          for(bb=0;bb<(1<<wsz);bb=bb+1) if(($random(seed) & 3)!=0 || o%2==0) strb=strb | (1<<((ba+bb)%DB));
          if(($random(seed) & 7)==0) strb=0;
          if(strb) any=1;
          w_err[wi]=is_denied(waddr) && any;
          if(!is_denied(waddr)) for(bb=0;bb<(1<<wsz);bb=bb+1) if(strb>>((ba+bb)%DB) & 1) expect_mem[ba+bb]=wd[((ba+bb)%DB)*8+:8];
          s_wdata=wd; s_wstrb=strb; s_wlast=o==wlen; s_wvalid=1;
          @(posedge clk); while(!s_wready) @(posedge clk);
          @(negedge clk); s_wvalid=0;
        end
        w_err[wi]=is_denied(waddr) && any;
      end
    end
  join
  wait(reads_done==NR && writes_done==NW);
  repeat(20) @(negedge clk);
  for(i=32'h8000;i<65536;i=i+1) if(mem[i]!==expect_mem[i]) begin
    $display("mem %h got %h expected %h", i, mem[i], expect_mem[i]); fail("MEMORY", i);
  end
  $display("UH_PASS K=%0d seed=%0d reads=%0d writes=%0d msgs=%0d cycles=%0d", K, seed0, NR, NW, msgs, cycle);
  $finish;
end

initial begin repeat(30000) @(negedge clk); fail("CYCLE_LIMIT", cycle); end

task nlv_fail_report(input [255:0] rule_name, input [255:0] who, input [63:0] value);
  begin $display("ASSERT_FAIL:%0s %0s value=%0d", rule_name, who, value); $fatal(1); end
endtask
nlv_tl_checker #(.NAME("target"),.SOURCE_W((K-1)>0 ? $clog2(K) : 1),.SIZE_W(4),.UH(1),.SOURCE_LIMIT(K)) checker_t (
  .clk(clk),.rst_n(reset_n),.allow_reject(1'b0),
  .a_opcode(t_a_opcode),.a_param(t_a_param),.a_size(t_a_size),.a_source(t_a_source),
  .a_address(t_a_address),.a_mask(t_a_mask),.a_data(t_a_data),.a_corrupt(t_a_corrupt),
  .a_valid(t_a_valid),.a_ready(t_a_ready),
  .d_opcode(t_d_opcode),.d_param(t_d_param),.d_size(t_d_size),.d_source(t_d_source),
  .d_data(t_d_data),.d_corrupt(t_d_corrupt),.d_denied(t_d_denied),
  .d_valid(t_d_valid),.d_ready(t_d_ready));
