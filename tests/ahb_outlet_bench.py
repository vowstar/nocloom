"""AXI source and independent byte-addressed AHB target scoreboard."""


def cases(lanes, source_lanes, window, fixed_single):
    """Case list (beats, AxSIZE, AxBURST, mode, offset) for a target word of
    lanes bytes: the main body and the burst shapes. A 32-bit target has its
    own list. A wider target runs full-width beats (AxSIZE = log2 lanes)
    and narrower beats down to bytes; writes stay below local 0x800 and reads
    from 0x800 up, so no read sees a write of its own case."""
    full = lanes.bit_length() - 1
    # A size that the source cannot carry, when there is one: SLVERR, no access.
    bad = [(4, source_lanes.bit_length(), 1, 3, 0)] if source_lanes < 128 else []
    if lanes == 4:
        body = [(4, 2, 1, 0, 0), (8, 2, 1, 1, 0), (4, 2, 1, 2, 0), (4, 2, 1, 5, 0),
                (4, 2, 1, 6, 0), (4, 2, 1, 7, 0), *bad, (4, 2, 1, 8, 0), (16, 2, 1, 4, 0),
                (8, 0, 1, 0, 1), (8, 1, 1, 0, 1), (8, 2, 1, 0, 1),
                # FIXED takes no stream transfer: SLVERR every beat, no target access.
                (8, 2, 0, 3, 0), (1, 2, 0, 3, 0),
                (8, 2, 2, 0, 4), (4, 2, 2, 0, 8),  # WRAP4 from the middle of its container
                # Write data or read acceptance held for about 1000 cycles inside a
                # fixed-length burst while the other direction waits: BUSY until it
                # resumes.
                (4, 2, 1, 15, 0), (16, 2, 1, 15, 0), (16, 2, 2, 15, 0), (4, 2, 1, 16, 0),
                (16, 2, 1, 16, 0), (16, 2, 2, 16, 0),
                # A write whose last word has no strobes releases the bus when its B
                # is sent; the read that waited behind it must then complete.
                (5, 2, 1, 17, 0), (32, 2, 1, 0, 0), (256, 2, 1, 0, 0), (4, 2, 1, 0, 732),
                # One unaligned beat: a write splits its strobes (three bytes,
                # an upper halfword, a byte); a read of the bytes up to the
                # container end partitions into legal aligned reads.
                (1, 2, 1, 0, 1), (1, 2, 1, 0, 2), (1, 2, 1, 0, 3), (1, 1, 1, 0, 1)]
        # Burst shapes: sparse words at the end, the start and the middle of
        # fixed and WRAP bursts, INCR across 1 KiB from 0x3F0 in both
        # directions, packet lengths that split into blocks, waited cycles at
        # block boundaries, an ERROR inside blocks, a packet ending without a
        # transfer in a waited BUSY cycle (mode 22), and a second packet after
        # one that ended without its last transfer (mode 23).
        shapes = [(4, 2, 1, 17, 0), (8, 2, 2, 17, 0), (4, 2, 1, 18, 0), (8, 2, 1, 18, 0),
                  (8, 2, 1, 20, 0), (8, 2, 2, 19, 4), (16, 2, 2, 19, 0), (16, 2, 1, 0, 720),
                  (16, 2, 1, 21, 1008), (2, 2, 1, 0, 0), (3, 2, 1, 0, 0), (5, 2, 1, 0, 0),
                  (7, 2, 1, 0, 0), (17, 2, 1, 0, 0), (20, 2, 1, 0, 0), (20, 2, 1, 21, 0),
                  (36, 2, 1, 21, 4), (20, 2, 1, 13, 0), (20, 2, 1, 17, 0), (4, 2, 1, 22, 0),
                  (8, 2, 2, 22, 0), (20, 2, 1, 22, 0), (4, 2, 1, 23, 0), (20, 2, 1, 23, 0)]
        if window is not None:
            # Sub-page window: writes from local 0x100 and reads from 0x000;
            # INCR past the window end stops at it and the rest answers DECERR.
            # 512 B: 0x1C4 is 15 words before the end (INCR8, INCR4 and three
            # SINGLE blocks, then DECERR); 0x1B0 ends exactly at it.
            assert window == 0x200
            body = []
            shapes = [(20, 2, 1, 0, 0xc4), (20, 2, 1, 0, 0x1c4), (20, 2, 1, 0, 0xb0),
                      (8, 2, 1, 0, 0xf0), (8, 2, 1, 0, 0x1f0), (6, 2, 1, 21, 0xe8),
                      (16, 2, 1, 17, 0xc4), (4, 2, 2, 0, 0xf0), (8, 2, 2, 0, 0xf0),
                      (20, 2, 1, 23, 0x1c4)]
        if fixed_single:
            # FIXED served as single transfers: every beat reaches the target at
            # the repeated address with its own size and strobes; the second
            # transfer of each direction errors in mode 13, so the B merges an
            # error that is neither the first nor the last beat; mode 14 reads
            # a FIFO, so every read beat must return its own transfer's data.
            at = body.index((8, 2, 0, 3, 0)) if body else 0
            body[at:at + 2] = [(8, 2, 0, 0, 0), (1, 2, 0, 0, 0), (8, 2, 0, 1, 0), (6, 0, 0, 0, 1),
                               (6, 1, 0, 0, 2), (5, 2, 0, 13, 0), (8, 2, 0, 14, 0),
                               (6, 0, 0, 14, 1), (6, 2, 0, 0, 1)] if body else []
        if window is None:
            # Mode 25: a full-rate source for the in-burst throughput.
            shapes += [(16, 2, 1, 25, 0), (16, 2, 2, 25, 0), (64, 2, 1, 25, 0), (256, 2, 1, 25, 0)]
        return body, shapes
    # Full-width beats are those of the narrower of the source and the target.
    full = min(lanes, source_lanes).bit_length() - 1
    # The longest packet that keeps writes below 0x800 (and at most 256 beats).
    longest = min(256, 0x700 // lanes)
    sixteen = min(16, longest)
    narrow = max(2, full - 1)
    # A WRAP16 of 128-byte beats goes out as SINGLE transfers, which never
    # show BUSY: the stall cases use WRAP8 there.
    wrap_long = 16 if lanes < 128 else 8
    # A source wider than the target: its full-width beats are clamped into
    # target words (INCR and WRAP keep their start and container; a FIXED
    # beat wider than the target answers SLVERR without access, as mode 3).
    clamped = []
    if source_lanes > lanes:
        wide = source_lanes.bit_length() - 1
        clamped = [(2, wide, 1, 0, 0), (4, wide, 1, 1, 0), (4, wide, 1, 24, 0),
                   (2, wide, 2, 0, 0), (4, wide, 2, 0, 2 * source_lanes), (4, wide, 0, 3, 0)]
    if window is not None:
        # A 512 B window from local 0x000: the writes start two beats (one at
        # 1024 bits) before its end, so the rest answers DECERR; the reads
        # start one window quarter lower and stay inside.
        assert window == 0x200
        beats = 2 if lanes == 128 else 4
        offset = 0x100 - (beats // 2) * lanes
        body = [(beats, full, 1, 0, offset), (beats, full, 1, 24, offset), (4, 2, 1, 0, 0xf0),
                (4, 2, 2, 0, 0xf0), (2, full, 2, 0, 0)]
        if fixed_single:
            body += [(beats, full, 0, 0, offset), (4, 0, 0, 0, 0xf1)]
        body += clamped
        # Mode 21 also runs words across block boundaries near the window end.
        return body, [(beats, full, 1, 21, offset), (beats, full, 1, 23, offset),
                      (6, 2, 1, 21, 0xe8), (20, 2, 1, 21, 0xc4)]
    body = [(4, full, 1, 0, 0), (8, full, 1, 1, 0), (4, full, 1, 2, 0), (4, full, 1, 5, 0),
            (4, full, 1, 6, 0), (4, full, 1, 7, 0), *bad, (4, full, 1, 8, 0),
            (sixteen, full, 1, 4, 0),
            # Narrow beats: bytes and halfwords are SINGLE transfers; words and
            # wider beats form bursts of HSIZE = AxSIZE.
            (8, 0, 1, 0, 1), (8, 1, 1, 0, 2), (8, 2, 1, 0, 4), (6, narrow, 1, 0, 0),
            (4, 2, 2, 0, 8), (8, narrow, 2, 0, 0), (20, 2, 1, 0, 0),
            # Sparse strobes split into blocks of several sizes.
            (8, full, 1, 24, 0), (8, narrow, 1, 24, 0), (8, 2, 1, 24, 0),
            (8, full, 0, 3, 0), (1, full, 0, 3, 0),
            # WRAP from the middle of its container; WRAP16 (SINGLE transfers
            # at 1024 bits, whose container is 2 KiB).
            (8, full, 2, 0, 4 * lanes), (4, full, 2, 0, 2 * lanes), (16, full, 2, 0, 0),
            (4, full, 1, 15, 0), (sixteen, full, 1, 15, 0), (wrap_long, full, 2, 15, 0),
            (4, full, 1, 16, 0), (sixteen, full, 1, 16, 0), (wrap_long, full, 2, 16, 0),
            (5, full, 1, 17, 0), (longest, full, 1, 0, 0),
            # Across a 1 KiB boundary: the write two beats before 0x400.
            (4, full, 1, 0, 0x300 - 2 * lanes), (5, full, 1, 13, 0),
            # One unaligned beat: the read partitions its suffix into aligned blocks.
            (1, full, 1, 0, 1), (1, full, 1, 0, 1 << (full - 1)), (1, 2, 1, 0, 5),
            # One-beat writes: a word's lower half (bytes on a wider target)
            # and the halfword start block of a doubleword at offset 6.
            (1, 2, 1, 18, 0)] + ([(1, 3, 1, 0, 6)] if source_lanes >= 8 else [])
    shapes = [(4, full, 1, 17, 0), (8, full, 2, 17, 0), (4, full, 1, 18, 0), (8, full, 1, 18, 0),
              (8, full, 1, 20, 0), (8, full, 2, 19, lanes), (2, full, 1, 0, 0), (3, full, 1, 0, 0),
              (5, full, 1, 0, 0), (7, full, 1, 0, 0), (min(17, longest), full, 1, 0, 0),
              (sixteen, full, 1, 21, 0), (4, full, 1, 21, 0x300 - 2 * lanes),
              (sixteen, full, 1, 13, 0), (sixteen, full, 1, 17, 0), (4, full, 1, 22, 0),
              (8, full, 2, 22, 0), (4, full, 1, 23, 0), (sixteen, full, 1, 23, 0),
              (20, 2, 1, 21, 0)]
    if fixed_single:
        at = body.index((8, full, 0, 3, 0))
        body[at:at + 2] = [(8, full, 0, 0, 0), (1, full, 0, 0, 0), (8, full, 0, 1, 0),
                           (6, 0, 0, 0, 1), (6, 1, 0, 0, 2), (5, full, 0, 13, 0),
                           (8, full, 0, 14, 0), (6, 0, 0, 14, 1), (4, narrow, 0, 24, 0)]
    # Mode 25: a full-rate source for the in-burst throughput.
    shapes += [(sixteen, full, 1, 25, 0), (16 if lanes < 128 else 8, full, 2, 25, 0),
               (longest, full, 1, 25, 0)]
    return body + clamped, shapes



def one_packet(body, shapes, mapping, source, target, lanes, source_lanes, window,
               wrap_container_limit=None):
    """Behind a network source every INCR is cut into packets of at most the
    pair's max_bytes from its alignment word and at its packet boundary; the
    scoreboard follows one AHB packet per case, so each INCR case keeps the
    source beats of its first packet at both its write and its read start. A
    WRAP stays one packet unless the source cuts WRAP bursts (wrap_container_limit, the
    pair's WRAP limit in bytes, is given): then a WRAP that starts at its
    container base at both starts reaches the target as one INCR (burst 4:
    the source sends WRAP, the target sees INCR), one past wrap_container_limit becomes
    two INCR packets and is left out, and the others stay WRAP. A narrow WRAP
    that the source folds into one beat (container within the source word)
    and a narrow INCR that it packs into words are left out. A source
    narrower than the target is merged into target words: write_bench runs it
    with merged, which checks the data and the bus rules, not the shapes."""
    assert source_lanes >= lanes, (source_lanes, lanes)
    # AHB packets: min(16 target words, 1 KiB) from the alignment word
    # max(source, target bytes), cut at the 1 KiB boundary and the window.
    limit, align = min(16 * lanes, 1024), max(lanes, source_lanes)
    cross = min(1024, window or 1024)
    write_base = 0x100 if window is not None or lanes > 4 else 0x120
    read_base = 0x000 if window is not None else 0x800

    def fit(case):
        beats, size, burst, mode, offset = case
        if burst == 2 and 1 << size < source_lanes and beats << size <= source_lanes:
            return None  # folded into one INCR beat at the source (not modelled here)
        if burst == 1 and beats > 1 and 1 << size < min(source_lanes, lanes):
            return None  # packed into words at the source (not modelled here)
        if burst == 2 and lanes > 4 and 1 << size < lanes:
            return None  # merged into target words in front of a wide target
        if burst == 2 and wrap_container_limit is not None:
            span = beats << size
            if all((x + offset) % span == 0 for x in (write_base, read_base)):
                # One INCR packet; a longer container is cut further.
                return (beats, size, 4, mode, offset) if span <= min(limit, 1024) else None
            if span > wrap_container_limit:
                return None  # two INCR packets (start to top, base to start)
        if burst != 1:
            return case
        for start in (write_base + offset, read_base + offset):
            first = start >> size << size
            end = min(start // align * align + limit, start // cross * cross + cross)
            beats = max(1, min(beats, (end - first) >> size))
        return (beats, size, burst, mode, offset)
    return ([c for c in map(fit, body) if c], [c for c in map(fit, shapes) if c])


def write_bench(work, mapping=None, source=0, target=0, contend=False, fixed_single=False,
                dependent=False, complete=False, blocks=False, window=None, hresp_width=None,
                waits='fixed', min_rate_pct=0, decerr=3, wrap_container_limit=None, merged=False,
                gather=0, run_word=4, protocol_only=False):
    """complete/blocks select the outlet's write_bursts: complete and
    undefined_length_bursts: false; window (bytes, below 1 KiB) selects the
    sub-page window cases. hresp_width, when given, must match the HRESP port
    width. A 1-bit HRESP (AHB-Lite/AHB5) answers ERROR only; a 2-bit HRESP
    (AMBA 2) also answers RETRY and SPLIT, which cancel the next address and
    terminate the current packet as an error. The target model stores no
    write for them. waits selects the
    target's wait states: 'fixed' (2 to 4 in every data phase), 'zero' or
    'random'. min_rate_pct, when nonzero, is the least in-burst transfers per
    hundred cycles of each direction in the full-rate (mode 25) cases.
    decerr is the code of a window miss: 3 with error codes, else 2.
    wrap_container_limit: the source cuts WRAP bursts at this pair limit (one_packet).
    merged: a source narrower than the target, whose beats the network merges
    into target words; the bench checks the bytes written and read, the
    responses and the bus rules, but not the transfer shapes or counts.
    gather: the transport word (bytes) the network gathers the source's write
    beats into before they leave a narrower link; a write held inside its first
    word reaches the target only once that word is complete, so the stall case
    holds its data after that word.
    Returns the number of cases."""
    signals, instances = {}, {}
    quiet = []
    if mapping is None:
        for line in (work / 'ports.tsv').read_text().splitlines():
            part, module, name, width, input_ = line.split()
            if name in ('nl_clk', 'nl_reset_n'):
                net = name
            elif name.startswith('nl_axi_'):
                net = 'a_' + name[7:]
            elif name.startswith('nl_ahb_'):
                net = 'h_' + name[7:]
            else:
                net = 'p_' + name[3:]
            kind = 'reg' if name in ('nl_clk', 'nl_reset_n') or input_ == '1' and (
                name.startswith(('nl_axi_', 'nl_ahb_'))) else 'wire'
            if net in signals:
                assert signals[net][1] == int(width)
            else:
                signals[net] = (kind, int(width))
            instances.setdefault((part, module), []).append(f'.{name}({net})')
    else:
        ports = []
        for port in mapping['ports']:
            name, field = port['name'], port['signal']
            if field == 'clock':
                net = 'nl_clk'
            elif field == 'reset_n':
                net = 'nl_reset_n'
            elif name.startswith(f'nl_i_s{source}_'):
                net = 'a_' + field
            elif name.startswith(f'nl_t_t{target}_'):
                net = 'h_' + field
            else:
                net = name
                if port['direction'] == 'output' and field in ('awvalid', 'arvalid', 'wvalid', 'hsel'):
                    quiet.append(net)
            signals[net] = ('reg' if port['direction'] == 'input' else 'wire', port['width'])
            ports.append(f'.{name}({net})')
        instances[('dut', mapping['module'])] = ports
    # Target response is combinational from registered target-side phase state.
    for n in ('h_hreadyout', 'h_hresp', 'h_hrdata'):
        signals[n] = ('wire', signals[n][1])
    width = signals['h_hresp'][1]
    assert width in (1, 2) and (hresp_width is None or width == hresp_width), width
    text = r'''
`timescale 1ns/1ps
module tb;
@DECL@
@INST@
initial nl_clk=0;
always #5 nl_clk=~nl_clk;
integer cases=0,cycle=0,n,sz,burst,mode,wbase,rbase,wi,ri,packet_ri,awseen,arseen,bs,bt;
// tburst is the burst the target sees: INCR for a WRAP that the source cuts
// (run_case burst 4), else the source's burst.
integer tburst;
// The target's view of the burst: a source beat wider than the target word
// is clamped into tn beats of tsz (one target word each) at the same start
// address and WRAP container; otherwise tn = n and tsz = sz.
integer tn,tsz;
// Transfers seen per direction, and those of a second source.
integer wk,rk,ok_k;
// The current BUSY run and the longest run seen while the other direction
// had a request pending.
integer busy_run,long_busy;
// A second source contends in mode 0 of the contention bench.
reg other_enable=0;
// Outlet shaping of this bench (write_bursts: complete, no undefined-length
// INCR) and the local window size; words at or above it answer DECERR.
// merged: the source beats are merged into target words (write_bench).
integer wb_complete=0,no_incr=0,limit=4096,merged=0,gather_bytes=0,hold_at=2;
// AHB burst rules on the bus, independent of the expected shapes: the open
// burst's HBURST, transfers still due (fixed-length only), last address and
// controls. sparse_from is the first write word whose strobes are not all
// set; own holds when a packet keeps the bus for its own later transfers.
integer mon_open,mon_code,mon_left,mon_addr,mon_size,mon_write,mon_prot,sparse_from,out_w;
integer own,own_stream,stream,jx,xc,xo,in_window_reads;
// Mode 23 sends the write and the read packet twice, one after the other;
// again_w and again_r mark the second packet. In mode 21, boundary_seen
// counts NONSEQ transfers of a packet that keeps the bus (a block or 1 KiB
// boundary) and boundary_waits the waited cycles among them.
integer again_w,again_r,boundary_seen=0,boundary_waits=0;
// Burst transfers of full-width and of narrower beats, and split transfers
// (smaller than their beat) with each HSIZE.
integer cover_full=0,cover_narrow=0,cover_split=0,cover_split_sizes=0;
// Error responses whose first cycle met the next transfer already in its
// address phase: ERROR, and RETRY/SPLIT (whose next address is cancelled).
integer cover_error_next=0,cover_retry_next=0;
// In-burst transfers and cycles per direction, over the run (rate_*) and
// over the case (case_*); clk_count counts rising edges.
integer rate_wb=0,rate_wc=0,rate_rb=0,rate_rc=0,clk_count=0,last_accept=0;
integer case_wb,case_wc,case_rb,case_rc,min_rate_pct=0;
always @(posedge nl_clk) clk_count<=clk_count+1;
integer j,k,a,lower,upper,expected_code,expected_write_bytes,target_write_bytes;
integer target_addresses,target_writes,target_reads,expected_transfers,lane,size_k,x,first_x;
// Target byte lanes of a beat and the blocks it splits into.
reg [@B1@:0] scan,chunk,block,tail_mask;
integer dp_addr,dp_size,dp_wait,dp_resp;
reg dp_valid=0,dp_write=0,dp_second=0,finished;
reg [7:0] memory[0:4095],expected[0:4095];
reg [@SD1@:0] expected_data,read_mask,old_rdata;
reg [1:0] old_rresp;reg old_rlast,held_r,held_b;
reg [1:0] old_bresp;reg [@CTRL1@:0] old_control;reg held_control=0,old_busy_incr=0;
reg held_cancel=0;reg [@B@*8-1:0] old_hwdata;
reg [@SB1@:0] mask;
// The driver loop advances cycle in the posedge time step, so the target
// uses a copy taken at the falling edge: every process that samples at the
// rising edge sees the same HREADY whatever the simulator's process order.
integer hcycle=0;
always @(negedge nl_clk) hcycle<=cycle;
// Wait profile of the target (wait_mode): 0 inserts 2 to 4 wait states in
// every data phase and HREADY low on every seventh cycle without one; 1 has
// zero wait states and HREADY high without a data phase; 2 draws both from a
// pseudo-random sequence (0 to 3 wait states in a quarter of the data phases,
// HREADY low in an eighth of the cycles without one). The sequence also
// advances at the falling edge.
integer wait_mode=0;
reg [15:0] lfsr=16'hace1;
always @(negedge nl_clk) lfsr<={lfsr[14:0],lfsr[15]^lfsr[13]^lfsr[12]^lfsr[10]};
// Mode 21 holds HREADY low on every other cycle without a data phase, so
// block boundaries and BUSY gaps meet waited cycles. Mode 22 holds it low
// from cycle 190 to 219 while the last write word, which has no strobes,
// arrives at cycle 200: the packet ends while the bus shows BUSY in a
// waited cycle.
assign h_hreadyout=!dp_valid ? (mode==21 ? hcycle%2==0 :
                             (wait_mode==0 ? hcycle%7!=2 : wait_mode==1 || lfsr[7:5]!=0) &&
                             (mode!=22 || hcycle<190 || hcycle>=220)) :
                dp_wait==0 && (dp_resp==0 || dp_second);
assign h_hresp=dp_valid && dp_wait==0 ? dp_resp : 0;
// Mode 14 reads a FIFO: every read transfer returns a new value.
reg [7:0] fifo_count=0;
assign h_hrdata=@HRDATA@^
               (mode==14?{@B@{fifo_count}}:@D@'h0);
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
// Source data: byte lane k of beat index differs from every other lane, so
// a byte on a wrong lane is seen.
function [@SD1@:0] data_word;
 input integer index;
 integer k;
 begin for(k=0;k<@SB@;k=k+1) data_word[k*8+:8]=(8'h20+k*16+k/16)^index;end
endfunction
// Source strobes of a beat: its bytes from the beat address to the end of
// its AxSIZE container; the mode patterns sit at the container's first lane.
function [@SB1@:0] byte_mask;
 input integer index;
 integer first,last,byte_,base_lane,c;
 begin
  first=address(wbase,index);last=(first/(1<<sz)+1)*(1<<sz);byte_mask=0;
  for(byte_=0;byte_<@SB@;byte_=byte_+1)
   if((first/@SB@)*@SB@+byte_>=first && (first/@SB@)*@SB@+byte_<last) byte_mask[byte_]=1;
  base_lane=((first>>sz)<<sz)%@SB@;c=1<<sz;
  // Mode 1: alternate bytes, the other bytes, none, the container's upper
  // half (a target word half on full-width beats, bytes on narrower ones).
  if(mode==1) begin
   case(index%4) 0:byte_mask=byte_mask&(5<<base_lane);1:byte_mask=byte_mask&(10<<base_lane);2:byte_mask=0;
    3:byte_mask=byte_mask&~(((1<<(c/2))-1)<<base_lane);endcase
  end
  if(mode==7) byte_mask=0;
  if((mode==17 || mode==22 || mode==23) && index==n-1) byte_mask=0;
  // Mode 18: the container's lower half of the last word (as mode 1); mode 19: partial middle
  // word; mode 20: partial first word only (byte 0 unset).
  if(mode==18 && index==n-1) byte_mask=byte_mask&(((1<<(c/2))-1)<<base_lane);
  if(mode==19 && index==n/2) byte_mask=byte_mask&(6<<base_lane);
  if(mode==20 && index==0) byte_mask=byte_mask&(14<<base_lane);
  // Mode 24: sparse strobes that split a container into blocks of several
  // sizes: without its first byte, without its last byte, every other byte,
  // and without both ends.
  if(mode==24) begin
   if(index%4==0 || index%4==3) byte_mask[base_lane]=0;
   if(index%4==1 || index%4==3) byte_mask[base_lane+c-1]=0;
   if(index%4==2) for(byte_=base_lane+1;byte_<base_lane+c;byte_=byte_+2) byte_mask[byte_]=0;
  end
 end
endfunction
// Strobes of a whole container: a beat with fewer is sparse.
function [@SB1@:0] container_mask;
 input integer index;
 integer first,byte_,base_lane;
 begin
  first=address(wbase,index);base_lane=((first>>sz)<<sz)%@SB@;container_mask=0;
  for(byte_=0;byte_<(1<<sz);byte_=byte_+1) container_mask[base_lane+byte_]=1;
 end
endfunction
// The container's local address.
function integer container;
 input integer base,index;
 container=((address(base,index)>>sz)<<sz)-4096;
endfunction
// The address and container of target beat index.
function integer taddress;
 input integer base,index;
 integer step,span;
 begin
  step=1<<tsz;span=tn*step;
  if(index==0 || tburst==0) taddress=base;
  else if(tburst==2) taddress=(base/span)*span+(((base/step)*step+index*step)%span);
  else taddress=(base/step)*step+index*step;
 end
endfunction
function integer tcontainer;
 input integer base,index;
 tcontainer=((taddress(base,index)>>tsz)<<tsz)-4096;
endfunction
// Target lanes strobed in target write beat k: the bytes of its source beat
// that fall in its container.
function [@B1@:0] tbeat_strobes;
 input integer k;
 integer j,q,x,lo;
 reg [@SB1@:0] m;
 begin
  tbeat_strobes=0;j=k/(tn/n);m=byte_mask(j);lo=((taddress(wbase,k)>>tsz)<<tsz);
  for(q=0;q<@SB@;q=q+1) begin
   x=(address(wbase,j)/@SB@)*@SB@+q;
   if(m[q] && x>=lo && x<lo+(1<<tsz)) tbeat_strobes[x%@B@]=1;
  end
 end
endfunction
// Target lanes of the container of target write beat k.
function [@B1@:0] tcontainer_mask;
 input integer k;
 integer q,lo;
 begin
  tcontainer_mask=0;lo=((taddress(wbase,k)>>tsz)<<tsz)%@B@;
  for(q=0;q<(1<<tsz);q=q+1) tcontainer_mask[lo+q]=1;
 end
endfunction
// Single and FIXED reads partition exactly the requested suffix into aligned blocks.
integer read_cursor;
function integer read_size;
 input integer base;
 integer start_,end_,k;
 begin
  read_size=tsz;
  if(tn==1 || tburst==0) begin
   start_=taddress(base,0)+read_cursor;
   end_=(taddress(base,0)/(1<<tsz)+1)*(1<<tsz);
   read_size=0;
   for(k=1;k<=tsz;k=k+1)
    if(start_%(1<<k)==0 && start_+(1<<k)<=end_) read_size=k;
  end
 end
endfunction
function integer read_address;
 input integer base;
 read_address=taddress(base,0)-4096+read_cursor;
endfunction
function integer read_transfers;
 input integer base;
 integer start_,end_,k,step_;
 begin
  read_transfers=1;
  if(tn==1 || tburst==0) begin
   start_=taddress(base,0);end_=(start_/(1<<tsz)+1)*(1<<tsz);read_transfers=0;
   while(start_<end_) begin
    step_=1;
    for(k=1;k<=tsz;k=k+1)
     if(start_%(1<<k)==0 && start_+(1<<k)<=end_) step_=1<<k;
    start_=start_+step_;read_transfers=read_transfers+1;
   end
  end
 end
endfunction
function integer burst_beats;
 input integer code;
 burst_beats=code<2 ? 0 : 4<<((code-2)/2);
endfunction
// Next address of a burst: WRAP turns inside its container.
function integer next_addr;
 input integer a,code,size_;
 integer span;
 begin
  span=burst_beats(code)<<size_;
  if(code>=2 && code%2==0) next_addr=(a/span)*span+((a+(1<<size_))%span);
  else next_addr=a+(1<<size_);
 end
endfunction
// HBURST of a packet from the local byte address of its first beat: a packet
// of 4, 8 or 16 beats of at least a word is INCRn (WRAPn), one beat or a
// two-beat WRAP SINGLE, other counts INCR; an INCR across a 1 KiB boundary or
// the window end is INCR and a WRAP container past the window end or larger
// than 1 KiB SINGLE. Byte and halfword packets are SINGLE.
function integer packet_code;
 input integer base;
 integer span;
 begin
  span=tn<<tsz;
  if(tsz<2 || tburst==0) packet_code=0;
  else if(tburst==2) begin
   packet_code=tn==4?2:tn==8?4:tn==16?6:0;
   if((base/span)*span+span>limit || span>1024) packet_code=0;
  end else begin
   packet_code=tn==1?0:tn==4?3:tn==8?5:tn==16?7:1;
   if(tn>1 && (((base>>tsz)<<tsz)%1024+span>1024 || ((base>>tsz)<<tsz)+span>limit)) packet_code=1;
  end
 end
endfunction
// Expected HBURST of word j's transfers, plus 8 when the word starts with
// NONSEQ. From the first sparse word (index sparse) on, transfers are
// NONSEQ SINGLE. Writes use INCR for incrementing packets and SINGLE for
// SINGLE; without undefined-length INCR an INCR packet goes out as INCR16,
// INCR8, INCR4 and SINGLE blocks bounded by the packet end, the 1 KiB
// boundary and the window end, and a complete write as SINGLE transfers.
function integer expect_word;
 input integer write_,base,j,sparse;
 integer c,s,len,a,blocked;
 begin
  c=packet_code(base-4096);
  if(write_) c=c%2==1 ? 1 : 0;
  blocked=no_incr && c==1;
  if(blocked && write_) begin c=0;blocked=0;end
  if(j>=sparse) expect_word=8;
  else if(!blocked) begin
   a=tcontainer(base,j);
   expect_word=c+((j==0 || c==0 || (c==1 && a%1024==0)) ? 8 : 0);
  end else begin
   s=0;expect_word=-1;
   while(expect_word<0) begin
    a=tcontainer(base,s);
    len=(tn-s>=16 && a%1024+(16<<tsz)<=1024 && a+(16<<tsz)<=limit) ? 16 :
        (tn-s>=8 && a%1024+(8<<tsz)<=1024 && a+(8<<tsz)<=limit) ? 8 :
        (tn-s>=4 && a%1024+(4<<tsz)<=1024 && a+(4<<tsz)<=limit) ? 4 : 1;
    if(j<s+len) expect_word=(len==16?7:len==8?5:len==4?3:0)+(j==s ? 8 : 0);
    s=s+len;
   end
  end
 end
endfunction
// The packet word a transfer belongs to (the last one for repeated words,
// which only SINGLE packets have).
function integer word_of;
 input integer base,local_;
 integer k;
 begin
  word_of=0;
  for(k=0;k<tn;k=k+1) if(tcontainer(base,k)==((local_>>tsz)<<tsz)) word_of=k;
 end
endfunction
// Lanes strobed by any target write beat of the container at local_: a
// network source clears every other byte, so HWDATA carries zero there.
reg [@B1@:0] unstrobed_ws;
function [@B1@:0] word_strobes;
 input integer base,local_;
 integer k;
 begin
  word_strobes=0;
  for(k=0;k<tn;k=k+1)
   if(merged ? tcontainer(base,k)/@B@==local_/@B@ : tcontainer(base,k)==((local_>>tsz)<<tsz))
    word_strobes=word_strobes|tbeat_strobes(k);
 end
endfunction
// A burst packet keeps the bus after word j while a later word of it still
// makes a transfer (a write word before the first sparse one).
function integer keeps_bus;
 input integer write_,base,j;
 integer k;
 begin
  keeps_bus=0;
  if(packet_code(base-4096)!=0 && (!write_ || j<sparse_from))
   for(k=j+1;k<tn;k=k+1)
    if(tcontainer(base,k)<limit && (!write_ || tbeat_strobes(k)!=0)) keeps_bus=1;
 end
endfunction
// Automatic: several processes check in the same time step, and a shared
// static argument would let one call overwrite another's condition.
task automatic check;
 input condition;input [8*48-1:0] label_;
 begin
  if(condition!==1'b1) begin
   $display("ASSERT_FAIL:AHB_OUTLET.%0s case=%0d cycle=%0d wi=%0d ri=%0d target=%0d/%0d",label_,cases,cycle,wi,ri,target_writes,target_reads);$fatal(1);
  end
 end
endtask
always @(posedge nl_clk) begin
 if(!nl_reset_n) begin dp_valid<=0;dp_wait<=0;dp_second<=0;fifo_count<=0;end
 else begin
  if(dp_valid) begin
   if(dp_wait>0) dp_wait<=dp_wait-1;
   else if(dp_resp!=0 && !dp_second) dp_second<=1;
   else if(h_hready) begin
    if(dp_write) begin
     target_writes=target_writes+1;@UNSTROBED@
     if(dp_resp==0) begin
      for(k=0;k<(1<<dp_size);k=k+1) memory[dp_addr+k]=h_hwdata[((dp_addr+k)%@B@)*8+:8];
      target_write_bytes=target_write_bytes+(1<<dp_size);
     end
    end else begin target_reads=target_reads+1;fifo_count<=fifo_count+1;end
    dp_valid<=0;
   end
  end
  if(h_hsel && h_htrans[1] && h_hready) begin
   check(mode!=3 && mode!=8,"REJECT_QUIET");
   // HSIZE never exceeds the bus (AHB-Lite 3.4) and HADDR is aligned to it.
   check(h_hsize<=@L@,"HSIZE_BUS");
   check(h_haddr<limit && h_haddr%(1<<h_hsize)==0,"ADDRESS");
   check(h_hmastlock===0,"CONTROL");
   // No burst crosses a 1 KiB boundary: an INCR restarts there with NONSEQ.
   check(!(h_htrans===3 && h_haddr%1024==0 && h_hburst%2==1),"KIB_CROSS");
   // A second source's reads sit at 3072 and above: one INCR8 burst.
   stream=h_hwrite ? 0 : other_enable && h_haddr>=3072 ? 2 : 1;
   // A packet that keeps the bus lets no other packet in until its last
   // transfer, across its blocks too.
   check(!own || own_stream==stream,"OWNERSHIP");
   if(mode==21 && own && h_htrans===2) boundary_seen=boundary_seen+1;
   // In-burst throughput: a SEQ, or a NONSEQ of a packet that keeps the bus
   // (a block or 1 KiB boundary), adds one transfer and its distance in
   // cycles from the previous address phase.
   if(h_htrans===3 || own) begin
    if(h_hwrite) begin rate_wb=rate_wb+1;rate_wc=rate_wc+clk_count-last_accept;end
    else begin rate_rb=rate_rb+1;rate_rc=rate_rc+clk_count-last_accept;end
   end
   last_accept=clk_count;
   if(stream==2) begin
    check(h_hburst===5 && h_htrans===(ok_k%8==0?2:3),"BURST_OTHER");
    own=ok_k%8<7;ok_k=ok_k+1;
   end else if(merged) begin
    if(h_hwrite) wk=wk+1;else rk=rk+1;
   end else if(h_hwrite) begin
    jx=word_of(wbase,h_haddr);xc=expect_word(1,wbase,jx,sparse_from);
    check(h_hburst===xc%8 && h_htrans===(xc>=8?2:3),"BURST");
    // A transfer stays within its beat; a burst carries whole beats, so its
    // HSIZE is AxSIZE.
    check(h_hsize<=tsz && (xc%8==0 || h_hsize===tsz),"HSIZE_BEAT");
    if(xc%8!=0) begin if(h_hsize==@L@) cover_full=cover_full+1;else cover_narrow=cover_narrow+1;end
    if(h_hsize<tsz) begin cover_split=cover_split+1;cover_split_sizes=cover_split_sizes|(1<<h_hsize);end
    own=keeps_bus(1,wbase,jx);wk=wk+1;
   end else begin
    jx=word_of(rbase,h_haddr);xc=expect_word(0,rbase,jx,tn);
    check(h_hburst===xc%8 && h_htrans===(xc>=8?2:3),"BURST");
    check(h_hsize===read_size(rbase),"HSIZE_BEAT");
    if(tn==1 || tburst==0) begin
     check(h_haddr===read_address(rbase),"READ_INTERVAL");
     read_cursor=read_cursor+(1<<h_hsize);
     if((taddress(rbase,0)%(1<<tsz))+read_cursor==(1<<tsz)) read_cursor=0;
    end
    if(xc%8!=0) begin if(h_hsize==@L@) cover_full=cover_full+1;else cover_narrow=cover_narrow+1;end
    own=keeps_bus(0,rbase,jx);rk=rk+1;
   end
   own_stream=stream;
   check(h_hprot===(h_hwrite?4'b1010:4'b0111),"PROTECTION");
   if(merged);
   else if(h_hwrite) check(h_haddr>=(tburst==2?((wbase-4096)/(tn*(1<<tsz)))*(tn*(1<<tsz)):tcontainer(wbase,0)) && h_haddr<tcontainer(wbase,0)+((tn<<tsz)>1024?(tn<<tsz):1024),"ADDRESS");
   else check(h_haddr>=(tburst==2?((rbase-4096)/(tn*(1<<tsz)))*(tn*(1<<tsz)):(tcontainer(rbase,0)/@B@)*@B@) && h_haddr<tcontainer(rbase,0)+((tn<<tsz)>1024?(tn<<tsz):1024),"ADDRESS");
   target_addresses=target_addresses+1;
   dp_valid<=1;dp_addr<=h_haddr;dp_size<=h_hsize;dp_write<=h_hwrite;dp_second<=0;
   dp_wait<=wait_mode==1 ? 0 : wait_mode==2 ? (lfsr[3:2]==0 ? lfsr[1:0] : 0) : 2+hcycle%3;
   dp_resp<=((mode==2 || mode==5 || mode==6) && h_haddr==(h_hwrite?wbase:rbase)-4096+(1<<tsz))?(mode==2?1:mode-3):
            (mode==13 && (h_hwrite?wk:rk)==2)?1:0;
  end
 end
end
// AHB burst rules at every HREADY-high address phase (AHB-Lite 3.5, AHB5
// 3.6): a fixed-length burst is exactly its beats, NONSEQ then SEQ, and ends
// with SEQ; a SEQ continues the open burst with its next address (WRAP
// inside the container) and unchanged HBURST, HSIZE, HWRITE and HPROT; BUSY
// appears only inside an open burst, showing that next address, so never
// after SINGLE or a completed fixed-length burst. A common reset or an
// ERROR response cancels the burst together with the target.
always @(posedge nl_clk)
 if(!nl_reset_n) begin mon_open=0;mon_left=0;busy_run=0;own=0;held_control=0;held_cancel=0;end
 else begin
  if(dp_valid && h_hresp>=2) begin mon_open=0;mon_left=0;own=0;end
  if(h_hready && (h_htrans===2 || h_htrans===0)) begin
   if(mon_open && mon_left>0)
    check(0,"EARLY_NONSEQ_OR_IDLE");
   mon_open=0;
  end
  if(h_hready && h_htrans===1) begin
   check(mon_open,"FIXED_BUSY_END");
   check(h_haddr===next_addr(mon_addr,mon_code,mon_size) && h_hburst===mon_code &&
         h_hwrite===mon_write && h_hsize===mon_size && h_hprot===mon_prot,"BUSY_CONTROL");
  end
  if(h_hready && h_htrans===3) begin
   check(mon_open && h_hburst===mon_code && h_hwrite===mon_write && h_hsize===mon_size &&
         h_hprot===mon_prot,"SEQ_CONTROL");
   check(h_haddr===next_addr(mon_addr,mon_code,mon_size),"SEQ_ADDRESS");
   mon_addr=h_haddr;
   if(mon_code>=2) begin mon_left=mon_left-1;mon_open=mon_left>0;end
  end
  if(h_hready && h_htrans===2) begin
   mon_code=h_hburst;mon_addr=h_haddr;mon_size=h_hsize;mon_write=h_hwrite;mon_prot=h_hprot;
   mon_left=burst_beats(h_hburst)>0 ? burst_beats(h_hburst)-1 : 0;
   mon_open=h_hburst==1 || mon_left>0;
   // Without undefined-length bursts HBURST is never INCR; a fixed INCRn
   // never crosses a 1 KiB boundary or the window end.
   if(no_incr) check(h_hburst!==1,"INCR_USED");
   if(h_hburst>1 && h_hburst%2==1)
    check(h_haddr%1024+(burst_beats(h_hburst)<<h_hsize)<=1024 &&
          h_haddr+(burst_beats(h_hburst)<<h_hsize)<=limit,"BLOCK_BOUNDS");
   // A WRAP container (beats times HSIZE) stays within 1 KiB and the window.
   if(h_hburst>1 && h_hburst%2==0)
    check((burst_beats(h_hburst)<<h_hsize)<=1024 &&
          (h_haddr/(burst_beats(h_hburst)<<h_hsize)+1)*(burst_beats(h_hburst)<<h_hsize)<=limit,"WRAP_BOUNDS");
   // Complete writes without undefined-length INCR are SINGLE transfers.
   if(wb_complete && no_incr && h_hwrite) check(h_hburst===0,"WRITE_SINGLE");
  end
  // While HREADY is LOW the address phase holds, except that an undefined-
  // length INCR may change from BUSY to any other transfer type (AHB 3.7).
  if(held_cancel) check(h_htrans===0 && !h_hsel && h_hwdata===old_hwdata,"ABORT_CANCEL");
  else if(held_control && !old_busy_incr) check({h_haddr,h_hsize,h_hburst,h_hprot,h_hwrite,h_htrans,h_hsel,h_hwdata}===old_control,"BUS_STABLE");
  held_cancel=dp_valid && !h_hready && h_hresp>=2;old_hwdata=h_hwdata;
  held_control=!h_hready && (h_hsel||dp_valid);old_control={h_haddr,h_hsize,h_hburst,h_hprot,h_hwrite,h_htrans,h_hsel,h_hwdata};
  old_busy_incr=h_htrans===1 && h_hburst===1;
  // The first response cycle met the next transfer in its address phase,
  // which holds for ERROR and is cancelled for RETRY/SPLIT.
  if(!h_hready && dp_valid && h_hresp!=0 && h_hsel && h_htrans[1]) begin
   if(h_hresp>=2) cover_retry_next=cover_retry_next+1;
   else cover_error_next=cover_error_next+1;
  end
  // A block's NONSEQ may wait on the previous data phase (the address phase
  // overlaps it) or on a waited cycle without one.
  if(mode==21 && !h_hready && own && h_hsel && h_htrans===2)
   boundary_waits=boundary_waits+1;
  busy_run=h_hsel && h_htrans===1 ? busy_run+1 : 0;
  if((mode==15 && arseen) || (mode==16 && awseen && wi>0))
   if(busy_run>long_busy) long_busy=busy_run;
 end
task reset;
 begin
  @(negedge nl_clk);nl_reset_n=0;a_awvalid=0;a_wvalid=0;a_arvalid=0;a_bready=0;a_rready=0;
  repeat(3) begin @(posedge nl_clk);#1;check({a_bvalid,a_rvalid,h_hsel,h_htrans}===0,"RESET");end
  @(negedge nl_clk);nl_reset_n=1;
 end
endtask
task run_case;
 input integer beats,size_,burst_,mode_,offset;
 begin
  reset;cases=cases+1;n=beats;sz=size_;burst=burst_==4?2:burst_;tburst=burst_==4?1:burst_;mode=mode_;wbase=@WBASE@+offset;rbase=@RBASE@+offset;
  tsz=sz>@L@?@L@:sz;tn=sz>@L@?n<<(sz-@L@):n;
  if(mode==8) begin wbase=32'h3120;rbase=32'h3800;end
  hold_at=(gather_bytes>>sz)>2 ? gather_bytes>>sz : 2;
  wi=0;ri=0;packet_ri=0;awseen=0;arseen=0;bs=0;bt=0;finished=0;again_w=0;again_r=0;
  wk=0;rk=0;read_cursor=0;ok_k=0;own=0;mon_open=0;mon_left=0;busy_run=0;long_busy=0;

  // From the first write word whose strobes do not cover its AxSIZE
  // container (an unaligned start leaves the first word partial), transfers
  // are single; a word outside the window has no target access and counts as
  // sparse too.
  sparse_from=tn;out_w=0;in_window_reads=0;
  for(j=tn-1;j>=0;j=j-1) begin
   if(tbeat_strobes(j)!=tcontainer_mask(j) || tcontainer(wbase,j)>=limit) sparse_from=j;
   if(tcontainer(wbase,j)>=limit) out_w=1;
   if(tcontainer(rbase,j)<limit) in_window_reads=in_window_reads+read_transfers(rbase);
  end
  // Merged words use SINGLE transfers wherever they are sparse.
  if(merged) sparse_from=0;
  target_addresses=0;target_writes=0;target_reads=0;target_write_bytes=0;expected_write_bytes=0;expected_transfers=0;
  held_r=0;held_b=0;case_wb=rate_wb;case_wc=rate_wc;case_rb=rate_rb;case_rc=rate_rc;
  for(j=0;j<4096;j=j+1) begin memory[j]=(j^8'hb3)&255;expected[j]=memory[j];end
  // Each source byte lands at its own address. Without byte enables on the
  // bus a target write beat is one transfer when its strobes are its whole
  // container, the start block of a one-beat or FIXED beat, or exactly the
  // lower or upper half of the target word, and one transfer per strobed
  // byte otherwise.
  if(mode!=3 && mode!=8) for(j=0;j<n;j=j+1) if(container(wbase,j)<limit) begin
   mask=byte_mask(j);a=container(wbase,j);expected_data=data_word(j);
   first_x=((address(wbase,j)-4096)/@SB@)*@SB@;
   for(k=0;k<@SB@;k=k+1) if(mask[k]) begin
    x=first_x+k;
    if(!((mode==2||mode==5||mode==6)&&a==wbase-4096+(1<<sz)) &&
       !((mode==5||mode==6)&&j>=1) && !(mode==13&&j==1)) begin
     expected[x]=expected_data[k*8+:8];expected_write_bytes=expected_write_bytes+(mode==23?2:1);
    end
   end
  end
  if(mode!=3 && mode!=8) for(j=0;j<tn;j=j+1) if(tcontainer(wbase,j)<limit &&
       (!(mode==5||mode==6) || j<2)) begin
   scan=tbeat_strobes(j);chunk=tcontainer_mask(j);block=0;
   for(k=0;k<@B@;k=k+1) block[k]=k<@B@/2;
   // The start block of a one-beat or FIXED beat: its bytes from the start
   // lane to the container end, when they form an aligned 2^k block.
   size_k=taddress(wbase,j)%@B@;x=(1<<tsz)-size_k%(1<<tsz);tail_mask=0;
   for(k=0;k<@B@;k=k+1) tail_mask[k]=chunk[k] && k>=size_k;
   first_x=(tn==1 || tburst==0) && (x&(x-1))==0 && scan===tail_mask;
   lane=0;for(k=0;k<@B@;k=k+1) lane=lane+scan[k];
   if(scan===chunk || first_x || scan===block || scan===~block) lane=lane!=0;
   expected_transfers=expected_transfers+lane*(mode==23?2:1);
  end
  for(cycle=0;cycle<15000 && !finished;cycle=cycle+1) begin
   @(negedge nl_clk);
   a_awvalid=!awseen && cycle>=(mode==16?20:5);a_awaddr=wbase;a_awlen=n-1;a_awsize=sz;a_awburst=burst;a_awid=5;a_awcache=2;a_awprot=5;
   // Mode 12 makes each write word wait for the read beat before it. AXI
   // A3.3 forbids this dependency on another transaction of the same
   // manager, so the outlet does not resolve it: a fixed-length write burst
   // that holds the bus waits for its next word, and the case never ends.
   // Mode 15 holds write data for about 1000 cycles after the second word, or after the
   // first transport word the network gathers (gather_bytes) when that holds more beats;
   // mode 22 holds the last word until cycle 200.
   // Mode 25 runs the source at full rate: data every cycle, B and R
   // always accepted.
   a_wvalid=wi<n && (mode==25 || cycle%5!=1) && (mode!=12 || wi<=ri) && (mode!=15 || wi!=hold_at || cycle>=1100) &&
            (mode!=16 || cycle>=20) && (mode!=22 || wi!=n-1 || cycle>=200);
   a_wdata=data_word(wi);a_wstrb=byte_mask(wi);a_wlast=wi==n-1;
   // Modes 12 and 15 let the write burst take the bus first.
   a_arvalid=!arseen && (mode!=12 && mode!=15 && mode!=17 || cycle>=20);a_araddr=rbase;a_arlen=n-1;a_arsize=sz;a_arburst=burst;a_arid=6;a_arcache=1;a_arprot=1;
   // Mode 16 holds RREADY low for about 1000 cycles: once the response path
   // fills, the read burst waits for a response slot in the middle.
   a_bready=mode==25 || cycle>80 && cycle%3!=0;
   a_rready=mode==25 || cycle>70 && cycle%4!=0 && (mode!=16 || cycle>=1100);
   @(posedge nl_clk);
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
    expected_code=mode==8||out_w?@DECERR@:(mode==2||mode==3||mode==5||mode==6||mode==13)?2:0;
    check(a_bid===5 && a_bresp===expected_code,"WRITE_RESULT");bs=1;
    if(a_bready) bt=bt+1;
   end
   // Mode 23: the second write packet starts after the first one's B.
   if(mode==23 && bt==1 && !again_w) begin again_w=1;awseen=0;wi=0;bt=0;end
   if(a_rvalid&&a_rready) begin
    check(ri<n && a_rid===6 && a_rlast===(ri==n-1),"READ_METADATA");
    expected_code=mode==8||(address(rbase,ri)/4)*4-4096>=limit?@DECERR@:mode==3?2:
      ((mode==2||mode==13)&&ri==1 || (mode==5||mode==6)&&ri>=1)?2:0;
    // ERROR affects its source beat. RETRY/SPLIT also terminate the remaining packet.
    check(a_rresp===expected_code,"READ_RESULT");
    if(expected_code==0) begin
     a=address(rbase,ri)-4096;lower=a;upper=(a/(1<<sz)+1)*(1<<sz);read_mask=0;expected_data=0;
     for(j=0;j<@SB@;j=j+1) begin
      expected_data[j*8+:8]=expected[(a/@SB@)*@SB@+j];
      if((a/@SB@)*@SB@+j>=lower && (a/@SB@)*@SB@+j<upper) read_mask[j*8+:8]=255;
      if(mode==14) expected_data[j*8+:8]=expected_data[j*8+:8]^ri;
     end
     check((a_rdata&read_mask)===(expected_data&read_mask),"READ_DATA");
    end
    ri=ri+1;
   end
   if(mode==23 && ri==n && !again_r) begin again_r=1;arseen=0;ri=0;packet_ri=0;end
   if(mode==4 && cycle==35) begin reset;finished=1;end
   else if(wi==n && bt==1 && ri==n && (mode!=23 || again_w && again_r)) finished=1;
  end
  check(finished,"PROGRESS");
  case_wb=rate_wb-case_wb;case_wc=rate_wc-case_wc;case_rb=rate_rb-case_rb;case_rc=rate_rc-case_rc;
  if(mode==25) begin
   $display("NOCLOOM_RATE ahb-outlet case=%0d beats=%0d size=%0d burst=%0d read=%0d/%0d write=%0d/%0d",
            cases,n,sz,burst,case_rb,case_rc,case_wb,case_wc);
   // In-burst transfers per cycle, in percent, at least min_rate_pct.
   if(min_rate_pct>0) check(case_rb*100>=case_rc*min_rate_pct && case_wb*100>=case_wc*min_rate_pct,"THROUGHPUT");
  end
  // Every fixed-length burst completes its promised address phases.
  check(!(mon_open && mon_left>0),"FIXED_END");
  // The long stall happened inside a burst while the other direction waited.
  // A complete write that goes out as SINGLE transfers (WRAP, or without
  // undefined-length INCR) keeps the bus with IDLE instead of BUSY. The
  // outlet's two response entries and the source's own buffering absorb a
  // short read: only a read of more than eight beats must stall in BUSY.
  if(!merged && ((mode==15 && n>hold_at && !(tburst==2 || no_incr)) ||
                 (mode==16 && tn>8)))
   check(long_busy>=900,"LONG_STALL_COVER");
  if(mode!=4) begin
   check(bt==1 && bs && awseen && arseen,"CONSERVATION");
   check(target_write_bytes==expected_write_bytes,"WRITE_BYTES");
   check(merged || target_writes==expected_transfers,"WRITE_ONCE");
   if(mode==3||mode==8) check(target_addresses==0,"REJECT_QUIET");
   else check(merged || target_reads==((mode==5||mode==6)?2:in_window_reads*(mode==23?2:1)),"READ_ONCE");
   for(j=0;j<4096;j=j+1) check(memory[j]===expected[j],"MEMORY");
  end
 end
endtask
initial begin
@INIT@
 dp_addr=0;dp_size=0;dp_wait=0;dp_resp=0;
@OPTIONS@
@BODY@ // With the fixed wait profile, waited cycles met a block or 1 KiB boundary
 // in the mode 21 cases (the other profiles need not reach one), and the
 // block cases reached block boundaries at all.
 check(wait_mode!=0 || boundary_seen==0 || boundary_waits>0,"BOUNDARY_WAIT_COVER");
 check(merged || !no_incr || boundary_seen>0,"BOUNDARY_COVER");
 $display("NOCLOOM_COVER ahb-outlet boundary_seen=%0d boundary_waits=%0d",boundary_seen,boundary_waits);
 $display("NOCLOOM_COVER ahb-outlet bursts full=%0d narrow=%0d split=%0d split_sizes=%0d",cover_full,cover_narrow,cover_split,cover_split_sizes);
 $display("NOCLOOM_RATE ahb-outlet total read=%0d/%0d write=%0d/%0d error_next=%0d retry_next=%0d",
          rate_rb,rate_rc,rate_wb,rate_wc,cover_error_next,cover_retry_next);
 $display("NOCLOOM_PASS ahb-outlet cases=%0d",cases);$finish;
end
endmodule
'''
    # Target word (HWDATA) and source word (WDATA) in bytes: every beat of a
    # case fits one target word, so a wider source runs beats no wider than
    # the target.
    lanes = signals['h_hwdata'][1] // 8
    source_lanes = signals['a_wdata'][1] // 8
    size = lanes.bit_length() - 1
    assert lanes in (4, 8, 16, 32, 64, 128) and source_lanes >= 4
    body, shapes = cases(lanes, source_lanes, window, fixed_single)
    if protocol_only:
        body = [(4, 2, 1, 18, 0), (8, 2, 1, 19, 0), (16, 2, 2, 17, 0),
                (4, 2, 1, 2, 0), (16, 2, 1, 21, 1008)]
        shapes = []
    if merged:
        # Errors injected at a target word reach every source beat merged
        # into it; the scoreboard expects them on one beat.
        assert source_lanes < lanes
        body, shapes = ([c for c in x if c[3] not in (2, 5, 6, 13)] for x in (body, shapes))
    elif mapping is not None:
        body, shapes = one_packet(body, shapes, mapping, source, target, lanes, source_lanes,
                                  window, wrap_container_limit)
    if dependent:
        # Negative control: write data that depends on reads of the same
        # manager is outside the AXI contract and is expected to hold.
        body, shapes = [(8, 2, 1, 12, 0)], []
        # Report the shape of the hold before PROGRESS fails, so that the
        # driver can tell this hold from a stall with another cause.
        progress = '  check(finished,"PROGRESS");\n'
        assert text.count(progress) == 1
        text = text.replace(progress, '  if(!finished) $display("NOCLOOM_HOLD wi=%0d ri=%0d wk=%0d rk=%0d '
                            'htrans=%0d hwrite=%0d hburst=%0d haddr=%0d busy_run=%0d target=%0d/%0d '
                            'mon_left=%0d",wi,ri,wk,rk,h_htrans,h_hwrite,h_hburst,h_haddr,busy_run,'
                            'target_writes,target_reads,mon_left);\n' + progress)
    if width == 1:
        # Modes 5 and 6 answer RETRY and SPLIT, which a 1-bit HRESP cannot
        # encode; mode 2 still answers ERROR in both directions.
        body = [c for c in body if c[3] not in (5, 6)]
    rendered = ''.join(f' run_case({",".join(map(str, c))});\n' for c in body + shapes)
    text = text.replace('@BODY@', rendered)
    options = [f' wait_mode={["fixed", "zero", "random"].index(waits)};',
               f' min_rate_pct={min_rate_pct};']
    if complete:
        options.append(' wb_complete=1;')
    if blocks:
        options.append(' no_incr=1;')
    if window is not None:
        options.append(f' limit={window};')
    if merged:
        options.append(' merged=1;')
    if gather:
        options.append(f' gather_bytes={gather};')
    text = text.replace('@OPTIONS@', '\n'.join(options))
    # Local write and read bases: 0x120 and 0x800 on a 32-bit target, 0x100
    # (aligned for every width) and 0x800 otherwise; 0x100 and 0x000 in a
    # sub-page window.
    write_base = 0x100 if window is not None or lanes > 4 else 0x120
    read_base = 0x000 if window is not None else 0x800
    base = 0x1000
    if mapping is not None:
        # Public ports do not expose the internal packet metadata probes.
        begin = text.index('   if(p_wrsp_valid')
        end = text.index('   if(a_bvalid)', begin)
        text = text[:begin] + text[end:]
        base = int(mapping['definition']['targets'][target]['begin'])
        text = text.replace('-4096', f'-{base}')
        text = text.replace("32'h3120", "32'h9120").replace("32'h3800", "32'h9800")
        if quiet:
            text = text.replace(' else begin\n  if(dp_valid)', ' else begin\n  check({' + ','.join(quiet) + '}===0,"NONSELECTED");\n  if(dp_valid)')
    # Behind a network source every byte without its strobe arrives as zero;
    # the bare outlet passes its input data as given.
    text = text.replace('@UNSTROBED@', '' if mapping is None else
                        '\n     unstrobed_ws=word_strobes(wbase,dp_addr);'
                        '\n     for(k=0;k<@B@;k=k+1) if(!unstrobed_ws[k]) '
                        'check(h_hwdata[k*8+:8]===0,"UNSTROBED_BYTE");')
    text = text.replace('@WBASE@', str(base + write_base)).replace('@RBASE@', str(base + read_base))
    hrdata = ','.join(f'memory[(dp_addr&{4096 - lanes})+{k}]' if k else f'memory[dp_addr&{4096 - lanes}]'
                      for k in reversed(range(lanes)))
    for key, value in {'@HRDATA@': '{' + hrdata + '}', '@B1@': lanes - 1, '@B@': lanes,
                       '@D@': lanes * 8, '@L@': size, '@SB1@': source_lanes - 1,
                       '@SB@': source_lanes, '@SD1@': source_lanes * 8 - 1,
                       '@CTRL1@': max(127, lanes * 8 + 63), '@DECERR@': decerr, '@RUNW@': run_word}.items():
        text = text.replace(key, str(value))
    if contend:
        assert mapping is not None
        other = f'nl_i_s{1-source}_'
        base = int(mapping['definition']['targets'][target]['begin'])
        text = text.replace("integer cases=", "integer other_ar=0,other_r=0,other_byte;\nreg [31:0] other_expected;\ninteger cases=")
        text = text.replace("nl_reset_n=0;a_awvalid", "other_enable=0;nl_reset_n=0;a_awvalid")
        text = text.replace("mode=mode_;wbase=", "mode=mode_;other_enable=(mode_==0);wbase=")
        extra = r"""
always @(negedge nl_clk) begin
 @O@arvalid=nl_reset_n && other_enable && !other_ar;
 @O@araddr=@BASE@+3072;@O@arlen=7;@O@arsize=2;@O@arburst=1;@O@arid=9;@O@arcache=1;@O@arprot=1;
 @O@rready=nl_reset_n && cycle>150 && cycle%3!=1;
end
always @(posedge nl_clk) begin
 if(!nl_reset_n) begin other_ar=0;other_r=0;end
 else begin
  if(@O@arvalid&&@O@arready) other_ar=1;
  if(@O@rvalid&&@O@rready) begin
   for(other_byte=0;other_byte<4;other_byte=other_byte+1)
    other_expected[other_byte*8+:8]=((3072+4*other_r+other_byte)^8'hb3)&255;
   check(other_r<8 && @O@rid===9 && @O@rresp===0 && @O@rlast===(other_r==7) && @O@rdata===other_expected,"SECOND_SOURCE");
   other_r=other_r+1;
  end
 end
end
""".replace('@O@', other).replace('@BASE@', str(base))
        text = text.replace('initial begin\n@INIT@', extra + '\ninitial begin\n@INIT@')
        text = text.replace('else check(h_haddr>=', 'else check((mode==0 && h_haddr>=3072 && h_haddr<3104) || h_haddr>=')
        text = text.replace('wi==n && bt==1 && ri==n', 'wi==n && bt==1 && ri==n && (mode!=0 || other_r==8)')
        reads = 'target_reads==((mode==5||mode==6)?2:in_window_reads*(mode==23?2:1))'
        assert reads + ',' in text
        text = text.replace(reads + ',', reads + '+(mode==0?8:0),')
    declarations = '\n'.join(f'{k} [{w-1}:0] {n};' for n, (k, w) in signals.items())
    instances_text = '\n'.join(f'{m} {p}({",".join(ps)});' for (p, m), ps in instances.items())
    initial = '\n'.join(f'{n}=0;' for n, (k, _) in signals.items() if k == 'reg' and n != 'nl_clk')
    text = text.replace('@DECL@', declarations).replace('@INST@', instances_text).replace('@INIT@', initial)
    (work / 'tb.sv').write_text(text)
    begin = text.index('initial begin\n')
    return text[begin:].count(' run_case(')
