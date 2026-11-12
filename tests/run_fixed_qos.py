"""Fixed priority eligibility with raw FIFO arrivals and whole-packet holding, a rate-limited
APB source (qos_shaper_bench), a regulated APB source (qos_scheduler_bench) and AXI4 sources with
an AxQOS map, a limiter or a regulator (qos_axi_bench) in a network."""
import argparse
import json
import random
import re
import subprocess
import tempfile
from pathlib import Path
from cross_hardware_sim import simulate
from treadle_model import Arbiter
from sim_engine import generated_rtl
import qos_shaper_bench
import qos_scheduler_bench
import ahb_fixed_levels
import qos_axi_bench
import qos_pressure_bench
from qos_pressure_stimulus import stimulus


class Prioritized(Arbiter):
    levels = (1, 1, 7)

    def choose(self, valid):
        highest = max((self.levels[i] for i, v in enumerate(valid) if v), default=0)
        return super().choose([v and self.levels[i] == highest for i, v in enumerate(valid)])


def vectors(policy):
    model = Prioritized(policy, 3, rank=[1, 2, 0] if policy == 'fixed_priority' else None)
    rows = []
    coverage = dict(filtered=0, held_over_higher=0, ties=0, fifo_age=0)

    def add(valid, last, ready, label):
        expected = model.step(valid, last, ready)
        live = [i for i, v in enumerate(valid) if v]
        if live and not expected['held']:
            highest = max(model.levels[i] for i in live)
            coverage['filtered'] += any(model.levels[i] < highest for i in live)
            coverage['ties'] += sum(model.levels[i] == highest for i in live) > 1
        if expected['held'] and live:
            coverage['held_over_higher'] += any(model.levels[i] > model.levels[expected['winner']] for i in live)
        rows.append(dict(valid=valid[:], last=last[:], ready=ready, expected=expected, label=label))
        return expected

    # A high packet holds the output while lower equal levels arrive in a
    # known age order contrary to the reset tie order. Both wait through tail.
    for v, l, r in [([0,0,1],[1,1,0],1), ([1,0,1],[1,1,0],0),
                    ([1,1,1],[1,1,0],0), ([1,1,1],[1,1,1],1)]:
        add(v,l,r,'ARRIVAL')
    out = add([1,1,0],[1,1,1],1,'FIFO_AGE' if policy == 'fifo' else 'TIE')
    if policy == 'fifo':
        assert out['winner'] == 0
        coverage['fifo_age'] += 1
    left = 1 - out['winner']
    add([int(i == left) for i in range(3)],[1,1,1],1,'DRAIN')
    # Late high-level traffic cannot interrupt the low-level packet, including
    # a legal inter-fragment gap and receiver backpressure.
    add([1,0,0],[0,1,1],1,'LOW_HEAD')
    add([0,0,1],[0,1,1],1,'HELD_GAP')
    add([1,0,1],[0,1,1],0,'HELD_STALL')
    add([1,0,1],[1,1,1],1,'HELD_TAIL')
    add([0,0,1],[1,1,1],1,'HIGH_AFTER_TAIL')
    rng = random.Random(9187)
    remaining = [0,0,0]
    presented = [False]*3
    for cycle in range(420):
        for i in range(3):
            if remaining[i] == 0 and rng.randrange(4) == 0:
                remaining[i] = rng.randrange(1,6)
            if remaining[i] and not presented[i] and rng.randrange(3):
                presented[i] = True
        valid = list(map(int,presented));last=[int(n == 1) for n in remaining]
        out = add(valid,last,int(rng.randrange(4)!=0),'POLICY')
        if out['transfer']:
            i=out['winner'];remaining[i]-=1;presented[i]=False
    assert all(coverage[k] > 0 for k in ('filtered','held_over_higher','ties'))
    return rows, coverage


