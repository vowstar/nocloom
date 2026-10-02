"""Transaction bench for the one-way Gray-count asynchronous FIFO crossing.

The bench drives both clocks from plusargs (period, per-edge jitter, phase), so one
compiled design runs every clock ratio. A sampling-skew model on each synchronizer
first stage lets every bit that changed within a window before the sampling edge
resolve to its old or new value independently. Gray counts tolerate that; any count
encoding with several changing bits does not.

Each side has its own reset (nl_reset_n_0 sender, nl_reset_n_1 receiver). The bench
asserts both together and releases them in a random order with a random gap; the
sender starts first, with the receiver clock stopped and its reset held, and may
fill the FIFO but no further (with the reset handshake: write nothing).

With +oneside=K (reset handshake designs) the bench then resets one side alone K
times while both clocks run and the other side keeps sending or taking. Every
delivered word must be a written word later than the last one delivered (no stale,
repeated or invented word); words may be lost only when written before the latest
one-side release, and the receiver may withdraw its output only while the sender is
in reset. Every word written after the last release is delivered and the FIFO ends
empty. With reset payload registers (reset_all) a word the receiver selected just as
the sender's alone reset arrived may instead leave with a cleared payload inside the
synchronization window.
"""
import re

PASS = "ASYNC_FIFO_PASS"
# The behavioral synchronizer cell model (nocloom_synchronizer.v) that inline_synchronizers flattens.
SYNCHRONIZER_MODEL = """(* ASYNC_REG = "TRUE" *) reg [STAGES - 1:0] stage;
always @(posedge clock or negedge reset_n)
  if (!reset_n)
    stage <= {STAGES{1'b0}};
  else
    stage <= {stage[STAGES - 2:0], d};
assign q = stage[STAGES - 1];
"""
SYNCHRONIZER = re.compile(
    r"^nocloom_synchronizer #\(\.STAGES\((\d+)\)\) nl_internal_v(\d+)_sync\d* \(\.clock\((\w+)\), "
    r"\.reset_n\((\w+)\), \.d\((\w+)(?:\[\d+\])?\), \.q\((\w+)(?:\[\d+\])?\)\);\n", re.M)


def inline_synchronizers(rtl, cells):
    """The module with each chain of synchronizer cell instances (one per bit) flattened into its
    stages, as the behavioral cell model in cells describes them: the first stage is register
    nl_internal_v<head> (the instance name), later stages nl_internal_v<head>_s<k> and the last
    the net the instances drive, each cleared with its clock and reset group."""
    if SYNCHRONIZER_MODEL not in cells:
        raise AssertionError("the synchronizer cell is not the behavioral model")
    chains = {}
    for n, head, clock, reset, d, q in SYNCHRONIZER.findall(rtl):
        chains.setdefault(head, [int(n), clock, reset, d, q, 0])[5] += 1
    rtl = SYNCHRONIZER.sub("", rtl)
    for head, (n, clock, reset, d, q, width) in chains.items():
        r = f"[{width - 1}:0] " if width > 1 else ""
        names = [f"nl_internal_v{head}"] + [f"nl_internal_v{head}_s{k}" for k in range(1, n - 1)] + [q]
        if rtl.count(f"wire {r}{q};\n") != 1:
            raise AssertionError(f"synchronizer output {q} not declared once")
        rtl = rtl.replace(f"wire {r}{q};\n", "".join(f"reg {r}{x};\n" for x in names))
        clear = "".join(f"    {x} <= {width}'d0;\n" for x in names)
        shift = "".join(f"    {x} <= {s};\n" for x, s in zip(names, [d] + names[:-1]))
        header = f"always @(posedge {clock} or negedge {reset}) begin\n  if (!{reset}) begin\n"
        if header not in rtl:
            tail = "endmodule\n`default_nettype wire\n"
            rtl = rtl[: -len(tail)] + header + "  end else begin\n  end\nend\n" + tail
        at = rtl.index(header) + len(header)
        rtl = rtl[:at] + clear + rtl[at:]
        at = rtl.index("  end else begin\n", at) + len("  end else begin\n")
        rtl = rtl[:at] + shift + rtl[at:]
    return rtl


