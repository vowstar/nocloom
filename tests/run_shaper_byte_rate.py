"""The byte rate that a shaper admits for 4-byte APB transfers, measured at the target.

One 32-bit APB4 source reads one APB4 target without a pause. The bench prints the cycle of each
access that the target completes, and nothing inside the network is observed. The checks:

- without a shaper the source completes a transfer every PERIOD cycles, more than twice the
  largest rate under test, so the source always has a transfer waiting at the shaper;
- with a shaper of RATE bytes per 1024 cycles, the bytes that reach the target in WINDOW cycles
  are RATE * WINDOW / 1024 within TOLERANCE bytes. The cases with bytes_per_32768_cycles have
  rates with a part below a byte in 1024 cycles, down to one transfer in 16384 cycles.

The tolerance comes from the budget counter. It counts 1/1024 or 1/32768 bytes and drains the
rate in every cycle, so the bytes admitted in a window differ from RATE * WINDOW / 1024 only by the
change of the counter between the ends of the window. With a transfer waiting, the counter
stays between burst_bytes + 1 bytes less the drain of the PERIOD + 1 cycles the source needs to
offer its next transfer, and burst_bytes + 1 bytes plus one transfer: a span of 4 + RATE *
(PERIOD + 1) / 1024 bytes. The target sees a transfer after the shaper does: the tolerance
adds a margin of one transfer, 4 bytes, for it.

The bench measures the rate only. The admission bound and the counter of every cycle are
checked by qos_shaper_bench.py.

Negative controls, each a copy of the generated network with one constant changed: a charge of
one block of eight bytes for a transfer, a charge of 5 bytes at the two faster rates, and a rate
without its part below a byte in 1024 cycles at the two slower rates. The check must reject each.
"""
import argparse
import json
import math
import subprocess
import tempfile
from pathlib import Path

from cross_hardware_sim import simulate
from sim_engine import generated_rtl

BYTES, BURST_BYTES = 4, 18
# Bytes, initiator cycles per rate unit, and the measurement window.
CASES = ((100, 1024, 16384), (333, 1024, 16384), (109, 32768, 65536), (8, 32768, 131072))
WARM = 2048

TB = r'''`timescale 1ps/1ps
module tb;
reg clk=0, reset_n=0, test=0;
always #625 clk=~clk;
integer cyc=0;
always @(posedge clk) cyc <= cyc+1;
reg [31:0] ia=0;
reg isel=0, ien=0;
wire [31:0] ird;
wire irdy, ierr;
wire [15:0] ta;
wire tsel, ten, twrite;
wire [31:0] twd;
integer seq=0;
nocloom_byte_rate dut(.clk(clk), .reset_n(reset_n), .test(test),
 .nl_i_s0_psel(isel), .nl_i_s0_penable(ien), .nl_i_s0_pwrite(1'b0), .nl_i_s0_paddr(ia),
 .nl_i_s0_pwdata(32'b0), .nl_i_s0_pstrb(4'b1111), .nl_i_s0_pprot(3'b000),
 .nl_i_s0_pready(irdy), .nl_i_s0_prdata(ird), .nl_i_s0_pslverr(ierr),
 .nl_t_t0_pready(1'b1), .nl_t_t0_prdata(32'ha5000000 ^ {16'b0,ta}), .nl_t_t0_pslverr(1'b0),
 .nl_t_t0_paddr(ta), .nl_t_t0_pprot(), .nl_t_t0_pwrite(twrite), .nl_t_t0_psel(tsel),
 .nl_t_t0_penable(ten), .nl_t_t0_pwdata(twd), .nl_t_t0_pstrb());
initial begin
  repeat(20) @(negedge clk); reset_n=1;
  repeat(4) @(negedge clk);
  forever begin
    ia=(seq%1024)*4; isel=1; ien=0;
    @(negedge clk); ien=1;
    @(posedge clk);
    while(irdy !== 1'b1) @(posedge clk);
    if(ierr !== 1'b0 || ird !== (32'ha5000000 ^ ia)) begin
      $display("ASSERT_FAIL:BYTE_RATE.RESPONSE seq=%0d",seq); $fatal(1);
    end
    @(negedge clk); ien=0; isel=0; seq=seq+1;
  end
end
always @(posedge clk) if(cyc >= @CYCLES@) begin
  $display("CHECK_PASS byte_rate %0d",seq); $finish;
end
always @(posedge clk) if(reset_n && tsel && ten) $display("T %0d",cyc);
initial begin #@TIMEOUT@; $display("ASSERT_FAIL:BYTE_RATE.TIMEOUT"); $fatal(1); end
endmodule
'''