def bench(rows):
    decl = ['`timescale 1ns/1ps','module tb;',
            'reg clk=0,reset_n=0,ready=0;reg[2:0] valid=0,last=0;',
            'wire[2:0] taken;wire sink_valid,held,transfer,release_;wire[1:0] winner,payload;',
            'always #5 clk=~clk;']
    ports=['.nl_clk(clk)','.nl_reset_n(reset_n)','.nl_ready(ready)',
           '.nl_sink_valid(sink_valid)','.nl_sink_payload(payload)',
           '.nl_winner(winner)','.nl_held(held)','.nl_transfer(transfer)','.nl_release(release_)']
    for i,level in enumerate((1,1,7)):
        ports += [f'.nl_valid{i}(valid[{i}])',f'.nl_last{i}(last[{i}])',
                  f'.nl_taken{i}(taken[{i}])',f".nl_priority{i}(3'd{level})"]
    decl += ['nocloom_arbiter dut('+','.join(ports)+');','initial begin',
             'repeat(3) @(negedge clk);reset_n=1;']
    for n,row in enumerate(rows):
        v=sum(x<<i for i,x in enumerate(row['valid']));l=sum(x<<i for i,x in enumerate(row['last']));o=row['expected'];w=o['winner']
        check=f"held==1'b{int(o['held'])} && sink_valid==1'b{int(o['sink_valid'])} && transfer==1'b{int(o['transfer'])} && release_==1'b{int(o['release'])}"
        if w is not None:
            check+=f" && winner==2'd{w} && payload==2'd{w} && taken==3'd{(1<<w) if row['ready'] else 0}"
        decl += [f"@(negedge clk);valid=3'd{v};last=3'd{l};ready=1'b{row['ready']};",
                 f'#1;if(({check}) !== 1\'b1) begin $display("ASSERT_FAIL:FIXED_QOS.{row["label"]} cycle={n}");$fatal(1);end',
                 '@(posedge clk);']
    decl += ['@(negedge clk);reset_n=0;valid=0;ready=0;',
             '#1;if(held!==0) begin $display("ASSERT_FAIL:FIXED_QOS.RESET");$fatal(1);end',
             '$display("CHECK_PASS fixed_qos");$finish;end',
             'initial begin #20000;$fatal(1,"TIMEOUT");end','endmodule']
    return '\n'.join(decl)+'\n'


def limiter(cli, root):
    """The rate-limited APB network: checked with gated and ungated clocks, which must agree,
    and two altered limiters, which the checks must reject."""
    case = root / 'limiter'; case.mkdir()
    qos_shaper_bench.write_config(case / 'qos_shaper.yaml')
    q = subprocess.run([cli, 'generate', str(case / 'qos_shaper.yaml'), '--out-dir', str(case / 'gen')],
                       capture_output=True, text=True, timeout=300)
    (case / 'generate.log').write_text(q.stdout + q.stderr); assert q.returncode == 0, q.stderr
    rtl = generated_rtl(case / 'gen' / 'nocloom_qos_shaper.v')
    runs, rejected = {}, {}
    for name, text, test in [('gated', rtl, 0), ('ungated', rtl, 1),
                             *((m, t, 1) for m, t in qos_shaper_bench.mutants(rtl).items())]:
        sim = case / name; sim.mkdir()
        (sim / 'network.v').write_text(text)
        (sim / 'tb.sv').write_text(qos_shaper_bench.bench(case / 'gen', test))
        code = simulate(sim, sim / 'compiled'); log = (sim / 'simulation.log').read_text()
        assert code == 0, log[-3000:]
        try:
            runs[name] = qos_shaper_bench.check(log)
        except AssertionError as e:
            assert name not in ('gated', 'ungated'), (name, str(e))
            rejected[name] = str(e)
    assert set(rejected) == set(qos_shaper_bench.mutants(rtl)), rejected
    assert runs['gated'] == runs['ungated'], 'clock gating changed the entry cycles'
    # A rate between the whole units of 4 per 1024 and a burst past 16 KiB, which the budget holds.
    wide = case / 'wide'; wide.mkdir()
    qos_shaper_bench.set_shaper(130, 17000)
    qos_shaper_bench.write_config(wide / 'qos_shaper.yaml')
    q = subprocess.run([cli, 'generate', str(wide / 'qos_shaper.yaml'), '--out-dir', str(wide / 'gen')],
                       capture_output=True, text=True, timeout=300)
    assert q.returncode == 0, q.stderr
    (wide / 'network.v').write_text(generated_rtl(wide / 'gen' / 'nocloom_qos_shaper.v'))
    (wide / 'tb.sv').write_text(qos_shaper_bench.bench(wide / 'gen', 0))
    assert simulate(wide, wide / 'compiled') == 0
    qos_shaper_bench.check((wide / 'simulation.log').read_text())
    qos_shaper_bench.set_shaper(260, 64)
    return dict(entries=[len(e) for e in runs['ungated']], rejected=rejected)


