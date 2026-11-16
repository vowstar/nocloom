"""Block selection, read footprints and write completion with reachability checks."""
import argparse
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile


def selector(maximum):
    return f"""module formal_top(input wire nl_clk,
      input wire [31:0] address, available);
wire [3:0] size;
nocloom_selector dut(.nl_clk(nl_clk), .nl_reset_n(1'b1),
  .nl_range_address(address), .nl_range_available(available), .nl_range_size(size));
wire [32:0] bytes=33'd1 << size;
wire [32:0] twice=bytes << 1;
always @* begin
  assume(available>0 && available<={max(1024,maximum)});
  assume({{1'b0,address}}+{{1'b0,available}}<=33'h100000000);
  assert(size<={maximum.bit_length()-1});
  assert(bytes<=available);
  assert(({{1'b0,address}} & (bytes-1))==0);
  assert({{1'b0,address}}+bytes<=33'h100000000);
  if(size<{maximum.bit_length()-1} && twice<=available)
    assert(({{1'b0,address}} & (twice-1))!=0);
  cover(size=={maximum.bit_length()-1});
  cover(address[1:0]==3 && size==0 && available==9);
end
endmodule
"""


def footprint_properties(maximum):
    return r"""
reg [12:0] expected_address=0, expected_base=0, fixed_start=0;
reg [12:0] expected_top=0;
reg [11:0] remaining=0;
reg [1:0] saved_burst=0;
reg [2:0] saved_size=0;
wire [12:0] stride=13'd1 << nl_rreq_size;
wire [12:0] span=({5'd0,nl_rreq_length}+1) << nl_rreq_size;
wire [12:0] skew={1'b0,nl_rreq_start_address[11:0]}-{1'b0,nl_rreq_address[11:0]};
wire [12:0] block_bytes=13'd1 << nl_tl_a_size;
wire [12:0] following={1'b0,expected_address}+{1'b0,block_bytes};
function [12:0] byte_product(input [12:0] count, input [12:0] unit_bytes);
  begin
    case(unit_bytes)
      1: byte_product=count;
      2: byte_product=count << 1;
      3: byte_product=(count << 1)+count;
      4: byte_product=count << 2;
      default: byte_product=0;
    endcase
  end
endfunction
function integer pop4(input [3:0] value);
  integer k;
  begin pop4=0; for(k=0;k<4;k=k+1) pop4=pop4+value[k]; end
endfunction
wire [12:0] saved_stride=13'd1 << saved_size;
wire [12:0] saved_span=({5'd0,nl_probe_tl_x_packet_x_length}+1) << saved_size;
wire [12:0] saved_address={1'b0,nl_probe_tl_x_packet_x_address};
wire [10:0] message_beats=nl_probe_tl_x_size<=2 ? 1 : 11'd1 << (nl_probe_tl_x_size-2);
wire [12:0] message_stride=13'd1 << (nl_probe_tl_x_size<2 ? nl_probe_tl_x_size : 2);
wire [2:0] response_bytes=saved_stride-(nl_probe_tl_x_response_cursor & (saved_stride-1));
wire [3:0] response_mask=((5'd1 << response_bytes)-1) << nl_probe_tl_x_response_cursor[1:0];
wire [12:0] saved_skew=fixed_start-{1'b0,nl_probe_tl_x_packet_x_address};
wire [12:0] pending_d=nl_probe_tl_x_message && nl_probe_tl_x_a_done ?
  ({2'd0,nl_probe_tl_x_d_left} << (nl_probe_tl_x_size<2 ? nl_probe_tl_x_size : 2)) : 0;
wire [12:0] staged=(nl_probe_tl_x_dfull ? pop4(nl_probe_tl_x_dmask) : 0)+
  pop4(nl_probe_tl_x_assembled)+(nl_probe_tl_x_rfull ?
    saved_stride-(saved_burst==0 || nl_probe_tl_x_first ? saved_skew : 0) : 0);
wire [12:0] owed=byte_product({4'd0,nl_probe_tl_x_words_left},
  saved_stride-(saved_burst==0 ? saved_skew : 0))-
  (saved_burst!=0 && nl_probe_tl_x_first ? saved_skew : 0);
wire [12:0] received_end={1'b0,nl_probe_tl_x_cursor}+{1'b0,pending_d};
wire [12:0] issued_end=(saved_burst==0 && received_end==expected_top) ? fixed_start :
  (saved_burst==2 && received_end==expected_top) ? expected_base : received_end;
wire [4:0] prefix={1'b0,nl_probe_tl_x_assembled} >> nl_probe_tl_x_response_cursor[1:0];
wire [12:0] buffered_bytes=pop4(nl_probe_tl_x_assembled)+pop4(nl_probe_tl_x_dmask);
wire [12:0] buffered_end={1'b0,nl_probe_tl_x_response_cursor}+buffered_bytes;
wire [12:0] receive_cursor=saved_burst==0 && buffered_end==expected_top ? fixed_start :
  saved_burst==2 && buffered_end>=expected_top ? buffered_end-expected_top+expected_base : buffered_end;
wire [12:0] buffered_start={1'b0,nl_probe_tl_x_response_cursor}+pop4(nl_probe_tl_x_assembled);
wire [3:0] buffer_mask=((5'd1 << pop4(nl_probe_tl_x_dmask))-1) << buffered_start[1:0];
wire [8:0] consumed_words=({1'b0,nl_probe_tl_x_packet_x_length}+1)-nl_probe_tl_x_words_left;
wire [8:0] produced_words=consumed_words+nl_probe_tl_x_rfull;
wire [12:0] response_offset=(saved_address-expected_base)+(produced_words << saved_size);
wire [12:0] next_response=produced_words==0 || saved_burst==0 ? fixed_start :
  saved_burst==2 ? expected_base+(response_offset & (saved_span-1)) : expected_base+response_offset;
always @* begin
  assume(!nl_wreq_valid);
  assert(!nl_probe_tl_x_write && !nl_probe_tl_x_wfull);
  assert(nl_probe_tl_x_dfull==(nl_probe_tl_x_dmask!=0));
  if(nl_probe_tl_x_rfull) assert(nl_probe_tl_x_assembled==0);
  if(!nl_probe_tl_x_busy) begin
    assert(!nl_probe_tl_x_message && !nl_probe_tl_x_dfull && !nl_probe_tl_x_rfull);
    assert(nl_probe_tl_x_words_left==0 && nl_probe_tl_x_assembled==0 && remaining==0);
  end
  if(nl_probe_tl_x_rfull && nl_probe_tl_x_response_x_last) begin
    assert(!nl_probe_tl_x_message);
    assert(nl_probe_tl_x_words_left==1);
  end
  assume(nl_rreq_size<=2 && !nl_rreq_error);
  assume(nl_rreq_address[31:12]==20'hfffff && nl_rreq_start_address[31:12]==20'hfffff);
  if(nl_rreq_burst==1) assume({1'b0,nl_rreq_address[11:0]}+span<=4096);
  assume((nl_rreq_address & (stride-1))==0);
  assume(nl_rreq_start_address>=nl_rreq_address && skew<stride);
  assume(nl_rreq_burst!=3);
  if(nl_rreq_burst==2) begin
    assume(nl_rreq_length==1 || nl_rreq_length==3 || nl_rreq_length==7 || nl_rreq_length==15);
    assume(skew==0);
  end
  if(nl_probe_tl_x_busy) begin
    assert(nl_probe_tl_x_words_left<=({1'b0,nl_probe_tl_x_packet_x_length}+1));
    assert((prefix & (prefix+1))==0);
    assert(buffered_bytes<=4);
    assert(nl_probe_tl_x_dmask==buffer_mask);
    assert(nl_probe_tl_x_cursor==receive_cursor[11:0]);
    assert(nl_probe_tl_x_response_cursor==next_response[11:0]);
    assert(nl_probe_tl_x_first==(consumed_words==0));
    if(nl_probe_tl_x_rfull) assert(nl_probe_tl_x_response_x_last==(nl_probe_tl_x_words_left==1));
    assert((nl_probe_tl_x_assembled & ~response_mask)==0);
    assert(nl_probe_tl_x_assembled!=response_mask);
    if(nl_probe_tl_x_dfull) assert((nl_probe_tl_x_assembled & nl_probe_tl_x_dmask)==0);
    assert(saved_size<=2 && saved_burst<=2);
    assert(saved_skew<saved_stride);
    assert((nl_probe_tl_x_packet_x_address & (saved_stride-1))==0);
    assert(nl_probe_tl_x_packet_x_error==0);
    if(saved_burst==2) begin
      assert(saved_skew==0);
      assert(nl_probe_tl_x_packet_x_length==1 || nl_probe_tl_x_packet_x_length==3 ||
             nl_probe_tl_x_packet_x_length==7 || nl_probe_tl_x_packet_x_length==15);
    end
    assert(expected_base==(saved_burst==2 ? saved_address & ~(saved_span-1) : saved_address));
    assert(expected_top==expected_base+(saved_burst==2 ? saved_span : saved_stride));
    if(saved_burst!=1) begin
      assert(expected_address>=expected_base && expected_address<expected_top);
      if(saved_burst==0) begin
        assert(expected_address>=fixed_start);
        assert(buffered_end<=expected_top);
      end
    end else assert(expected_base+saved_span<=4096);
    assert(remaining+pending_d+staged==owed);
    if(remaining!=0 || pending_d!=0) assert(expected_address==issued_end);
    assert(saved_size==nl_probe_tl_x_packet_x_size);
    assert(saved_burst==nl_probe_tl_x_packet_x_burst);
    assert(fixed_start=={1'b0,nl_probe_tl_x_packet_x_start_address});
    assert(remaining<=1024);
    assert(nl_probe_tl_x_words_left>0 && nl_probe_tl_x_words_left<=256);
    assert(expected_base<4096);
    assert(expected_top>0 && expected_top<=4096);
    assert(expected_address<=4096);
  end
  if(nl_probe_tl_x_message) begin
    assert(nl_probe_tl_x_local_code==0);
    if(!nl_probe_tl_x_d_done) assert((nl_probe_tl_x_cursor & (message_stride-1))==0);
    assert(nl_probe_tl_x_a_left<=1);
    assert(nl_probe_tl_x_a_done==(nl_probe_tl_x_a_left==0));
    assert(nl_probe_tl_x_d_done==(nl_probe_tl_x_d_left==0));
    if(!nl_probe_tl_x_a_done) begin
      assert(!nl_probe_tl_x_d_done);
      assert(nl_probe_tl_x_d_left==message_beats);
    end
  end
  if(nl_tl_a_valid) begin
    assert(nl_tl_a_opcode==4);
    assert(nl_tl_a_address==expected_address);
    assert(block_bytes<=remaining);
    if(saved_burst!=1) assert(following<=expected_top);
  end
end
always @(posedge nl_clk) if(nl_reset_n) begin
  if(nl_probe_tl_x_accept) begin
    expected_address<={1'b0,nl_rreq_start_address[11:0]};
    fixed_start<={1'b0,nl_rreq_start_address[11:0]};
    saved_burst<=nl_rreq_burst; saved_size<=nl_rreq_size;
    expected_base<=nl_rreq_burst==2 ? ({1'b0,nl_rreq_address[11:0]} & ~(span-1)) : {1'b0,nl_rreq_address[11:0]};
    expected_top<=nl_rreq_burst==2 ? ({1'b0,nl_rreq_address[11:0]} & ~(span-1))+span : {1'b0,nl_rreq_address[11:0]}+stride;
    remaining<=nl_rreq_burst==0 ? byte_product({5'd0,nl_rreq_length}+1,stride-skew) : span-skew;
  end
  if(nl_probe_tl_x_a_take) begin
    remaining<=remaining-block_bytes;
    expected_address<=saved_burst==0 && following==expected_top ? fixed_start :
                      saved_burst==2 && following==expected_top ? expected_base : following;
  end
  if(nl_probe_tl_x_release) assert(remaining==0);
  cover(nl_probe_tl_x_release && saved_burst==2);
  cover(nl_probe_tl_x_release && saved_burst==0 && fixed_start[1:0]==3);
end
""" + f"always @* if(nl_probe_tl_x_message) begin assert(nl_probe_tl_x_size<={maximum.bit_length()-1}); assert(nl_probe_tl_x_d_left<={max(1,maximum//4)}); end\n"


