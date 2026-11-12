"""APB bank selection, local translation and holes in register windows."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import sim_engine


BENCH = r"""`timescale 1ns/1ps
module tb;
reg clk = 0, reset_n = 0;
always #5 clk = ~clk;
reg sel = 0, en = 0, wr = 0;
reg [31:0] addr = 0, wd = 0;
reg [3:0] strb = 0;
wire ready, err, ram_sel;
wire [31:0] rd;
integer checks = 0;
nocloom_register_window dut(.clk(clk), .reset_n(reset_n), .test(1'b0),
  .nl_i_cfg_psel(sel), .nl_i_cfg_penable(en), .nl_i_cfg_pwrite(wr),
  .nl_i_cfg_paddr(addr), .nl_i_cfg_pwdata(wd), .nl_i_cfg_pstrb(strb),
  .nl_i_cfg_pprot(3'b0), .nl_i_cfg_pready(ready), .nl_i_cfg_prdata(rd),
  .nl_i_cfg_pslverr(err), .nl_t_ram_psel(ram_sel),
  .nl_t_ram_pready(1'b1), .nl_t_ram_prdata(32'hdeadbeef), .nl_t_ram_pslverr(1'b0));
always @(posedge clk) if (reset_n && ram_sel) begin
  $display("CHECK_FAIL external_target"); $fatal(1);
end
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
    $display("CHECK_FAIL register_window addr=%h write=%b data=%h expected=%h error=%b wanted=%b",
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
"""


def transactions(folder, base, size, local, banks):
    data = json.loads((folder / "nocloom_register_window.map.json").read_text())
    records = data["definition"]["log_registers"]["csr"]["banks"]
    assert [b["base"] for b in records] == banks
    checks = []

    def access(write, address, value=0, error=False, expected=0):
        checks.append(f"access({int(write)}, 32'h{address:x}, 32'h{value:x}, "
                      f"{int(error)}, 32'h{expected:x});")

    addresses = [base + bank - local for bank in banks]
    for address in addresses:
        access(False, address + 8)
        access(False, address + 12)
    # Move a single enabled bit across all banks and verify every bank after each write.
    for selected, address in enumerate(addresses):
        access(True, address + 8, value=1)
        access(True, address + 12, value=1)
        for i, other in enumerate(addresses):
            access(False, other + 8, expected=int(i == selected))
            access(False, other + 12, expected=int(i == selected))
        access(True, address + 8)
        access(True, address + 12)
    for record, address in zip(records, addresses):
        fields = record["registers"]
        first = min(f["offset"] for f in fields)
        last = max(f["offset"] for f in fields)
        identity = sum(f.get("value", 0) << f["lsb"] for f in fields if f["offset"] == first)
        # First and last registers are read-only without a captured record.
        for offset, expected in ((first, identity), (last, 0), (0xfc, 0)):
            access(False, address + offset, expected=expected)
            access(True, address + offset, value=0xffffffff)
            access(False, address + offset, expected=expected)
    # The bank footprint is 256 bytes. Check both edges of each remaining local interval.
    holes = [a for a in range(local, local + size, 0x100) if a not in banks]
    for hole in holes:
        for byte in (0, 0xfc):
            address = base + hole + byte - local
            access(False, address, error=True)
            access(True, address, value=0xffffffff, error=True)
    for address in (base - 4, base + size, base + size + 0x1000):
        access(False, address, error=True)
        access(True, address, value=0xffffffff, error=True)
    for address in addresses:
        access(False, address + 8)
        access(False, address + 12)
    return BENCH.replace("@CHECKS@", "\n  ".join(checks)), len(checks)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--work", required=True)
    args = parser.parse_args()
    Path(args.work).mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work))
    print(f"Work directory: {work}", flush=True)
    results = []
    for size in (0x4000,):
        for shift, local in ((0, 0), (size, 0), (size, 0x1000)):
            base = size * 256 + shift
            banks = [local, 0x1800 if local else 0x1000, 0x2000, size - 0x100]
            folder = work / f"size-{size:x}-base-{base:x}-local-{local:x}"
            command = [args.fixture, str(folder), *map(hex, (base, size, local, *banks))]
            subprocess.run(command, check=True, timeout=30)
            (folder / "input.json").write_text(json.dumps(dict(
                base=base, size=size, local=local, banks=banks, command=command), indent=2) + "\n")
            bench, count = transactions(folder, base, size, local, banks)
            (folder / "tb.sv").write_text(bench)
            (folder / "dut.v").write_text(sim_engine.generated_rtl(folder / "nocloom_register_window.v"))
            sim = sim_engine.Simulation(folder, "dut.v", compile_timeout=120, run_timeout=30)
            result = sim.run()
            assert result.returncode == 0, result.stdout
            assert f"CHECK_PASS checks={count}" in result.stdout, result.stdout
            results.append(dict(base=base, size=size, local=local, banks=banks, checks=count))
    (work / "results.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
