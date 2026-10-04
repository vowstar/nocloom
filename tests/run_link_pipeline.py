"""Registered link transactions and a tagged, width-converting public graph."""
import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path
from cross_hardware_sim import simulate
from axi4_width_stress_bench import configuration, bench
from sim_engine import generated_rtl

# MODE 0 both stages (two slots), 1 forward only, 2 backward only, 3 half-rate (one slot).
TB = r'''
`timescale 1ns/1ps
module tb;
localparam MODE=@MODE@, CAP=MODE==0?2:1;
reg clk=0,reset_n=0,in_valid=0,out_ready=0,in_last=0,in_head=0;
reg[256:0] in_data=0;
wire in_ready,out_valid,out_last,out_head,full,spare;wire[256:0] out_data;
nocloom_link_pipeline dut(.nl_clk(clk),.nl_reset_n(reset_n),.nl_in_valid(in_valid),
 .nl_out_ready(out_ready),.nl_in_data(in_data),.nl_in_last(in_last),.nl_in_head(in_head),.nl_in_ready(in_ready),
 .nl_out_valid(out_valid),.nl_out_data(out_data),.nl_out_last(out_last),.nl_out_head(out_head),.nl_full(full),.nl_spare(spare));always #5 clk=~clk;
reg[256:0] data[0:2047];reg lasts[0:2047];
integer head=0,tail=0,count=0,pushes=0,pops=0,both=0,stalls=0,peak=0;
integer streak=0,maxstreak=0,cycle=0,resets=0;
integer sustained=0,sustained_pops=0,sustained_pushes=0;
reg accepted=0;integer distinct_full=0;
reg held=0;reg[256:0] held_data;reg held_last;
task check;input ok;input[8*40-1:0] label;begin
 if(ok!==1'b1) begin $display("ASSERT_FAIL:LINK_PIPELINE.%0s cycle=%0d count=%0d",label,cycle,count);$fatal(1);end
end endtask
always @(posedge clk or negedge reset_n) begin
 if(!reset_n) begin head=0;tail=0;count=0;held=0;streak=0;accepted=0;end
 else begin
  cycle=cycle+1;accepted=in_valid&&in_ready;
  if(count==CAP && in_valid && in_data!==data[tail-1]) distinct_full=distinct_full+1;
  // Forward stages register valid; backward stages register ready.
  check(in_ready==(MODE==1 ? count==0||out_ready : MODE==3 ? count==0 : count<CAP),"READY_OCCUPANCY");
  check(out_valid==(MODE==2 ? count>0||in_valid : count>0),"VALID_OCCUPANCY");
  if(held) check(out_valid && out_data==held_data && out_last==held_last,"HELD_FIELDS");
  held=out_valid&&!out_ready;held_data=out_data;held_last=out_last;
  if(in_valid&&in_ready&&out_valid&&out_ready) both=both+1;
  if(sustained && in_valid && in_ready) sustained_pushes=sustained_pushes+1;
  if(sustained && out_valid && out_ready) sustained_pops=sustained_pops+1;
  // A backward stage passes an accepted word through in the same cycle.
  if(in_valid && in_ready) begin data[tail]=in_data;lasts[tail]=in_last;tail=tail+1;count=count+1;pushes=pushes+1;end
  if(out_valid && out_ready) begin
   check(count>0 && out_data===data[head] && out_last===lasts[head],"FIFO_FIELDS");
   head=head+1;count=count-1;pops=pops+1;streak=streak+1;
  end else streak=0;
  if(streak>maxstreak) maxstreak=streak;
  if(out_valid&&!out_ready) stalls=stalls+1;
  if(count>peak) peak=count;
  check(count<=CAP,"CAPACITY");
 end
end
task drive;input v;input r;input integer n;begin
 @(negedge clk);
 if(!in_valid || accepted) begin in_valid=v;in_data={n[0],{8{32'h87650000+n}}};in_last=(n%7==0);end
 out_ready=r;
end endtask
integer i;reg[31:0] rng=32'h76543210;
initial begin
 repeat(3) @(negedge clk);reset_n=1;
 // Fill both slots, hold, then release; inputs change behind a full stage.
 drive(1,0,1);drive(1,0,2);for(i=0;i<8;i=i+1) drive(1,0,3);
 drive(0,1,0);drive(0,1,0);drive(0,1,0);
 // Full-rate modes sustain one word per cycle; half-rate sustains one every two cycles.
 sustained=1;for(i=0;i<64;i=i+1) drive(1,1,100+i);sustained=0;
 drive(0,1,0);drive(0,1,0);
 // Independent bubbles/backpressure, including simultaneous push and pop.
 for(i=0;i<320;i=i+1) begin rng=rng*1664525+1013904223;drive(rng[9],rng[17],200+i);end
 for(i=0;i<4;i=i+1) drive(0,1,0);
 // Cancel two live slots; no cancelled beat may escape after reset.
 drive(1,0,901);drive(1,0,902);drive(0,0,0);
 @(negedge clk);check(MODE==1 || MODE==3 ? full&&!spare : MODE==2 ? spare&&!full : full&&spare,"RESET_REACHED_FULL");reset_n=0;resets=resets+1;
 #1;check(!out_valid&&!in_ready,"RESET_GATES");
 repeat(3) @(negedge clk);in_valid=0;out_ready=1;reset_n=1;
 for(i=0;i<4;i=i+1) drive(0,1,0);
 for(i=0;i<16;i=i+1) drive(1,1,1000+i);
 for(i=0;i<4;i=i+1) drive(0,1,0);
 @(negedge clk);check(count==0 && !out_valid,"DRAIN");
 $display("REACH_VALUES peak=%0d both=%0d maxstreak=%0d stalls=%0d resets=%0d distinct_full=%0d sustained=%0d/%0d",peak,both,maxstreak,stalls,resets,distinct_full,sustained_pushes,sustained_pops);
 check(peak==CAP && (MODE==3 ? both==0 && maxstreak==1 && sustained_pushes==32 && sustained_pops==32 : both>64 && maxstreak>=64) && stalls>=8 && resets==1 && distinct_full>=8,"REACHABILITY");
 $display("CHECK_PASS link_pipeline pushes=%0d pops=%0d peak=%0d simultaneous=%0d stalls=%0d streak=%0d resets=%0d distinct_full=%0d",pushes,pops,peak,both,stalls,maxstreak,resets,distinct_full);$finish;
end
initial begin #20000;$display("ASSERT_FAIL:LINK_PIPELINE.TIMEOUT");$fatal(1);end
endmodule
'''

