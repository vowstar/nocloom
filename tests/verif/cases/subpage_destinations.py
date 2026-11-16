"""Sparse connectivity inside a shared page preserves destination and miss routing."""

import random
from nlverif import config as nlconfig


def configuration(label, axi=False, high=False, named=False, flows=False):
    offset = 1 << 32 if high else 0
    cfg = dict(
        schema_version=10, name='subpage_destinations', address_width=40 if high else 32,
        reset_ports=[dict(name='reset_n')], test_ports=[dict(name='test')],
        clock_domains=[dict(name='main', clock_port='clk_main', reset_port='reset_n', test_port='test')],
        initiators=[dict(name='s'+str(i), clock_domain='main', protocol='axi4',
                         data_width=32, id_width=2, len_width=8, max_outstanding=4,
                         **(dict(default_target='t'+str(i)) if named else {})) for i in range(2)],
        targets=[dict(name='t0', clock_domain='main', protocol='axi4' if axi else 'apb3',
                      data_width=32, address_width=32,
                      mappings=[dict(base=hex(offset+0x1400), size='0x200', local_address='0x400')],
                      **(dict(attributes='amba', len_width=8) if axi else {})),
                 dict(name='t1', clock_domain='main', protocol='axi4', data_width=32,
                      address_width=32, len_width=8, attributes='amba', max_outstanding=4,
                      mappings=[dict(base=hex(offset+0x1800), size='0x400', local_address='0x800')])],
        connectivity=dict(s0=['t0'], s1=['t1']),
        output=dict(backend='verilog', language='verilog_2001'))
    bfms = [dict(name='s0', kind='axi_master', endpoint='initiator:s0'),
            dict(name='s1', kind='axi_master', endpoint='initiator:s1'),
            dict(name='t0', kind='axi_slave' if axi else 'apb_slave',
                 endpoint='target:t0', salt=1, **({} if axi else dict(dialect=3))),
            dict(name='t1', kind='axi_slave', endpoint='target:t1', salt=2)]
    if flows:
        target = cfg['targets'][0]
        target['flows'] = 2
        target['mappings'] += cfg['targets'][1]['mappings']
        target['mappings'][1]['flow'] = 1
        cfg['targets'] = [target]
        cfg['connectivity'] = {f's{i}': [dict(target='t0', target_path=i)] for i in range(2)}
        bfms.pop()
    return dict(label=label, dut=dict(source='cli', config=cfg),
                scenario_args=dict(offset=offset, target='t0' if flows else 't1'), bfms=bfms,
                model=dict(routes=nlconfig.routes(cfg), keeps_decerr=True,
                           targets=nlconfig.targets(cfg, error_data_zero=True)))


CASE = dict(
    name='subpage_destinations',
    contracts=['AXI.ROUTE.LOCAL_OFFSET', 'AXI.ERR.UNMAPPED_DECERR', 'AXI.ERR.READ_ZERO'],
    configs=[configuration('mixed'), configuration('axi', axi=True),
             configuration('high', high=True), configuration('named', named=True),
             configuration('target_flows', axi=True, flows=True)],
    negative_runs_all=True, max_cycles=30000,
    faults=[dict(id='miss_okay', op='text', contract='AXI.ERR.UNMAPPED_DECERR',
                 subs=[(r'(assign nl_i_s1_rresp = )([^;]+);', r"\g<1>2'b00;", 1)],
                 expect=['SOURCE_R_RESP'])])


def scenario(t, seed, offset=0, target='t1'):
    rng = random.Random(seed)
    for s in ('s0', 's1'):
        t.ready(s, 'b', 'modulo', 5, 0)
        t.ready(s, 'r', 'modulo', 4, 1)
        t.set(s, 'w_gap', 1)
    for ch in ('aw', 'ar', 'w'):
        t.ready(target, ch, 'modulo', 3, 0)
    t.set(target, 'b_lat', 5)
    t.set(target, 'r_lat', 7)
    t.reset(8)
    t.delay(20)
    # Every source visits both mappings, their edges, the page holes and another page.
    # Only one mapping is reachable by each source. The model reads that connectivity.
    addresses = [(0x1400, 4), (0x15f0, 4), (0x1800, 4), (0x1bf0, 4),
                 (0x13fc, 4), (0x15fc, 4), (0x17fc, 4), (0x1bfc, 4),
                 (0x1600, 1), (0x1c00, 1), (0x3000, 4)]
    for index, (addr, beats) in enumerate(addresses):
        for s in ('s0', 's1'):
            t.write(s, offset+addr, beats=beats, id=index % 4)
        t.drain()
        for s in ('s0', 's1'):
            t.read(s, offset+addr, beats=beats, id=index % 4)
        t.drain()
    # Reuse an ID across a connected window, an unreachable window and a page hole.
    for k in range(12):
        for s in ('s0', 's1'):
            addr, beats = rng.choice(addresses)
            t.read(s, offset+addr, beats=beats, id=1)
    t.drain()
    t.end()