def regulator(cli, root):
    """The regulated APB network: checked with gated and ungated clocks, which must agree, and
    altered regulators, which the checks must reject."""
    case = root / 'regulator'; case.mkdir()
    qos_scheduler_bench.write_config(case / 'qos_scheduler.yaml')
    q = subprocess.run([cli, 'generate', str(case / 'qos_scheduler.yaml'), '--out-dir', str(case / 'gen')],
                       capture_output=True, text=True, timeout=300)
    (case / 'generate.log').write_text(q.stdout + q.stderr); assert q.returncode == 0, q.stderr
    rtl = generated_rtl(case / 'gen' / 'nocloom_qos_scheduler.v')
    altered = qos_scheduler_bench.mutants(rtl, case / 'gen')
    runs, rejected = {}, {}
    for name, text, test in [('gated', rtl, 0), ('ungated', rtl, 1),
                             *((m, t, 1) for m, t in altered.items())]:
        sim = case / name; sim.mkdir()
        (sim / 'network.v').write_text(text)
        (sim / 'tb.sv').write_text(qos_scheduler_bench.bench(case / 'gen', test))
        code = simulate(sim, sim / 'compiled'); log = (sim / 'simulation.log').read_text()
        assert code == 0, log[-3000:]
        try:
            runs[name] = qos_scheduler_bench.check(log)
        except AssertionError as e:
            assert name not in ('gated', 'ungated'), (name, str(e))
            rejected[name] = str(e)
    assert set(rejected) == set(altered), rejected
    assert runs['gated'] == runs['ungated'], 'clock gating changed the entry cycles'
    # Another threshold moves the level change and nothing else in the checks.
    other = case / 'threshold'; other.mkdir()
    qos_scheduler_bench.set_threshold(75)
    qos_scheduler_bench.write_config(other / 'qos_scheduler.yaml')
    q = subprocess.run([cli, 'generate', str(other / 'qos_scheduler.yaml'), '--out-dir', str(other / 'gen')],
                       capture_output=True, text=True, timeout=300)
    assert q.returncode == 0, q.stderr
    (other / 'network.v').write_text(generated_rtl(other / 'gen' / 'nocloom_qos_scheduler.v'))
    (other / 'tb.sv').write_text(qos_scheduler_bench.bench(other / 'gen', 0))
    assert simulate(other, other / 'compiled') == 0
    qos_scheduler_bench.check((other / 'simulation.log').read_text())
    qos_scheduler_bench.set_threshold(50)
    # A rate between the whole units of 4 per 1024 and a burst past 16 KiB, which the budget holds.
    wide = case / 'wide'; wide.mkdir()
    qos_shaper_bench.set_shaper(130, 17000)
    qos_shaper_bench.write_config(wide / 'qos_shaper.yaml')
    q = subprocess.run([cli, 'generate', str(wide / 'qos_shaper.yaml'), '--out-dir', str(wide / 'gen')],
                       capture_output=True, text=True, timeout=300)
    assert q.returncode == 0, q.stderr
    (wide / 'network.v').write_text(generated_rtl(wide / 'gen' / 'nocloom_qos_shaper.v'))
    (wide / 'tb.sv').write_text(qos_shaper_bench.bench(wide / 'gen', 0))
    assert simulate(wide, wide / 'compiled') == 0
    qos_shaper_bench.check((wide / 'simulation.log').read_text())
    qos_shaper_bench.set_shaper(260, 64)
    return dict(entries=[len(e) for e in runs['ungated'][:2]], raises=runs['ungated'][2],
                rejected=rejected)