BYPASS_TB = r'''
`timescale 1ns/1ps
module tb;
localparam MODE=@MODE@,BYPASS=@BYPASS@,DIRECT=BYPASS&&MODE!=0,CAP=BYPASS?(MODE==0?1:0):(MODE==0?2:1);
reg clk=0,reset_n=0,in_valid=0,out_ready=0,in_last=0,in_head=0;
reg[256:0] in_data=0;wire in_ready,out_valid,out_last,out_head,full,spare;wire[256:0] out_data;
nocloom_link_pipeline dut(.nl_clk(clk),.nl_reset_n(reset_n),.nl_in_valid(in_valid),
 .nl_out_ready(out_ready),.nl_in_data(in_data),.nl_in_last(in_last),.nl_in_head(in_head),.nl_in_ready(in_ready),
 .nl_out_valid(out_valid),.nl_out_data(out_data),.nl_out_last(out_last),.nl_out_head(out_head),.nl_full(full),.nl_spare(spare),
 .nl_bypass(BYPASS[0]));always #5 clk=~clk;
reg[256:0] data[0:31];reg lasts[0:31];reg heads[0:31];
integer head=0,tail=0,count=0,pushes=0,pops=0,cycle=0,word_index=0,peak=0;
integer direct_same=0,spare_seen=0;
task check;input ok;input[8*40-1:0] label;begin
 if(ok!==1'b1) begin $display("ASSERT_FAIL:LINK_BYPASS.%0s cycle=%0d count=%0d",label,cycle,count);$fatal(1);end
end endtask
always @(negedge clk) begin
 if(cycle<3) begin reset_n<=0;in_valid<=0;out_ready<=0;end
 else begin
  reset_n<=1;out_ready<=!((cycle>=11)&&(cycle<=13));
  if(!(in_valid&&!in_ready)) begin
   if(word_index<8) begin in_valid<=1;in_data<={8{32'h87650000+word_index}};in_last<=word_index==7;in_head<=word_index==0;end
   else if(word_index==8) begin in_valid<=1;in_data<=257'd20;in_last<=1;in_head<=1;end
   else if(word_index==9 && cycle<19) begin in_valid<=0;in_data<=0;in_last<=0;in_head<=0;end
   else if(word_index==9) begin in_valid<=1;in_data<=257'd30;in_last<=1;in_head<=1;end
   else begin in_valid<=0;in_data<=0;in_last<=0;in_head<=0;end
  end
 end
end
always @(posedge clk) begin
 cycle<=cycle+1;
 if(reset_n) begin
  if(DIRECT) begin
   check(in_ready==out_ready,"DIRECT_READY");check(out_valid==in_valid,"DIRECT_VALID");
   if(in_valid) check(out_data==in_data && out_last==in_last && out_head==in_head,"DIRECT_FIELDS");
   if(in_valid&&in_ready) begin check(out_valid&&out_ready,"DIRECT_SAME_CYCLE");direct_same=direct_same+1;end
  end
  if(in_valid&&in_ready) begin data[tail]=in_data;lasts[tail]=in_last;heads[tail]=in_head;tail=tail+1;count=count+1;pushes=pushes+1;word_index<=word_index+1;end
  if(out_valid&&out_ready) begin
   check(count>0 && out_data===data[head] && out_last===lasts[head] && out_head===heads[head],"FIFO_FIELDS");
   head=head+1;count=count-1;pops=pops+1;
  end
  if(spare) spare_seen=spare_seen+1;
  if(count>peak) peak=count;
  check(count<=CAP,"CAPACITY");
  if(cycle==30) begin
   check(pushes==10 && pops==10 && count==0,"COUNTS");
   if(DIRECT) check(direct_same==10,"DIRECT_COVERAGE");
   if(BYPASS&&MODE==0) check(spare_seen>0,"BACKWARD_RETAINED");
   $display("CHECK_PASS link_bypass mode=%0d bypass=%0d pushes=%0d pops=%0d direct=%0d spare=%0d",MODE,BYPASS,pushes,pops,direct_same,spare_seen);$finish;
  end
 end
end
initial begin #2000;$display("ASSERT_FAIL:LINK_BYPASS.TIMEOUT");$fatal(1);end
endmodule
'''

