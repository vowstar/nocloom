"""Check the target watchdog and the last APB answer it can accept."""

import argparse
from pathlib import Path
import re
import subprocess
import tempfile

from sim_engine import Simulation


BENCH = r"""
`timescale 1ns/1ps
module tb;
localparam integer CYCLES = @CYCLES@;
@DECLARATIONS@
@MODULE@ dut(@PORTS@);
always #5 nl_clk = ~nl_clk;
integer tick, accesses = 0, accepted = 0;
reg stalled;

always @(posedge nl_clk) begin
  if (!nl_reset_n) begin
    accesses = 0;
    accepted = 0;
  end else begin
    if (nl_rreq_valid && nl_rreq_ready) accepted = accepted + 1;
    if (nl_apb_psel && nl_apb_penable && nl_apb_pready) begin
      if (nl_apb_pwrite || nl_apb_paddr != 12'h004)
        $fatal(1, "CHECK_FAIL TARGET_REQUEST");
      accesses = accesses + 1;
    end
  end
end

task automatic episode(input hang);
begin : pending_read
  @(negedge nl_clk); nl_reset_n = 0; nl_apb_pready = 0;
  repeat (4) @(negedge nl_clk);
  nl_reset_n = 1;
  repeat (2) @(negedge nl_clk);
  nl_rreq_valid = 1;
  @(posedge nl_clk);
  if (!nl_rreq_ready) $fatal(1, "CHECK_FAIL REQUEST_READY");
  @(negedge nl_clk); nl_rreq_valid = 0;
  while (!(nl_apb_psel && nl_apb_penable)) @(negedge nl_clk);

  for (tick = 1; tick <= CYCLES; tick = tick + 1) begin
    if (!hang && tick == CYCLES)
      nl_apb_pready = 1;
    @(posedge nl_clk);
    if (nl_status_timed_out || (hang && nl_rrsp_valid))
      $fatal(1, "CHECK_FAIL EARLY_EXPIRY");
    #1;
    if (nl_status_timed_out !== (hang && tick == CYCLES))
      $fatal(1, "CHECK_FAIL EXPIRY_EDGE tick=%0d", tick);
    @(negedge nl_clk);
  end

  stalled = (accesses == 0);
  if (stalled !== hang || accepted != 1)
    $fatal(1, "CHECK_FAIL ACCESS_COUNT");
  repeat (12) begin
    if (nl_rrsp_valid) begin
      if (nl_rrsp_result !== (hang ? 2'd2 : 2'd0) ||
          nl_rrsp_data !== (hang ? 32'd0 : 32'h416c3492) || !nl_rrsp_last)
        $fatal(1, "CHECK_FAIL RESPONSE");
      disable pending_read;
    end
    @(negedge nl_clk);
  end
  $fatal(1, "CHECK_FAIL RESPONSE_TIMEOUT");
end
endtask

initial begin
  nl_rrsp_ready = 0;
  nl_rreq_address = 'h1004;
  nl_rreq_size = 2;
  nl_rreq_burst = 1;
  nl_rreq_first = 1;
  nl_rreq_last = 1;
  nl_apb_prdata = 32'h416c3492;
  episode(0);
  episode(1);
  $display("CHECK_PASS TIMEOUT_WINDOW cycles=%0d", CYCLES);
  $finish;
end
initial #(25 * CYCLES + 300) $fatal(1, "CHECK_FAIL TEST_TIMEOUT");
endmodule
"""


def test(fixture, root, cycles):
    root.mkdir()
    result = subprocess.run([fixture, root, 'apb4', str(cycles), 'completion'],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    rtl = (root / 'outlet.v').read_text()
    module = re.search(r'^module (\w+)\s*\(', rtl, re.M)[1]
    declarations, connections = [], []
    count_width = None
    for line in (root / 'ports.tsv').read_text().splitlines():
        name, width, is_input = line.split('\t')
        kind = 'reg' if is_input == '1' else 'wire'
        span = f' [{int(width) - 1}:0]' if width != '1' else ''
        declarations.append(f'{kind}{span} {name}' + (' = 0;' if kind == 'reg' else ';'))
        connections.append(f'.{name}({name})')
        if name == 'nl_probe_timeout_count':
            count_width = int(width)
    assert count_width == (cycles - 1).bit_length(), count_width
    bench = BENCH.replace('@CYCLES@', str(cycles)).replace('@MODULE@', module).replace(
        '@DECLARATIONS@', '\n'.join(declarations)).replace('@PORTS@', ','.join(connections))
    forms = [('normal', rtl)]
    if cycles == 17:
        width = (cycles - 1).bit_length()
        mutant, count = re.subn(rf"(^assign nl_internal_v\d+ = ){width}'d{cycles - 1};$",
                                rf"\g<1>{width}'d{cycles - 2};", rtl, flags=re.M)
        assert count == 1, count
        forms.append(('early', mutant))
    for mode, source in forms:
        work = root / mode
        work.mkdir()
        (work / 'rtl.v').write_text(source)
        (work / 'tb.sv').write_text(bench)
        result = Simulation(work, 'rtl.v').run()
        log = (work / 'simulation.log').read_text()
        if mode == 'normal':
            assert result.returncode == 0 and 'CHECK_PASS TIMEOUT_WINDOW' in log, log
        else:
            assert result.returncode != 0 and 'CHECK_FAIL EXPIRY_EDGE' in log, log


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix='run-', dir=args.work))
    for timeout in (16, 17, 31, 1000, 1048576):
        test(args.fixture, base / str(timeout), timeout)
