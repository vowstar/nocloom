"""APB transactions at mapped local register addresses."""

import json
import re
from pathlib import Path

from register_window import BANK_BYTES, CONTROLS, bits, named, word
import sim_engine


def enabled(index):
    """The rw fields that the bench sets in a bank: the first by parity, the second always."""
    return CONTROLS if index % 2 else CONTROLS[1:]


def simulate(folder, banks, *, header=False):
    folder = Path(folder)
    data = json.loads((folder / "nocloom_test_bus.map.json").read_text())
    record = data["definition"]["log_registers"]["csr"]
    assert record["bank_bytes"] == BANK_BYTES
    records = record.get("banks", [record])
    assert [b["base"] for b in records] == banks
    checks = []

    def read(address, expected=0, error=False):
        checks.append(f"access(0, 32'h{address:x}, 0, {int(error)}, 32'h{expected:x});")

    def write(address, value, error=False):
        checks.append(f"access(1, 32'h{address:x}, 32'h{value:x}, {int(error)}, 0);")

    for i, (bank, base) in enumerate(zip(records, banks)):
        fields = bank["registers"]
        held = named(fields, "record_held", "rw1c")["offset"]
        # The address oracle uses the configured global base and local base.
        address = 0x100000 + base - 0x1000
        if header:
            macros = {}
            for path in folder.glob("*.h"):
                macros.update((k, int(v, 16)) for k, v in re.findall(
                    r"#define (\w+) \((0x[0-9a-f]+)U(?:LL)?\)", path.read_text()))
            # A register is at the window's base plus the bank's offset plus the field's offset.
            prefix = "NOCLOOM_TEST_BUS_LOGGER_CONTROL_CSR_WINDOW_0"
            layout = "NOCLOOM_TEST_BUS_ERROR_LOG_"
            address = macros[prefix + "_BASE"] + macros[
                prefix + "_" + bank["bank"].upper() + "_OFFSET"]
            for f in fields:
                assert macros[layout + f["name"].upper() + "_OFFSET"] == f["offset"], f
            assert macros[layout + "BANK_BYTES"] == BANK_BYTES
            held = macros[layout + "RECORD_HELD_OFFSET"]
            # The first word reads what the header states for it.
            assert macros[prefix + "_" + bank["bank"].upper() + "_ID_VALUE"] == word(fields, 0)
        first = min(f["offset"] for f in fields)
        last = max(f["offset"] for f in fields)
        for offset in (first, last, BANK_BYTES - 4):
            assert not bits(fields, offset, ("rw", "rw1c"))
            read(address + offset, word(fields, offset))
            write(address + offset, 0xffffffff)
            read(address + offset, word(fields, offset))
        # Each rw field is written at its bits beside the ones already set, and read back.
        controls = [named(fields, name, "rw")["offset"] for name in CONTROLS]
        for k, offset in enumerate(controls):
            names = [name for name in CONTROLS[:k + 1] if name in enabled(i)]
            write(address + offset, bits(fields, offset, ("rw",), names))
            read(address + offset, word(fields, offset, names))
        # No record is held.
        read(address + held, word(fields, held, enabled(i)))
        # Omitting local-base translation must not select any bank.
        wrong = 0x100000 + base
        for offset in sorted({first, last, *controls, held, BANK_BYTES - 4}):
            read(wrong + offset, error=True)
            write(wrong + offset, 0xffffffff, error=True)
    for i, (bank, base) in enumerate(zip(records, banks)):
        address = 0x100000 + base - 0x1000
        fields = bank["registers"]
        for offset in sorted({named(fields, name, "rw")["offset"] for name in CONTROLS}):
            read(address + offset, word(fields, offset, enabled(i)))
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