UNGATED_TB = r'''
`timescale 1ns/1ps
module tb;
reg clk=0,reset_n=0,in_valid=0,out_ready=0,in_last=0,in_head=0,bypass=0;
reg[256:0] in_data=0;wire in_ready,out_valid,out_last,out_head,full,spare,region_enable;wire[256:0] out_data;
nocloom_link_pipeline dut(.nl_clk(clk),.nl_reset_n(reset_n),.nl_in_valid(in_valid),
 .nl_out_ready(out_ready),.nl_in_data(in_data),.nl_in_last(in_last),.nl_in_head(in_head),.nl_in_ready(in_ready),
 .nl_out_valid(out_valid),.nl_out_data(out_data),.nl_out_last(out_last),.nl_out_head(out_head),.nl_full(full),.nl_spare(spare),
 .nl_bypass(bypass),.nl_region_enable(region_enable));always #5 clk=~clk;
integer accepted_closed=0,woken=0,pops=0;
task check;input ok;input[8*40-1:0] label;begin
 if(ok!==1'b1) begin $display("ASSERT_FAIL:LINK_UNGATED.%0s",label);$fatal(1);end
end endtask
initial begin
 repeat(3) @(negedge clk);reset_n=1;
 repeat(10) @(negedge clk);check(!region_enable,"REGION_CLOSED");
 in_valid=1;in_data={8{32'h12345678}};in_last=1;in_head=1;#1;
 check(in_ready && !region_enable,"ACCEPT_WHILE_CLOSED");accepted_closed=1;
 @(negedge clk);check(full && out_valid && region_enable,"OUTPUT_WAKE");woken=1;
 check(out_data==in_data && out_last && out_head,"HELD_FIELDS");in_valid=0;out_ready=1;
 @(negedge clk);pops=pops+1;check(!out_valid,"DRAINED");
 repeat(6) @(negedge clk);check(!region_enable,"RECLOSED");
 check(accepted_closed==1 && woken==1 && pops==1,"COVERAGE");
 $display("CHECK_PASS link_ungated accepted_closed=%0d woken=%0d pops=%0d",accepted_closed,woken,pops);$finish;
end
initial begin #2000;$display("ASSERT_FAIL:LINK_UNGATED.TIMEOUT");$fatal(1);end
endmodule
'''


