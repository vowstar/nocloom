"""Bounded multi-burst sources across request and response clock crossings.

The public configuration keeps every target at capacity one. Four-beat AXI
transactions pass as one packet; 64-beat APB transactions split at the public
128-byte limit. The shared bench checks bytes, IDs, ordering, capacity and reset.
"""
import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from multi_outstanding import configuration
from multi_outstanding_bench import Cmd, Model, write_bench
from run_multi_outstanding import generate
from cross_hardware_sim import simulate
from sim_engine import generated_rtl


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', required=True)
    args = parser.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    (base / 'latest.txt').write_text(str(work) + '\n')
    mode = 'clocked_packets'
    source = work / 'input.yaml'
    source.write_text(json.dumps(configuration(mode), indent=1) + '\n')
    proc = generate(args.cli, source, work / 'generated')
    assert proc.returncode == 0, proc.stderr
    rtl = work / 'generated/nocloom_outstanding.v'
    mapping = json.loads((work / 'generated/nocloom_outstanding.map.json').read_text())
    cuts = mapping['network']['cdc']['cuts']
    assert cuts and all(any(f['name'] == 'tag' and f['width'] == 2 for f in c['fields'])
                        for c in cuts)
    assert 'outstanding' in mapping['definition']['contract']['packets']
    model = Model(mode)

    def commands(source, write, entries):
        return [model.expect(source, Cmd(write, model.addr(target, source, index * 4 if target == 'tp' else index), beats, ident))
                for index, (target, beats, ident) in enumerate(entries)]

    eps = []
    for direction, write in [('rd', False), ('wr', True)]:
        mixed = [('ta', 4, 0), ('tb', 4, 1), ('ta', 2, 2), ('tb', 3, 3),
                 ('tp', 64, 1), ('ta', 1, 2), ('tp', 64, 3), ('tb', 4, 0)]
        eps.append({'name': 'bounded_' + direction,
                    'knobs': ['ta_rlat = 12;', 'ta_blat = 12;', 'tb_rlat = 8;', 'tb_blat = 8;'],
                    'cmds': {('s0', direction): commands(0, write, mixed),
                             ('s1', direction): commands(1, write, [('tb', 4, 0), ('ta', 4, 1)])},
                    'checks': [(f's0_{"w" if write else "r"}max >= 2', 'MULTI_PASSTHROUGH')]})
    # A reset cancels both directions, followed by clean reuse of the tags.
    eps.append({'name': 'reset_pending', 'knobs': ['ta_rlat = 200;', 'ta_blat = 200;'],
                'cmds': {('s0', 'rd'): commands(0, False, [('ta', 4, 0), ('tb', 4, 1)]),
                         ('s1', 'wr'): commands(1, True, [('ta', 4, 0), ('tb', 4, 1)])},
                'reset_after': 80})
    eps.append({'name': 'after_reset', 'cmds': {
        ('s0', 'rd'): commands(0, False, [('ta', 4, 0), ('tp', 64, 1)]),
        ('s1', 'wr'): commands(1, True, [('tb', 4, 0), ('tp', 64, 1)])}})
    # The shared bench's compact APB address encoding assumes <=16 beats.
    # This test needs two public 128-byte packets: use disjoint 256-byte
    # command regions and derive each write datum from the full command.
    case = work / 'bench'
    case.mkdir(exist_ok=True)
    write_bench(case, mapping, mode, eps, True, False)
    tb = (case / 'tb.sv').read_text()
    old = "((196608 + {nl_t_tp_paddr[31:6], 6'd0}) ^\n                            ((nl_t_tp_paddr[5:2]) * 32'h01010101))"
    assert old in tb
    tb = tb.replace('nl_t_tp_pwdata === ' + old,
                    "{1'b1, nl_t_tp_pwdata} === apb_expected(196608 + nl_t_tp_paddr)")
    fn = r"""
function [32:0] apb_expected;
 input [31:0] address;
 integer source, slot, at, hits;
 reg [31:0] value;
 begin
  hits=0; value=0;
  for(source=0;source<2;source=source+1)
   for(slot=0;slot<wr_n[source];slot=slot+1) begin
    at=source*MAXC+slot;
    if(address>=wr_addr[at] && address<=wr_addr[at]+4*wr_len[at]) begin
     hits=hits+1;
     value=wr_addr[at] ^ (((address-wr_addr[at])/4)*32'h01010101);
    end
   end
  apb_expected={hits==1,value};
 end
endfunction
"""
    tb = tb.replace('endmodule', fn + '\nendmodule')
    (case / 'network.v').write_text(generated_rtl(rtl))
    (case / 'tb.sv').write_text(tb)
    rc = simulate(case, work / 'compiled', ())
    log = (case / 'simulation.log').read_text()
    assert rc == 0 and 'NOCLOOM_PASS multiple outstanding' in log and 'ASSERT_FAIL' not in log, log[-2000:]
    result = {'episodes': [ep['name'] for ep in eps], 'returncode': rc,
              'sha256_tb': hashlib.sha256(tb.encode()).hexdigest()}

    result['sha256_rtl'] = hashlib.sha256(rtl.read_bytes()).hexdigest()
    (work / 'results.json').write_text(json.dumps(result, indent=1) + '\n')
    print('NOCLOOM_PASS bounded multi-burst CDC episodes=' + str(len(eps)))


if __name__ == '__main__':
    main()
