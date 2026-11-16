"""AXI byte ranges through full-size TileLink target messages."""
import argparse
import json
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_engine


def bench(mapping, cfg, body, scenarios, exact):
    declarations,connections=[],[]
    for p in mapping['ports']:
        name,width=p['name'],p['width']
        alias=name.replace('nl_i_s_','s_').replace('nl_t_t_','t_')
        incoming=p['direction']=='input'
        declarations.append(f"{'reg' if incoming else 'wire'} [{width-1}:0] {alias}"+('=0;' if incoming else ';'))
        connections.append(f'.{name}({alias})')
    source=next((p['width'] for p in mapping['ports'] if p['name']=='nl_t_t_a_source'),0)
    if source==0: declarations.extend(['wire t_a_source=0;','reg t_d_source=0;'])
    cdc=cfg['targets'][0]['clock_domain']=='far'
    if not cdc: declarations.append('reg clk_far=0;')
    target=cfg['targets'][0]
    params=f"localparam DATA_WIDTH={cfg['initiators'][0]['data_width']}, CAPACITY={target['max_outstanding']}, MAXIMUM={target['max_transfer_bytes']}, CDC={int(cdc)}, EXACT_BLOCK={int(exact)};"
    bridge=cfg['initiators'][0]['protocol']=='tl_uh'
    gates=[g for g in mapping['network'].get('clock_gates',[]) if g.get('target')=='t' and g.get('zone')=='protocol']
    probe='1\'b1' if not gates else 'dut.nl_internal_v'+gates[0]['enable'].split('/')[-1]
    body=body.replace('GATE_PROBE',probe).replace('GATE_IDLE','check(!target_gate_open,"GATE_IDLE_SLEEP");' if gates else '')
    fields='awaddr araddr awid arid bid rid awlen arlen awsize arsize awprot arprot awburst arburst bresp rresp awcache arcache awlock arlock awvalid awready arvalid arready wdata rdata wstrb wlast wvalid wready bvalid bready rlast rvalid rready'.split()
    axi='' if bridge else f'nlv_tl_uh_axi_check #(.NAME("source"),.DATA_W({cfg["initiators"][0]["data_width"]})) checker_s(.clk(clk),.rst_n(reset_n),'+','.join(f'.{f}(s_{f})' for f in fields)+');'
    tlfields='a_opcode a_param a_size a_source a_address a_mask a_data a_corrupt a_valid a_ready d_opcode d_param d_size d_source d_data d_corrupt d_denied d_valid d_ready'.split()
    tl=f'nlv_tl_checker #(.NAME("target"),.SOURCE_W({max(1,source)}),.SIZE_W(4),.UH(1),.SOURCE_LIMIT(CAPACITY)) checker_t(.clk(target_clk),.rst_n(reset_n),.allow_reject(1\'b0),'+','.join(f'.{f}(t_{f})' for f in tlfields)+');'
    if bridge:
        axi='nlv_tl_checker #(.NAME("source"),.SOURCE_W(16),.SIZE_W(4),.UH(1)) checker_s(.clk(clk),.rst_n(reset_n),.allow_reject(1\'b0),'+','.join(f'.{f}(s_{f})' for f in tlfields)+');'
    return '`timescale 1ns/1ps\nmodule tb;\n'+'\n'.join(declarations)+'\nnocloom_uh_large dut('+','.join(connections)+');\n'+params+'\n'+body.replace('SCENARIOS',scenarios)+'\n'+axi+'\n'+tl+'\nendmodule\n'


