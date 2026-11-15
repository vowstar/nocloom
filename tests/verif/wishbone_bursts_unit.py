"""Measure bus cadence and exercise bank ownership and reserved read credit."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_engine

BODY = r"""
  integer ack_model, family, cycles = 0, waited = 0, bus_count = 0, expected_count = 0;
  integer first_cycle = -1, last_cycle = 0, reads = 0, writes = 0, answers = 0;
  integer expect_r_count = 0, receive_r_count = 0, both_banks = 0, overlap = 0;
  integer i, j, start_read, saved_count;
  reg predict = 0;
  reg [31:0] predicted_address = 0;
  reg [31:0] memory [0:16383];
  reg [31:0] expected_memory [0:16383];
  reg [31:0] expected_address [0:1023], expected_data [0:1023];
  reg [3:0] expected_mask [0:1023];
  reg expected_write [0:1023];
  reg [2:0] expected_tag [0:1023];
  reg [31:0] response_data [0:1023];
  reg response_last [0:1023];
  reg [1:0] response_id [0:1023];
  reg response_source [0:1023];
  reg [1:0] response_tag [0:1023];
  integer packet_words [0:1023];
  reg [1:0] packet_identity [0:1023];
  integer packet_index = 0;
  integer word_index = 0;
  integer b_count = 0;
  integer packet_ends [0:31];
  reg [1:0] packet_ids [0:31];
  integer write_packets = 0;
  wire tags_enabled = TAGS;
  wire [31:0] effective_address = predict && ack_model != 0 && ack_model != 3
      ? predicted_address : {16'b0, nl_wb_adr};
  task nlv_fail_report(input string rule, input string name, input integer detail);
    $fatal(1, "CHECK_FAIL:%s:%s", name, rule);
  endtask
  nlv_wishbone_check #(.NAME("target"), .ADDR_W(16), .MASTER(1),
      .REGISTERED_FEEDBACK(TAGS)) check (
      .clk(clk), .rst_n(rst_n), .cyc(nl_wb_cyc), .stb(nl_wb_stb), .we(nl_wb_we),
      .ack(nl_wb_ack), .err(nl_wb_err), .rty(nl_wb_rty), .stall(1'b0),
      .constant_ack(ack_model == 0), .adr(nl_wb_adr), .dat_w(nl_wb_dat_w),
      .dat_r(nl_wb_dat_r), .sel(nl_wb_sel), .cti(CTI), .bte(BTE));
  always @(negedge clk) begin
    nl_wb_ack = ack_model == 0 || (rst_n && nl_wb_cyc &&
        waited >= (ack_model == 3 ? 1 : (predict ? 0 : 1) + (ack_model == 2 ? 2 : 0)));
    nl_wb_dat_r = memory[effective_address >> 2];
  end
  always @(posedge clk) begin
    cycles = cycles + 1;
    if (cycles > 20000) $fatal(1, "CHECK_FAIL:progress");
    if (!rst_n) begin predict = 0; waited = 0; end
    else begin
      BANK_OBSERVE
      if (!nl_wb_cyc) begin predict = 0; waited = 0; end
      if (nl_wb_stb && first_cycle < 0) first_cycle = cycles;
      if (nl_wb_cyc && nl_wb_stb) begin
        if (nl_wb_ack || nl_wb_err || nl_wb_rty) begin
          if (bus_count >= expected_count) $fatal(1, "CHECK_FAIL:extra_bus_word");
          if ({16'b0, nl_wb_adr} !== expected_address[bus_count] ||
              nl_wb_we !== expected_write[bus_count] || nl_wb_sel !== expected_mask[bus_count])
            $fatal(1, "CHECK_FAIL:bus_shape:%d", bus_count);
          if (CTI !== expected_tag[bus_count]) $fatal(1, "CHECK_FAIL:tag:%d", bus_count);
          if (nl_wb_we) begin
            if (nl_wb_dat_w !== expected_data[bus_count])
              $fatal(1, "CHECK_FAIL:bank_data:%d", bus_count);
            for (j = 0; j < 4; j = j + 1)
              if (nl_wb_sel[j]) begin
                memory[nl_wb_adr >> 2][j*8+:8] = nl_wb_dat_w[j*8+:8];
                expected_memory[expected_address[bus_count] >> 2][j*8+:8] =
                    expected_data[bus_count][j*8+:8];
              end
            writes = writes + 1;
          end else begin
            response_data[expect_r_count] = expected_memory[expected_address[bus_count] >> 2];
            response_last[expect_r_count] = word_index == packet_words[packet_index] - 1;
            response_id[expect_r_count] = packet_identity[packet_index];
            response_tag[expect_r_count] = packet_identity[packet_index];
            response_source[expect_r_count] = packet_identity[packet_index] & 1;
            expect_r_count = expect_r_count + 1;
            reads = reads + 1;
          end
          word_index = word_index + 1;
          if (word_index == packet_words[packet_index]) begin
            word_index = 0; packet_index = packet_index + 1;
          end
          $display("WORD_CYCLE:%0d:%0d:%0d:%0d", nl_wb_we, bus_count, cycles, waited);
          bus_count = bus_count + 1;
          predict = CTI == 1 || CTI == 2;
          predicted_address = CTI == 1 ? effective_address : effective_address + 4;
          waited = 0;
          last_cycle = cycles;
        end else waited = waited + 1;
      end
      if (nl_rrsp_valid && nl_rrsp_ready) begin
        if (receive_r_count >= expect_r_count || nl_rrsp_data !== response_data[receive_r_count] ||
            nl_rrsp_last !== response_last[receive_r_count] || nl_rrsp_result !== 0 ||
            nl_rrsp_id !== response_id[receive_r_count] ||
            nl_rrsp_source !== response_source[receive_r_count] ||
            nl_rrsp_tag !== response_tag[receive_r_count])
          $fatal(1, "CHECK_FAIL:read_credit_or_identity:%d", receive_r_count);
        receive_r_count = receive_r_count + 1;
      end
      if (nl_wrsp_valid && nl_wrsp_ready) begin
        if (b_count >= write_packets || bus_count < packet_ends[b_count] ||
            nl_wrsp_result !== 0 || nl_wrsp_id !== packet_ids[b_count] ||
            nl_wrsp_tag !== packet_ids[b_count] || nl_wrsp_source !== (packet_ids[b_count] & 1))
          $fatal(1, "CHECK_FAIL:completion_identity");
        b_count = b_count + 1;
      end
    end
  end
  task describe(input integer a, n, write, identity);
    integer k;
    begin
      packet_words[answers] = n;
      packet_identity[answers] = identity;
      answers = answers + 1;
      for (k = 0; k < n; k = k + 1) begin
        expected_address[expected_count] = a + k * 4;
        expected_mask[expected_count] = 15;
        expected_write[expected_count] = write;
        expected_data[expected_count] = 32'hc0010000 ^ (identity << 12) ^ k;
        expected_tag[expected_count] = TAGS && n > 1 ? (k == n - 1 ? 7 : 2) : 0;
        expected_count = expected_count + 1;
      end
      if (write) begin
        packet_ends[write_packets] = expected_count;
        packet_ids[write_packets] = identity;
        write_packets = write_packets + 1;
      end
    end
  endtask
  task send_write(input integer a, n, identity);
    integer k;
    begin
      describe(a, n, 1, identity);
      for (k = 0; k < n; k = k + 1) begin
        @(negedge clk);
        nl_wreq_valid = 1; nl_wreq_first = k == 0; nl_wreq_last = k == n - 1;
        nl_wreq_address = a + k * 4; nl_wreq_start_address = a;
        nl_wreq_length = n - 1; nl_wreq_size = 2; nl_wreq_burst = 1;
        nl_wreq_data = 32'hc0010000 ^ (identity << 12) ^ k;
        nl_wreq_be = 15; nl_wreq_count = 4;
        nl_wreq_id = identity; nl_wreq_tag = identity; nl_wreq_source = identity & 1;
        @(posedge clk); while (!nl_wreq_ready) @(posedge clk);
      end
      @(negedge clk); nl_wreq_valid = 0;
    end
  endtask
  task send_read(input integer a, n, identity);
    begin
      describe(a, n, 0, identity);
      @(negedge clk);
      nl_rreq_valid = 1; nl_rreq_first = 1; nl_rreq_last = 1;
      nl_rreq_address = a; nl_rreq_start_address = a;
      nl_rreq_length = n - 1; nl_rreq_size = 2; nl_rreq_burst = 1; nl_rreq_count = 4;
      nl_rreq_id = identity; nl_rreq_tag = identity; nl_rreq_source = identity & 1;
      @(posedge clk); while (!nl_rreq_ready) @(posedge clk);
      @(negedge clk); nl_rreq_valid = 0;
    end
  endtask
  initial begin
    if (!$value$plusargs("MODE=%d", ack_model)) ack_model = 0;
    if (!$value$plusargs("FAMILY=%d", family)) family = 0;
    for (i = 0; i < 16384; i = i + 1) begin
      memory[i] = 32'h90100000 ^ i;
      expected_memory[i] = 32'h90100000 ^ i;
    end
    nl_wrsp_ready = 1; nl_rrsp_ready = 1;
    repeat (4) @(negedge clk);
    rst_n = 1; repeat (2) @(negedge clk);
    if (family == 0) begin
      send_write(256, 8, 1);
      wait (b_count == 1); @(negedge clk);
      $display("WRITE_CYCLES:%0d", last_cycle - first_cycle + 1);
      first_cycle = -1;
      send_read(256, 8, 2);
      wait (receive_r_count == 8); @(negedge clk);
      $display("READ_CYCLES:%0d", last_cycle - first_cycle + 1);
    end else if (family == 1) begin
      nl_wrsp_ready = 0;
      fork
        begin
          send_write(256, 32, 1);
          send_write(384, 7, 2);
          send_write(512, 16, 3);
          send_write(640, 3, 0);
        end
        begin repeat (400) @(negedge clk); nl_wrsp_ready = 1; end
      join
      wait (b_count == 4); @(negedge clk);
      if (!both_banks || !overlap) $fatal(1, "CHECK_FAIL:bank_overlap_unreached");
      send_read(256, 32, 2);
      send_write(768, 32, 1);
      wait (b_count == 5 && receive_r_count == 32); @(negedge clk);
    end else begin
      nl_rrsp_ready = 0;
      send_read(256, 32, 1);
      repeat (40) @(negedge clk);
      if (bus_count != 2 || nl_wb_stb !== 0 || nl_wb_cyc !== 1)
        $fatal(1, "CHECK_FAIL:pause_credit");
      saved_count = bus_count;
      repeat (5) @(negedge clk);
      if (bus_count != saved_count) $fatal(1, "CHECK_FAIL:gap_ack_consumed");
      nl_rrsp_ready = 1;
      wait (receive_r_count == 32); @(negedge clk);
    end
    repeat (5) @(negedge clk);
    if (bus_count != expected_count) $fatal(1, "CHECK_FAIL:word_conservation");
    $display("CHECK_PASS"); $finish;
  end
"""


def bench(component, tags):
    declarations, connections = [], []
    for port in component["ports"]:
        name, width = port["name"], port["width"]
        span = f"[{width - 1}:0] " if width > 1 else ""
        if port["endpoint"].startswith("domain:"):
            value = "clk" if port["signal"] == "clock" else "rst_n"
            declarations.append(f"wire {name} = {value};")
        else:
            kind = "reg" if port["input"] else "wire"
            declarations.append(f"{kind} {span}{name}" + (" = 0;" if port["input"] else ";"))
        connections.append(f".{name}({name})")
    observe = """
      if (probe_w_bank0_used && probe_w_bank1_used) both_banks = 1;
      if (probe_w_accept && nl_wb_cyc && nl_wb_stb) overlap = 1;
      if (probe_w_bank0_count > 32 || probe_w_bank1_count > 32)
        $fatal(1, "CHECK_FAIL:bank_capacity");
    """ if tags else ""
    body = BODY.replace("TAGS", str(int(tags))).replace("CTI", "nl_wb_cti" if tags else "3'd0")
    body = body.replace("BTE", "nl_wb_bte" if tags else "2'd0").replace("BANK_OBSERVE", observe)
    return ("module tb;\nreg clk = 0, rst_n = 0; always #5 clk = !clk;\n" +
            "\n".join(declarations) + "\n" + component["module"] + " dut(" +
            ",".join(connections) + ");\n" + body + "\nendmodule\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--family", choices=("throughput", "banks", "pressure"), default="throughput")
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    rows = []
    for tags in ((True, False) if args.family == "throughput" else (True,)):
        target = work / ("tags" if tags else "classic")
        target.mkdir()
        subprocess.run([args.fixture, "wishbone_unit", str(target), "--param", f"tags={int(tags)}"],
                       check=True, capture_output=True, text=True)
        component, = json.loads((target / "ports.json").read_text())
        subprocess.run(["iverilog", "-g2001", "-s", component["module"], "-o",
                        str(target / "syntax.vvp"), str(target / "network.v")], check=True)
        (target / "tb.sv").write_text(bench(component, tags))
        rtl = (target / "network.v").read_text()
        (target / "network.v").write_text(rtl + (Path(__file__).parent / "sv/nlv_wishbone_check.sv").read_text())
        sim = sim_engine.Simulation(target, "network.v", engine=args.sim, config=args.sim_config,
                                    success=("CHECK_PASS",))
        modes = range(4) if args.family == "throughput" else (0,)
        for mode in modes:
            family = {"throughput": 0, "banks": 1, "pressure": 2}[args.family]
            result = sim.run([f"+MODE={mode}", f"+FAMILY={family}"])
            (target / f"mode-{mode}.log").write_text(result.stdout)
            words = [tuple(map(int, values)) for values in
                     re.findall(r"WORD_CYCLE:(\d+):(\d+):(\d+):(\d+)", result.stdout)]
            counts = dict(re.findall(r"(WRITE|READ)_CYCLES:(\d+)", result.stdout))
            want = ([8, 9, 25, 16] if tags else [22, 30, 46, 30])[mode]
            passed = result.returncode == 0 and "CHECK_PASS" in result.stdout
            if family == 0:
                passed = passed and counts == {"WRITE": str(want), "READ": str(want)}
                gap = ([1, 1, 3, 2] if tags else [3, 4, 6, 4])[mode]
                for direction in (0, 1):
                    sample = [row for row in words if row[0] == direction]
                    passed = passed and len(sample) == 8
                    passed = passed and all(b[2] - a[2] == gap for a, b in zip(sample, sample[1:]))
            row = dict(tags=tags, mode=mode, family=args.family, counts=counts, word_cycles=words, passed=passed)
            rows.append(row)
            print(json.dumps(row))
    (work / "results.json").write_text(json.dumps(rows, indent=2))
    return 0 if all(row["passed"] for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