def execute(cli, config, case):
    case.mkdir(parents=True, exist_ok=True)
    path = case / 'input.yaml'
    path.write_text(json.dumps(config, indent=1) + '\n')
    result = subprocess.run([cli, 'generate', str(path), '--out-dir', str(case / 'gen')],
                            capture_output=True, text=True, timeout=120)
    (case / 'generate.log').write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stderr
    return json.loads((case / 'gen/nocloom_stress.map.json').read_text())


def pipeline_configuration(mode):
    cfg = configuration(mode, False)
    sources = [s['name'] for s in cfg['initiators']]
    targets = [s['name'] for s in cfg['targets']]
    def graph(reverse):
        start, end = ('c', 'a') if reverse else ('a', 'c')
        return dict(nodes=[dict(name=n, transport_word_bytes=w) for n,w in [('a',16),('b',4),('c',8)]],
                    links=[],
                    entries=[dict(endpoint=n,node=start) for n in (targets if reverse else sources)],
                    exits=[dict(endpoint=n,node=end) for n in (sources if reverse else targets)])
    cfg['topology'] = {name: graph(rev) for name,rev in [('request',False),('response',True)]}
    for name,edges in [('request',[('a','b'),('b','c')]),('response',[('c','b'),('b','a')])]:
        cfg['topology'][name]['links'] = [{'from':a,'to':b,'pipeline':dict(forward=True,backward=True)} for a,b in edges]
    return cfg