def instrument(rtl, meta):
    """Insert the sampling-skew model on every synchronizer first stage."""
    extra = []
    for row in meta["first_stages"]:
        x, y, w = row["stage"], row["source"], row["width"]
        clock = meta["clocks"][row["domain"]]
        old = f"    nl_internal_v{x} <= nl_internal_v{y};"
        if rtl.count(old) != 1:
            raise AssertionError(f"first stage v{x} not found exactly once")
        rtl = rtl.replace(old, f"    nl_internal_v{x} <= nl_meta_v{x}(nl_internal_v{y});")
        r = f"[{w - 1}:0] " if w > 1 else ""
        extra += [
            f"integer nl_meta_seed_v{x};",
            f"reg {r}nl_meta_new_v{x}, nl_meta_old_v{x};",
            f"realtime nl_meta_at_v{x};",
            # The bench seeds nl_meta_seed_v* at time zero.
            f"initial begin nl_meta_new_v{x}=0; nl_meta_old_v{x}=0; nl_meta_at_v{x}=-1.0e9; end",
            f"always @(nl_internal_v{y}) begin nl_meta_old_v{x}=nl_meta_new_v{x}; nl_meta_new_v{x}=nl_internal_v{y}; nl_meta_at_v{x}=$realtime; end",
            f"function {r}nl_meta_v{x}(input {r}now);",
            "  begin",
            f"    nl_meta_v{x}=now;",
            f"    if (tb.meta_on && ($realtime-nl_meta_at_v{x}) < tb.meta_window)",
            f"      nl_meta_v{x}=now ^ ($random(nl_meta_seed_v{x}) & (nl_meta_new_v{x} ^ nl_meta_old_v{x}));",
            "  end",
            "endfunction",
            f"always @(posedge nl_internal_v{clock}) if (tb.meta_on && nl_reset_n_0 && nl_reset_n_1 && ($realtime-nl_meta_at_v{x}) < tb.meta_window && (nl_meta_new_v{x} ^ nl_meta_old_v{x}) != 0) tb.meta_events = tb.meta_events + 1;",
        ]
    marker = "endmodule\n`default_nettype wire\n"
    if not rtl.endswith(marker):
        raise AssertionError("unexpected module tail")
    return rtl[: -len(marker)] + "\n".join(extra) + "\n" + marker


