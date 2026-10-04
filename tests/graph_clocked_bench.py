"""Drive and observe each external graph face on its assigned clock only."""
import re

from graph_cross_bench import write_bench as write_single_clock_bench


def write_bench(work, mapping, decerr=3):
    # Reuse native port wiring and the independent byte-pattern oracle. None of
    # the single-clock driver, sampling loop or reset sequence is reused.
    write_single_clock_bench(work, mapping, decerr)
    original = (work / 'tb.sv').read_text()
    assert original.count('task reset;') == 1
    text = original.split('task reset;')[0]
    assert text.count('task check;') == 1
    text = text.replace('task check;', 'task automatic check;')
    clock_anchor = 'initial clk_main=0;\nalways #5 clk_main=~clk_main;'
    assert text.count(clock_anchor) == 1
    clocks = []
    periods = [10, 14, 22, 26, 13, 17, 19, 23, 29, 31, 37, 41]
    for index, domain in enumerate(mapping['domains']):
        name = domain['name']
        clock = 'clk_' + name
        # Stop an internal domain, never the administrative/source clock.
        stopped = name == ('middle' if mapping['mode'] == 'endpoints' else 'node0')
        # A declared period (and phase) replaces the fixed table entry.
        period = domain['period_ps']/1000 if 'period_ps' in domain else periods[index]
        phase = domain['phase_ps']/1000 if 'phase_ps' in domain else index*0.137
        clocks.append(f'initial begin {clock}=0; #{phase:.3f}; '
                      f'forever #{period/2} '
                      f'{"if(peer_running) " if stopped else ""}{clock}=~{clock}; end')
        domain['period_ps'] = round(period*1000)
        domain['phase_ps'] = round(phase*1000)
        domain['stopped_initially'] = stopped
    text = text.replace(clock_anchor, 'reg peer_running=0;\n'+'\n'.join(clocks))
    # Each endpoint has its own counters: no cross-domain shared increments.
    text += r'''
reg traffic=0, draining=0, cancel_mode=0, read_cancel_mode=0, quiet_after_reset=1;
integer source_tick[0:2], target_tick[0:2];
integer count_aw[0:2],count_ar[0:2],count_w[0:2],count_r[0:2],count_b[0:2],count_rd[0:2];
integer drain_source[0:2],drain_target[0:2];
integer hold_checks=0, reset_checks=0, drain_checks=0;
'''
    for index in range(3):
        source_clock = 'clk_' + mapping['objects'][f'source/s{index}']
        target_clock = 'clk_' + mapping['objects'][f'target/t{index}']
        source = r'''
initial begin : source_S_initial
 asent[S]=0;arsent[S]=0;wsent[S]=0;bseen[S]=0;rseen[S]=0;source_tick[S]=0;drain_source[S]=0;
end
always @(negedge SOURCE_CLOCK) begin
 if(!reset_n || !traffic || ((cancel_mode || read_cancel_mode) && S!=0)) begin
  i_awvalid[S]=0;i_wvalid[S]=0;i_arvalid[S]=0;
  i_bready[S]=reset_n;i_rready[S]=reset_n;
 end else begin
  i_awvalid[S]=!asent[S] && !read_cancel_mode && !(S==1 && phase==0 && source_tick[S]<9);
  i_awaddr[S]=target_of[S]*65536+'h1000+S*256;
  i_awlen[S]=beats(S)-1;i_awsize[S]=size_for(S);i_awburst[S]=1;i_awid[S]=id_for(S,0);i_awprot[S]=S;i_awcache[S]=S==1?0:2;
  i_wvalid[S]=wsent[S]<beats(S) && !read_cancel_mode && !(S==1 && phase==1 && source_tick[S]<9);
  i_wdata[S]=data_word(S,wsent[S],0);i_wstrb[S]=strobe_word(S,wsent[S]);i_wlast[S]=wsent[S]==beats(S)-1;
  i_arvalid[S]=!arsent[S] && !cancel_mode;i_araddr[S]=target_of[S]*65536+'h2000+S*256;
  i_arlen[S]=beats(S)-1;i_arsize[S]=size_for(S);i_arburst[S]=1;i_arid[S]=id_for(S,1);i_arprot[S]=S;i_arcache[S]=S==1?0:2;
  i_bready[S]=!(phase==7 && S==0 && cycle<6000);
  i_rready[S]=!(phase==6 && S==0 && cycle<6000) && source_tick[S]%5!=0;
 end
end
always @(posedge SOURCE_CLOCK or negedge reset_n) begin
 if(!reset_n) begin
  asent[S]=0;arsent[S]=0;wsent[S]=0;bseen[S]=0;rseen[S]=0;source_tick[S]=0;
 end else begin
  if(traffic) source_tick[S]=source_tick[S]+1;
  if(i_awvalid[S]&&i_awready[S]) asent[S]=asent[S]+1;
  if(i_arvalid[S]&&i_arready[S]) arsent[S]=arsent[S]+1;
  if(i_wvalid[S]&&i_wready[S]) wsent[S]=wsent[S]+1;
  if(i_bvalid[S]&&i_bready[S]) begin
   check(!cancel_mode && !read_cancel_mode && bseen[S]==0 && i_bid[S]===id_for(S,0),"B_ID_ONCE");
   check(i_bresp[S]===(permitted(S,target_of[S])?(target_error(target_of[S])?2:0):DECERR),"B_RESULT");
   bseen[S]=bseen[S]+1;
   if(phase<3) begin
    if(permitted(S,target_of[S])) allowed_w[3*S+target_of[S]]=1;
    else denied_w[3*S+target_of[S]]=1;
   end
  end
  if(i_rvalid[S]&&i_rready[S]) begin
   check(!cancel_mode && rseen[S]<beats(S) && i_rid[S]===id_for(S,1) && i_rlast[S]===(rseen[S]==beats(S)-1),"R_FRAME");
   check(i_rresp[S]===(permitted(S,target_of[S])?(target_error(target_of[S])&&(rseen[S]==0||(in_first_run(S,rseen[S])&&i_rresp[S]==2))?2:0):DECERR),"R_RESULT");
   check(i_rdata[S]===read_word(S,rseen[S],target_of[S]),"R_DATA");
   rseen[S]=rseen[S]+1;
   if(phase<3 && rseen[S]==beats(S)) begin
    if(permitted(S,target_of[S])) allowed_r[3*S+target_of[S]]=1;
    else denied_r[3*S+target_of[S]]=1;
   end
  end
  if(traffic && !cancel_mode && S==1 && source_tick[S]==8) begin
   if(phase==0) check(wsent[S]==1 && asent[S]==0,"LITE_W_BEFORE_AW");
   if(phase==1) check(asent[S]==1 && wsent[S]==0,"LITE_AW_BEFORE_W");
  end
  if(draining || quiet_after_reset) begin
   check(!i_bvalid[S]&&!i_rvalid[S],"LATE_RESPONSE");
   drain_source[S]=drain_source[S]+1;
   drain_checks=drain_checks+1;
  end
 end
end
'''
        target = r'''
initial begin : target_T_initial
 wb[T]=0;rb[T]=0;wo[T]=0;ro[T]=0;wc[T]=0;rc[T]=0;target_tick[T]=0;
 count_aw[T]=0;count_ar[T]=0;count_w[T]=0;count_r[T]=0;count_b[T]=0;count_rd[T]=0;drain_target[T]=0;
end
always @(negedge TARGET_CLOCK) begin
 if(!reset_n) begin
  t_awready[T]=0;t_wready[T]=0;t_bvalid[T]=0;t_arready[T]=0;t_rvalid[T]=0;
 end else begin
  t_awready[T]=!wb[T] && target_tick[T]%3!=0;
  t_wready[T]=wb[T]&&wc[T]<low_beats(wo[T])&&target_tick[T]%4!=0;
  t_bvalid[T]=wb[T]&&wc[T]==low_beats(wo[T]);t_bid[T]=target_id(T,wo[T],0);t_bresp[T]=target_error(T)?2:0;
  t_arready[T]=!rb[T]&&target_tick[T]%4!=0;t_rvalid[T]=rb[T]&&rc[T]<low_beats(ro[T]);
  t_rid[T]=target_id(T,ro[T],1);t_rresp[T]=target_error(T)&&rc[T]==factor(ro[T])-1?2:0;t_rlast[T]=rc[T]==low_beats(ro[T])-1;
  t_rdata[T]=data_word(ro[T],rc[T]/factor(ro[T]),1) >> ((rc[T]%factor(ro[T]))*32);
 end
end
always @(posedge TARGET_CLOCK or negedge reset_n) begin
 if(!reset_n) begin
  wb[T]=0;rb[T]=0;wo[T]=0;ro[T]=0;wc[T]=0;rc[T]=0;target_tick[T]=0;
  count_aw[T]=0;count_ar[T]=0;count_w[T]=0;count_r[T]=0;count_b[T]=0;count_rd[T]=0;
 end else begin
  target_tick[T]=target_tick[T]+1;
  if(draining || quiet_after_reset) begin
   check(!t_awvalid[T]&&!t_wvalid[T]&&!t_arvalid[T],"LATE_REQUEST");
   drain_target[T]=drain_target[T]+1;
   drain_checks=drain_checks+1;
  end
  if(!peer_running) check(!t_awvalid[T]&&!t_wvalid[T]&&!t_arvalid[T],"STOPPED_PEER");
  if(t_awvalid[T]&&t_awready[T]) begin
   wo[T]=(t_awaddr[T]-'h1000)/256;
   check(wo[T]>=0 && wo[T]<3,"W_OWNER");
   check(permitted(wo[T],T),"FORBIDDEN_WRITE");
   check(target_of[wo[T]]==T && t_awaddr[T]==='h1000+wo[T]*256 && t_awlen[T]===low_beats(wo[T])-1 && t_awsize[T]===2 && t_awburst[T]===1 && t_awid[T]===target_id(T,wo[T],0),"AW_DESCRIPTOR");
   check(t_awprot[T]===wo[T] && t_awcache[T]===(wo[T]==1?0:2),"AW_ATTRIBUTES");
   wb[T]=1;wc[T]=0;count_aw[T]=count_aw[T]+1;
  end
  if(t_wvalid[T]&&t_wready[T]) begin
   check(wb[T] && wc[T]<low_beats(wo[T]) && t_wlast[T]===(wc[T]==low_beats(wo[T])-1),"W_FRAME");
   check(t_wstrb[T]===(((strobe_word(wo[T],wc[T]/factor(wo[T])) >> ((wc[T]%factor(wo[T]))*4))&15)),"W_STROBE");
   check(((t_wdata[T]^((data_word(wo[T],wc[T]/factor(wo[T]),0) >> ((wc[T]%factor(wo[T]))*32)) & 256'hffffffff))&mask_word(t_wstrb[T]))===0,"W_DATA");
   check((t_wdata[T]&~mask_word(t_wstrb[T]))===0,"W_UNSTROBED_BYTE");
   wc[T]=wc[T]+1;count_w[T]=count_w[T]+1;
  end
  if(t_bvalid[T]&&t_bready[T]) begin wb[T]=0;count_b[T]=count_b[T]+1;end
  if(t_arvalid[T]&&t_arready[T]) begin
   ro[T]=(t_araddr[T]-'h2000)/256;
   check(ro[T]>=0 && ro[T]<3,"R_OWNER");
   check(permitted(ro[T],T),"FORBIDDEN_READ");
   check(target_of[ro[T]]==T && t_araddr[T]==='h2000+ro[T]*256 && t_arlen[T]===low_beats(ro[T])-1 && t_arsize[T]===2 && t_arburst[T]===1 && t_arid[T]===target_id(T,ro[T],1),"AR_DESCRIPTOR");
   check(t_arprot[T]===ro[T] && t_arcache[T]===(ro[T]==1?0:2),"AR_ATTRIBUTES");
   rb[T]=1;rc[T]=0;count_ar[T]=count_ar[T]+1;
  end
  if(t_rvalid[T]&&t_rready[T]) begin
   rc[T]=rc[T]+1;count_r[T]=count_r[T]+1;
   if(rc[T]==low_beats(ro[T])) begin rb[T]=0;count_rd[T]=count_rd[T]+1;end
  end
 end
end
'''
        text += re.sub(r'\bS\b', str(index), source).replace('source_S_', f'source_{index}_').replace('SOURCE_CLOCK', source_clock)
        text += re.sub(r'\bT\b', str(index), target).replace('target_T_', f'target_{index}_').replace('TARGET_CLOCK', target_clock)
    # Sample stalled payloads at the actual receiving interface clock. The
    # checker consumes only public ports and the declared assignment metadata.
    for side, channels in [('i', ('b', 'r')), ('t', ('aw', 'w', 'ar'))]:
        for index in range(3):
            domain = mapping['objects'][f'{"source/s" if side == "i" else "target/t"}{index}']
            for channel in channels:
                prefix = f'nl_{side}_{"s" if side == "i" else "t"}{index}_'
                fields = [p for p in mapping['ports'] if p['name'].startswith(prefix+channel)
                          and p['direction']=='output' and not p['name'].endswith('valid')]
                width = sum(p['width'] for p in fields)
                payload = '{'+','.join(f"{side}_{p['name'][len(prefix):]}[{index}][{p['width']-1}:0]" for p in fields)+'}'
                tag = f'{side}{index}_{channel}'
                valid, ready = f'{side}_{channel}valid[{index}]', f'{side}_{channel}ready[{index}]'
                text += f'''
integer holds_{tag}=0;
reg held_{tag}=0;
reg [{width-1}:0] saved_{tag};
always @(posedge clk_{domain} or negedge reset_n) begin
 if(!reset_n) begin held_{tag}=0;holds_{tag}=0;end
 else begin
  if(held_{tag}) begin
   check({valid}===1'b1 && {payload}===saved_{tag},"STALL_{tag}");
   hold_checks=hold_checks+1;holds_{tag}=holds_{tag}+1;
  end
  held_{tag}=({valid}===1'b1 && {ready}!==1'b1);
  saved_{tag}={payload};
 end
end
'''
    text += r'''
task common_reset;
 begin
  traffic=0;draining=0;
  #0.123;reset_n=0;quiet_after_reset=1;
  #400.246;reset_n=1;
  #1000.369;
 end
endtask
initial begin
 phase=0;cycle=0;s=0;t=0;reset_n=1;
 for(s=0;s<3;s=s+1) target_of[s]=s;
 common_reset;
 for(phase=0;phase<10;phase=phase+1) begin
  if(phase==9) begin
   phase=8;cancel_mode=1;common_reset;target_of[0]=0;quiet_after_reset=0;traffic=1;
   for(cycle=0;cycle<30000 && count_w[0]==0;cycle=cycle+1) #10;
   check(count_w[0]>0 && count_w[0]<low_beats(0),"RESET_MID_PACKET");
   common_reset;cancel_mode=0;draining=1;#4000;draining=0;reset_checks=reset_checks+1;
   // Reset again while a 16-beat read response is still crossing back.
   read_cancel_mode=1;common_reset;quiet_after_reset=0;traffic=1;
   for(cycle=0;cycle<30000 && rseen[0]==0;cycle=cycle+1) #10;
   check(rseen[0]>0 && rseen[0]<beats(0),"RESET_MID_RESPONSE");
   common_reset;read_cancel_mode=0;draining=1;#4000;draining=0;reset_checks=reset_checks+1;
   phase=9;
  end
  common_reset;
  expected_requests=0;expected_beats=0;
  for(s=0;s<3;s=s+1) begin
   target_of[s]=phase<3?(s+phase)%3:(phase<6?phase-3:(phase<8?0:s));
   if(permitted(s,target_of[s])) begin expected_requests=expected_requests+1;expected_beats=expected_beats+low_beats(s);end
  end
  done=0;quiet_after_reset=0;traffic=1;
  for(cycle=0;cycle<30000&&!done;cycle=cycle+1) begin
   #10;
   if(phase==0 && cycle==60) begin
    check(asent[0]==1 && wsent[0]>0,"STOPPED_LOCAL_ACCEPT");peer_running=1;
   end
   // A stalled multibeat read may hold a shared target and the responses
   // behind it (docs/protocols.md, shared read and write networks): while source 0
   // holds RREADY (phase 6) or BREADY (phase 7) low it takes nothing on the
   // held channel; what the other sources received meanwhile is reported,
   // not required. EXACT_COUNT proves every source finishes after release.
   if(cycle==5000 && phase==6) begin
    check(rseen[0]==0&&i_rvalid[0]&&holds_i0_r>2,"R_HOLD_SOURCE");
    $display("HOLD_OBSERVATION phase=6 b=%0d%0d%0d r2=%0d/%0d",bseen[0],bseen[1],bseen[2],rseen[2],beats(2));
   end
   if(cycle==5000 && phase==7) begin
    check(bseen[0]==0&&i_bvalid[0]&&holds_i0_b>2,"B_HOLD_SOURCE");
    $display("HOLD_OBSERVATION phase=7 b=%0d%0d%0d r0=%0d/%0d r2=%0d/%0d",bseen[0],bseen[1],bseen[2],rseen[0],beats(0),rseen[2],beats(2));
   end
   done=1;
   for(s=0;s<3;s=s+1) if(bseen[s]!=1 || rseen[s]!=beats(s)) done=0;
  end
  if(!done) begin
   for(s=0;s<3;s=s+1) $display("SOURCE_PROGRESS index=%0d aw=%0d ar=%0d w=%0d b=%0d r=%0d awready=%0b wready=%0b arready=%0b bvalid=%0b rvalid=%0b",s,asent[s],arsent[s],wsent[s],bseen[s],rseen[s],i_awready[s],i_wready[s],i_arready[s],i_bvalid[s],i_rvalid[s]);
   for(t=0;t<3;t=t+1) $display("TARGET_PROGRESS index=%0d aw=%0d ar=%0d w=%0d r=%0d b=%0d rd=%0d wb=%0d rb=%0d awvalid=%0b wvalid=%0b arvalid=%0b bready=%0b rready=%0b",t,count_aw[t],count_ar[t],count_w[t],count_r[t],count_b[t],count_rd[t],wb[t],rb[t],t_awvalid[t],t_wvalid[t],t_arvalid[t],t_bready[t],t_rready[t]);
  end
  check(done,"EXACT_COUNT");
  // Source completion may precede a remote target observing its return ack.
  // Allow physical edges to drain before checking both sides' conservation.
  #2000;
  awcount=0;arcount=0;wcount=0;rcount=0;bdone=0;rdone=0;
  for(t=0;t<3;t=t+1) begin
   awcount=awcount+count_aw[t];arcount=arcount+count_ar[t];wcount=wcount+count_w[t];rcount=rcount+count_r[t];
   bdone=bdone+count_b[t];rdone=rdone+count_rd[t];
  end
  for(s=0;s<3;s=s+1) check(asent[s]==1&&arsent[s]==1&&wsent[s]==beats(s),"SOURCE_EXACT_COUNT");
  check(awcount==expected_requests && arcount==expected_requests && wcount==expected_beats && rcount==expected_beats && bdone==expected_requests && rdone==expected_requests,"TARGET_EXACT_COUNT");
  sum_aw=sum_aw+awcount;sum_ar=sum_ar+arcount;sum_w=sum_w+wcount;sum_r=sum_r+rcount;
  traffic=0;draining=1;#4000;draining=0;
 end
 check(allowed_w==9'h173 && allowed_r==9'h173 && denied_w==9'h08c && denied_r==9'h08c,"PAIR_COVERAGE");
 for(s=0;s<3;s=s+1) check(drain_source[s]>=100 && drain_target[s]>=100,"DRAIN_ARRIVAL");
 check(hold_checks>0 && reset_checks==2 && drain_checks>0,"MONITOR_ARRIVAL");
 $display("AXI4_TARGET_PASS graph_async phases=10 admitted_pairs=6 denied_pairs=3 reset_mid_packet=1 reset_mid_response=1 aw=%0d ar=%0d w=%0d r=%0d holds=%0d drains=%0d",sum_aw,sum_ar,sum_w,sum_r,hold_checks,drain_checks);$finish;
end
initial begin #5000000;$fatal(1,"ASSERT_FAIL:GRAPH_CROSS.TIMEOUT");end
endmodule
'''
    # Unused protocol inputs (for example LOCK) must be legal constants, not X.
    # Initialize every external input array, including omitted Lite lanes.
    inputs = sorted({p['name'].split('_', 3)[1]+'_'+p['name'].split('_', 3)[3]
                     for p in mapping['ports']
                     if p['direction']=='input' and p['name'].startswith(('nl_i_', 'nl_t_'))})
    initialize = '\n'.join(f'{field}[{index}]=0;' for field in inputs for index in range(3))
    assert text.count('initial begin\n phase=0;') == 1
    text = text.replace('initial begin\n phase=0;', 'initial begin\n'+initialize+'\n phase=0;')
    text = re.sub(r'\bDECERR\b', str(decerr), text)
    (work / 'tb.sv').write_text(text)
