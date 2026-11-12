"""APB transactions at mapped local register addresses."""

import json
import re
from pathlib import Path

import sim_engine


def simulate(folder, banks, *, header=False):
    folder = Path(folder)
    data = json.loads((folder / "nocloom_test_bus.map.json").read_text())
    record = data["definition"]["log_registers"]["csr"]
    records = record.get("banks", [record])
    assert [b["base"] for b in records] == banks
    checks = []
    for i, (bank, base) in enumerate(zip(records, banks)):
        # The address oracle uses the configured global base and local base.
        address = 0x100000 + base - 0x1000
        if header:
            macros = {}
            for path in folder.glob("*.h"):
                macros.update((k, int(v, 16)) for k, v in re.findall(
                    r"#define (\w+) \((0x[0-9a-f]+)U(?:LL)?\)", path.read_text()))
            prefix = "NOCLOOM_TEST_BUS_LOGGER_CONTROL_CSR_WINDOW_0"
            address = macros[prefix + "_BASE"] + macros[
                prefix + "_" + bank["bank"].upper() + "_RECORD_VALID_OFFSET"] - 0x10
        fields = bank["registers"]
        first = min(f["offset"] for f in fields)
        last = max(f["offset"] for f in fields)
        identity = sum(f.get("value", 0) << f["lsb"] for f in fields if f["offset"] == first)
        for offset, expected in ((first, identity), (last, 0), (0xfc, 0)):
            checks += [f"access(0, 32'h{address + offset:x}, 0, 0, 32'h{expected:x});",
                       f"access(1, 32'h{address + offset:x}, 32'hffffffff, 0, 0);",
                       f"access(0, 32'h{address + offset:x}, 0, 0, 32'h{expected:x});"]
        checks += [f"access(1, 32'h{address + 8:x}, {i % 2}, 0, 0);",
                   f"access(0, 32'h{address + 8:x}, 0, 0, {i % 2});",
                   f"access(1, 32'h{address + 12:x}, 1, 0, 0);",
                   f"access(0, 32'h{address + 12:x}, 0, 0, 1);",
                   f"access(0, 32'h{address + 16:x}, 0, 0, 0);"]
        # Omitting local-base translation must not select any bank.
        wrong = 0x100000 + base
        for offset in (first, last, 8, 0x10):
            checks += [f"access(0, 32'h{wrong + offset:x}, 0, 1, 0);",
                       f"access(1, 32'h{wrong + offset:x}, 32'hffffffff, 1, 0);"]
    for i, base in enumerate(banks):
        address = 0x100000 + base - 0x1000
        checks += [f"access(0, 32'h{address + 8:x}, 0, 0, {i % 2});",
                   f"access(0, 32'h{address + 12:x}, 0, 0, 1);"]
    bench = r"""`timescale 1ns/1ps
module tb;
reg clk = 0, reset_n = 0;
always #5 clk = ~clk;
reg sel = 0, en = 0, wr = 0;
reg [31:0] addr = 0, wd = 0;
reg [3:0] strb = 0;
wire ready, err;
wire [31:0] rd;
integer checks = 0;
nocloom_test_bus dut(.clk_main(clk), .reset_n(reset_n), .test(1'b0),
  .nl_i_logger_control_psel(sel), .nl_i_logger_control_penable(en),
  .nl_i_logger_control_pwrite(wr), .nl_i_logger_control_paddr(addr),
  .nl_i_logger_control_pwdata(wd), .nl_i_logger_control_pstrb(strb),
  .nl_i_logger_control_pprot(3'b0), .nl_i_logger_control_pready(ready),
  .nl_i_logger_control_prdata(rd), .nl_i_logger_control_pslverr(err),
  .nl_t_ram_pready(1'b1), .nl_t_ram_prdata(32'h0), .nl_t_ram_pslverr(1'b0));
task access(input w, input [31:0] a, input [31:0] d,
            input bad, input [31:0] expected);
integer cycles;
begin
  @(negedge clk);
  sel = 1; en = 0; wr = w; addr = a; wd = d; strb = w ? 4'hf : 0;
  @(negedge clk); en = 1;
  @(posedge clk); cycles = 0;
  while (!ready && cycles < 100) begin
    cycles = cycles + 1; @(posedge clk);
  end
  if (cycles == 100 || err !== bad || (!w && rd !== expected)) begin
    $display("CHECK_FAIL mapped_register addr=%h write=%b data=%h expected=%h error=%b wanted=%b",
             a, w, rd, expected, err, bad);
    $fatal(1);
  end
  checks = checks + 1;
  @(negedge clk); sel = 0; en = 0; strb = 0;
end
endtask
initial begin
  repeat(5) @(negedge clk); reset_n = 1;
  repeat(3) @(negedge clk);
  @CHECKS@
  $display("CHECK_PASS checks=%0d", checks);
  $finish;
end
initial begin
  #2000000; $display("CHECK_FAIL timeout"); $fatal(1);
end
endmodule
""".replace("@CHECKS@", "\n  ".join(checks))
    work = folder / "transactions"
    work.mkdir()
    (work / "tb.sv").write_text(bench)
    (work / "dut.v").write_text(sim_engine.generated_rtl(folder / "nocloom_test_bus.v"))
    sim = sim_engine.Simulation(work, "dut.v", compile_timeout=120, run_timeout=30)
    result = sim.run()
    assert result.returncode == 0 and "CHECK_PASS" in result.stdout, result.stdout