def run(args, maximum, width, capacity, scenarios, exact=True, cdc=False, gated=False, bridge=False, exact_reads=True):
    work=Path(tempfile.mkdtemp(prefix='large-',dir=args.work)).resolve()
    cfg=runpy.run_path(str(args.case))["configuration"](maximum,width,capacity,cdc,gated)
    cfg['targets'][0]['read_side_effects']=exact_reads
    if bridge:
        cfg['initiators'][0]=dict(name='s',clock_domain='main',protocol='tl_uh',data_width=32,source_width=16,size_width=4,max_transfer_bytes=4096,max_outstanding=4,default_target='t')
    (work/'input.yaml').write_text(json.dumps(cfg))
    generated=work/'generated'
    r=subprocess.run([str(args.cli.resolve()),'generate',str(work/'input.yaml'),'--out-dir',str(generated)],capture_output=True,text=True,timeout=60)
    (work/'generation.log').write_text(r.stdout+r.stderr)
    if r.returncode:
        print(r.stderr,work,flush=True); return r.returncode
    mapping=json.loads(next(generated.glob('*.map.json')).read_text())
    pins={p['name']: p for p in mapping['ports']}
    if not bridge:
        assert pins['nl_i_s_awlen']['width']==8 and pins['nl_i_s_arlen']['width']==8
        assert mapping['definition']['transport']['len_bits']==(min(maximum,width//8*256)//4-1).bit_length()
    (work/'tb.sv').write_text(bench(mapping,cfg,args.bench.read_text(),scenarios,exact))
    sv=Path(__file__).parent / "sv"
    rtl=sim_engine.generated_rtl(generated/'nocloom_uh_large.v')
    rtl+=(sv/'nlv_tl_checker.sv').read_text()+(sv/'nlv_tl_uh_axi_check.sv').read_text()
    (work/'network.v').write_text(rtl)
    r=sim_engine.Simulation(work,'network.v',engine=args.sim,config=args.sim_config,compile_timeout=180,run_timeout=120,success=('UH_LARGE_PASS',)).run()
    print(r.stdout,work,flush=True)
    return r.returncode


def matrix(args):
    cases=[(32,[(4096,255),(8192,256)]),
           (64,[(4100,128),(8192,128),(12292,129)]),
           (128,[(4100,128),(8192,128),(12300,129),(16388,256),(20480,256)])]
    for width,pairs in cases:
        scenarios=''.join(f'write_packet({a},{n},1,0); read_packet({a},{n},0);' for a,n in pairs)
        if width==128:
            scenarios+='write_packet(131072,256,0,1); read_packet(131072,256,1);'
        result=run(args,4096,width,3,scenarios,exact=False,cdc=width!=32,gated=True)
        if result: return result
    scenarios='write_packet(4096,256,0,0);'+''.join(
        f'corrupt_word={n};read_packet(4096,256,0);' for n in (255,256,1022,1023))
    scenarios+='corrupt_word=-1;deny_read=1;read_packet(4096,256,0);deny_read=0;deny_write=1;write_packet(8192,256,1,0);'
    return run(args,4096,128,3,scenarios,cdc=True,gated=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',type=Path,required=True)
    parser.add_argument('--cli',type=Path,required=True)
    parser.add_argument('--bench',type=Path,required=True)
    parser.add_argument('--work',type=Path,required=True)
    parser.add_argument('--mode',choices=('large','matrix','segments','bridge'),default='large')
    sim_engine.add_arguments(parser)
    args=parser.parse_args(); args.work.mkdir(parents=True,exist_ok=True)
    if args.mode=='matrix': return matrix(args)
    if args.mode=='segments': return run(args,4096,128,3,'',exact_reads=False)
    if args.mode=='bridge': return run(args,4096,32,3,'',bridge=True)
    for maximum in (2048,4096):
        count=maximum//16
        scenarios=f'write_packet(4096,{count},0,0); read_packet(4096,{count},0);'
        for capacity in (1,3):
            result=run(args,maximum,128,capacity,scenarios)
            if result: return result
    scenarios='begin : split_packet integer prior; prior=messages; write_packet(8192,256,0,0); check(messages-prior==2,"TWO_LIMITED_PUTS"); prior=messages; read_packet(8192,256,0); check(messages-prior==2,"TWO_LIMITED_GETS"); end'
    return run(args,2048,128,3,scenarios,exact=False)

if __name__=='__main__': raise SystemExit(main())