def write_bench(work, meta):
    fields = meta["fields"]
    nb = meta["n"]
    hs = 1 if meta.get("handshake") else 0
    ra = 1 if meta.get("reset_all") else 0
    ports = {p["name"]: p for p in meta["ports"]}
    seeds = [row["stage"] for row in meta["first_stages"]]
    lines = [
        "`timescale 1ns/1ps",
        "module tb;",
        "reg nl_clk_0=0,nl_clk_1=0,nl_reset_n_0=0,nl_reset_n_1=0;",
        "reg nl_stream_valid=0,nl_stream_out_ready=0;",
    ]
    connections = []
    for name, p in ports.items():
        if name not in ("nl_clk_0", "nl_clk_1", "nl_reset_n_0", "nl_reset_n_1", "nl_stream_valid",
                        "nl_stream_out_ready"):
            w = p["width"]
            lines.append(("reg " if p["input"] else "wire ") + (f"[{w - 1}:0] " if w > 1 else "") + name
                         + ("=0;" if p["input"] else ";"))
        connections.append(f".{name}({name})")
    lines += [
        "integer ta=10000,tb_=10000,jitter=0,phase=0,pvalid=100,pready=100,words=200,resets=0;",
        "integer mode=0,warm=100,measure=300,want=0,seed=1,slow=10000;",
        "integer oneside=0,mark=0,ev,found,lost=0,side; reg alone_a=0;",
        "integer rs_a,rs_b,rs_tx,rs_rx,rs_ctl,unused;",
        "reg meta_on=0,cdc_monitor=1; real meta_window=0.0; integer meta_events=0;",
        "reg run_a=0,run_b=0,active=0,taken=0,stalled=0,seen_head=0;",
        "integer gen=0,tx=0,rx=0,delivered=0,canceled=0,bcount=0,lat,k,g,target,max_lat=0,min_lat=1000;",
        "real acc_time[0:4095]; real bedge[0:63]; real t1=0.0,t2=0.0; real cycles;",
    ]
    for i, f in enumerate(fields):
        lines.append(f"reg [{f['width'] - 1}:0] saved_{i};")
    lines += [
        f"{meta['module']} dut(" + ",".join(connections) + ");",
        'task fail(input string name); begin $display("ASSERT_FAIL:ASYNC_FIFO.%0s gen=%0d tx=%0d rx=%0d t=%0t",name,gen,tx,rx,$realtime); $fatal(1); end endtask',
    ] + [f"function integer pick_{n}(input integer range); begin pick_{n}=$random(rs_{n}); if(pick_{n}<0) pick_{n}=-pick_{n}; pick_{n}=pick_{n}%range; end endfunction"
         for n in ("a", "b", "tx", "rx", "ctl")] + [
        "function [31:0] value0(input integer sequence_, input integer epoch);",
        "  value0=sequence_*32'h9E3779B1 ^ epoch*32'h85EBCA6B ^ 32'h01234567; endfunction",
        "function [4:0] value1(input integer sequence_, input integer epoch);",
        "  value1=(sequence_*7+epoch*13+3)%32; endfunction",
        # Clocks: even picosecond half periods and jitter; control events use odd picoseconds.
        "initial begin #0.002; #(phase/1000.0); forever begin #((ta/2+2*pick_a(jitter/2+1))/1000.0); if(run_a) nl_clk_0=~nl_clk_0; end end",
        "initial begin #0.002; forever begin #((tb_/2+2*pick_b(jitter/2+1))/1000.0); if(run_b) nl_clk_1=~nl_clk_1; end end",
        "always @(negedge nl_clk_0) begin",
        " if(!nl_reset_n_0) begin nl_stream_valid=0; taken=0; end",
        " else begin",
        "  if(!nl_stream_valid || taken) nl_stream_valid=active && tx<words && pick_tx(100)<pvalid;",
        "  taken=0;",
        "  nl_stream_in_f0=value0(tx,gen); nl_stream_in_f1=value1(tx,gen);",
        " end",
        "end",
        "always @(posedge nl_clk_0) begin",
        " if(nl_stream_ready!==1'b0 && nl_stream_ready!==1'b1) fail(\"X_READY\");",
        " if(nl_reset_n_0) begin",
        "  if(nl_stream_valid && nl_stream_ready) begin acc_time[tx]=$realtime; tx=tx+1; taken=1; end",
        " end",
        "end",
    ]
    # Each count register that feeds a synchronizer may change one bit per update.
    for role in ("write_count", "read_count"):
        q = f"dut.nl_internal_v{meta[role]}"
        w = meta["count_bits"]
        lines += [
            f"reg [{w - 1}:0] prev_{role}=0, change_{role};",
            f"always @({q}) begin",
            f" change_{role}={q} ^ prev_{role};",
            # x & (x - 1) clears the lowest set bit: nonzero means several bits changed.
            # With the reset handshake a count also clears to zero while the peer is masked.
            f" if(nl_reset_n_{int(role == 'read_count')} && cdc_monitor && (change_{role} & (change_{role} - 1'b1)) != 0 && !({hs} && {q} == 0)) fail(\"CDC_ONE_BIT\");",
            f" prev_{role}={q};",
            "end",
        ]
    lines += [
        "always @(negedge nl_clk_1) nl_stream_out_ready=nl_reset_n_1 && pick_rx(100)<pready;",
        "always @(posedge nl_clk_1) begin",
        " bcount=bcount+1; bedge[bcount%64]=$realtime;",
        " if(nl_stream_out_valid!==1'b0 && nl_stream_out_valid!==1'b1) fail(\"X_VALID\");",
        " if(!nl_reset_n_1) begin stalled=0; seen_head=0; if(nl_stream_out_valid) fail(\"RESET_VALID\"); end",
        " else begin",
        "  if(!nl_stream_out_valid && (nl_stream_out_f0!==0 || nl_stream_out_f1!==0)) fail(\"INVALID_VISIBILITY\");",
        "  if(stalled && !alone_a && (!nl_stream_out_valid || nl_stream_out_f0!==saved_0 || nl_stream_out_f1!==saved_1)) fail(\"STALL\");",
        "  if(nl_stream_out_valid && !seen_head) begin",
        "   if(rx>=tx) fail(\"PHANTOM\");",
        # Receiver edges strictly after the write: nB synchronizer stages, one load
        # into the forward register, and this sampling edge.
        "   lat=0; for(k=0;k<16;k=k+1) if(bedge[(bcount-k+64)%64]>acc_time[rx]) lat=lat+1;",
        f"   if(lat<{nb + 2}) fail(\"RECEIVE_LATENCY\");",
        "   if(lat>max_lat) max_lat=lat; if(lat<min_lat) min_lat=lat;",
        "   seen_head=1;",
        "  end",
        "  if(nl_stream_out_valid && nl_stream_out_ready) begin",
        "   if(rx>=tx) fail(\"PHANTOM\");",
        "   if(oneside) begin",
        # The first written, not yet delivered word it equals; skipped words are lost.
        "    found=-1; for(k=rx;k<tx && found<0;k=k+1) if(nl_stream_out_f0===value0(k,gen) && nl_stream_out_f1===value1(k,gen)) found=k;",
        # reset_all: a word the receiver selected just as the sender's alone reset arrived
        # leaves with a cleared payload inside the synchronization window; it is the oldest
        # word in flight (the read positions still advance in order).
        f"    if(found<0 && {ra}!=0 && alone_a && nl_stream_out_f0===0 && nl_stream_out_f1===0) found=rx;",
        "    if(found<0) fail(\"FALSE_DATA\");",
        "    if(found>rx && found-1>=mark) fail(\"RECOVERY_LOSS\");",
        "    lost=lost+found-rx; rx=found;",
        "   end else if(nl_stream_out_f0!==value0(rx,gen) || nl_stream_out_f1!==value1(rx,gen)) fail(\"ORDER\");",
        "   rx=rx+1; delivered=delivered+1; seen_head=0;",
        "   if(rx==warm) t1=$realtime; if(rx==warm+measure) t2=$realtime;",
        "  end",
        "  stalled=nl_stream_out_valid && !nl_stream_out_ready; saved_0=nl_stream_out_f0; saved_1=nl_stream_out_f1;",
        " end",
        "end",
        # Release the two resets in a random order, the second up to four slow periods later.
        "task release_both; begin",
        " if(pick_ctl(2)) begin nl_reset_n_0=1; #(2*pick_ctl(slow*2)/1000.0); nl_reset_n_1=1; end",
        " else begin nl_reset_n_1=1; #(2*pick_ctl(slow*2)/1000.0); nl_reset_n_0=1; end",
        "end endtask",
        "initial begin",
        " for(k=0;k<64;k=k+1) bedge[k]=-1.0;",
        " unused=($value$plusargs(\"ta=%d\",ta)); unused=($value$plusargs(\"tb=%d\",tb_));",
        " unused=($value$plusargs(\"jitter=%d\",jitter)); unused=($value$plusargs(\"phase=%d\",phase));",
        " unused=($value$plusargs(\"pvalid=%d\",pvalid)); unused=($value$plusargs(\"pready=%d\",pready));",
        " unused=($value$plusargs(\"words=%d\",words)); unused=($value$plusargs(\"resets=%d\",resets));",
        " unused=($value$plusargs(\"mode=%d\",mode)); unused=($value$plusargs(\"measure=%d\",measure));",
        " unused=($value$plusargs(\"expect=%d\",want)); unused=($value$plusargs(\"seed=%d\",seed));",
        " unused=($value$plusargs(\"oneside=%d\",oneside));",
        " if(oneside && resets) fail(\"BENCH_ARGUMENTS\");",
        " if($test$plusargs(\"meta\")) meta_on=1;",
        " if($test$plusargs(\"no_cdc_monitor\")) cdc_monitor=0;",
        " if(ta%4 || tb_%4 || jitter%2 || phase%2 || words>4096 || words<1) fail(\"BENCH_ARGUMENTS\");",
        " slow=ta>tb_?ta:tb_; meta_window=(ta<tb_?ta:tb_)/2000.0;",
        " rs_a=seed*11+1; rs_b=seed*13+2; rs_tx=seed*17+3; rs_rx=seed*19+4; rs_ctl=seed*23+5;",
    ]
    for x in seeds:
        lines.append(f" dut.nl_meta_seed_v{x}=seed*29+{x};")
    lines += [
        # An explicit falling edge applies both asynchronous resets at time zero.
        " #0.001; nl_reset_n_0=1; nl_reset_n_1=1; #0.002; nl_reset_n_0=0; nl_reset_n_1=0;",
        " run_a=1; repeat(5) @(negedge nl_clk_0);",
        # The sender leaves reset first while the receiver, reset and unclocked, takes
        # nothing: the sender fills the FIFO and stops at its depth.
        " #0.001; nl_reset_n_0=1; active=1;",
        f" repeat({4 * meta['depth'] + 20}) @(negedge nl_clk_0);",
        f" if(tx>{meta['depth']} || (pvalid==100 && tx!={0 if hs else meta['depth']})) fail(\"STOPPED_RECEIVER_BOUND\");",
        " #0.001; nl_reset_n_1=1; run_b=1;",
        # One side alone, held long enough for the other side to see it, both clocks running.
        " for(ev=0; ev<oneside; ev=ev+1) begin",
        "  wait(tx>=(ev+1)*words/(oneside+1)); #(0.001+2*pick_ctl(slow/2)/1000.0);",
        "  side=pick_ctl(2);",
        "  if(side==0) begin alone_a=1; nl_reset_n_0=0; end else nl_reset_n_1=0;",
        f"  #(({nb + 4}*slow+2*pick_ctl(slow*2))/1000.0);",
        "  if(side==0) nl_reset_n_0=1; else nl_reset_n_1=1;",
        "  mark=tx; alone_a=0;",
        " end",
        " for(g=0; g<=resets; g=g+1) begin",
        "  if(g<resets) begin",
        "   target=pick_ctl(words/2+1);",
        "   wait(rx>=target || tx>=words); #(0.001+2*pick_ctl(slow/2)/1000.0);",
        "   canceled=canceled+(tx-rx); nl_reset_n_0=0; nl_reset_n_1=0; active=0; nl_stream_valid=0; taken=0;",
        "   gen=gen+1; tx=0; rx=0; seen_head=0; stalled=0;",
        "   #((2*slow+2*pick_ctl(slow*2))/1000.0);",
        "   if(nl_stream_out_valid) fail(\"RESET_CLEAR\");",
        "   active=1; release_both;",
        "  end else begin",
        "   wait(rx==words);",
        "   #(40*slow/1000.0);",
        "   if(tx!=words || rx!=words || nl_stream_out_valid) fail(\"EXACT_COUNT\");",
        "  end",
        " end",
        " if(mode==1) begin",
        "  cycles=(t2-t1)*1000.0/slow;",
        "  $display(\"ASYNC_FIFO_THROUGHPUT words=%0d slow_cycles=%0.3f\",measure,cycles);",
        "  if(want==1 && measure+1<cycles) fail(\"THROUGHPUT\");",
        "  if(want==2 && measure+1>=cycles) fail(\"NO_BUBBLE\");",
        " end",
        f" $display(\"{PASS} delivered=%0d canceled=%0d generations=%0d meta_events=%0d latency_min=%0d latency_max=%0d lost=%0d\",delivered,canceled,resets+1,meta_events,min_lat,max_lat,lost);",
        " $finish;",
        "end",
        "initial begin #1; wait(slow>0); #((1.0*words*(resets+1)*slow*400.0/(pvalid<pready?pvalid:pready)+(2000.0+100.0*oneside)*slow)/1000.0); fail(\"TIMEOUT\"); end",
        "endmodule",
    ]
    if len(fields) != 2 or fields[0]["width"] != 32 or fields[1]["width"] != 5:
        raise AssertionError("bench expects the fixture layout data[31:0], tag[4:0]")
    (work / "tb.sv").write_text("\n".join(lines) + "\n")


def parse(text):
    result = {}
    m = re.search(PASS + r" delivered=(\d+) canceled=(-?\d+) generations=(\d+) meta_events=(\d+) latency_min=(\d+) latency_max=(\d+) lost=(\d+)", text)
    if m:
        result.update(zip(("delivered", "canceled", "generations", "meta_events", "latency_min", "latency_max",
                           "lost"), map(int, m.groups())))
    m = re.search(r"ASYNC_FIFO_THROUGHPUT words=(\d+) slow_cycles=([0-9.]+)", text)
    if m:
        result["words"] = int(m.group(1))
        result["slow_cycles"] = float(m.group(2))
    m = re.search(r"ASSERT_FAIL:ASYNC_FIFO\.(\w+)", text)
    if m:
        result["failure"] = m.group(1)
    return result