def network(cli, work):
    rows = []
    for mode in ('mo_wide', 'bounded32'):
        cfg = pipeline_configuration(mode)
        if mode == 'bounded32':
            # One link of each kind: forward, backward, both and forward.
            for link, pipe in zip(cfg['topology']['request']['links'] + cfg['topology']['response']['links'],
                                  [dict(forward=True), dict(backward=True), dict(forward=True, backward=True),
                                   dict(forward=True)]):
                link['pipeline'] = pipe
        sources = [source["name"] for source in cfg["initiators"]]
        case = work / mode
        mapping = execute(cli, cfg, case)
        (case / 'network.v').write_text(generated_rtl(case / 'gen/nocloom_stress.v'))
        stimulus = bench(mode, mapping, False)
        if mode == 'mo_wide':
            # Hold replies while the source contexts fill, so the depth cover
            # does not depend on random response latency.
            for source in sources:
                for channel, rate in [('b', 'p_br'), ('r', 'p_rr')]:
                    old = f'nl_i_{source}_{channel}ready = rnd(100) < {rate};'
                    assert stimulus.count(old) == 1
                    stimulus = stimulus.replace(
                        old, f'nl_i_{source}_{channel}ready = (segcycles >= 256) && rnd(100) < {rate};')
        (case / 'tb.sv').write_text(stimulus)
        # A miss answers early, so the wide mode needs more segments to fill its sources.
        rc = simulate(case, case / 'compiled', ('+seed=17', f"+segs={12 if mode == 'mo_wide' else 6}"))
        log = (case / 'simulation.log').read_text()
        passed = rc == 0 and re.search(r'STRESS_PASS .*unexpected_err=0(?:\s|$)',log) is not None
        if mode == 'mo_wide':
            match = re.search(r'MULTI_BURSTS overtakes=(\d+)( s\d+_w=\d+ s\d+_r=\d+)+',log)
            depth = dict(re.findall(r'(s\d+_[wr])=(\d+)',match[0])) if match else {}
            # Bursts in flight through the pipes; overtakes are checked over seeds by
            # axi4_width_stress_mo_wide (a miss now travels to the default target).
            passed = passed and match is not None and all(int(depth.get(f'{s}_{d}',0))>=3 for s in sources for d in 'wr')
        rows.append(dict(mode=mode,status='PASS' if passed else 'FAIL',returncode=rc,
                         rtl_sha256=hashlib.sha256((case/'network.v').read_bytes()).hexdigest(),
                         tb_sha256=hashlib.sha256((case/'tb.sv').read_bytes()).hexdigest()))
        (work/'results.json').write_text(json.dumps(rows,indent=1)+'\n')
        assert passed, log[-3000:]
    # The target leaves error-page memory unchanged, so corruption there must
    # fail the same memory check as corruption in a successful write region.
    case = work / 'memory_error_page'
    case.mkdir()
    clean = work / 'bounded32'
    (case / 'network.v').write_bytes((clean / 'network.v').read_bytes())
    text = (clean / 'tb.sv').read_text()
    assert text.count('resync(0);') == 1
    (case / 'tb.sv').write_text(text.replace('resync(0);',
        'mem[32\'hF000] = ~mem[32\'hF000]; $display("REACHED_ERROR_PAGE_CORRUPTION"); resync(0);'))
    rc = simulate(case, case / 'compiled', ('+seed=17', '+segs=6'))
    log = (case / 'simulation.log').read_text()
    caught = (rc != 0 and 'REACHED_ERROR_PAGE_CORRUPTION' in log
              and re.findall(r'ASSERT_FAIL:STRESS\.(\w+)', log) == ['MEM_MODEL'])
    rows.append(dict(mode='memory_error_page', status='CAUGHT' if caught else 'FAIL',
                     returncode=rc,
                     rtl_sha256=hashlib.sha256((case/'network.v').read_bytes()).hexdigest(),
                     tb_sha256=hashlib.sha256((case/'tb.sv').read_bytes()).hexdigest()))
    (work/'results.json').write_text(json.dumps(rows,indent=1)+'\n')
    assert caught, log[-3000:]


