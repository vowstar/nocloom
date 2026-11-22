"""Check the port lists of the timeout formal benches without running a prover.

Every bench of run_timeout_formal.py is built from the fixture's real outlet
ports. Its module header declares the clock first and each other port it
drives once. Tools other than Yosys reject a port declared twice.
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_timeout_formal as bench  # noqa: E402

KINDS = ('apb4', 'apb3', 'axi4_lite', 'ahb_lite', 'axi4', 'axi4x2')


def outlet_ports(fixture, work, kind, answer, cycles=bench.CYCLES):
    directory = Path(tempfile.mkdtemp(prefix=f'{kind}-{answer}-', dir=work))
    subprocess.run([fixture, str(directory), kind, str(cycles), answer,
                    *bench.PROBES.get(kind, ())], check=True, timeout=120)
    return bench.ports(directory)


def expect_header(text, expected, label):
    bench.check_header(text)
    declared = bench.header_ports(text)
    assert declared == expected, (label, declared, expected)
    print(f'PASS {label}: {len(declared)} ports, clock once')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--work', required=True)
    args = parser.parse_args()
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=work))
    for kind in KINDS:
        tied = () if kind in ('axi4', 'axi4x2') else ('nl_rrsp_ready', 'nl_wrsp_ready')
        for answer in bench.ANSWERS:
            port_list = outlet_ports(args.fixture, work, kind, answer)
            assert ('nl_clk', 1, True) in port_list, (kind, 'the outlet has no clock input')
            expected = ['nl_clk'] + [name for name, _, incoming in port_list
                                     if incoming and name not in tied and name != 'nl_clk']
            top = bench.harness(kind, port_list, answer, bench.CYCLES)
            expect_header(top, expected, f'{kind} {answer}')
            if kind in ('axi4', 'axi4x2'):
                reach = bench.axi_reach_harness(kind, port_list, bench.CYCLES, top)
                expect_header(reach, ['nl_clk'], f'{kind} {answer} reach')
    shared = bench.shared_harness(outlet_ports(args.fixture, work, 'axi4x4', 'completion', 16), 16)
    expect_header(shared, ['nl_clk'], 'axi4x4 shared')
    # The check itself rejects a repeated clock.
    repeated = 'module formal_top(input wire nl_clk, input wire [0:0] nl_clk);\nendmodule\n'
    try:
        bench.check_header(repeated)
    except AssertionError as error:
        assert 'nl_clk' in str(error), error
    else:
        raise AssertionError('a repeated clock passed the header check')
    print('PASS timeout formal bench headers')


if __name__ == '__main__':
    main()