def axi(cli, root):
    """AXI4 sources with an AxQOS map and a limiter, then with a regulator: each checked with
    gated and ungated clocks, which must agree, and altered configurations the checks reject."""
    case = root / 'axi'; case.mkdir()
    runs, rejected = {}, {}
    variants = [('limited', False, {}, 0), ('limited_ungated', False, {}, 1),
                ('regulated', True, {}, 0), ('regulated_ungated', True, {}, 1),
                ('regulated_four', True, dict(outstanding=4), 0),
                ('regulated_four_ungated', True, dict(outstanding=4), 1)]
    variants += [(m, reg, kw, 1) for m, (reg, kw) in qos_axi_bench.mutants().items()]
    for name, regulated, kw, test in variants:
        sim = case / name; sim.mkdir()
        qos_axi_bench.write_config(sim / 'qos_axi.yaml', regulated, **kw)
        q = subprocess.run([cli, 'generate', str(sim / 'qos_axi.yaml'), '--out-dir', str(sim / 'gen')],
                           capture_output=True, text=True, timeout=300)
        (sim / 'generate.log').write_text(q.stdout + q.stderr); assert q.returncode == 0, q.stderr
        (sim / 'network.v').write_text(generated_rtl(sim / 'gen' / 'nocloom_qos_axi.v'))
        (sim / 'tb.sv').write_text(qos_axi_bench.bench(sim / 'gen', test))
        code = simulate(sim, sim / 'compiled'); log = (sim / 'simulation.log').read_text()
        assert code == 0, log[-3000:]
        try:
            runs[name] = qos_axi_bench.check(log, regulated, kw.get('outstanding', 1) > 1)
        except AssertionError as e:
            assert name in qos_axi_bench.mutants(), (name, str(e))
            rejected[name] = str(e)
    assert set(rejected) == set(qos_axi_bench.mutants()), rejected
    for kind in ('limited', 'regulated', 'regulated_four'):
        assert runs[kind] == runs[kind + '_ungated'], f'clock gating changed the {kind} rows'
    return dict(cycles={k: len(v) for k, v in runs.items()}, rejected=rejected)


