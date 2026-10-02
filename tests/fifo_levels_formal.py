"""Joint induction of confirmed FIFO levels, including physical slot ownership."""
import json
import re
import subprocess

from async_fifo_bench import inline_synchronizers
from async_fifo_formal import clock_events, finish


def level_model(rtl, meta):
    value = lambda x: f"nl_internal_v{x}"
    original = rtl
    rtl, extra = clock_events(rtl, meta, True)
    depth, width = meta["depth"], meta["count_bits"]
    rst = [value(x) for x in meta["resets"]]
    ri, wi = value(meta["read_index"]), value(meta["write_index"])
    wc, rc = value(meta["write_count"]), value(meta["read_count"])
    pub, enabled = value(meta["published"]), value(meta["enabled"])
    slots = [value(x) for x in meta["slots"]]
    extra += ["reg f_init=1; always @(posedge nl_tick) f_init<=0;",
              f"wire reset_n={rst[0]} && {rst[1]};",
              f"reg p0,p1; always @(posedge nl_tick) begin p0<={rst[0]};p1<={rst[1]};end",
              f"always @* if(f_init) assume(!{rst[0]} && !{rst[1]});",
              f"always @* if(!f_init && ((p0&&!{rst[0]})||(p1&&!{rst[1]}))) assume(!{rst[0]}&&!{rst[1]});",
              f"always @* if(!{rst[0]}) assume(!nl_stream_valid);",
              f"function [{width-1}:0] decode;input [{width-1}:0] x;integer k;begin decode=x;"
              f"for(k=1;k<{width};k=k+1)decode=decode^(x>>k);end endfunction",
              f"wire [{width-1}:0] confirmed=decode({pub})-decode({rc});",
              "reg [2:0] expected;integer j,dist;",
              f"always @* begin expected=0;for(j=0;j<{depth};j=j+1)begin dist=(j+{depth}-{ri})%{depth};case(j)"]
    extra += [f"{i}:if(dist<confirmed && {slot}>expected)expected={slot};"
              for i, slot in enumerate(slots)]
    extra += [f"endcase end if(!{enabled}) expected=0;end"]
    checks = []
    guard = "!f_init && reset_n"

    def check(name, predicate, enable=guard, sampled=False):
        checks.append(name)
        expression = f"!({enable}) || ({predicate})"
        extra.append(f"reg {name}=1; always @(posedge nl_tick) {name}<={expression};" if sampled
                     else f"wire {name}={expression};")

    check("LEVEL_CAPACITY", f"!{enabled}||confirmed<={depth}")
    check("LEVEL_COVERS_QUEUE", "nl_queue==expected")
    check("LEVEL_DECAY_BOUND", "confirmed!=0 || nl_queue==0")
    check("LEVEL_WAKE", f"!({value(meta['read'])} || {value(meta['out_valid_state'])}) || nl_wake")
    for i, slot in enumerate(slots):
        check(f"LEVEL_CONFIRMED_ONLY_{i}",
              f"nl_active{i}==({enabled}&&(({i}+{depth}-{ri})%{depth}<confirmed))")
        check(f"LEVEL_SLOT_STABLE_{i}", f"{slot}==$past({slot})",
              guard+f" && $past(reset_n) && nl_active{i} && $past(nl_active{i})", True)
    first, level = (value(meta["fields"][i]["received"]) for i in (1, 2))
    check("LEVEL_OUTPUT_HOLD", f"nl_effective>={level}",
          guard+f" && {value(meta['output_valid'])} && {first}")
    check("LEVEL_VISIBLE_BOUND", "!$past(confirmed>0)||$past(nl_queue)==$past(expected)",
          guard+" && $past(reset_n && nl_step1)", True)
    check("LEVEL_RESET_EPOCH", "!nl_stream_out_valid", f"!f_init && !$past({rst[1]})", True)

    # Decode the actual flattened synchronizer chains, checking every connection.
    chains = {}
    for row in meta["first_stages"]:
        chain = [value(row["stage"])]
        for _ in range(meta["n"]-1):
            successors = re.findall(r"(\w+) <= "+re.escape(chain[-1])+r";", original)
            if len(successors) != 1:
                raise AssertionError("counter stage must have one successor")
            chain.append(successors[0])
        chains[row["domain"]] = chain
    prior_wc, prior_rc = f"nl_prior_v{meta['write_count']}", f"nl_prior_v{meta['read_count']}"
    ordered = chains[0][::-1]+[prior_rc, rc]+chains[1][::-1]+[prior_wc, wc]
    gaps = []
    for i, (older, newer) in enumerate(zip(ordered, ordered[1:])):
        gaps.append(f"f_gap{i}")
        extra.append(f"wire [{width-1}:0] f_gap{i}=decode({newer})-decode({older});")
    extra += [f"wire [{width-1}:0] f_occ=decode({wc})-decode({rc});",
              f"wire [{width-1}:0] f_wdelta=decode({wc})-decode({prior_wc});",
              f"wire [{width-1}:0] f_rdelta=decode({rc})-decode({prior_rc});"]
    total = "+".join("{32'd0,"+gap+"}" for gap in gaps)
    # These relations also hold during reset release. Gating them by both resets loses induction.
    check("POINTER_DELTA", "f_wdelta<=1 && f_rdelta<=1", "!f_init")
    check("POINTER_CHAIN", f"({total})<={depth}", "!f_init")
    check("INDEX_RANGE", f"{wi}<{depth} && {ri}<{depth}", "!f_init")
    check("INDEX_DISTANCE", f"(({wi}+{depth}-{ri})%{depth})==(f_occ%{depth})", "!f_init")
    for i in range(depth):
        check(f"OWNED_SLOT_{i}", f"!nl_active{i} || !(nl_step0 && {value(meta['write'])} && {wi}=={i})")
    extra.append("always @* FIFO_LEVEL_ALL:assert(&{"+",".join(checks)+"});")
    return finish(rtl, extra), checks


def level_jobs(root, fixture, passed, violated):
    jobs = []
    for depth, stages in ((7, 3), (10, 4)):
        for fault in ("none", "retain_max"):
            work = root/f"levels_d{depth}_n{stages}_{fault}"
            work.mkdir()
            subprocess.run([fixture, str(work), "levels_formal", str(depth), str(stages), fault],
                           check=True, timeout=60)
            rtl = inline_synchronizers((work/"generated.v").read_text(), (work/"cells.v").read_text())
            meta = json.loads((work/"bindings.json").read_text())
            negative = fault != "none"
            text, members = level_model(rtl, meta)
            row = dict(shape=[depth, stages], fault=fault, stage="levels_negative" if negative else "levels_prove",
                       properties=members, top=meta["module"])
            accept = (lambda status, log: status == "FAIL" and "FIFO_LEVEL_ALL" in violated(log)) if negative else passed
            jobs.append((row, work/"proof", text, "bmc" if negative else "prove", "smtbmc z3",
                         14 if negative else 4, accept))
    return jobs