def write_properties(maximum):
    return r"""
reg [12:0] expected=0, bottom=0, top=0, origin=0;
reg [11:0] remaining=0;
reg [2:0] size=0;
reg [1:0] burst=0;
wire [12:0] stride=13'd1 << size;
wire [12:0] address={1'b0,nl_probe_tl_x_packet_x_address};
wire [12:0] skew=origin-address;
wire [12:0] span=({5'd0,nl_probe_tl_x_packet_x_length}+1) << size;
wire [12:0] unit_bytes=stride-(burst==0 ? skew : 0);
wire [12:0] requested=13'd1 << nl_wreq_size;
wire [12:0] request_span=({5'd0,nl_wreq_length}+1) << nl_wreq_size;
wire [12:0] request_skew={1'b0,nl_wreq_start_address[11:0]}-{1'b0,nl_wreq_address[11:0]};
function [12:0] product(input [12:0] n, input [12:0] bytes);
  begin
    case(bytes)
      1: product=n;
      2: product=n << 1;
      3: product=(n << 1)+n;
      4: product=n << 2;
      default: product=0;
    endcase
  end
endfunction
wire [8:0] consumed=({1'b0,nl_probe_tl_x_packet_x_length}+1)-nl_probe_tl_x_words_left;
wire [12:0] word_offset=address-bottom+(consumed << size);
wire [12:0] word_start=consumed==0 || burst==0 ? origin :
  burst==2 ? bottom+(word_offset & (span-1)) : bottom+word_offset;
wire [11:0] offset=nl_probe_tl_x_cursor-word_start[11:0];
wire [12:0] owed=product({4'd0,nl_probe_tl_x_words_left},unit_bytes)-
  (burst!=0 && nl_probe_tl_x_first ? skew : 0);
wire [10:0] beats=nl_probe_tl_x_size<=2 ? 1 : 11'd1 << (nl_probe_tl_x_size-2);
wire [12:0] step=13'd1 << (nl_probe_tl_x_size<2 ? nl_probe_tl_x_size : 2);
wire [12:0] pending=nl_probe_tl_x_message && nl_probe_tl_x_a_left<beats ?
  ({2'd0,nl_probe_tl_x_a_left} << (nl_probe_tl_x_size<2 ? nl_probe_tl_x_size : 2)) : 0;
wire [12:0] issued={1'b0,nl_probe_tl_x_cursor}+pending;
wire [12:0] normalized=burst==0 && issued==top ? origin :
  burst==2 && issued==top ? bottom : issued;
wire first_a=nl_probe_tl_x_a_take && nl_probe_tl_x_a_left==beats;
wire skip=nl_probe_tl_x_busy && nl_probe_tl_x_wfull && !nl_probe_tl_x_message &&
  nl_probe_tl_x_word_x_be==0;
wire [12:0] message_bytes=13'd1 << nl_tl_a_size;
wire [12:0] advance=skip ? stride-(word_start & (stride-1))-offset : message_bytes;
wire [12:0] following=expected+advance;
always @* begin
  assume(!nl_rreq_valid);
  assert(!nl_probe_tl_x_rfull && !nl_probe_tl_x_dfull);
  if(!nl_probe_tl_x_busy)
    assert(!nl_probe_tl_x_message && !nl_probe_tl_x_wfull &&
           nl_probe_tl_x_words_left==0 && remaining==0);
  assume(nl_wreq_size<=2 && !nl_wreq_error && nl_wreq_burst!=3);
  assume(nl_wreq_address[31:12]==20'hfffff && nl_wreq_start_address[31:12]==20'hfffff);
  assume((nl_wreq_address & (requested-1))==0);
  assume(nl_wreq_start_address>=nl_wreq_address && request_skew<requested);
  if(nl_wreq_burst==1) assume({1'b0,nl_wreq_address[11:0]}+request_span<=4096);
  if(nl_wreq_burst==2) begin
    assume(request_skew==0);
    assume(nl_wreq_length==1 || nl_wreq_length==3 || nl_wreq_length==7 || nl_wreq_length==15);
  end
  if(nl_probe_tl_x_busy) begin
    assert(nl_probe_tl_x_write);
    assert(size<=2 && burst<=2 && skew<stride);
    assert((address & (stride-1))==0);
    assert(size==nl_probe_tl_x_packet_x_size && burst==nl_probe_tl_x_packet_x_burst);
    assert(origin=={1'b0,nl_probe_tl_x_packet_x_start_address});
    assert(nl_probe_tl_x_packet_x_error==0);
    assert(nl_probe_tl_x_words_left<=({1'b0,nl_probe_tl_x_packet_x_length}+1));
    assert(nl_probe_tl_x_first==(consumed==0));
    assert(remaining+pending+offset==owed);
    if(remaining!=0 || pending!=0) assert(expected==normalized);
    if(nl_probe_tl_x_words_left!=0)
      assert(offset<stride-(burst==0 || nl_probe_tl_x_first ? skew : 0));
    else assert(offset==0 && !nl_probe_tl_x_wfull);
    assert(bottom==(burst==2 ? address & ~(span-1) : address));
    assert(top==bottom+(burst==2 ? span : stride));
    assert(top>0 && top<=4096 && remaining<=1024);
    if(burst==2) begin
      assert(skew==0);
      assert(nl_probe_tl_x_packet_x_length==1 || nl_probe_tl_x_packet_x_length==3 ||
             nl_probe_tl_x_packet_x_length==7 || nl_probe_tl_x_packet_x_length==15);
    end
    if(burst!=1) begin
      assert(expected>=bottom && expected<top);
      if(burst==0) assert(expected>=origin);
    end
    else assert(bottom+span<=4096);
  end
  if(nl_probe_tl_x_message) begin
    assert(nl_probe_tl_x_local_code==0);
    assert(nl_probe_tl_x_a_left<=beats);
    assert(nl_probe_tl_x_a_done==(nl_probe_tl_x_a_left==0));
    assert(nl_probe_tl_x_busy);
    if(!nl_probe_tl_x_a_done)
      assert(step<=stride-(nl_probe_tl_x_cursor & (stride-1)));
    if(!nl_probe_tl_x_a_done && nl_probe_tl_x_a_left==beats) begin
      assert(nl_probe_tl_x_wfull);
      assert(nl_tl_a_address==nl_probe_tl_x_cursor);
    end
  end
  if(nl_tl_a_valid && nl_probe_tl_x_a_left==beats) begin
    assert(nl_tl_a_opcode<=1 && nl_tl_a_address==expected);
    assert(message_bytes<=remaining);
    if(burst!=1) assert(following<=top);
  end
  if(skip) assert(advance<=remaining && pending==0);
  if(nl_wrsp_valid) assert(remaining==0 && pending==0);
end
always @(posedge nl_clk) if(nl_reset_n) begin
  if(nl_probe_tl_x_accept) begin
    expected<={1'b0,nl_wreq_start_address[11:0]};
    origin<={1'b0,nl_wreq_start_address[11:0]};
    size<=nl_wreq_size; burst<=nl_wreq_burst;
    bottom<=nl_wreq_burst==2 ? ({1'b0,nl_wreq_address[11:0]} & ~(request_span-1)) : {1'b0,nl_wreq_address[11:0]};
    top<=nl_wreq_burst==2 ? ({1'b0,nl_wreq_address[11:0]} & ~(request_span-1))+request_span : {1'b0,nl_wreq_address[11:0]}+requested;
    remaining<=nl_wreq_burst==0 ? product({5'd0,nl_wreq_length}+1,requested-request_skew) : request_span-request_skew;
  end
  if(first_a || skip) begin
    remaining<=remaining-advance;
    expected<=burst==0 && following==top ? origin :
              burst==2 && following==top ? bottom : following;
  end
  cover(nl_probe_tl_x_release && burst==2);
  cover(nl_probe_tl_x_release && burst==0 && origin[1:0]==3);
end
""" + f"always @* if(nl_probe_tl_x_message) assert(nl_probe_tl_x_size<={maximum.bit_length()-1});\n"