def pressure(cli, fixture, root):
    """Finite traffic checks pressure and grants without fixed event cycles."""
    out = {}
    for kind in ('chain', 'cdc', 'wide'):
        case = root / kind
        case.mkdir()
        source = Path(__file__).with_name(f'qos_pressure_{"chain" if kind == "wide" else kind}.yaml')
        config = json.loads(source.read_text())
        if kind == 'wide':
            config['initiators'][0]['data_width'] = 64
            config['transport'] = {}
        (case / 'config.yaml').write_text(json.dumps(config, indent=1) + '\n')
        gen = case / 'gen'
        run = subprocess.run([cli, 'generate', str(case / 'config.yaml'), '--out-dir', str(gen)],
                             capture_output=True, text=True, timeout=300)
        (case / 'generate.log').write_text(run.stdout + run.stderr)
        assert run.returncode == 0, run.stderr
        observed = case / 'observed'
        run = subprocess.run([fixture, 'pressure_network', str(observed), '--probes',
                              '--list-bindings', '--param', 'config=' + str(case / 'config.yaml')],
                             capture_output=True, text=True, timeout=300)
        assert run.returncode == 0, run.stderr
        rtl_path = next(gen.glob('nocloom_qos_*_network.v'))
        assert (observed / 'network.v').read_text() == rtl_path.read_text(), 'pressure fixture differs'
        rtl = generated_rtl(rtl_path)
        probes = json.loads((observed / 'probes.json').read_text())
        bindings = json.loads((observed / 'bindings.json').read_text())
        runs = []
        for seed in (13, 29):
            sim = case / f'seed-{seed}'
            sim.mkdir()
            tb, groups = qos_pressure_bench.bench(kind, probes, seed)
            (sim / 'network.v').write_text(rtl)
            (sim / 'tb.sv').write_text(tb)
            assert simulate(sim, sim / 'compiled') == 0
            runs.append(qos_pressure_bench.check((sim / 'simulation.log').read_text(), groups, kind == 'wide'))
        fault = case / 'no_stored_pressure'
        fault.mkdir()
        (fault / 'tb.sv').write_text(tb)
        (fault / 'network.v').write_text(qos_pressure_bench.mutations(rtl, bindings, kind))
        assert simulate(fault, fault / 'compiled') == 0
        try:
            qos_pressure_bench.check((fault / 'simulation.log').read_text(), groups, kind == 'wide')
        except AssertionError as error:
            rejected = str(error)
        else:
            raise AssertionError(f'{kind}: missing stored pressure survived')
        protocol = case / 'unstable_address'
        protocol.mkdir()
        (protocol / 'tb.sv').write_text(tb)
        (protocol / 'network.v').write_text(qos_pressure_bench.unstable_address(rtl))
        code = simulate(protocol, protocol / 'compiled')
        assert code != 0 and 'AXI_AR_STABLE' in (protocol / 'simulation.log').read_text(), kind
        restored = case / 'restored'
        restored.mkdir()
        (restored / 'tb.sv').write_text(tb)
        (restored / 'network.v').write_text(rtl)
        assert simulate(restored, restored / 'compiled') == 0
        qos_pressure_bench.check((restored / 'simulation.log').read_text(), groups, kind == 'wide')
        out[kind] = dict(runs=runs, rejected=rejected, protocol_rejected='AXI_AR_STABLE')
    return out


def pressure_observation(gen, resource, label):
    """Check an unheld grant against each actual input pressure and valid."""
    mapping = json.loads(next(gen.glob('*.map.json')).read_text())
    anchors = mapping['network']['formal']['scheduling'][resource]
    value = lambda key: 'dut.nl_internal_v' + anchors[key].removeprefix('value/')
    count = len([key for key in anchors if re.fullmatch(r'valid[0-9]+', key)])
    winner = value('winner')
    lines = [f"  if({value('acquire')} && !{value('held')}) begin",
             f'    $display("{label} %0d %0d 0", cyc, {winner});',
             f'    if({winner} >= {count}) $fatal(1,"ASSERT_FAIL:PRESSURE_LEVEL.INDEX");']
    for i in range(count):
        higher = ' | '.join(f"({value('valid'+str(j))} && "
                            f"({value('level'+str(i))} < {value('level'+str(j))}))"
                            for j in range(count))
        lines.append(f'    if({winner}=={i} && (!{value("valid"+str(i))} || ({higher}))) '
                     '$fatal(1,"ASSERT_FAIL:PRESSURE_LEVEL.GRANT");')
    live = ' + '.join(f"{{31'b0,{value('valid'+str(i))}}}" for i in range(count))
    lines += [f'    if(({live})>1) $display("REACHED:PRESSURE_COMPETE_{label}");', '  end']
    return '\n'.join(lines)


