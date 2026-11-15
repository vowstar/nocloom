"""Unbounded Wishbone sequencing, replay and termination safety."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_process

PROPERTIES = r"""
  (* anyseq *) reg [4:0] offered_length, read_length;
  (* anyseq *) reg [2:0] offered_size, read_size;
  (* anyseq *) reg [1:0] offered_burst, read_burst;
  (* anyseq *) reg [15:0] offered_base, read_base;
  reg [4:0] held_length;
  reg [2:0] held_size;
  reg [1:0] held_burst;
  reg [15:0] held_base;
  wire [4:0] length = left == 0 ? offered_length : held_length;
  wire [2:0] size = left == 0 ? offered_size : held_size;
  wire [1:0] burst = left == 0 ? offered_burst : held_burst;
  wire [15:0] base = left == 0 ? offered_base : held_base;
  (* anyconst *) reg [4:0] tracked;
  reg [5:0] left = 0;
  reg [15:0] waddress = 0;
  wire [15:0] step = 16'd1 << size;
  wire [15:0] wrap_mask = (({11'd0, length} + 16'd1) << size) - 1;
  wire [15:0] wa = left == 0 ? base : waddress;
  wire [15:0] next_wa = burst == 0 ? wa : burst == 2 ?
      (base & ~wrap_mask) | ((wa + step) & wrap_mask) : wa + step;
  assign nl_wreq_first = left == 0;
  assign nl_wreq_last = left == 1 || (left == 0 && length == 0);
  assign nl_wreq_address = {16'd0, wa};
  assign nl_wreq_start_address = {16'd0, base};
  assign nl_wreq_length = {3'd0, length};
  assign nl_wreq_size = size;
  assign nl_wreq_burst = burst;
  assign nl_rreq_first = 1;
  assign nl_rreq_last = 1;
  assign nl_rreq_address = {16'd0, read_base};
  assign nl_rreq_start_address = {16'd0, read_base};
  assign nl_rreq_length = {3'd0, read_length};
  assign nl_rreq_size = read_size;
  assign nl_rreq_burst = read_burst;
  assign nl_wreq_error = 0;
  assign nl_rreq_error = 0;
  wire wt = nl_wreq_valid && nl_wreq_ready;
  wire done = nl_wb_cyc && nl_wb_stb && (nl_wb_ack || nl_wb_err || nl_wb_rty);
  reg past_valid = 0;
  reg owed = 0;
  reg [15:0] expected_address;
  reg expected_write;
  reg [3:0] expected_sel;
  reg [1:0] expected_bte;
  reg [2:0] expected_kind;
  (* anyconst *) reg [4:0] tracked_bit;
  reg tracked_data0, tracked_data1;
  reg tracked_valid0 = 0, tracked_valid1 = 0;
  reg [2:0] write_packets = 0;
  reg [7:0] read_words = 0;
  reg [3:0] continuous = 0;
  reg saw_error = 0, saw_gap_ack = 0, saw_pause = 0;
  reg [15:0] last_bus_address = 0;
  wire [15:0] bus_wrap_mask = (({8'd0, probe_bus_descriptor_length} + 16'd1) <<
                              probe_bus_descriptor_size) - 16'd1;
  wire [15:0] linear_address = probe_bus_descriptor_address +
                               ({10'd0, probe_bus_cursor} << probe_bus_descriptor_size);
  integer slot;
  function legal_wrap(input [7:0] n);
    legal_wrap = n == 1 || n == 3 || n == 7 || n == 15;
  endfunction
  function [15:0] successor(input [15:0] a, input [1:0] b);
    reg [15:0] mask;
    begin
      mask = b == 1 ? 15 : b == 2 ? 31 : b == 3 ? 63 : 65535;
      successor = (a & ~mask) | ((a + 16'd4) & mask);
    end
  endfunction
  always @* begin
    assume(offered_size <= 2 && offered_burst <= 2);
    assume(offered_base < 65408 && (offered_base & ((16'd1 << offered_size) - 1)) == 0);
    if (offered_burst == 2)
      assume(offered_length == 1 || offered_length == 3 || offered_length == 7 || offered_length == 15);
    assume(read_size <= 2 && read_burst <= 2);
    assume(read_base < 65408 && (read_base & ((16'd1 << read_size) - 1)) == 0);
    if (read_burst == 2)
      assume(read_length == 1 || read_length == 3 || read_length == 7 || read_length == 15);
    assume(!(nl_wb_ack && nl_wb_err) && !(nl_wb_ack && nl_wb_rty) && !(nl_wb_err && nl_wb_rty));
  end
  always @(posedge clk) begin
    past_valid <= 1;
    if (!rst_n) begin
      left <= 0;
      owed <= 0;
      tracked_valid0 <= 0;
      tracked_valid1 <= 0;
      write_packets <= 0;
      read_words <= 0;
      continuous <= 0;
      saw_error <= 0;
      saw_gap_ack <= 0;
      saw_pause <= 0;
    end else begin
      write_packets <= write_packets + (wt && nl_wreq_first) - (nl_wrsp_valid && nl_wrsp_ready);
      read_words <= read_words + ((nl_rreq_valid && nl_rreq_ready) ? {3'd0, read_length} + 1 : 0)
                    - (nl_rrsp_valid && nl_rrsp_ready);
      assert(write_packets == {2'd0, probe_w_bank0_used} + {2'd0, probe_w_bank1_used} +
                              {1'b0, probe_w_response_t0_count});
      assert(read_words == (probe_r_pending ? {1'b0, probe_r_descriptor_length} + 1 : 0) +
             ((probe_bus_busy && !probe_bus_write) ?
              {1'b0, probe_bus_descriptor_length} + 1 - probe_bus_cursor : 0) +
             probe_r_response_t0_count);
      assert(!nl_wrsp_valid || write_packets != 0);
      assert(!nl_rrsp_valid || read_words != 0);
      if (!nl_wb_stb) continuous <= 0;
      else if (done && continuous < 8) continuous <= continuous + 1;
      if (done) last_bus_address <= nl_wb_adr;
      if (done && nl_wb_err && (nl_wb_cti == 1 || nl_wb_cti == 2)) saw_error <= 1;
      if (nl_wb_cyc && !nl_wb_stb && nl_wb_ack) saw_gap_ack <= 1;
      if (probe_bus_phase == 2 && probe_r_response_t0_count == 2) saw_pause <= 1;
      if (wt) begin
        if (left == 0) begin
          held_length <= offered_length;
          held_size <= offered_size;
          held_burst <= offered_burst;
          held_base <= offered_base;
        end
        left <= left == 0 ? {1'b0, length} : left - 1;
        waddress <= next_wa;
      end
      if (wt && probe_w_fill_bank == 0) begin
        if (nl_wreq_first) tracked_valid0 <= 0;
        if (probe_w_received == {1'b0, tracked}) begin
          tracked_data0 <= nl_wreq_data[tracked_bit];
          tracked_valid0 <= 1;
        end
      end
      if (wt && probe_w_fill_bank == 1) begin
        if (nl_wreq_first) tracked_valid1 <= 0;
        if (probe_w_received == {1'b0, tracked}) begin
          tracked_data1 <= nl_wreq_data[tracked_bit];
          tracked_valid1 <= 1;
        end
      end
      assert(nl_wb_stb <= nl_wb_cyc);
      assert(nl_wb_stb == (probe_bus_phase == 1));
      assert(nl_wb_cyc == (probe_bus_phase == 1 || probe_bus_phase == 2));
      if (probe_bus_phase == 2) assert(owed);
      if (probe_bus_phase == 1 || probe_bus_phase == 2)
        assert(probe_bus_busy && !probe_bus_finished);
      assert(!nl_wb_stb || probe_bus_busy);
      assert(probe_w_filling == (left != 0));
      if (probe_w_filling && probe_bus_busy && probe_bus_write)
        assert(probe_w_fill_bank != probe_w_head_bank);
      assert(probe_w_received < 32);
      if (!probe_w_filling) assert(probe_w_received == 0);
      if (probe_w_filling) begin
        assert(left + probe_w_received == {1'b0, held_length} + 1);
        if (probe_w_fill_bank == 0) begin
          assert(probe_w_bank0_used && !probe_w_bank0_ready);
          assert(probe_w_bank0_count == probe_w_received);
          assert(probe_w_bank0_size == held_size);
          assert(probe_w_bank0_length == {3'd0, held_length});
        end else begin
          assert(probe_w_bank1_used && !probe_w_bank1_ready);
          assert(probe_w_bank1_count == probe_w_received);
          assert(probe_w_bank1_size == held_size);
          assert(probe_w_bank1_length == {3'd0, held_length});
        end
      end
      assert(probe_w_bank0_count <= 32 && probe_w_bank1_count <= 32);
      assert(probe_r_response_t0_count <= 2);
      if (probe_r_pending)
        assert(!probe_bus_busy && !probe_w_bank0_used && !probe_w_bank1_used);
      if (probe_bus_busy && !probe_bus_write) assert(!probe_bus_finished);
      assert(probe_r_descriptor_length <= 31);
      assert(!probe_r_descriptor_error && !probe_bus_descriptor_error);
      assert(!probe_w_bank0_error && !probe_w_bank1_error);
      assert(probe_r_descriptor_burst <= 2 && probe_bus_descriptor_burst <= 2);
      assert(probe_w_bank0_burst <= 2 && probe_w_bank1_burst <= 2);
      assert(probe_r_descriptor_size <= 2 && probe_bus_descriptor_size <= 2);
      assert(probe_w_bank0_size <= 2 && probe_w_bank1_size <= 2);
      assert(probe_r_descriptor_address < 65408 && probe_bus_descriptor_address < 65408);
      assert(probe_w_bank0_address < 65408 && probe_w_bank1_address < 65408);
      if (probe_r_descriptor_burst == 2) assert(legal_wrap(probe_r_descriptor_length));
      if (probe_w_bank0_burst == 2) assert(legal_wrap(probe_w_bank0_length));
      if (probe_w_bank1_burst == 2) assert(legal_wrap(probe_w_bank1_length));
      if (probe_bus_descriptor_burst == 2) assert(legal_wrap(probe_bus_descriptor_length));
      if (probe_w_bank0_ready)
        assert({2'd0, probe_w_bank0_count} == probe_w_bank0_length + 1);
      if (probe_w_bank1_ready)
        assert({2'd0, probe_w_bank1_count} == probe_w_bank1_length + 1);
      assert((probe_r_descriptor_address & ((16'd1 << probe_r_descriptor_size) - 1)) == 0);
      assert((probe_w_bank0_address & ((16'd1 << probe_w_bank0_size) - 1)) == 0);
      assert((probe_w_bank1_address & ((16'd1 << probe_w_bank1_size) - 1)) == 0);
      assert((probe_bus_descriptor_address & ((16'd1 << probe_bus_descriptor_size) - 1)) == 0);
      if (probe_bus_busy) begin
        if (probe_bus_descriptor_burst == 0)
          assert(probe_bus_address == probe_bus_descriptor_address);
        if (probe_bus_descriptor_burst == 1) assert(probe_bus_address == linear_address);
        if (probe_bus_descriptor_burst == 2)
          assert(probe_bus_address == ((probe_bus_descriptor_address & ~bus_wrap_mask) |
                                      (linear_address & bus_wrap_mask)));
      end
      assert(!probe_w_bank0_ready || probe_w_bank0_used);
      assert(!probe_w_bank0_used || probe_w_bank0_count != 0);
      assert(!probe_w_bank1_used || probe_w_bank1_count != 0);
      if (probe_bus_busy && !probe_bus_finished) begin
        assert(probe_bus_descriptor_length <= 31);
        assert(probe_bus_cursor <= probe_bus_descriptor_length);
      end
      assert(!probe_w_bank1_ready || probe_w_bank1_used);
      assert(probe_w_bank0_prefix <= probe_w_bank0_count);
      if (probe_w_bank0_used && probe_w_bank0_count > {1'b0, tracked}) assert(tracked_valid0);
      if (probe_w_bank1_used && probe_w_bank1_count > {1'b0, tracked}) assert(tracked_valid1);
      if (tracked_valid0) assert(bank0_data[tracked][tracked_bit] == tracked_data0);
      if (tracked_valid1) assert(bank1_data[tracked][tracked_bit] == tracked_data1);
      assert(probe_w_bank1_prefix <= probe_w_bank1_count);
      if (probe_w_bank0_used && probe_w_bank0_prefix != 0) assert(probe_w_bank0_size == 2);
      if (probe_w_bank1_used && probe_w_bank1_prefix != 0) assert(probe_w_bank1_size == 2);
      for (slot = 0; slot < 32; slot = slot + 1) begin
        if (probe_w_bank0_used && probe_w_bank0_count > slot) begin
          if (probe_w_bank0_uniform) assert(bank0_mask[slot] == probe_w_bank0_first_mask);
          if (probe_w_bank0_prefix > slot) assert(bank0_mask[slot] == 15);
        end
        if (probe_w_bank1_used && probe_w_bank1_count > slot) begin
          if (probe_w_bank1_uniform) assert(bank1_mask[slot] == probe_w_bank1_first_mask);
          if (probe_w_bank1_prefix > slot) assert(bank1_mask[slot] == 15);
        end
      end
      if (probe_bus_busy && !probe_bus_finished && probe_bus_mode != 0) begin
        assert(probe_bus_descriptor_size == 2);
        if (!probe_bus_write) assert(probe_bus_prefix == probe_bus_descriptor_length + 1);
        if (probe_bus_mode == 1) begin
          assert(probe_bus_descriptor_burst == 1);
          assert(probe_bus_prefix <= probe_bus_descriptor_length + 1);
        end
        if (probe_bus_mode == 2) begin
          assert(probe_bus_prefix == probe_bus_descriptor_length + 1);
          assert(probe_bus_descriptor_burst == 2);
          assert(probe_bus_descriptor_length == 3 || probe_bus_descriptor_length == 7 ||
                 probe_bus_descriptor_length == 15);
          assert(probe_bus_wrap == (probe_bus_descriptor_length == 3 ? 1 :
                                    probe_bus_descriptor_length == 7 ? 2 : 3));
        end
        if (probe_bus_mode == 3) assert(probe_bus_descriptor_burst == 0);
        if (probe_bus_write) begin
          if (probe_w_head_bank == 0) begin
            if (probe_bus_mode == 3) begin
              assert(probe_w_bank0_uniform);
              assert(probe_w_bank0_first_mask != 0);
            end
            else assert(probe_bus_prefix == probe_w_bank0_prefix);
          end else begin
            if (probe_bus_mode == 3) begin
              assert(probe_w_bank1_uniform);
              assert(probe_w_bank1_first_mask != 0);
            end
            else assert(probe_bus_prefix == probe_w_bank1_prefix);
          end
        end
      end
      if (probe_bus_phase == 1 && !nl_wb_we) assert(probe_r_response_t0_count <= 1);
      if (probe_bus_busy && probe_bus_write) begin
        if (probe_w_head_bank == 0) begin
          assert(probe_w_bank0_used && probe_w_bank0_ready);
          assert(probe_bus_descriptor_length == probe_w_bank0_count - 1);
          assert(probe_bus_cursor <= probe_w_bank0_count);
          if (!probe_bus_finished) assert(probe_bus_cursor < probe_w_bank0_count);
        end else begin
          assert(probe_w_bank1_used && probe_w_bank1_ready);
          assert(probe_bus_descriptor_length == probe_w_bank1_count - 1);
          assert(probe_bus_cursor <= probe_w_bank1_count);
          if (!probe_bus_finished) assert(probe_bus_cursor < probe_w_bank1_count);
        end
      end
      if (probe_w_bank0_used && probe_w_bank0_count > {1'b0, tracked}) begin
        if (probe_w_bank0_uniform) assert(bank0_mask[tracked] == probe_w_bank0_first_mask);
        if (probe_w_bank0_prefix > {1'b0, tracked}) assert(bank0_mask[tracked] == 15);
      end
      if (probe_w_bank1_used && probe_w_bank1_count > {1'b0, tracked}) begin
        if (probe_w_bank1_uniform) assert(bank1_mask[tracked] == probe_w_bank1_first_mask);
        if (probe_w_bank1_prefix > {1'b0, tracked}) assert(bank1_mask[tracked] == 15);
      end
      if (nl_wb_stb) begin
        assert(nl_wb_adr == {probe_bus_address[15:2], 2'b0});
        assert(nl_wb_bte == (probe_bus_mode == 2 ? probe_bus_wrap : 0));
        if (probe_bus_mode == 3)
          assert(nl_wb_cti == (probe_bus_cursor == probe_bus_descriptor_length ? 7 : 1));
        else if (probe_bus_mode == 2)
          assert(nl_wb_cti == (probe_bus_cursor == probe_bus_descriptor_length ? 7 : 2));
        else if (probe_bus_mode == 1 && probe_bus_cursor < probe_bus_prefix)
          assert(nl_wb_cti == (probe_bus_cursor + 1 == probe_bus_prefix ? 7 : 2));
        else assert(nl_wb_cti == 0);
        if (nl_wb_we) begin
          if (probe_w_head_bank == 0) begin
            assert(nl_wb_dat_w[tracked_bit] == bank0_data[probe_bus_cursor[4:0]][tracked_bit]);
            assert(nl_wb_sel == bank0_mask[probe_bus_cursor[4:0]]);
          end else begin
            assert(nl_wb_dat_w[tracked_bit] == bank1_data[probe_bus_cursor[4:0]][tracked_bit]);
            assert(nl_wb_sel == bank1_mask[probe_bus_cursor[4:0]]);
          end
        end else begin
          if (probe_bus_descriptor_size == 2) assert(nl_wb_sel == 15);
          if (probe_bus_descriptor_size == 1)
            assert(nl_wb_sel == (probe_bus_address[1] ? 12 : 3));
          if (probe_bus_descriptor_size == 0)
            assert(nl_wb_sel == (4'b1 << probe_bus_address[1:0]));
        end
      end
      if (nl_wb_stb && nl_wb_we && probe_bus_cursor == {1'b0, tracked}) begin
        if (probe_w_head_bank == 0) begin
          assert(tracked_valid0);
          assert(nl_wb_dat_w[tracked_bit] == tracked_data0);
        end else begin
          assert(tracked_valid1);
          assert(nl_wb_dat_w[tracked_bit] == tracked_data1);
        end
      end
      if (past_valid && $past(rst_n && nl_wb_stb && !done))
        assert({nl_wb_cyc, nl_wb_stb, nl_wb_we, nl_wb_adr, nl_wb_sel, nl_wb_dat_w[tracked_bit],
                nl_wb_cti, nl_wb_bte} ==
               $past({nl_wb_cyc, nl_wb_stb, nl_wb_we, nl_wb_adr, nl_wb_sel, nl_wb_dat_w[tracked_bit],
                       nl_wb_cti, nl_wb_bte}));
      if (owed) begin
        assert(probe_bus_busy && !probe_bus_finished);
        assert(probe_bus_phase == 1 || probe_bus_phase == 2);
        assert(probe_bus_mode != 0);
        assert(expected_address == {probe_bus_address[15:2], 2'b0});
        assert(expected_write == probe_bus_write);
        assert(expected_bte == (probe_bus_mode == 2 ? probe_bus_wrap : 0));
        assert(expected_kind == (probe_bus_mode == 3 ? 1 : 2));
        if (probe_bus_mode == 1) assert(probe_bus_cursor < probe_bus_prefix);
        if (probe_bus_mode != 3 || !probe_bus_write) assert(expected_sel == 15);
        else if (probe_w_head_bank == 0) assert(expected_sel == probe_w_bank0_first_mask);
        else assert(expected_sel == probe_w_bank1_first_mask);
        assert(nl_wb_cyc);
        if (nl_wb_stb) begin
          assert(nl_wb_adr == expected_address && nl_wb_we == expected_write);
          assert(nl_wb_sel == expected_sel && nl_wb_bte == expected_bte);
          assert(nl_wb_cti == expected_kind || nl_wb_cti == 7);
        end
      end
      if (done) begin
        owed <= nl_wb_cti == 1 || nl_wb_cti == 2;
        expected_address <= nl_wb_cti == 1 ? nl_wb_adr : successor(nl_wb_adr, nl_wb_bte);
        expected_write <= nl_wb_we;
        expected_sel <= nl_wb_sel;
        expected_bte <= nl_wb_bte;
        expected_kind <= nl_wb_cti;
      end
      cover(probe_w_bank0_used && probe_w_bank1_used);
      cover(nl_wb_stb && nl_wb_bte == 1 && nl_wb_cti == 7);
      cover(nl_wb_stb && nl_wb_bte == 2 && nl_wb_cti == 7);
      cover(nl_wb_stb && nl_wb_bte == 3 && nl_wb_cti == 7);
      cover(probe_bus_phase == 2 && probe_r_response_t0_count == 2);
      cover(done && nl_wb_err && nl_wb_cti == 2);
      cover(continuous == 8);
      cover(saw_gap_ack && done && nl_wb_cti == 7);
      cover(saw_pause && done && !nl_wb_we && nl_wb_cti == 7);
      cover(saw_error && done && nl_wb_ack && nl_wb_cti == 7);
      cover(done && nl_wb_bte == 1 && owed && nl_wb_adr < last_bus_address);
      cover(done && nl_wb_bte == 2 && owed && nl_wb_adr < last_bus_address);
      cover(done && nl_wb_bte == 3 && owed && nl_wb_adr < last_bus_address);
      cover(done && nl_wb_cti == 7 && probe_bus_mode == 1 &&
            probe_bus_cursor < probe_bus_descriptor_length);
      cover(wt && nl_wreq_last && done && nl_wb_we && probe_bus_cursor == probe_bus_descriptor_length && (probe_w_response_t0_count < 2 || nl_wrsp_ready));
    end
  end
"""


def harness(component):
    driven = {"first", "last", "address", "start_address", "length", "size", "burst", "error"}
    declarations, connections = [], []
    for port in component["ports"]:
        name, width = port["name"], port["width"]
        span = f"[{width - 1}:0] " if width > 1 else ""
        if port["endpoint"].startswith("domain:"):
            declarations.append(f"wire {name} = " + ("clk;" if port["signal"] == "clock" else "rst_n;"))
        elif port["input"] and port["endpoint"] in ("wreq", "rreq") and port["signal"] in driven:
            declarations.append(f"wire {span}{name};")
        else:
            declarations.append(("(* anyseq *) reg " if port["input"] else "wire ") + span + name + ";")
        connections.append(f".{name}({name})")
    banks = []
    for k in (0, 1):
        banks.append(f"wire [31:0] bank{k}_data [0:31];")
        banks.append(f"wire [3:0] bank{k}_mask [0:31];")
        for j in range(32):
            banks.append(f"assign bank{k}_data[{j}] = probe_w_bank{k}_data{j};")
            banks.append(f"assign bank{k}_mask[{j}] = probe_w_bank{k}_mask{j};")
    declarations.extend(banks)
    word = next(p["width"] for p in component["ports"] if p["name"] == "nl_wb_adr") == 14
    declarations.append("wire [15:0] bus_byte_address = " +
                        ("{nl_wb_adr, 2'b0};" if word else "nl_wb_adr;"))
    return ("module tb(input clk);\nreg rst_n = 0; always @(posedge clk) rst_n <= 1;\n" +
            "\n".join(declarations) + "\n" + component["module"] + " dut(" +
            ",".join(connections) + ");\n" + PROPERTIES.replace("nl_wb_adr", "bus_byte_address") +
            "\nendmodule\n")


def source_harness(component):
    driven = {"nl_wrsp_valid", "nl_rrsp_valid", "nl_wrsp_first", "nl_wrsp_last",
              "nl_rrsp_first", "nl_rrsp_last"}
    declarations, connections = [], []
    for port in component["ports"]:
        name, width = port["name"], port["width"]
        span = f"[{width - 1}:0] " if width > 1 else ""
        if port["endpoint"].startswith("domain:"):
            declarations.append(f"wire {name} = " + ("clk;" if port["signal"] == "clock" else "rst_n;"))
        else:
            kind = "(* anyseq *) reg " if port["input"] and name not in driven else "wire "
            declarations.append(kind + span + name + ";")
        connections.append(f".{name}({name})")
    body = r"""
  (* anyseq *) reg return_write, return_read;
  reg wpending = 0, rpending = 0;
  reg saw_abort = 0, saw_drain = 0, saw_write = 0;
  wire wt = nl_wreq_valid && nl_wreq_ready;
  wire rt = nl_rreq_valid && nl_rreq_ready;
  wire bt = nl_wrsp_valid && nl_wrsp_ready;
  wire dt = nl_rrsp_valid && nl_rrsp_ready;
  assign nl_wrsp_valid = return_write && wpending;
  assign nl_rrsp_valid = return_read && rpending;
  assign nl_wrsp_first = 1;
  assign nl_wrsp_last = 1;
  assign nl_rrsp_first = 1;
  assign nl_rrsp_last = 1;
  always @* begin
    if (!rst_n || !nl_wb_cyc || !nl_wb_stb) assert(!nl_wb_ack && !nl_wb_err);
    assert(!(nl_wb_ack && nl_wb_err));
  end
  always @(posedge clk) begin
    if (!rst_n) begin
      wpending <= 0; rpending <= 0;
      saw_abort <= 0; saw_drain <= 0; saw_write <= 0;
    end else begin
      wpending <= (wpending || wt) && !bt;
      rpending <= (rpending || rt) && !dt;
      assert(!(wt && wpending) && !(rt && rpending));
      assert(!(wpending && rpending));
      if (wpending || rpending) assert(probe_wb_busy);
      if (nl_wreq_valid) begin
        assert(probe_wb_busy);
        assert(nl_wreq_length == 0 && nl_wreq_burst == 1);
      end
      if (nl_rreq_valid) begin
        assert(probe_wb_busy);
        assert(nl_rreq_length == 0 && nl_rreq_burst == 1);
      end
      if (probe_wb_busy && (!nl_wb_cyc || !nl_wb_stb)) saw_abort <= 1;
      if (saw_abort && (bt || dt)) saw_drain <= 1;
      if (saw_drain && nl_wb_ack && probe_wb_write) saw_write <= 1;
      cover(saw_abort && probe_wb_abandoned && (wpending || rpending));
      cover(saw_drain && nl_wb_ack && !probe_wb_write);
      cover(saw_write && nl_wb_ack && !probe_wb_write);
    end
  end
"""
    return ("module tb(input clk);\nreg rst_n = 0; always @(posedge clk) rst_n <= 1;\n" +
            "\n".join(declarations) + "\n" + component["module"] + " dut(" +
            ",".join(connections) + ");\n" + body + "\nendmodule\n")


def check(work, body, timeout, engine="smtbmc z3"):
    (work / "formal.sv").write_text(body)
    sby = """[tasks]
prove
cover
[options]
prove: mode prove
cover: mode cover
prove: depth 4
prove: aigsmt z3
cover: depth 96
[engines]
prove: smtbmc z3
cover: smtbmc z3
[script]
read -formal network.v formal.sv
prep -top tb -flatten
async2sync
dffunmap
[files]
network.v
formal.sv
"""
    (work / "check.sby").write_text(sby)
    if engine != "smtbmc z3":
        (work / "check.sby").write_text(sby.replace("prove: smtbmc z3", "prove: " + engine))
    with (work / "sby.log").open("w") as log:
        result = sim_process.run(["sby", "check.sby"], cwd=work, stdout=log,
                                 stderr=subprocess.STDOUT, timeout=timeout)
    print((work / "sby.log").read_text()[-4000:])
    print(json.dumps(dict(work=work.name, status=result.returncode,
                          inputs={p: hashlib.sha256((work / p).read_bytes()).hexdigest()
                                  for p in ("network.v", "formal.sv", "check.sby")})))
    return result.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    deadline = time.monotonic() + 550
    recipes = ("wishbone_source",) if args.source_only else ("wishbone_unit", "wishbone_source")
    for recipe in recipes:
        for word in (0, 1):
            target = work / f"{recipe}_{word}"
            target.mkdir()
            subprocess.run([args.fixture, recipe, str(target), "--param", f"word={word}"],
                           check=True, capture_output=True)
            component, = json.loads((target / "ports.json").read_text())
            body = source_harness(component) if recipe == "wishbone_source" else harness(component)
            engine = "abc pdr" if recipe == "wishbone_source" else "smtbmc z3"
            if check(target, body, max(1, deadline - time.monotonic()), engine):
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