def error_properties(maximum):
    return footprint_properties(maximum) + r"""
reg [7:0] error_queue=0;
reg [3:0] queued_bytes=0;
reg [3:0] observed_size=0;
wire incoming_error=nl_tl_d_denied || nl_tl_d_corrupt;
wire [3:0] received_size=nl_probe_tl_x_a_take ? nl_tl_a_size : observed_size;
wire [3:0] push_bytes=!nl_probe_tl_x_d_take ? 0 : received_size==0 ? 1 : received_size==1 ? 2 : 4;
wire [3:0] response_bytes_count=saved_stride-(saved_burst==0 || nl_probe_tl_x_first ? saved_skew : 0);
wire pop=nl_rrsp_valid && nl_rrsp_ready;
wire [3:0] pop_bytes=pop ? response_bytes_count : 0;
wire [3:0] kept_bytes=queued_bytes-pop_bytes;
function [7:0] error_mask(input [3:0] bytes);
  begin error_mask=(9'd1 << bytes)-1; end
endfunction
wire [7:0] response_errors=error_queue & error_mask(response_bytes_count);
wire [3:0] response_prefix=nl_probe_tl_x_rfull ? response_bytes_count : 0;
wire [3:0] assembled_bytes=pop4(nl_probe_tl_x_assembled);
wire [3:0] received_bytes=pop4(nl_probe_tl_x_dmask);
wire [7:0] assembled_errors=(error_queue >> response_prefix) & error_mask(assembled_bytes);
wire [7:0] received_errors=(error_queue >> (response_prefix+assembled_bytes)) & error_mask(received_bytes);
reg [7:0] next_errors;
always @* begin
  next_errors=error_queue >> pop_bytes;
  for(integer k=0;k<4;k=k+1)
    if(k<push_bytes) next_errors[kept_bytes+k]=incoming_error;
  assert(queued_bytes==staged && queued_bytes<=8);
  if(pop) assert(queued_bytes>=pop_bytes);
  if(nl_probe_tl_x_d_take) assert(kept_bytes+push_bytes<=8);
  assert(nl_probe_tl_x_read_error==(|assembled_errors));
  if(nl_probe_tl_x_dfull)
    assert(received_errors==(nl_probe_tl_x_derror ? error_mask(received_bytes) : 0));
  if(nl_probe_tl_x_message && nl_probe_tl_x_a_done && !nl_probe_tl_x_d_done)
    assert(observed_size==nl_probe_tl_x_size);
  if(nl_rrsp_valid) begin
    assert(queued_bytes>=response_bytes_count);
    assert(nl_rrsp_result==((|response_errors) ? 2 : 0));
    if(|response_errors) assert(nl_rrsp_data==0);
  end
end
always @(posedge nl_clk) if(nl_reset_n) begin
  error_queue<=next_errors;
  queued_bytes<=kept_bytes+push_bytes;
  if(nl_probe_tl_x_a_take) observed_size<=nl_tl_a_size;
  cover(nl_rrsp_valid && (|response_errors));
  cover(nl_rrsp_valid && !(|response_errors) && saved_size<2);
end
"""


