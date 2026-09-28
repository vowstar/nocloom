"""AXI burst source and independent APB4, APB3 or APB2 byte-memory target scoreboard."""


def write_bench(work, mapping=None, source=0, target=1, fixed_single=False, dialect='apb4',
                cases=None, drop_checks=(), decerr=3, run_word=4):
    # decerr is the code of a window miss: 3 with error codes, else 2.
    # cases replaces the outlet case list; drop_checks ('wrsp', 'b') removes
    # write response order checks, so that a negative control shows that
    # another check catches a fault on its own.
    assert mapping is None or (cases is None and not drop_checks)
    assert set(drop_checks) <= {'wrsp', 'b'}
    signals, instances = {}, {}
    quiet = []
    clocks = []
    clock_nets = {}
    if mapping is None:
        for line in (work / 'ports.tsv').read_text().splitlines():
            part, module, name, width, input_ = line.split()
            if name in ('nl_clk', 'nl_reset_n'):
                net = name
            elif name.startswith('nl_axi_'):
                net = 'a_' + name[7:]
            elif name.startswith('nl_apb_'):
                net = 'h_' + name[7:]
            else:
                net = 'p_' + name[3:]
            kind = 'reg' if name in ('nl_clk', 'nl_reset_n') or input_ == '1' and (
                name.startswith(('nl_axi_', 'nl_apb_'))) else 'wire'
            if net in signals:
                assert signals[net][1] == int(width)
            else:
                signals[net] = (kind, int(width))
            instances.setdefault((part, module), []).append(f'.{name}({net})')
    else:
        connections = []
        for port in mapping['ports']:
            name, field = port['name'], port['signal']
            if field == 'clock':
                # Bench nets keep the domain names; the DUT ports are named by the definition.
                domain = port['endpoint'].split(':', 1)[1]
                net = 'nl_clk' if domain == 'main' else 'nl_clk_' + domain
                clock_nets[name] = net
                clocks.append(net)
            elif field == 'reset_n':
                net = 'nl_reset_n'
            elif name.startswith(f'nl_i_s{source}_'):
                net = 'a_' + field
            elif name.startswith(f'nl_t_t{target}_'):
                net = 'h_' + field
            else:
                net = name
                if port['direction'] == 'output' and field in ('awvalid', 'arvalid', 'wvalid', 'hsel', 'psel'):
                    quiet.append(net)
            signals[net] = ('reg' if port['direction'] == 'input' else 'wire', port['width'])
            connections.append(f'.{name}({net})')
        instances[('dut', mapping['module'])] = connections
    assert dialect in ('apb4', 'apb3', 'apb2') and (dialect == 'apb4' or mapping is None)
    if mapping is None:
        # The fixture's target port set follows its dialect.
        apb = {line.split()[2][7:] for line in (work / 'ports.tsv').read_text().splitlines()
               if line.split()[0] == 'target' and line.split()[2].startswith('nl_apb_')}
        common = {'paddr', 'pwdata', 'prdata', 'pwrite', 'psel', 'penable'}
        assert apb == common | {'apb4': {'pstrb', 'pprot', 'pready', 'pslverr'},
                                'apb3': {'pready', 'pslverr'}, 'apb2': set()}[dialect], apb
    # APB2 has no PREADY or PSLVERR port: the model keeps both as local wires
    # and never waits or fails (each access completes after one ACCESS cycle).
    for name in ('h_pready', 'h_pslverr', 'h_prdata'):
        signals[name] = ('wire', signals[name][1] if name in signals else 1)
    # APB3 and APB2 have no PSTRB or PPROT port. A served write is a whole
    # word and a read has no strobes; protection has nothing to check.
    local = []
    if 'h_pstrb' not in signals:
        signals['h_pstrb'] = ('wire', 4)
        local.append("assign h_pstrb=h_pwrite?4'hf:4'h0;")
    if 'h_pprot' not in signals:
        signals['h_pprot'] = ('wire', 3)
        local.append("assign h_pprot=h_pwrite?3'b101:3'b001;")
    assert (dialect == 'apb4') == (not local)
    text = r'''

`timescale 1ns/1ps
module tb;
@DECL@
@INST@
initial nl_clk=0;
always #5 nl_clk=~nl_clk;
integer cases=0,cycle=0,n,sz,burst,mode,wbase,rbase,wi,ri,packet_ri,awseen,arseen,bs,bt;
integer j,k,a,lower,upper,expected_code,expected_write_bytes,target_write_bytes;
integer target_addresses,target_writes,target_reads,expected_transfers,scan,chunk,lane;
integer dp_addr,dp_wait,dp_resp;
// Mode 15 measures throughput: target edges since reset, the edge of the
// last completion of each direction, and the widest gap between two.
integer tick=0,last_w,last_r,gaps_w,gaps_r,widest_w,widest_r;
// Target dialect: 4 (APB4), 3 (APB3: no PSTRB/PPROT) or 2 (APB2: also no
// PREADY/PSLVERR).
localparam DIALECT=@DIALECT@;
// Every completion of a case in order: its target edge and direction. Mode 18
// checks the order of concurrent reads and writes on them.
integer ctick[0:1023];reg cdir[0:1023];
integer ccount,fw,fr;
// A strobeless partial word fails alone. The packet accumulates its error.
reg strobe_failed;
integer setup_count,wait_count,error_count,after_error,b_holds,r_holds;
reg dp_valid=0,dp_write=0,finished,cancelled;
reg [3:0] dp_strobe;reg [2:0] dp_prot;reg [31:0] dp_data;
reg [7:0] memory[0:4095],expected[0:4095];
reg [31:0] expected_data,read_mask,old_rdata;
reg [1:0] old_rresp;reg old_rlast,held_r,held_b;
reg [1:0] old_bresp;reg [127:0] old_control;reg held_control;
reg [3:0] mask;
// The driver loop advances cycle in the posedge time step, so the target
// takes its wait length from a copy taken at the falling edge: every
// simulator then gives each access the same number of wait states.
integer hcycle=0;
always @(negedge nl_clk) hcycle<=cycle;
assign h_pready=dp_valid && dp_wait==0;
// An error outside the completion phase must not poison the next response.
assign h_pslverr=dp_valid ? dp_resp!=0 : 1'b1;
// Mode 14 reads a FIFO: every read access returns a new value.
reg [7:0] fifo_count=0;
assign h_prdata={memory[dp_addr+3],memory[dp_addr+2],memory[dp_addr+1],memory[dp_addr]}^
               (mode==14?fifo_count*32'h01010101:32'h0);
function integer address;
 input integer base,index;
 integer step,span;
 begin
  step=1<<sz;span=n*step;
  if(index==0 || burst==0) address=base;
  else if(burst==2) address=(base/span)*span+(((base/step)*step+index*step)%span);
  else address=(base/step)*step+index*step;
 end
endfunction
function [31:0] data_word;
 input integer index;
 begin data_word=32'h50403020 ^ (index*32'h01010101);end
endfunction
function [3:0] byte_mask;
 input integer index;
 integer first,last,byte_;
 begin
  first=address(wbase,index);last=(first/(1<<sz)+1)*(1<<sz);byte_mask=0;
  for(byte_=0;byte_<4;byte_=byte_+1)
   if((first/4)*4+byte_>=first && (first/4)*4+byte_<last) byte_mask[byte_]=1;
  if(mode==1) begin
   case(index%4) 0:byte_mask=byte_mask&5;1:byte_mask=byte_mask&10;2:byte_mask=0;endcase
  end
  if(mode==7) byte_mask=0;
  if(mode==12) byte_mask=byte_mask&(index%16);
  // Modes 19 and 20 send a partial word last or second.
  if(mode==19 && index==n-1) byte_mask=byte_mask&3;
  if(mode==20 && index==1) byte_mask=byte_mask&12;
 end
endfunction
task automatic check;
 input condition;input [8*48-1:0] label_;
 begin
  if(condition!==1'b1) begin
   $display("ASSERT_FAIL:APB_BURST_OUTLET.%0s case=%0d cycle=%0d wi=%0d ri=%0d target=%0d/%0d",label_,cases,cycle,wi,ri,target_writes,target_reads);$fatal(1);
  end
 end
endtask
always @(posedge nl_clk) begin
 if(!nl_reset_n) begin dp_valid<=0;dp_wait<=0;fifo_count<=0;end
 else begin
  tick=tick+1;
  // Sampled before this edge's completion is counted: a write response is
  // never offered before the last access of its packet has completed.
  if(a_bvalid) check(target_writes==expected_transfers,"B_AFTER_LAST_ACCESS");
@WRSP@
  if(h_psel && !h_penable) begin
   check(!dp_valid,"SETUP_ONCE");
   check(mode!=3 && mode!=8,"REJECT_QUIET");
   check(h_paddr<4096 && h_paddr%4==0,"ADDRESS");
   check(h_pprot===(h_pwrite?3'b101:3'b001),"PROTECTION");
   if(h_pwrite) begin
    check(h_pstrb!=0,"WRITE_STROBE");
    check(h_paddr>=(burst==2?((wbase-4096)/(n*(1<<sz)))*(n*(1<<sz)):((wbase-4096)/4)*4) && h_paddr<((wbase-4096)/4)*4+1024,"ADDRESS");
   end else begin
    check(h_pstrb===0,"READ_STROBE");
    check(h_paddr===((address(rbase,target_reads)/4)*4)-4096,"READ_ADDRESS");
    check(h_paddr>=(burst==2?((rbase-4096)/(n*(1<<sz)))*(n*(1<<sz)):((rbase-4096)/4)*4) && h_paddr<((rbase-4096)/4)*4+1024,"ADDRESS");
   end
   setup_count=setup_count+1;target_addresses=target_addresses+1;
   dp_valid<=1;dp_addr<=h_paddr;dp_write<=h_pwrite;dp_strobe<=h_pstrb;dp_prot<=h_pprot;dp_data<=h_pwdata;
   dp_wait<=(DIALECT==2 || mode==6 || mode==15 || mode==16 || mode==18)?0:2+hcycle%3;
   // Mode 17 fails only the last access of each direction.
   dp_resp<=DIALECT!=2 && (((mode==2 || mode==5) && h_paddr==(h_pwrite?wbase:rbase)-4096+4) ||
            ((mode==13 || mode==16) && (h_pwrite?target_writes:target_reads)==1) ||
            (mode==17 && (h_pwrite?target_writes:target_reads)==n-1));
  end
  if(h_psel && h_penable) begin
   check(dp_valid,"SETUP");
   check({h_paddr,h_pwrite,h_pstrb,h_pprot,h_pwdata}==={dp_addr,dp_write,dp_strobe,dp_prot,dp_data},"BUS_STABLE");
   if(dp_wait>0) begin dp_wait<=dp_wait-1;wait_count=wait_count+1;end
   if(h_pready) begin
    if(error_count!=0) after_error=after_error+1;
    if(h_pslverr) error_count=error_count+1;
    if(ccount<1024) begin ctick[ccount]=tick;cdir[ccount]=dp_write;ccount=ccount+1;end
    if(dp_write) begin
     if(last_w>=0) begin gaps_w=gaps_w+1;if(tick-last_w>widest_w) widest_w=tick-last_w;end
     last_w=tick;
     target_writes=target_writes+1;
     // Mode 5 models a legal peripheral that commits despite PSLVERR.
     if(dp_resp==0 || mode==5) for(k=0;k<4;k=k+1) if(dp_strobe[k]) begin
      memory[dp_addr+k]=h_pwdata[k*8+:8];target_write_bytes=target_write_bytes+1;
     end
    end else begin
     if(last_r>=0) begin gaps_r=gaps_r+1;if(tick-last_r>widest_r) widest_r=tick-last_r;end
     last_r=tick;
     target_reads=target_reads+1;fifo_count<=fifo_count+1;
    end
    dp_valid<=0;
   end
  end
  if(dp_valid) check(h_psel && h_penable,"ACCESS_CONTIGUOUS");
 end
end
task cancel;
 begin
  nl_reset_n=0;a_awvalid=0;a_wvalid=0;a_arvalid=0;a_bready=0;a_rready=0;
  repeat(3) begin @(posedge nl_clk);#1;check({a_bvalid,a_rvalid,h_psel,h_penable}===0,"RESET");end
  @(negedge nl_clk);nl_reset_n=1;
 end
endtask
task reset;
 begin @(negedge nl_clk);cancel;end
endtask
task run_case;
 input integer beats,size_,burst_,mode_,offset;
 begin
  reset;cases=cases+1;n=beats;sz=size_;burst=burst_;mode=mode_;wbase=32'h1120+offset;rbase=32'h1800+offset;
  if(mode==8) begin wbase=32'h3120;rbase=32'h3800;end
  wi=0;ri=0;packet_ri=0;awseen=0;arseen=0;bs=0;bt=0;finished=0;cancelled=0;
  target_addresses=0;target_writes=0;target_reads=0;target_write_bytes=0;expected_write_bytes=0;expected_transfers=0;
  held_r=0;held_b=0;held_control=0;
  setup_count=0;wait_count=0;error_count=0;after_error=0;b_holds=0;r_holds=0;
  last_w=-1;last_r=-1;gaps_w=0;gaps_r=0;widest_w=0;widest_r=0;
  ccount=0;strobe_failed=0;
  for(j=0;j<4096;j=j+1) begin memory[j]=(j^8'hb3)&255;expected[j]=memory[j];end
  if(mode!=3 && mode!=8) for(j=0;j<n;j=j+1) begin
   mask=byte_mask(j);a=(address(wbase,j)/4)*4-4096;expected_data=data_word(j);
   // Each partial word fails without access. Later full words still execute.
   if(DIALECT!=4 && mask!=0 && mask!=15) begin
    strobe_failed=1;mask=0;
   end
   for(k=0;k<4;k=k+1) if(mask[k] && !(mode==2&&a==wbase-4096+4) && !((mode==13||mode==16)&&j==1) && !(mode==17&&j==n-1)) begin
    expected[a+k]=expected_data[k*8+:8];expected_write_bytes=expected_write_bytes+1;
   end
   if(mask!=0) expected_transfers=expected_transfers+1;
  end
  for(cycle=0;cycle<15000 && !finished;cycle=cycle+1) begin
   @(negedge nl_clk);
   a_awvalid=!awseen && cycle>=5;a_awaddr=wbase;a_awlen=n-1;a_awsize=sz;a_awburst=burst;a_awid=5;a_awcache=2;a_awprot=5;
   a_wvalid=wi<n;a_wdata=data_word(wi);a_wstrb=byte_mask(wi);a_wlast=wi==n-1;
   a_arvalid=!arseen && (mode!=15 || bt==1);a_araddr=rbase;a_arlen=n-1;a_arsize=sz;a_arburst=burst;a_arid=6;a_arcache=1;a_arprot=1;
   a_bready=b_holds>=2 && cycle>80 && cycle%3!=0 || mode==15 || mode==18;a_rready=cycle>70 && cycle%4!=0 || mode==15 || mode==18;
   if((mode==4 && h_psel && !h_penable) || (mode==9 && h_psel && h_penable && !h_pready) ||
      (mode==10 && a_rvalid && !a_rready) || (mode==11 && a_bvalid && !a_bready)) begin
    cancelled=1;cancel;
    repeat(8) begin @(posedge nl_clk);#1;check({h_psel,h_penable,a_rvalid,a_bvalid}===0,"RESET_NO_REPLAY");end
    finished=1;
   end
   else begin
   @(posedge nl_clk);
   if(held_control) check(h_psel && h_penable && {h_paddr,h_pprot,h_pwrite,h_pstrb,h_pwdata}===old_control,"BUS_STABLE");
   held_control=h_psel && (!h_penable || !h_pready);old_control={h_paddr,h_pprot,h_pwrite,h_pstrb,h_pwdata};
   if(a_rvalid&&!a_rready) r_holds=r_holds+1;
   if(a_bvalid&&!a_bready) b_holds=b_holds+1;
   if(held_r) check(a_rvalid && {a_rdata,a_rresp,a_rlast}==={old_rdata,old_rresp,old_rlast},"READ_STABLE");
   held_r=a_rvalid&&!a_rready;old_rdata=a_rdata;old_rresp=a_rresp;old_rlast=a_rlast;
   if(held_b) check(a_bvalid && a_bresp===old_bresp,"WRITE_STABLE");
   held_b=a_bvalid&&!a_bready;old_bresp=a_bresp;
   if(a_awvalid&&a_awready) awseen=1;
   if(a_arvalid&&a_arready) arseen=1;
   if(a_wvalid&&a_wready) wi=wi+1;
   if(p_wrsp_valid && p_wrsp_ready) begin
    check(p_wrsp_id===5 && p_wrsp_first && p_wrsp_last && p_wrsp_count===(1<<sz) && p_wrsp_address===((wbase/(1<<sz))*(1<<sz)),"PACKET_METADATA");
   end
   if(p_rrsp_valid && p_rrsp_ready) begin
    a=address(rbase,packet_ri);
    check(packet_ri<n && p_rrsp_id===6 && p_rrsp_first===(packet_ri==0) && p_rrsp_last===(packet_ri==n-1) && p_rrsp_count===(1<<sz) && p_rrsp_address===((a/(1<<sz))*(1<<sz)),"PACKET_METADATA");
    packet_ri=packet_ri+1;
   end
   if(a_bvalid) begin
    expected_code=mode==8?@DECERR@:(mode==2||mode==3||mode==5||mode==13||mode==16||mode==17||strobe_failed)?2:0;
    check(a_bid===5 && a_bresp===expected_code,"WRITE_RESULT");bs=1;
    if(a_bready) bt=bt+1;
   end
   if(a_rvalid&&a_rready) begin
    check(ri<n && a_rid===6 && a_rlast===(ri==n-1),"READ_METADATA");
    expected_code=mode==8?@DECERR@:mode==3?2:((mode==2||mode==5||mode==13||mode==16)&&ri==1 || mode==17&&ri==n-1)?2:0;
    // A response run answers as one: the failed beat's word may fail with it.
    if(a_rresp===2 && (mode==2||mode==5||mode==13||mode==16||mode==17)
       && address(rbase,ri)/@RUNW@==address(rbase,mode==17?n-1:1)/@RUNW@) expected_code=2;
    check(a_rresp===expected_code,"READ_RESULT");
    if(expected_code==0) begin
     a=address(rbase,ri)-4096;lower=a;upper=(a/(1<<sz)+1)*(1<<sz);read_mask=0;expected_data=0;
     for(j=0;j<4;j=j+1) begin
      expected_data[j*8+:8]=expected[(a/4)*4+j];
      if((a/4)*4+j>=lower && (a/4)*4+j<upper) read_mask[j*8+:8]=255;
     end
     if(mode==14) expected_data=expected_data^(ri*32'h01010101);
     check((a_rdata&read_mask)===(expected_data&read_mask),"READ_DATA");
    end
    ri=ri+1;
   end
   if(wi==n && bt==1 && ri==n) finished=1;
   end
  end
  check(finished,"PROGRESS");
  if(mode==4 || mode==9 || mode==10 || mode==11) check(cancelled,"RESET_ARRIVAL");
  if(mode!=4 && mode!=9 && mode!=10 && mode!=11) begin
   check(bt==1 && bs && awseen && arseen,"CONSERVATION");
   check(target_write_bytes==expected_write_bytes,"WRITE_BYTES");
   check(target_writes==expected_transfers,"WRITE_ONCE");
   check(setup_count==target_writes+target_reads,"COMPLETE_ONCE");
   if(mode==2 || mode==5 || mode==13 || mode==16) check(error_count==2 && after_error>=2,"ERROR_CONTINUES");
   if(mode==6 || mode==15 || mode==16) check(wait_count==0,"ZERO_WAIT");
   // With PREADY high and responses taken at once, consecutive words of a
   // burst complete every idle, SETUP and ACCESS cycle.
   if(mode==15) $display("APB_BURST_THROUGHPUT words=%0d write_gaps=%0d read_gaps=%0d widest_write=%0d widest_read=%0d",n,gaps_w,gaps_r,widest_w,widest_r);
   if(mode==15) check(gaps_w==n-1 && gaps_r==n-1 && widest_w<=3 && widest_r<=3,"THROUGHPUT");
   if(mode==17) check(error_count==2,"LAST_WORD_ERROR");
   // Mode 18 offers both directions at once: AR from the first cycle, AW
   // from the fifth. The network hands a target one direction at a time, in
   // arrival order: the outlet issues a read
   // word alone while no write word is buffered and a write word whenever
   // one is, so the write packet takes the bus as one block in the middle
   // of the read packet. Reads before the block show a lone read issues;
   // reads after it show the write wins while read words remain. Every
   // completion follows the previous one by exactly three edges (two with
   // back-to-back accesses), also at
   // each direction change: this checks the cadence of the shared APB engine
   // (idle, SETUP and ACCESS per word, one transfer at a time).
   if(mode==18) begin
    fw=-1;fr=-1;
    for(j=0;j<ccount;j=j+1)
     if(cdir[j]) begin if(fw<0) fw=j;end else if(fr<0) fr=j;
    $display("APB_BURST_ORDER words=%0d completions=%0d first_read=%0d writes=%0d..%0d",n,ccount,fr,fw,fw+n-1);
    check(ccount==2*n && fr==0 && fw>=1 && fw+n<ccount,"WRITE_PRIORITY");
    for(j=fw;j<fw+n;j=j+1) check(cdir[j],"WRITE_PRIORITY");
    for(j=1;j<ccount;j=j+1) check(ctick[j]-ctick[j-1]==3,"DIRECTION_CADENCE");
   end
   if(mode==7) check(b_holds>0 && r_holds>0,"BACKPRESSURE_ARRIVAL");
   if(mode==3||mode==8) check(target_addresses==0,"REJECT_QUIET");
   else check(target_reads==n,"READ_ONCE");
   for(j=0;j<4096;j=j+1) check(memory[j]===expected[j],"MEMORY");
  end
  $display("APB_BURST_CASE case=%0d mode=%0d beats=%0d writes=%0d reads=%0d bytes=%0d errors=%0d after_error=%0d waits=%0d b_hold=%0d r_hold=%0d cancelled=%0d gap_w=%0d gap_r=%0d",cases,mode,n,target_writes,target_reads,target_write_bytes,error_count,after_error,wait_count,b_holds,r_holds,cancelled,widest_w,widest_r);
 end
endtask
initial begin
@INIT@
 dp_addr=0;dp_wait=0;dp_resp=0;dp_strobe=0;dp_prot=0;dp_data=0;
 run_case(4,2,1,0,0);
 run_case(8,2,1,1,0);
 run_case(16,2,1,12,0);
 run_case(4,2,1,2,0);
 run_case(4,2,1,5,0);
 run_case(4,2,1,6,0);
 run_case(4,2,1,7,0);
 run_case(4,3,1,3,0);
 run_case(4,2,1,8,0);
 run_case(16,2,1,4,0);
 run_case(16,2,1,9,0);
 run_case(16,2,1,10,0);
 run_case(1,2,1,11,0);
 run_case(8,0,1,0,1);
 run_case(8,1,1,0,1);
 run_case(8,2,1,0,1);
 // APB has no stream transfer: FIXED completes with SLVERR and no access.
 run_case(8,2,0,3,0);
 run_case(8,2,2,0,4);
 run_case(32,2,1,0,0);
 run_case(256,2,1,0,0);
 run_case(4,2,1,0,732);
 $display("NOCLOOM_PASS apb-burst-outlet cases=%0d",cases);$finish;
end
endmodule
'''
    if fixed_single:
        # FIXED served as single transfers: every enabled beat is one APB
        # access at the repeated address with its own strobes; mode 13 fails
        # the second access of each direction, inside the burst; mode 14
        # reads a FIFO, so every read beat must return its own access's data.
        rejected = ' run_case(8,2,0,3,0);\n'
        assert rejected in text
        text = text.replace(rejected, ' run_case(8,2,0,0,0);\n run_case(1,2,0,0,0);\n'
                            ' run_case(8,2,0,1,0);\n run_case(16,2,0,12,0);\n'
                            ' run_case(6,0,0,0,1);\n run_case(6,1,0,0,2);\n'
                            ' run_case(5,2,0,13,0);\n'
                            ' run_case(8,2,0,14,0);\n run_case(6,0,0,14,1);\n')
    if mapping is None:
        # Throughput is measured at the outlet alone: in a network the
        # source path paces the words. Mode 16 fails the second access of
        # each direction without wait states, so the next write word is taken
        # in the cycle its predecessor's PSLVERR completes.
        # Mode 17 fails only the last access of each direction, also in a
        # one-word packet: B must wait for that access and carry its SLVERR.
        # Mode 18 runs reads and writes together without wait states.
        text = text.replace(' run_case(4,2,1,0,732);\n',
                            ' run_case(4,2,1,0,732);\n run_case(16,2,1,15,0);\n'
                            ' run_case(8,2,1,16,0);\n run_case(4,2,1,17,0);\n'
                            ' run_case(1,2,1,17,0);\n run_case(16,2,1,18,0);\n')
        if dialect != 'apb4' and cases is None:
            # Strobeless dialects: full words, cadence and write priority, and a
            # partial word last (also alone) or second, followed by full words.
            # APB2 has no wait state and no PSLVERR.
            cases = [(4, 2, 1, 0, 0), (8, 2, 2, 0, 4), (16, 2, 1, 15, 0), (16, 2, 1, 18, 0),
                     (4, 2, 1, 19, 0), (1, 2, 1, 19, 0), (6, 2, 1, 20, 0), (8, 0, 1, 0, 1)]
            if dialect == 'apb3':
                cases += [(4, 2, 1, 2, 0), (4, 2, 1, 17, 0), (1, 2, 1, 17, 0)]
        if cases is not None:
            begin = text.index(' run_case(4,2,1,0,0);\n')
            end = text.index(' $display("NOCLOOM_PASS')
            text = text[:begin] + ''.join(f' run_case({",".join(map(str, c))});\n' for c in cases) + text[end:]
    # The outlet's own write response face, without the source path latency:
    # an APB2 access cannot fail or wait, so only here can a response offered
    # during the last ACCESS cycle be told apart from one offered after it.
    text = text.replace('@DECERR@', str(decerr))
    text = text.replace('@RUNW@', str(run_word))
    text = text.replace('@WRSP@\n', '' if mapping is not None or 'wrsp' in drop_checks else
                        '  if(p_wrsp_valid) check(target_writes==expected_transfers,"WRSP_AFTER_LAST_ACCESS");\n')
    if 'b' in drop_checks:
        line = '  if(a_bvalid) check(target_writes==expected_transfers,"B_AFTER_LAST_ACCESS");\n'
        assert text.count(line) == 1
        text = text.replace(line, '')
    if mapping is not None:
        # Behind a network source a narrow INCR is packed into words and a
        # long burst is cut into 128-byte packets, each released on its own;
        # the scoreboard follows one unpacked packet per case.
        for line in (' run_case(8,0,1,0,1);\n', ' run_case(8,1,1,0,1);\n',
                     ' run_case(256,2,1,0,0);\n'):
            assert text.count(line) == 1, line
            text = text.replace(line, '')
        begin = text.index('   if(p_wrsp_valid')
        end = text.index('   if(a_bvalid)', begin)
        text = text[:begin] + text[end:]
        base = int(mapping['definition']['targets'][target]['begin'])
        text = text.replace("32'h1120", str(base+0x120)).replace("32'h1800", str(base+0x800))
        text = text.replace('-4096', f'-{base}')
        text = text.replace("32'h3120", "32'hd120").replace("32'h3800", "32'hd800")
        if quiet:
            text = text.replace('  if(h_psel && !h_penable)', '  check({' + ','.join(quiet) + '}===0,"NONSELECTED");\n  if(h_psel && !h_penable)')
        # The map's storage obligations of the target's burst owners: a read
        # packet is released once, when its last response leaves after every
        # read access; a write packet once, with its write response after
        # every write access. Both are sampled on the target clock. Unmapped
        # bursts (mode 8) and oversized beats (mode 3, INCR) are answered
        # before this target; FIXED (mode 3) is answered here without access.
        stores = {s['resource']: s for s in mapping['network'].get('storage', [])}
        name = mapping['definition']['targets'][target]['name']
        owners = [stores.get(f'burst_service/{name}/{d}') for d in ('read', 'write')]
        # A map that publishes storage must name both owners of this target.
        if stores:
            read, write = owners
            target_clock = 'nl_clk_target' if 'nl_clk_target' in clocks else 'nl_clk'
            assert read and write and all(clock_nets.get(s.get('clock'), 'nl_clk') == target_clock for s in owners)
            wire = lambda s: 'dut.nl_internal_v' + s['release'].removeprefix('value/')
            text = text.replace('integer dp_addr,dp_wait,dp_resp;', 'integer dp_addr,dp_wait,dp_resp,released_r,released_w;')
            text = text.replace('ccount=0;strobe_failed=0;', 'ccount=0;released_r=0;released_w=0;strobe_failed=0;')
            text = text.replace('  tick=tick+1;\n', '  tick=tick+1;\n'
                                f'  if({wire(read)}) begin released_r=released_r+1;check(target_reads==(mode==3?0:n),"READ_RELEASE");end\n'
                                f'  if({wire(write)}) begin released_w=released_w+1;check(target_writes==expected_transfers,"WRITE_RELEASE");end\n')
            text = text.replace('   check(bt==1 && bs && awseen && arseen,"CONSERVATION");\n',
                                '   check(bt==1 && bs && awseen && arseen,"CONSERVATION");\n'
                                '   check(released_r==(mode==8||mode==3&&burst!=0?0:1) && released_w==(mode==8||mode==3&&burst!=0?0:1),"RELEASE_ONCE");\n')
            assert text.count('released_r') == 5
        if 'nl_clk_target' in clocks:
            text = text.replace('always @(posedge nl_clk) begin', 'always @(posedge nl_clk_target) begin', 1)
            text = text.replace('cycle<15000', 'cycle<60000')
            # CDC latency must not bypass the response backpressure stimulus.
            text = text.replace('a_bready=cycle>80', 'a_bready=b_holds>=3 && cycle>80')
            text = text.replace('a_rready=cycle>70', 'a_rready=r_holds>=3 && cycle>70')
            text = text.replace('reg dp_valid=0', 'reg post_cancel=0;\nreg dp_valid=0')
            text = text.replace('cancelled=1;cancel;', 'cancelled=1;cancel;post_cancel=1;')
            text = text.replace('repeat(8) begin', 'repeat(100) begin')
            text = text.replace('reset;cases=cases+1', 'reset;post_cancel=0;cases=cases+1')
            text = text.replace('  if(h_psel && !h_penable)',
                                '  if(post_cancel) check({h_psel,h_penable}===0,"RESET_NO_REPLAY");\n  if(h_psel && !h_penable)')
            # APB stability is already checked at each target edge above.
            # A source edge cannot require SETUP to finish in one source cycle.
            text = '\n'.join(line for line in text.split('\n')
                             if not line.startswith(('   if(held_control)', '   held_control=')))
        extra = ''
        for i, clock in enumerate(c for c in clocks if c != 'nl_clk'):
            if clock == 'nl_clk_target':
                extra += f'initial begin {clock}=0; #1000; forever #{7+4*i} {clock}=~{clock}; end\n'
            else:
                extra += f'initial {clock}=0;\nalways #{7+4*i} {clock}=~{clock};\n'
        text = text.replace('always #5 nl_clk=~nl_clk;', 'always #5 nl_clk=~nl_clk;\n'+extra)
    declarations = '\n'.join(f'{kind} [{width-1}:0] {name};' for name, (kind, width) in signals.items())
    instances_text = '\n'.join(f'{module} {part}({",".join(ports)});' for (part, module), ports in instances.items())
    initial = '\n'.join(f'{name}=0;' for name, (kind, _) in signals.items() if kind == 'reg' and name != 'nl_clk' and name not in clocks)
    text = text.replace('@DECL@', declarations).replace('@INST@', '\n'.join([instances_text, *local]))
    text = text.replace('@INIT@', initial).replace('@DIALECT@', dialect[-1])
    (work / 'tb.sv').write_text(text)
