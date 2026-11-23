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
reg [133:0] requests [0:@COUNT@-1];
nocloom_register_window dut(.clk(clk), .reset_n(reset_n), .test(1'b0),
  .nl_i_cfg_psel(sel), .nl_i_cfg_penable(en), .nl_i_cfg_pwrite(wr),
  .nl_i_cfg_paddr(addr), .nl_i_cfg_pwdata(wd), .nl_i_cfg_pstrb(strb),
  .nl_i_cfg_pprot(3'b0), .nl_i_cfg_pready(ready), .nl_i_cfg_prdata(rd),
  .nl_i_cfg_pslverr(err), .nl_t_ram_psel(ram_sel),
  .nl_t_ram_pready(1'b1), .nl_t_ram_prdata(32'hdeadbeef), .nl_t_ram_pslverr(1'b0));
always @(posedge clk) if (reset_n && ram_sel) begin
  $display("CHECK_FAIL external_target"); $fatal(1);
end
task access(input [133:0] request);
reg w, bad;
reg [31:0] a, d, mask, expected;
reg [3:0] s;
integer cycles;
begin
  {w, a, d, s, bad, mask, expected} = request;
  @(negedge clk);
  sel = 1; en = 0; wr = w; addr = a; wd = d; strb = w ? s : 0;
  @(negedge clk); en = 1;
  @(posedge clk); cycles = 0;
  while (!ready && cycles < 100) begin
    cycles = cycles + 1; @(posedge clk);
  end
  if (cycles == 100 || err !== bad || (!w && (rd & mask) !== expected)) begin
    $display("CHECK_FAIL register_window addr=%h write=%b data=%h expected=%h error=%b wanted=%b",
             a, w, rd, expected, err, bad);
    $fatal(1);
  end
  checks = checks + 1;
  @(negedge clk); sel = 0; en = 0; strb = 0;
end
endtask
initial begin
  @REQUESTS@
  repeat(5) @(negedge clk); reset_n = 1;
  repeat(3) @(negedge clk);
  for (integer i = 0; i < @COUNT@; i = i + 1)
    access(requests[i]);
  $display("CHECK_PASS checks=%0d", checks);
  $finish;
end
initial begin
  #2000000; $display("CHECK_FAIL timeout"); $fatal(1);
end
endmodule
"""


BANK_BYTES = 64
CONTROLS = ("alarm_enable", "wait_when_full")


def named(fields, name, kind):
    found = [f for f in fields if f["name"] == name]
    assert len(found) == 1 and found[0]["access"] == kind, (name, found)
    return found[0]


def bits(fields, offset, kinds, names=None):
    """The bits of a word that fields of these access kinds (and names) cover."""
    return sum(((1 << f["width"]) - 1) << f["lsb"] for f in fields
               if f["offset"] == offset and f["access"] in kinds
               and (names is None or f["name"] in names))


def word(fields, offset, controls=()):
    """What a word reads without a held record: its fixed values and the set rw fields."""
    fixed = sum(f["value"] << f["lsb"] for f in fields
                if f["offset"] == offset and "value" in f)
    return fixed | bits(fields, offset, ("rw",), controls)


def byte_strobes(mask):
    return sum(1 << k for k in range(4) if mask >> 8 * k & 0xff)


def transactions(folder, base, size, local, banks):
    data = json.loads((folder / "nocloom_register_window.map.json").read_text())
    window = data["definition"]["log_registers"]["csr"]
    assert window["bank_bytes"] == BANK_BYTES
    records = window["banks"]
    assert [b["base"] for b in records] == banks
    checks = []

    def access(write, address, value=0, error=False, expected=0, strobes=0xf, mask=0xffffffff):
        checks.append(f"requests[{len(checks)}] = {{1'b{int(write)}, 32'h{address:x}, "
                      f"32'h{value:x}, 4'h{strobes:x}, 1'b{int(error)}, "
                      f"32'h{mask:x}, 32'h{expected:x}}};")

    addresses = [base + bank - local for bank in banks]
    registers = [record["registers"] for record in records]
    # The words of the two rw fields, and the bytes of each that a write of them strobes.
    places = []
    for fields in registers:
        offsets = sorted({named(fields, name, "rw")["offset"] for name in CONTROLS})
        for offset in offsets:
            written = byte_strobes(bits(fields, offset, ("rw",)))
            # A write of an rw bit never reaches the byte of an rw1c bit.
            assert not written & byte_strobes(bits(fields, offset, ("rw1c",)))
        assert named(fields, "record_held", "rw1c")["offset"] in offsets
        places.append(offsets)
    for address, fields, offsets in zip(addresses, registers, places):
        for offset in offsets:
            access(False, address + offset, expected=word(fields, offset))
    # Set each rw bit, then both, in one bank at a time and verify every bank after each
    # write.
    for selected, address in enumerate(addresses):
        for enabled in ((CONTROLS[0],), (CONTROLS[1],), CONTROLS):
            for offset in places[selected]:
                access(True, address + offset,
                       value=bits(registers[selected], offset, ("rw",), enabled))
            for i, other in enumerate(addresses):
                for offset in places[i]:
                    access(False, other + offset,
                           expected=word(registers[i], offset, enabled if i == selected else ()))
        for offset in places[selected]:
            access(True, address + offset)
    for fields, address, offsets in zip(registers, addresses, places):
        first = min(f["offset"] for f in fields)
        last = max(f["offset"] for f in fields)
        # The first and the last register and the bank's last word are read-only without a held
        # record.
        for offset in (first, last, BANK_BYTES - 4):
            assert not bits(fields, offset, ("rw", "rw1c"))
            expected = word(fields, offset)
            access(False, address + offset, expected=expected)
            access(True, address + offset, value=0xffffffff)
            access(False, address + offset, expected=expected)
        # Only the rw bits of a word take a write, and only under the strobes of their bytes.
        for offset in offsets:
            control = bits(fields, offset, ("rw",))
            mine = byte_strobes(control)
            clear, enabled = word(fields, offset), word(fields, offset, CONTROLS)
            assert control and mine != 0xf and clear != enabled
            for value, strobes, expected in ((0xffffffff, 0xf, enabled), (0, 0xf & ~mine, enabled),
                                             (0, mine, clear), (0xffffffff, 0xf & ~mine, clear),
                                             (0xffffffff, mine, enabled), (0, 0xf, clear)):
                access(True, address + offset, value=value, strobes=strobes)
                access(False, address + offset, expected=expected)
    # A bank is 64 bytes. Check both edges of every other block of the window.
    holes = [a for a in range(local, local + size, BANK_BYTES) if a not in banks]
    for hole in holes:
        for byte in (0, BANK_BYTES - 4):
            address = base + hole + byte - local
            access(False, address, error=True)
            access(True, address, value=0xffffffff, error=True)
    # An error of the window itself reaches no logger.
    for address, fields, offsets in zip(addresses, registers, places):
        for offset in offsets:
            access(False, address + offset, expected=word(fields, offset))
    for address in (base - 4, base + size, base + size + 0x1000):
        access(False, address, error=True)
        access(True, address, value=0xffffffff, error=True)
    # An access outside every window is a decode error of a source that reaches the watched
    # target. The chain's first logger holds the first of them, a read, and lost the later ones.
    # No record reaches the loggers after it.
    assert window["error_classes"] == dict(decode=1, target=2)
    fields, address = registers[0], addresses[0]
    held, overflow = (named(fields, name, "rw1c") for name in ("record_held", "record_overflow"))
    kind, write = (named(fields, name, "ro") for name in ("record_class", "record_write"))
    state = held["offset"]
    assert {overflow["offset"], kind["offset"], write["offset"]} == {state}
    known = sum(((1 << f["width"]) - 1) << f["lsb"] for f in (held, overflow, kind, write))
    known |= bits(fields, state, ("rw",))
    both = bits(fields, state, ("rw1c",))
    mine = byte_strobes(both)
    assert both == 1 << held["lsb"] | 1 << overflow["lsb"] and mine != 0xf

    def status(is_held, is_overflow):
        access(False, address + state, mask=known, expected=(
            is_held << held["lsb"] | is_overflow << overflow["lsb"] | 1 << kind["lsb"]))

    status(1, 1)
    # The record states the bytes of the access: one APB word.
    size_field = named(fields, "record_bytes", "ro")
    access(False, address + size_field["offset"], expected=4 << size_field["lsb"],
           mask=((1 << size_field["width"]) - 1) << size_field["lsb"])
    for other, more in zip(addresses[1:], registers[1:]):
        for offset in sorted({f["offset"] for f in more
                              if f["access"] in ("ro", "rw1c") and "value" not in f}):
            access(False, other + offset, expected=word(more, offset))
    # A status bit clears on a written one under the strobe of its byte, each bit by itself, and
    # the record's fields stay.
    access(True, address + state)
    status(1, 1)
    access(True, address + state, value=both, strobes=0xf & ~mine)
    status(1, 1)
    access(True, address + state, value=1 << held["lsb"], strobes=mine)
    status(0, 1)
    access(True, address + state, value=1 << overflow["lsb"], strobes=mine)
    status(0, 0)
    for address, fields, offsets in zip(addresses, registers, places):
        for offset in offsets:
            access(False, address + offset, mask=bits(fields, offset, ("rw",)))
    return (BENCH.replace("@COUNT@", str(len(checks)))
            .replace("@REQUESTS@", "\n  ".join(checks))), len(checks)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--work", required=True)
    args = parser.parse_args()
    Path(args.work).mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work))
    print(f"Work directory: {work}", flush=True)
    results = []
    for size in (0x3000, 0x5000):
        for shift, local in ((0, 0), (size, 0), (size, 0x1000)):
            base = size * 256 + shift
            banks = [local, 0x1800 if local else 0x1000, 0x2000, size - BANK_BYTES]
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