def endpoint(ports, module, maximum, footprint=False, write=False, errors=False):
    inputs, wires, connections = [], [], []
    for name, width, incoming in ports:
        if name == "nl_clk":
            continue
        decl = f"wire [{width-1}:0] {name}"
        (inputs if incoming else wires).append(decl)
        inactive = name == ("nl_wreq_valid" if footprint or errors else "nl_rreq_valid")
        signal = "1'b0" if inactive else name
        connections.append(f".{name}({signal})")
    extra = (error_properties(maximum) if errors else write_properties(maximum) if write
             else footprint_properties(maximum) if footprint else "")
    if maximum > 1024:
        extra = (extra.replace("reg [11:0] remaining", "reg [12:0] remaining")
            .replace("wire [8:0] consumed", "wire [10:0] consumed")
            .replace("wire [8:0] produced", "wire [10:0] produced")
            .replace("remaining<=1024", "remaining<=4096")
            .replace("words_left<=256", "words_left<=1024")
            .replace("remaining+pending_d+staged==owed", "{2'd0,remaining}+{2'd0,pending_d}+{2'd0,staged}=={2'd0,owed}")
            .replace("remaining+pending+offset==owed", "{2'd0,remaining}+{2'd0,pending}+{2'd0,offset}=={2'd0,owed}"))
    if footprint or write or errors:
        return f"""module formal_top(input wire nl_clk, {', '.join('input '+s for s in inputs)});
{';'.join(wires)};
{module} dut(.nl_clk(nl_clk), {','.join(connections)});
reg started=0;
always @(posedge nl_clk) started<=1;
always @* begin
  assume(nl_reset_n==started);
  assume(!nl_tl_d_valid || (nl_probe_tl_x_message && !nl_probe_tl_x_d_done &&
                           (nl_probe_tl_x_a_done || nl_probe_tl_x_a_take)));
end
{extra}
always @(posedge nl_clk) if(nl_reset_n)
  cover(nl_probe_tl_x_a_take && nl_tl_a_size=={maximum.bit_length()-1});
endmodule
"""
    return f"""module formal_top(input wire nl_clk, {', '.join('input '+s for s in inputs)});
{';'.join(wires)};
{module} dut(.nl_clk(nl_clk), {','.join(connections)});
reg started=0, past_valid=0, early_seen=0, local_miss=0, early_denied=0;
always @(posedge nl_clk) begin started<=1; past_valid<=nl_reset_n; end
always @* begin
  assume(nl_reset_n==started);
  assume(!nl_wreq_valid || !nl_rreq_valid);
  assume(!nl_rreq_valid);
  assert(!nl_probe_tl_x_rfull && !nl_probe_tl_x_dfull);
  if(nl_probe_tl_x_busy) assert(nl_probe_tl_x_write);
  assume(!nl_tl_d_valid || (nl_probe_tl_x_message && !nl_probe_tl_x_d_done &&
                           (nl_probe_tl_x_a_done || nl_probe_tl_x_a_take)));
  assert(!nl_probe_tl_x_message_complete || (nl_probe_tl_x_a_done && nl_probe_tl_x_d_done));
  assert(!nl_probe_tl_x_a_take || (nl_probe_tl_x_message && !nl_probe_tl_x_a_done));
  assert(!nl_probe_tl_x_d_take || (nl_probe_tl_x_message && !nl_probe_tl_x_d_done));
  if(nl_reset_n && nl_probe_tl_x_message && nl_probe_tl_x_write && !nl_probe_tl_x_d_done)
    assert(nl_tl_d_ready);
  assert(!nl_tl_a_valid || nl_probe_tl_x_message);
  if((nl_probe_tl_x_message && nl_probe_tl_x_write) || nl_probe_tl_x_wfull)
    assert(nl_probe_tl_x_busy);
  if(nl_probe_tl_x_wfull) assert(nl_probe_tl_x_write && nl_probe_tl_x_words_left!=0);
  if(nl_probe_tl_x_busy && nl_probe_tl_x_packet_x_error!=0) assert(!nl_tl_a_valid);
  if(nl_probe_tl_x_message) begin
    if(nl_probe_tl_x_local_code==0) begin
      assert(nl_probe_tl_x_a_done==(nl_probe_tl_x_a_left==0));
      assert(nl_probe_tl_x_d_done==(nl_probe_tl_x_d_left==0));
    end
    assert(nl_probe_tl_x_a_left<={max(1, maximum//4)});
    assert(nl_probe_tl_x_d_left<={max(1, maximum//4)});
    if(!nl_probe_tl_x_a_done) assert(nl_probe_tl_x_a_left!=0);
    if(!nl_probe_tl_x_d_done) assert(nl_probe_tl_x_d_left!=0);
  end
end
always @(posedge nl_clk) if(past_valid && nl_reset_n) begin
  if($past(nl_probe_tl_x_busy && !nl_probe_tl_x_release)) assert(nl_probe_tl_x_busy);
  if(!$past(nl_probe_tl_x_busy) && nl_probe_tl_x_busy) assert($past(nl_probe_tl_x_accept));
  if($past(nl_probe_tl_x_message && !nl_probe_tl_x_message_complete)) assert(nl_probe_tl_x_message);
  if($past(nl_tl_a_valid && !nl_tl_a_ready)) begin
    assert(nl_tl_a_valid);
    assert(nl_tl_a_size==$past(nl_tl_a_size));
    assert(nl_tl_a_address==$past(nl_tl_a_address));
    assert(nl_tl_a_opcode==$past(nl_tl_a_opcode));
    assert(nl_tl_a_mask==$past(nl_tl_a_mask));
    assert(nl_tl_a_data==$past(nl_tl_a_data));
  end
  if(nl_probe_tl_x_d_take && nl_probe_tl_x_write) early_seen<=1;
  if(nl_probe_tl_x_accept)
    local_miss<=(nl_probe_tl_x_w_take ? nl_wreq_error : nl_rreq_error)!=0;
  if(nl_probe_tl_x_d_take && nl_probe_tl_x_write && nl_tl_d_denied && !nl_probe_tl_x_a_done)
    early_denied<=1;
  cover(local_miss && nl_probe_tl_x_release);
  cover(early_denied && nl_wrsp_valid);
  {'' if footprint else f'cover(nl_probe_tl_x_a_take && nl_probe_tl_x_d_take && nl_probe_tl_x_write && nl_probe_tl_x_a_left=={max(1,maximum//4)});'}
  {'' if footprint else 'cover(early_seen && nl_probe_tl_x_message_complete && nl_probe_tl_x_a_done && nl_probe_tl_x_d_done);'}
  cover(nl_probe_tl_x_a_take && nl_tl_a_size=={maximum.bit_length()-1});
  cover(nl_probe_tl_x_busy && nl_status_timed_out);
end
{extra}
endmodule
"""


