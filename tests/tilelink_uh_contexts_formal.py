"""Context ranges, ownership and packet retirement on the generated target."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import tempfile


def cover_guidance(capacity, scenario):
    # Concrete legal traffic guides reachability without constraining the proof.
    guide = r'''
`ifdef NL_CONTEXT_COVER
(* anyconst *) reg [1:0] cover_case;
reg [4:0] cover_reads=0;
reg [2:0] cover_writes=0;
reg [3:0] cover_q_d=0;
always @(posedge nl_clk) if(nl_reset_n) begin
  if(nl_rreq_valid && nl_rreq_ready) cover_reads<=cover_reads+1;
  if(nl_wreq_valid && nl_wreq_ready) cover_writes<=cover_writes+1;
  if(nl_tl_d_valid && nl_tl_d_ready && nl_tl_d_source==1) cover_q_d<=cover_q_d+1;
end
always @* begin
  assume(nl_tl_a_ready);
  assume(nl_wreq_id==0 && nl_wreq_source==0 && nl_wreq_tag==0);
  assume(nl_wreq_address==8192 && nl_wreq_length==3);
  assume(nl_wreq_count==4 && nl_wreq_be==15);
  assume(nl_rreq_count==4 && nl_rreq_be==15 && nl_rreq_first && nl_rreq_last);
  assume(nl_rreq_tag==0);
  assume(nl_tl_d_data==0 && !nl_tl_d_corrupt);
  assume(nl_wrsp_ready);
  if(cover_case==0) begin
    assume(!nl_wreq_valid);
    assume(nl_rreq_valid==(cover_reads<CAPACITY));
    assume(nl_rreq_address==4096+cover_reads*256 && nl_rreq_length==0);
    assume(nl_rreq_id==cover_reads[3:0] && nl_rreq_source==0);
    assume(!nl_tl_d_valid && !nl_rrsp_ready);
  end else if(cover_case==1) begin
    assume(nl_rreq_valid==(cover_reads==0));
    assume(nl_rreq_address==4096 && nl_rreq_length==0 && nl_rreq_id==1 && nl_rreq_source==0);
    assume(nl_wreq_valid==(nl_probe_tl_x_context0_x_tl_x_rfull &&
      (cover_writes==0 || (cover_writes<4 && nl_probe_tl_x_context1_x_tl_x_d_done))));
    assume(!nl_rrsp_ready && !nl_tl_d_denied);
    if(nl_probe_tl_x_context0_x_tl_x_message && nl_probe_tl_x_context0_x_tl_x_a_done && !nl_probe_tl_x_context0_x_tl_x_d_done) begin
      assume(nl_tl_d_valid && nl_tl_d_source==0 && nl_tl_d_size==2 && nl_tl_d_opcode==1);
    end else begin
      assume(nl_tl_d_valid==(nl_tl_a_valid && nl_tl_a_opcode!=4));
      assume(nl_tl_d_source==1 && nl_tl_d_size==4 && nl_tl_d_opcode==0);
    end
  end else if(cover_case==2) begin
    assume(!nl_rreq_valid && nl_rreq_address==4096 && nl_rreq_length==0);
    assume(nl_rreq_id==0 && nl_rreq_source==0);
    assume(nl_wreq_valid==(cover_writes==0 || (cover_writes<4 && nl_probe_tl_x_context0_x_tl_x_d_done)));
    assume(nl_rrsp_ready && nl_tl_d_denied);
    assume(nl_tl_d_valid==(nl_tl_a_valid && nl_probe_tl_x_context0_x_tl_x_a_left==4));
    assume(nl_tl_d_source==0 && nl_tl_d_size==4 && nl_tl_d_opcode==0);
  end else begin
    assume(!nl_wreq_valid && nl_rrsp_ready && !nl_tl_d_denied);
    assume(nl_rreq_valid==(cover_reads<2));
    assume(nl_rreq_address==(cover_reads==0 ? 4100 : 8192));
    assume(nl_rreq_length==(cover_reads==0 ? 2 : 7));
    assume(nl_rreq_id==cover_reads[3:0] && nl_rreq_source==0);
    assume(nl_tl_d_opcode==1);
    if(nl_probe_tl_x_context0_x_tl_x_size==2 || cover_q_d>=8) begin
      assume(nl_tl_d_source==0 && nl_tl_d_size==nl_probe_tl_x_context0_x_tl_x_size);
      assume(nl_tl_d_valid==(nl_probe_tl_x_context0_x_tl_x_message && nl_probe_tl_x_context0_x_tl_x_a_done && !nl_probe_tl_x_context0_x_tl_x_d_done));
    end else begin
      assume(nl_tl_d_source==1 && nl_tl_d_size==5);
      assume(nl_tl_d_valid==(nl_probe_tl_x_context0_x_tl_x_message && nl_probe_tl_x_context0_x_tl_x_a_done &&
        nl_probe_tl_x_context1_x_tl_x_message && nl_probe_tl_x_context1_x_tl_x_a_done && !nl_probe_tl_x_context1_x_tl_x_d_done));
    end
  end
end
`endif
'''
    if capacity >= 3:
        start = guide.index("  end else if(cover_case==1) begin")
        end = guide.index("  end else if(cover_case==2) begin", start)
        guide = guide[:start] + r'''  end else if(cover_case==1) begin
    assume(nl_rreq_valid==(cover_reads<2));
    assume(nl_rreq_address==4096+cover_reads*256 && nl_rreq_length==1 && nl_rreq_id==cover_reads+1 && nl_rreq_source==0);
    assume(nl_wreq_valid==(nl_probe_tl_x_context0_x_tl_x_rfull && nl_probe_tl_x_context0_x_tl_x_dfull &&
      nl_probe_tl_x_context1_x_tl_x_rfull && nl_probe_tl_x_context1_x_tl_x_dfull &&
      (cover_writes==0 || (cover_writes<4 && nl_probe_tl_x_context2_x_tl_x_d_done))));
    assume(!nl_rrsp_ready && !nl_tl_d_denied);
    if(nl_probe_tl_x_context0_x_tl_x_message && nl_probe_tl_x_context0_x_tl_x_a_done && !nl_probe_tl_x_context0_x_tl_x_d_done) begin
      assume(nl_tl_d_valid && nl_tl_d_source==0 && nl_tl_d_size==3 && nl_tl_d_opcode==1);
    end else if(nl_probe_tl_x_context1_x_tl_x_message && nl_probe_tl_x_context1_x_tl_x_a_done && !nl_probe_tl_x_context1_x_tl_x_d_done) begin
      assume(nl_tl_d_valid && nl_tl_d_source==1 && nl_tl_d_size==3 && nl_tl_d_opcode==1);
    end else begin
      assume(nl_tl_d_valid==(nl_tl_a_valid && nl_tl_a_opcode!=4));
      assume(nl_tl_d_source==2 && nl_tl_d_size==4 && nl_tl_d_opcode==0);
    end
''' + guide[end:]
        guide = guide.replace("assume(!nl_wreq_valid && nl_rrsp_ready && !nl_tl_d_denied);",
            "assume(nl_wreq_valid==(cover_reads>=2 && cover_writes<4)); assume(nl_rrsp_ready);")
        guide = guide.replace("    assume(nl_tl_d_opcode==1);\n    if(nl_probe_tl_x_context0_x_tl_x_size==2", r'''    if(nl_tl_a_valid && nl_tl_a_opcode!=4 && nl_probe_tl_x_context2_x_tl_x_a_left==4 && !nl_probe_tl_x_context2_x_tl_x_d_done) begin
      assume(nl_tl_d_valid && nl_tl_d_source==2 && nl_tl_d_size==4 && nl_tl_d_opcode==0 && nl_tl_d_denied);
    end else begin
    assume(nl_tl_d_opcode==1 && !nl_tl_d_denied);
    if(nl_probe_tl_x_context0_x_tl_x_size==2''')
        guide = guide.replace("    end\n  end\nend\n`endif", "    end\n    end\n  end\nend\n`endif")
    return guide.replace('CAPACITY', str(capacity)).replace('assume(nl_tl_a_ready);',
        f'assume(cover_case=={scenario}); assume(nl_tl_a_ready);')


def harness(ports, capacity, scenario=None, maximum=1024):
    inputs, wires, connections = [], [], []
    for name, width, incoming in ports:
        declaration = f"[{width-1}:0] {name}" if width > 1 else name
        (inputs if incoming else wires).append(declaration)
        connections.append(f".{name}({name})")
    assertions, remembered, covers, assumptions = [], [], [], []
    for k in range(capacity):
        slot = f"nl_probe_tl_x_context{k}_x_tl_x_"
        dep = f"nl_probe_tl_x_context{k}_x_dependencies"
        remembered.append(f"reg seen_{k}=0; always @(posedge nl_clk) begin\n"
                          f"if(!nl_reset_n || {slot}accept || {slot}release) seen_{k}<=0;\n"
                          f"else if({slot}a_take) seen_{k}<=1; end")
        assertions.extend([
            f"assert(!{slot}a_take || {slot}busy);",
            f"assert(!{slot}accept || !{slot}busy);",
            f"if({slot}busy) assert({slot}words_left<=({{1'b0,{slot}packet_x_length}}+1));",
            f"if({slot}d_take) assert(nl_tl_d_source=={k});",
            f"assert(!{slot}d_take || {slot}busy);",
            f"assert(!{slot}message_start || {dep}==0);",
            f"assert(!{slot}message || {dep}==0);",
            f"assert(!{slot}message || {slot}busy);",
            f"if(!{slot}busy) assert(!{slot}message && !{slot}rfull && !{slot}dfull && !{slot}wfull && {slot}words_left==0);",
            f"if({slot}rfull && {slot}response_x_last) assert(!{slot}message && {slot}words_left==1);",
            f"if({slot}wfull) assert({slot}busy && {slot}write && {slot}words_left!=0);",
            f"if({slot}message && {slot}local_code==0) begin assert({slot}a_done==({slot}a_left==0)); assert({slot}d_done==({slot}d_left==0)); end",
            f"if({slot}busy) assert({slot}packet_x_size==2 && {slot}cursor[1:0]==0 && {slot}response_cursor[1:0]==0);",
            f"if({slot}message) begin assert({slot}size>=2 && {slot}size<=10 && {slot}a_left<=256 && {slot}d_left<=256);",
            f"if({slot}write) assert({slot}d_left<=1); else assert({slot}a_left<=1); end",
            f"assert({slot}assembled==0);",
            f"if({slot}dfull) assert({slot}dmask==15 && !{slot}write && {slot}a_done);",
            f"if({slot}rfull) assert(!{slot}write && {slot}a_done);",
            f"assert({slot}dfull==({slot}dmask!=0));",
            f"if({slot}busy && !{slot}write) assert({{2'd0,{slot}d_left}}+{{12'd0,{slot}dfull}}+{{12'd0,{slot}rfull}} <= {{4'd0,{slot}words_left}});",
            f"if(!{slot}message) assert({slot}a_left==0 && {slot}d_left==0);",
            f"assert({slot}local_code==0);",
            f"if({slot}busy) assert({slot}packet_x_error==0 && {slot}packet_x_burst==1 && {slot}packet_x_address[1:0]==0 && {slot}packet_x_address=={slot}packet_x_start_address);",
            f"if({slot}busy && {slot}write) begin",
            f"if(nl_probe_tl_x_write_active && nl_probe_tl_x_write_owner=={k}) assert({{1'b0,{slot}words_left}}=={{1'b0,write_left}}+{{9'd0,{slot}wfull}});",
            f"else assert({slot}words_left=={slot}wfull); end",

            f"if(nl_probe_tl_x_write_active && nl_probe_tl_x_write_owner=={k}) assert({slot}busy && {slot}write);",
            f"if(nl_probe_tl_x_a_active && nl_probe_tl_x_a_owner=={k}) assert({slot}busy && {slot}message && !{slot}a_done);",
            f"if({slot}message && !{slot}a_done) assert(nl_probe_tl_x_a_active && nl_probe_tl_x_a_owner=={k});",
            f"if(nl_probe_tl_x_d_active && nl_probe_tl_x_d_owner=={k}) assert({slot}busy && {slot}message && !{slot}d_done && !{slot}write);",
            f"if(nl_probe_tl_x_read_active && nl_probe_tl_x_read_owner=={k}) assert({slot}busy && !{slot}write);",
            f"if(nl_probe_tl_x_write_response_active && nl_probe_tl_x_write_response_owner=={k}) assert({slot}busy && {slot}write && {slot}words_left==0 && !{slot}message);",
            f"if(response_segment_open && nl_probe_tl_x_read_owner=={k}) assert({slot}packet_x_id==segment_id && {slot}packet_x_source==segment_source && {slot}packet_x_tag==segment_tag);",
            f"assert(!{dep}[{k}]);",
            f"assert(!{slot}rfull || !nl_probe_tl_x_context{k}_x_cut || !{slot}response_x_last);",
            f"if(past_valid && $past({slot}busy && !{slot}release)) begin",
            f"assert({slot}busy);",
            *[f"assert({slot}packet_x_{field}==$past({slot}packet_x_{field}));"
              for field in ("id", "source", "tag")],
            "end",
        ])
        for j in range(capacity):
            older = f"nl_probe_tl_x_context{j}_x_tl_x_"
            assertions.append(f"if({dep}[{j}]) assert({older}busy);")
        for j in range(capacity):
            if j == k:
                continue
            older = f"nl_probe_tl_x_context{j}_x_tl_x_"
            age = f"older_write_{k}_{j}"
            remembered.append(f"reg {age}=0; always @(posedge nl_clk) begin\n"
                f"if(!nl_reset_n || {slot}release) {age}<=0;\n"
                f"else if({slot}accept) {age}<=nl_probe_tl_x_w_take && {older}busy && {older}write && !{older}release;\n"
                f"else if({older}release) {age}<=0; end")
            assertions.extend([
                f"if({age}) assert({older}busy && {older}write);",
                f"if({slot}busy && {slot}write) assert(nl_probe_tl_x_context{k}_x_response_dependencies[{j}]=={age});",
                f"if({slot}release && {slot}write) assert(!{age});",
                f"if(nl_wrsp_valid && nl_probe_tl_x_write_response_selected=={k}) assert(!{age});",
                f"if(nl_probe_tl_x_write_response_active && nl_probe_tl_x_write_response_owner=={k}) assert(!{age});",
            ])
        assumptions.append(f"if(nl_tl_d_valid && nl_tl_d_source=={k}) begin\n"
                           f"assume({slot}message && !{slot}d_done && {slot}local_code==0);\n"
                           f"assume({slot}a_done || {slot}a_take || "
                           f"({slot}write && {slot}a_left < ({slot}size<=2 ? 1 : 11'd1 << ({slot}size-2))));\n"
                           "end")
        covers.append(f"({slot}busy && seen_{k} && !{slot}write && {slot}message && !{slot}d_done)")
    for bank in range(2):
        prefix = f"nl_probe_tl_x_dbuffer{bank}_x_"
        assertions.append(f"assert(!{prefix}valid || {prefix}owner<{capacity});")
        for k in range(capacity):
            slot = f"nl_probe_tl_x_context{k}_x_tl_x_"
            assertions.append(f"if({prefix}valid && {prefix}owner=={k}) assert({slot}busy && !{slot}write);")
    assertions.append("assert(!nl_probe_tl_x_dbuffer0_x_valid || !nl_probe_tl_x_dbuffer1_x_valid || nl_probe_tl_x_dbuffer0_x_owner!=nl_probe_tl_x_dbuffer1_x_owner);")
    for k in range(capacity):
        owned = " || ".join(f"(nl_probe_tl_x_dbuffer{bank}_x_valid && nl_probe_tl_x_dbuffer{bank}_x_owner=={k})" for bank in range(2))
        assertions.append(f"if(nl_probe_tl_x_context{k}_x_tl_x_dfull) assert({owned});")
    for k in range(capacity):
        for j in range(k):
            a = f"nl_probe_tl_x_context{k}_x_tl_x_"
            b = f"nl_probe_tl_x_context{j}_x_tl_x_"
            assertions.append(
                f"if({a}busy && {a}write && ({a}words_left!=0 || {a}message) && "
                f"{b}busy && {b}write && ({b}words_left!=0 || {b}message)) "
                f"assert(nl_probe_tl_x_context{k}_x_dependencies[{j}] || "
                f"nl_probe_tl_x_context{j}_x_dependencies[{k}]);")
    puts = [f"(nl_probe_tl_x_context{k}_x_tl_x_message && "
            f"nl_probe_tl_x_context{k}_x_tl_x_write && "
            f"!nl_probe_tl_x_context{k}_x_tl_x_message_complete)" for k in range(capacity)]
    assertions.append("assert((" + "+".join("{5'd0," + value + "}" for value in puts) + ")<=1);")
    d_end = " || ".join(f"(nl_probe_tl_x_context{k}_x_tl_x_d_take && "
                           f"nl_probe_tl_x_context{k}_x_tl_x_d_left==1)" for k in range(capacity))
    takes = ",".join(f"nl_probe_tl_x_context{k}_x_tl_x_response_take" for k in reversed(range(capacity)))
    releases = ",".join(f"nl_probe_tl_x_context{k}_x_tl_x_release" for k in reversed(range(capacity)))
    pending = " || ".join(f"(p_owner=={k} && nl_probe_tl_x_context{k}_x_tl_x_message && "
                          f"nl_probe_tl_x_context{k}_x_tl_x_a_done && !nl_probe_tl_x_context{k}_x_tl_x_d_done && "
                          f"!nl_probe_tl_x_context{k}_x_tl_x_write)" for k in range(capacity))
    first_put = " || ".join(f"(nl_probe_tl_x_a_owner=={k} && "
                            f"nl_probe_tl_x_context{k}_x_tl_x_a_left>=3 && "
                            f"nl_probe_tl_x_context{k}_x_tl_x_a_left==(11'd1 << (nl_probe_tl_x_context{k}_x_tl_x_size-2)))"
                            for k in range(capacity))
    full_read = " || ".join(f"nl_probe_tl_x_context{k}_x_tl_x_rfull" for k in range(capacity))
    checks = [
        "cover(" + " && ".join(covers) + ");",
        f"cover(nl_tl_a_valid && nl_tl_a_ready && nl_tl_d_valid && nl_tl_d_ready && nl_tl_a_opcode!=4 && ({first_put}) && ({full_read}));",
        "cover(early_denied && nl_probe_tl_x_context0_x_tl_x_message_complete && nl_probe_tl_x_context0_x_tl_x_a_done);",
        "cover(segment_phase==4" + (" && overlap_ack" if capacity>=3 else "") + ");",
    ]
    reach = "\n".join(checks if scenario is None else [checks[scenario]])
    body = f"""module formal_top({','.join('input '+p for p in inputs)});
{';'.join('wire '+p for p in wires)};
nocloom_tl_uh_outlet_probe dut({','.join(connections)});
reg started=0, past_valid=0;
reg [8:0] write_left=0;
always @(posedge nl_clk) begin
  started<=1; past_valid<=nl_reset_n;
  if(!nl_reset_n) write_left<=0;
  else if(nl_wreq_valid && nl_wreq_ready)
    write_left<=write_left==0 ? {{1'b0,nl_wreq_length}} : write_left-1;
end
always @* begin
  assume(nl_reset_n==started);
  assume(!nl_wreq_valid || !nl_rreq_valid);
  assume(nl_wreq_size==2 && nl_rreq_size==2);
  assume(nl_wreq_burst==1 && nl_rreq_burst==1);
  assume(nl_wreq_start_address==nl_wreq_address && nl_rreq_start_address==nl_rreq_address);
  assume(nl_wreq_address[1:0]==0 && nl_rreq_address[1:0]==0);
  assume(!nl_wreq_error && !nl_rreq_error);
  assume(nl_wreq_first==(write_left==0));
  assume(nl_wreq_last==(write_left==0 ? nl_wreq_length==0 : write_left==1));
  {' '.join(assumptions)}
end
{' '.join(remembered)}
{'' if scenario is None else cover_guidance(capacity, scenario)}
wire [{capacity-1}:0] response_takes={{{takes}}}, packet_releases={{{releases}}};
reg [2:0] segment_phase=0;
reg [{(capacity-1).bit_length()-1}:0] p_owner=0, q_owner=0;
reg [3:0] q_words=0;
reg early_denied=0, overlap_ack=0, response_segment_open=0;
reg [3:0] segment_id=0, segment_source=0, segment_tag=0;
always @(posedge nl_clk) if(!nl_reset_n) response_segment_open<=0;
else if(nl_rrsp_valid && nl_rrsp_ready) begin
  response_segment_open<=!nl_rrsp_last && !nl_probe_cut;
  if(!response_segment_open) begin
    segment_id<=nl_rrsp_id; segment_source<=nl_rrsp_source; segment_tag<=nl_rrsp_tag;
  end
end
always @(posedge nl_clk) if(!nl_reset_n) begin
  segment_phase<=0; early_denied<=0; overlap_ack<=0;
end else begin
  if(nl_probe_tl_x_context0_x_tl_x_accept) early_denied<=0;
  if(nl_probe_tl_x_context0_x_tl_x_d_take && nl_probe_tl_x_context0_x_tl_x_write &&
     nl_tl_d_denied && nl_probe_tl_x_context0_x_tl_x_a_left>=3) early_denied<=1;
  {"if(nl_probe_tl_x_context2_x_tl_x_d_take && nl_probe_tl_x_context2_x_tl_x_write && nl_probe_tl_x_context2_x_tl_x_a_left>=3 && nl_probe_tl_x_context0_x_tl_x_busy && !nl_probe_tl_x_context0_x_tl_x_write && nl_probe_tl_x_context1_x_tl_x_busy && !nl_probe_tl_x_context1_x_tl_x_write) overlap_ack<=1;" if capacity>=3 else ""}
  case(segment_phase)
    0: for(integer k=0;k<{capacity};k=k+1) if(response_takes[k] && nl_probe_cut) begin
      p_owner<=k; segment_phase<=1;
    end
    1: if(packet_releases[p_owner]) segment_phase<=0;
       else if({pending}) for(integer k=0;k<{capacity};k=k+1)
         if(response_takes[k] && k!=p_owner) begin q_owner<=k; q_words<=1; segment_phase<=2; end
    2: if(packet_releases[p_owner] || (packet_releases[q_owner] && q_words<7)) segment_phase<=0;
       else if(response_takes[q_owner]) begin
         q_words<=q_words+1; if(q_words==7) segment_phase<=3;
       end
    3: if(packet_releases[p_owner]) segment_phase<=4;
  endcase
end
always @(posedge nl_clk) if(nl_reset_n) begin
  assert(nl_probe_tl_x_write_active==(write_left!=0));
  assert(!nl_probe_tl_x_write_response_active || nl_probe_tl_x_write_response_owner<{capacity});
  assert(!(nl_probe_tl_x_write_response_active && nl_probe_tl_x_read_active));
  assert(!(nl_rrsp_valid && nl_wrsp_valid));
  assert(!nl_wrsp_valid || !nl_probe_tl_x_read_active);
  if(response_segment_open) begin
    assert(nl_probe_tl_x_read_active && !nl_wrsp_valid);
    if(nl_rrsp_valid) assert(nl_rrsp_id==segment_id && nl_rrsp_source==segment_source && nl_rrsp_tag==segment_tag);
  end
  assert(!nl_tl_a_valid || nl_tl_a_source<{capacity});
  assert(!nl_probe_tl_x_a_active || nl_probe_tl_x_a_owner<{capacity});
  assert(!nl_probe_tl_x_d_active || nl_probe_tl_x_d_owner<{capacity});
  assert(!nl_probe_tl_x_write_active || nl_probe_tl_x_write_owner<{capacity});
  assert(!nl_probe_tl_x_read_active || nl_probe_tl_x_read_owner<{capacity});
  if(nl_probe_tl_x_d_active && nl_tl_d_source!=nl_probe_tl_x_d_owner) assert(!nl_tl_d_ready);
  if(nl_tl_d_source>={capacity}) assert(!nl_tl_d_ready);
  {chr(10).join(assertions)}
  if(past_valid && $past(nl_tl_a_valid && !nl_tl_a_ready)) begin
    assert(nl_tl_a_valid);
    assert(nl_tl_a_source==$past(nl_tl_a_source));
    assert(nl_tl_a_opcode==$past(nl_tl_a_opcode));
    assert(nl_tl_a_size==$past(nl_tl_a_size));
    assert(nl_tl_a_address==$past(nl_tl_a_address));
  end
  if(past_valid && $past(nl_probe_tl_x_d_active && !({d_end}))) begin
    assert(nl_probe_tl_x_d_active);
    assert(nl_probe_tl_x_d_owner==$past(nl_probe_tl_x_d_owner));
  end
  if(past_valid && $past(nl_rrsp_valid && !nl_rrsp_ready)) begin
    assert(nl_rrsp_valid);
    assert(nl_rrsp_id==$past(nl_rrsp_id) && nl_rrsp_source==$past(nl_rrsp_source) && nl_rrsp_tag==$past(nl_rrsp_tag));
  end
  if(past_valid && $past(nl_wrsp_valid && !nl_wrsp_ready)) begin
    assert(nl_wrsp_valid);
    assert(nl_wrsp_id==$past(nl_wrsp_id) && nl_wrsp_source==$past(nl_wrsp_source) && nl_wrsp_tag==$past(nl_wrsp_tag));
  end
  {reach}
end
endmodule
"""
    if maximum > 1024:
        body = body.replace("reg [8:0] write_left", "reg [10:0] write_left").replace("<=256", "<=1024").replace("size<=10", "size<=12")
    return body



def ranges_harness(ports, capacity, maximum=1024):
    """Bound actual selectors and message counters under arbitrary D inputs."""
    inputs, wires, connections, checks = [], [], [], []
    for name, width, incoming in ports:
        (inputs if incoming else wires).append(f"[{width-1}:0] {name}")
        connections.append(f".{name}({name})")
    for k in range(capacity):
        slot = f"nl_probe_tl_x_context{k}_x_tl_x_"
        checks.extend([
            f"assert(!{slot}accept || !{slot}busy);",
            f"assert({slot}a_left<=256 && {slot}d_left<=256);",
            f"assert(!{slot}message || {slot}size<=10);",
            f"if({slot}message && !{slot}a_done) assert({slot}a_left!=0);",
            f"if({slot}message && !{slot}d_done) assert({slot}d_left!=0);",
            f"assert(!{slot}a_take || ({slot}message && !{slot}a_done));",
            f"assert(!{slot}d_take || ({slot}message && !{slot}d_done && nl_tl_d_source=={k}));",
            f"if(nl_tl_a_valid && nl_probe_tl_x_a_owner=={k}) assert(nl_tl_a_source=={k});",
        ])
    body = ("module formal_top(" + ",".join("input "+p for p in inputs) + ");\n"
        + ";".join("wire "+p for p in wires) + ";\n"
        + "nocloom_tl_uh_outlet_probe dut(" + ",".join(connections) + ");\n"
        + "reg started=0; always @(posedge nl_clk) started<=1;\n"
        + "always @* assume(nl_reset_n==started);\n"
        + "always @(posedge nl_clk) if(nl_reset_n) begin\n"
        + f"assert(!nl_tl_a_valid || nl_tl_a_source<{capacity});\n"
        + f"assert(nl_probe_tl_x_free<{capacity});\n"
        + f"if(nl_tl_d_source>={capacity}) assert(!nl_tl_d_ready);\n"
        + "if(nl_probe_tl_x_d_active && nl_tl_d_source!=nl_probe_tl_x_d_owner) assert(!nl_tl_d_ready);\n"
        + "\n".join(checks) + "\nend\nendmodule\n")
    return body.replace("<=256", "<=1024").replace("size<=10", "size<=12") if maximum > 1024 else body


def full_pool_cover(ports, capacity):
    # Fixed legal requests let preprocessing remove unused payload logic.
    signals = dict(nl_reset_n="started", nl_tl_a_ready="1", nl_wrsp_ready="1",
                   nl_rreq_valid=f"accepted<{capacity}", nl_rreq_size="2", nl_rreq_burst="1",
                   nl_rreq_count="4", nl_rreq_be="15", nl_rreq_first="1", nl_rreq_last="1",
                   nl_rreq_address="4096+(accepted << 8)",
                   nl_rreq_start_address="4096+(accepted << 8)", nl_rreq_id="accepted[3:0]")
    declarations, connections, seen, full = [], [], [], []
    for name, width, incoming in ports:
        if name != "nl_clk":
            value = "="+signals.get(name, "0") if incoming else ""
            declarations.append(f"wire [{width-1}:0] {name}{value};")
        connections.append(f".{name}({name})")
    for k in range(capacity):
        slot=f"nl_probe_tl_x_context{k}_x_tl_x_"
        seen.append(f"reg seen{k}=0; always @(posedge nl_clk) if(nl_reset_n && {slot}a_take) seen{k}<=1;")
        full.append(f"(seen{k} && {slot}busy && {slot}message && !{slot}write && !{slot}d_done)")
    return ("module formal_top(input nl_clk);\nreg started=0; reg [4:0] accepted=0;\n"
        + "always @(posedge nl_clk) begin started<=1; if(nl_reset_n && nl_rreq_valid && nl_rreq_ready) accepted<=accepted+1; end\n"
        + "\n".join(declarations) + "\nnocloom_tl_uh_outlet_probe dut(" + ",".join(connections)+");\n"
        + "\n".join(seen) + "\nalways @(posedge nl_clk) if(nl_reset_n) cover("+" && ".join(full)+");\nendmodule\n")


def max_message_cover(ports, maximum):
    values = dict(nl_reset_n="started", nl_tl_a_ready="1", nl_wrsp_ready="1",
        nl_wreq_valid="is_write && !accepted", nl_rreq_valid="!is_write && !accepted",
        nl_wreq_address="4096", nl_wreq_start_address="4096", nl_wreq_size="2", nl_wreq_burst="1",
        nl_wreq_length=str(maximum//4-1), nl_wreq_count="4", nl_wreq_be="15", nl_wreq_first="1",
        nl_rreq_address="4096", nl_rreq_start_address="4096", nl_rreq_size="2", nl_rreq_burst="1",
        nl_rreq_length=str(maximum//4-1), nl_rreq_count="4", nl_rreq_be="15", nl_rreq_first="1", nl_rreq_last="1",
        nl_tl_d_valid="is_write && nl_tl_a_valid", nl_tl_d_source="nl_tl_a_source",
        nl_tl_d_size=str(maximum.bit_length()-1), nl_tl_d_denied="is_write")
    declarations, connections = [], []
    for name, width, incoming in ports:
        if name!="nl_clk":
            declarations.append(f"wire [{width-1}:0] {name}"+("="+values.get(name,"0") if incoming else "")+";")
        connections.append(f".{name}({name})")
    return ("module formal_top(input nl_clk);\nreg started=0, accepted=0; (* anyconst *) reg is_write;\n"
        + "always @(posedge nl_clk) begin started<=1; if(nl_reset_n && ((nl_wreq_valid && nl_wreq_ready) || (nl_rreq_valid && nl_rreq_ready))) accepted<=1; end\n"
        + "\n".join(declarations) + "\nnocloom_tl_uh_outlet_probe dut("+",".join(connections)+");\n"
        + "always @(posedge nl_clk) if(nl_reset_n) begin\n"
        + f"cover(!is_write && nl_tl_a_valid && nl_tl_a_ready && nl_tl_a_size=={maximum.bit_length()-1} && nl_probe_tl_x_context0_x_tl_x_d_left=={maximum//4});\n"
        + f"cover(is_write && nl_tl_a_valid && nl_tl_a_ready && nl_tl_d_valid && nl_tl_d_ready && nl_tl_a_size=={maximum.bit_length()-1} && nl_probe_tl_x_context0_x_tl_x_a_left=={maximum//4});\n"
        + "end\nendmodule\n")


def run(args, capacity):
    work = Path(tempfile.mkdtemp(prefix="pool-", dir=args.work)).resolve()
    subprocess.run([str(args.fixture.resolve()), str(work), "contexts", str(args.maximum),
                    str(capacity), str(16 if capacity==16 else (capacity-1).bit_length())], check=True, timeout=60)
    ports = [(n, int(w), int(i)) for n, w, i in
             (line.split() for line in (work / "ports.tsv").read_text().splitlines())]
    (work / "formal_top.sv").write_text(ranges_harness(ports, capacity, args.maximum) if capacity==16 else harness(ports, capacity, maximum=args.maximum))
    scenarios = list(range(1) if capacity == 16 else range(4))
    if args.maximum > 1024:
        scenarios.append(4)
    for scenario in scenarios:
        body = (max_message_cover(ports, args.maximum) if scenario==4 else
                full_pool_cover(ports, capacity) if capacity==16 else harness(ports, capacity, scenario, args.maximum))
        (work / f"cover{scenario}.sv").write_text(body)
    tasks = ["prove"] + [f"cover{k}" for k in scenarios]
    options = "\n".join(f"{task}: mode {'prove' if task=='prove' else 'cover'}\n"
                        f"{task}: depth {3 if task=='prove' else 96}\n"
                        f"{task}: timeout 300" for task in tasks)
    reads = "\n".join(f"{task}: read_verilog -formal -sv "
                      + ("formal_top.sv" if task=="prove" else f"-D NL_CONTEXT_COVER {task}.sv")
                      for task in tasks)
    (work / "pool.sby").write_text("[tasks]\n" + "\n".join(tasks)
        + "\n[options]\n" + options + "\n[engines]\nsmtbmc z3\n[script]\n"
        + "read_verilog -formal network.v\n" + reads + "\nprep -top formal_top -flatten\n[files]\n"
        + "network.v\nformal_top.sv\n" + "\n".join(f"cover{k}.sv" for k in scenarios) + "\n")
    with subprocess.Popen(["sby", "-j", "2", "pool.sby"], cwd=work, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, start_new_session=True) as process:
        try:
            output, _ = process.communicate(timeout=1000)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            output, _ = process.communicate()
            print(output, work, flush=True)
            return 1
    print(output, work, flush=True)
    return 0 if process.returncode == 0 and all(
        (work / f"pool_{task}/status").read_text().split()[0] == "PASS"
        for task in tasks) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--capacity", type=int)
    parser.add_argument("--maximum", type=int, default=1024)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    for capacity in ([args.capacity] if args.capacity else (2, 3, 4, 16) if args.maximum > 1024 else (2, 3, 4)):
        result = run(args, capacity)
        if result:
            return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