def ledger_pressure(cli, root):
    """Pressure through a switch chain and crossings, checked at actual grant decisions."""
    root.mkdir()
    out = {}
    for kind in ('chain', 'cdc'):
        case = root / kind
        case.mkdir()
        source = Path(__file__).with_name(f'qos_pressure_{kind}.yaml')
        (case / 'config.yaml').write_text(source.read_text())
        gen = case / 'gen'
        run = subprocess.run([cli, 'generate', str(case / 'config.yaml'), '--out-dir', str(gen)],
                             capture_output=True, text=True, timeout=300)
        (case / 'generate.log').write_text(run.stdout + run.stderr)
        assert run.returncode == 0, run.stderr
        rtl = next(gen.glob('nocloom_qos_*_network.v'))
        (case / 'network.v').write_text(generated_rtl(rtl))
        tb = Path(__file__).with_name(f'qos_pressure_{kind}_ledger_tb.sv').read_text()
        tb = tb.replace('@STIMULUS@', stimulus())
        tb = tb.replace('@GD_OBSERVE@', pressure_observation(
            gen, 'graph/req/req_node1>endpoint/t0', 'GD'))
        tb = tb.replace('@GA_OBSERVE@', pressure_observation(
            gen, 'graph/req/req_node0>node/req_node1', 'GA'))
        (case / 'tb.sv').write_text(tb)
        code = simulate(case, case / 'compiled')
        log = (case / 'simulation.log').read_text()
        assert code == 0 and 'CHECK_PASS pressure' in log, log[-3000:]
        decisions = [line for line in log.splitlines() if line.startswith('GD ')]
        assert decisions and 'REACHED:PRESSURE_COMPETE_GD' in log, (kind, 'no contention')
        anchors = json.loads(next(gen.glob('*.map.json')).read_text())
        winner = anchors['network']['formal']['scheduling'][
            'graph/req/req_node1>endpoint/t0']['winner']
        wire = 'nl_internal_v' + winner.removeprefix('value/')
        text = (case / 'network.v').read_text()
        changed, hits = re.subn(rf'^assign {wire} = .*;$',
                               f"assign {wire} = 1'b0;", text, flags=re.M)
        assert hits == 1, ('grant mutation did not bind', kind, hits)
        negative = case / 'wrong_grant'
        negative.mkdir()
        (negative / 'network.v').write_text(changed)
        (negative / 'tb.sv').write_text(tb)
        negative_rc = simulate(negative, negative / 'compiled')
        negative_log = (negative / 'simulation.log').read_text()
        assert negative_rc != 0 and 'ASSERT_FAIL:PRESSURE_LEVEL.' in negative_log, negative_log[-3000:]
        out[kind] = dict(decisions=len(decisions), wrong_grant='CAUGHT', seed=261105)
    return out


def main():
    p=argparse.ArgumentParser();p.add_argument('--fixture',required=True);p.add_argument('--cli',required=True);p.add_argument('--work',required=True);p.add_argument('--network-fixture',required=True);a=p.parse_args()
    work=Path(a.work);work.mkdir(parents=True,exist_ok=True);root=Path(tempfile.mkdtemp(prefix='run-',dir=work));(work/'latest.txt').write_text(str(root)+'\n')
    results=[dict(ahb_fixed=ahb_fixed_levels.run(a.cli, root / 'ahb-fixed')),dict(ledger_pressure=ledger_pressure(a.cli, root / 'ledger-pressure')),dict(limiter=limiter(a.cli, root)),dict(regulator=regulator(a.cli, root)),
             dict(axi=axi(a.cli, root)),dict(pressure=pressure(a.cli, a.network_fixture, root))]
    for policy in (*Arbiter.POLICIES, "level_round_robin"):
        case=root/policy;case.mkdir();args=[a.fixture,str(case),'output',policy,'3','priority']
        if policy=='fixed_priority':args+=['1,2,0']
        q=subprocess.run(args,capture_output=True,text=True,timeout=120);(case/'generate.log').write_text(q.stdout+q.stderr);assert q.returncode==0
        rows,coverage=vectors(policy);(case/'vectors.json').write_text(json.dumps(rows,indent=1)+'\n');(case/'tb.sv').write_text(bench(rows))
        code=simulate(case,case/'compiled');log=(case/'simulation.log').read_text();assert code==0 and 'CHECK_PASS fixed_qos' in log,log[-3000:]
        results.append(dict(policy=policy,cycles=len(rows),coverage=coverage))
    (root/'result.json').write_text(json.dumps(dict(status='PASS',results=results),indent=1)+'\n')

if __name__=='__main__':main()