def run(args):
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="proof-", dir=args.work)).resolve()
    subprocess.run([str(args.fixture.resolve()), str(work), args.kind, str(args.maximum)],
                   check=True, timeout=60)
    ports = [(n, int(w), int(i)) for n, w, i in
             (line.split() for line in (work / "ports.tsv").read_text().splitlines())]
    module = re.search(r"module\s+(\w+)", (work / "network.v").read_text())[1]
    body = selector(args.maximum) if args.kind == "selector" else endpoint(ports, module, args.maximum, args.kind=="footprint", args.kind=="write", args.kind=="errors")
    (work / "formal_top.sv").write_text(body)
    script = """[tasks]
prove
cover
[options]
prove: mode prove
prove: depth 32
prove: timeout 600
prove: aigsmt z3
cover: mode cover
cover: depth 48
cover: timeout 120
[engines]
prove: abc pdr
cover: smtbmc z3
[script]
read_verilog -formal network.v
read_verilog -formal -sv formal_top.sv
prep -top formal_top
[files]
network.v
formal_top.sv
"""
    if args.kind in ("footprint", "write", "errors"):
        script = script.replace("prove: abc pdr", "prove: smtbmc z3")
        script = script.replace("prove: depth 32", "prove: depth 4")
    (work / "safety.sby").write_text(script)
    with subprocess.Popen(["sby", "safety.sby"], cwd=work, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
        try:
            out, err = process.communicate(timeout=660)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            out, err = process.communicate()
            print(out, err)
            print("TIMEOUT", work)
            return 1
        code = process.returncode
    print(out, err)
    print(work)
    for task in ("prove", "cover"):
        status = work / f"safety_{task}" / "status"
        if not status.exists() or status.read_text().split()[0] != "PASS":
            return 1
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--kind", choices=("selector", "endpoint", "footprint", "write", "errors"), required=True)
    parser.add_argument("--maximum", type=int, default=1024)
    parser.add_argument("--all-maxima", action="store_true")
    args = parser.parse_args()
    for maximum in ([1 << n for n in range(2, 13)] if args.all_maxima else [args.maximum]):
        args.maximum = maximum
        result = run(args)
        if result:
            return result
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