def configuration_checks(cli, work):
    import copy
    cfg = pipeline_configuration('bounded32')
    artifacts = []
    for name, explicit in [('absent', False), ('false', True)]:
        d = copy.deepcopy(cfg)
        for graph in d['topology'].values():
            for edge in graph['links']:
                if explicit: edge['pipeline'] = edge['sender_pipeline'] = dict(forward=False, backward=False)
                else: del edge['pipeline']
            for attachment in graph['entries'] + graph['exits']:
                if explicit: attachment['pipeline'] = dict(forward=False, backward=False)
        off = dict(forward=False, backward=False)
        # The forward stages of a source's R and B and a target's AW, W and AR are the
        # endpoints' own registers: their explicit default is forward on. A source's AW, W and AR
        # keep a backward pipe by default.
        port = dict(forward=True, backward=False)
        for endpoint in d['initiators'] if explicit else []:
            endpoint['pipes'] = dict(request=dict(forward=False, backward=True), packets=dict(off),
                                     issue=False, response=dict(port))
        for endpoint in d['targets'] if explicit else []:
            endpoint['pipes'] = dict(arrival=dict(off), dispatch=False, response=dict(off), request=dict(port))
        execute(cli, d, work / name)
        artifacts.append({p.name:p.read_bytes() for p in (work/name/'gen').iterdir()})
    assert artifacts[0] == artifacts[1], 'false must preserve absent artifacts'
    rows = []
    # The same checks on a link's receiver and sender pipe, on an attachment's pipe and on each
    # endpoint pipe.
    places = [('', lambda d: d['topology']['request']['links'][0], 'pipeline'),
              ('sender_', lambda d: d['topology']['request']['links'][0], 'sender_pipeline'),
              ('entry_', lambda d: d['topology']['request']['entries'][0], 'pipeline')]
    for side, keys in (('initiators', ('request', 'packets')), ('targets', ('arrival', 'response', 'request'))):
        for key in keys:
            places.append((f'{side}_{key}_', lambda d, side=side: d[side][0].setdefault('pipes', {}), key))
    for side, key in (('initiators', 'issue'), ('targets', 'dispatch')):
        for name, value, code in [('integer', 1, 'NCL_SCHEMA_TYPE'), ('object', dict(forward=True), 'NCL_SCHEMA_TYPE')]:
            d = copy.deepcopy(cfg); d[side][0]['pipes'] = {key: value}
            path = work/f'{side}_{key}_{name}.yaml'; path.write_text(json.dumps(d))
            r = subprocess.run([cli,'validate',str(path)],capture_output=True,text=True,timeout=120)
            assert r.returncode != 0 and code in r.stderr, (side, key, name, r.stderr)
            rows.append(dict(case=f'{side}_{key}_{name}', diagnostic=code))
    # An outlet timer must see its target's handshakes: no AXI channel pipes behind it.
    d = copy.deepcopy(cfg); d['targets'][0].update(timeout=dict(cycles=1024), pipes=dict(request=dict(backward=True)))
    path = work/'timed_request.yaml'; path.write_text(json.dumps(d))
    r = subprocess.run([cli,'validate',str(path)],capture_output=True,text=True,timeout=120)
    assert r.returncode != 0 and 'NCL_ENDPOINT_PIPE' in r.stderr, r.stderr
    rows.append(dict(case='timed_request', diagnostic='NCL_ENDPOINT_PIPE'))
    # The forward stage that is an endpoint register stays on, and a source's AW, W and AR keep
    # one stage.
    for side, key, value in (('initiators', 'response', dict(forward=False)),
                             ('targets', 'request', dict(forward=False)),
                             ('initiators', 'request', dict(backward=False))):
        d = copy.deepcopy(cfg); d[side][0]['pipes'] = {key: value}
        path = work/f'{side}_{key}_unregistered.yaml'; path.write_text(json.dumps(d))
        r = subprocess.run([cli,'validate',str(path)],capture_output=True,text=True,timeout=120)
        assert r.returncode != 0 and 'NCL_ENDPOINT_PIPE' in r.stderr, (side, key, r.stderr)
        rows.append(dict(case=f'{side}_{key}_unregistered', diagnostic='NCL_ENDPOINT_PIPE'))
    for prefix, item, key in places:
        for name, value, code in [('integer',1,'NCL_SCHEMA_TYPE'),('string','true','NCL_SCHEMA_TYPE'),
                                  ('null',None,'NCL_SCHEMA_TYPE'),('boolean',True,'NCL_SCHEMA_TYPE'),
                                  ('forward_integer',dict(forward=1),'NCL_SCHEMA_TYPE'),
                                  ('half_speed',dict(forward=True,backward=True,half_speed=True),'NCL_SCHEMA_UNKNOWN_FIELD')]:
            d=copy.deepcopy(cfg);item(d)[key]=value
            path=work/(prefix+name+'.yaml');path.write_text(json.dumps(d))
            r=subprocess.run([cli,'validate',str(path)],capture_output=True,text=True,timeout=120)
            (work/(prefix+name+'.log')).write_text(r.stdout+r.stderr)
            assert r.returncode!=0 and code in r.stderr,(prefix+name,r.stderr)
            rows.append(dict(case=prefix+name,diagnostic=code))
    d = copy.deepcopy(cfg)
    for graph in d['topology'].values():
        graph['links'][0]['pipeline'] = dict(forward=True,backward=True,half_rate=True,ungated=True)
        graph['links'][0]['sender_pipeline'] = dict(forward=True,bypass_port='pipe_bypass')
        graph['entries'][0]['pipeline'] = dict(ungated=True)
    execute(cli,d,work/'extended_valid')
    for name,value in [('half_forward',dict(forward=True,half_rate=True)),
                       ('half_backward',dict(backward=True,half_rate=True)),
                       ('half_none',dict(half_rate=True)),
                       ('bypass_backward',dict(backward=True,bypass_port='pipe_bypass')),
                       ('bypass_none',dict(bypass_port='pipe_bypass')),
                       ('bypass_type',dict(forward=True,bypass_port=1)),
                       ('ungated_type',dict(forward=True,ungated='true'))]:
        d=copy.deepcopy(cfg);d['topology']['request']['links'][0]['pipeline']=value
        path=work/f'{name}.yaml';path.write_text(json.dumps(d))
        r=subprocess.run([cli,'validate',str(path)],capture_output=True,text=True,timeout=120)
        code='NCL_SCHEMA_TYPE' if name.endswith('_type') else 'NCL_AXI4_TOPOLOGY'
        assert r.returncode!=0 and code in r.stderr,(name,r.stderr)
        rows.append(dict(case=name,diagnostic=code))
    for side,key in (('initiators','packets'),('targets','arrival')):
        for name,value in [('half_rate',dict(forward=True,backward=True,half_rate=True)),
                           ('bypass_port',dict(forward=True,bypass_port='pipe_bypass')),
                           ('ungated',dict(forward=True,ungated=True))]:
            d=copy.deepcopy(cfg);d[side][0]['pipes']={key:value}
            path=work/f'{side}_{key}_{name}.yaml';path.write_text(json.dumps(d))
            r=subprocess.run([cli,'validate',str(path)],capture_output=True,text=True,timeout=120)
            assert r.returncode!=0 and 'NCL_ENDPOINT_PIPE' in r.stderr,(side,key,name,r.stderr)
            rows.append(dict(case=f'{side}_{key}_{name}',diagnostic='NCL_ENDPOINT_PIPE'))
    (work/'results.json').write_text(json.dumps(rows,indent=1)+'\n')