def config(shaper):
    source = {'name': 's0', 'clock_domain': 'main', 'protocol': 'apb4', 'data_width': 32}
    if shaper is not None:
        rate, unit = shaper
        source['qos'] = {'shaper': {f'bytes_per_{unit}_cycles': rate, 'burst_bytes': BURST_BYTES}}
    return {
        'schema_version': 10, 'name': 'byte_rate', 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [source],
        'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'apb4', 'data_width': 32,
                     'address_width': 16, 'attributes': 'amba',
                     'mappings': [{'base': '0x0', 'size': '0x1000'}]}],
        'connectivity': 'all', 'output': {'backend': 'verilog', 'language': 'verilog_2001'}}


def generate(cli, work, shaper):
    work.mkdir()
    (work / 'byte_rate.yaml').write_text(json.dumps(config(shaper)))
    q = subprocess.run([cli, 'generate', str(work / 'byte_rate.yaml'), '--out-dir', str(work / 'gen')],
                       capture_output=True, text=True, timeout=300)
    (work / 'generate.log').write_text(q.stdout + q.stderr)
    assert q.returncode == 0, q.stderr
    return generated_rtl(work / 'gen' / 'nocloom_byte_rate.v')


def accesses(sim, rtl, cycles):
    """The cycles of the target's accesses in a simulation of rtl in the new directory sim."""
    sim.mkdir()
    (sim / 'network.v').write_text(rtl)
    (sim / 'tb.sv').write_text(TB.replace('@CYCLES@', str(cycles))
                               .replace('@TIMEOUT@', str(1250 * (cycles + 4096))))
    code = simulate(sim, sim / 'compiled')
    log = (sim / 'simulation.log').read_text()
    assert code == 0 and 'CHECK_PASS byte_rate' in log, log[-3000:]
    return [int(line.split()[1]) for line in log.splitlines() if line.startswith('T ')]


def tolerance(rate, period):
    return 2 * BYTES + math.ceil(rate * (period + 1) / 1024)


def measured(cycles, window):
    return BYTES * sum(WARM <= c < WARM + window for c in cycles)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--cli', required=True)
    p.add_argument('--work', required=True)
    a = p.parse_args()
    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='run-', dir=work))
    (work / 'latest.txt').write_text(str(root) + '\n')
    free = accesses(root / 'free' / 'sim', generate(a.cli, root / 'free', None), 1024)
    gaps = {y - x for x, y in zip(free[4:], free[5:])}
    assert len(gaps) == 1, f'the source without a shaper has no fixed period: {sorted(gaps)}'
    period = gaps.pop()
    assert BYTES / period > 2 * max(rate / unit for rate, unit, _ in CASES), f'period {period} is too slow'
    results = dict(period=period, rates={})
    for rate, unit, window in CASES:
        label = f'{rate}/{unit}'
        case = root / f'rate{rate}_{unit}'
        rtl = generate(a.cli, case, (rate, unit))
        fine = unit == 32768
        if not fine:
            equivalent = generate(a.cli, case / 'equivalent', (32 * rate, 32768))
            assert rtl == equivalent, f'rate {label} differs with the finer rate field'
        bits = 33 if fine else 28
        expected, limit = rate * window / unit, tolerance(rate * 1024 / unit, period)
        got = measured(accesses(case / 'sim', rtl, WARM + window + 64), window)
        assert abs(got - expected) <= limit, f'rate {rate}: {got} bytes, expected {expected} within {limit}'
        # The constants of the counter: its whole bytes less `charge` add one transfer, and
        # `drain` is the rate.
        paid = lambda n: f"= 18'd{(1 << 18) - n};"
        charge, drain = paid(BYTES), f"= {bits}'d{rate};"
        assert rtl.count(charge) == 1 and rtl.count(drain) == 1, 'the constants of the counter'
        altered = {'block_charge': rtl.replace(charge, paid(8))}
        if not fine:
            altered['five_byte_charge'] = rtl.replace(charge, paid(5))
        else:
            altered['coarse_rate'] = rtl.replace(drain, f"= {bits}'d{rate // 32 * 32};")
        rejected = {}
        for name, text in altered.items():
            other = measured(accesses(case / name, text, WARM + window + 64), window)
            assert abs(other - expected) > limit, f'rate {rate}: {name} passed with {other} bytes'
            rejected[name] = other
        results['rates'][label] = dict(expected=expected, tolerance=limit,
                                       measured=got, rejected=rejected)
    (root / 'result.json').write_text(json.dumps(dict(status='PASS', **results), indent=1) + '\n')
    print('CHECK_PASS byte_rate', json.dumps(results))


if __name__ == '__main__':
    main()