def main():
    p=argparse.ArgumentParser();p.add_argument('--cli');p.add_argument('--fixture');p.add_argument('--work',required=True);p.add_argument('--configuration-only',action='store_true')
    args=p.parse_args();base=Path(args.work).resolve();base.mkdir(parents=True,exist_ok=True)
    work=Path(tempfile.mkdtemp(prefix="run-",dir=base));(base/"latest.txt").write_text(str(work)+"\n")
    if args.configuration_only:
        configuration_checks(args.cli,work)
    elif args.fixture:
        for index,mode in enumerate(('both','forward','backward','half')):
            case=work/mode;case.mkdir()
            subprocess.run([args.fixture,str(case),mode],check=True)
            (case/'tb.sv').write_text(TB.replace('@MODE@',str(index)))
            rc=simulate(case,case/'compiled');log=(case/'simulation.log').read_text()
            assert rc==0 and 'CHECK_PASS link_pipeline' in log,(mode,log[-3000:])
        for mode,index in (('both',0),('forward',1),('half',3)):
            for bypass in (0,1):
                name=f'{mode}_bypass{bypass}';case=work/name;case.mkdir()
                subprocess.run([args.fixture,str(case),mode,str(bypass)],check=True)
                (case/'tb.sv').write_text(BYPASS_TB.replace('@MODE@',str(index)).replace('@BYPASS@',str(bypass)))
                rc=simulate(case,case/'compiled');log=(case/'simulation.log').read_text()
                assert rc==0 and 'CHECK_PASS link_bypass' in log,(name,log[-3000:])
        for mode in ('forward','half'):
            case=work/f'{mode}_ungated';case.mkdir()
            subprocess.run([args.fixture,str(case),mode,'0','1'],check=True)
            (case/'tb.sv').write_text(UNGATED_TB)
            rc=simulate(case,case/'compiled');log=(case/'simulation.log').read_text()
            assert rc==0 and 'CHECK_PASS link_ungated' in log,(mode,log[-3000:])
    else:
        network(args.cli,work)

if __name__=='__main__':
    main()
