"""Bandwidth-controlled AXI traffic uses its source domain across asynchronous links."""

import random
from nlverif import config as nlconfig


def configuration(protocol, scheduler=False):
    qos = dict(shaper=dict(bytes_per_1024_cycles=256, burst_bytes=16))
    if scheduler:
        qos['scheduler'] = dict(under_level=3, over_level=1, threshold_percent=50)
    cfg = dict(
        schema_version=10, name='cdc_shaper', address_width=32,
        reset_ports=[dict(name='reset_n')], test_ports=[dict(name='test')],
        clock_domains=[dict(name=d, clock_port='clk_' + d, reset_port='reset_n',
                            test_port='test') for d in ('d0', 'd1')],
        initiators=[dict(name='s0', clock_domain='d1', protocol=protocol,
                         data_width=32, id_width=2, len_width=4,
                         max_outstanding=4, qos=qos)],
        targets=[dict(name='t0', clock_domain='d0', protocol='axi4', data_width=32,
                      id_width=4, len_width=8, attributes='amba', max_outstanding=4,
                      mappings=[dict(base='0x10000', size='0x10000')])],
        connectivity='all',
        topology=dict(request=dict(
            nodes=[dict(name='a', clock_domain='d1'), dict(name='b', clock_domain='d0')],
            links=[{'from': 'a', 'to': 'b'}],
            entries=[dict(endpoint='s0', node='a')], exits=[dict(endpoint='t0', node='b')]),
            response='mirror'),
        output=dict(backend='verilog', language='verilog_2001'))
    return dict(
        label=protocol + ('_scheduler' if scheduler else '_shaper'),
        dut=dict(source='cli', config=cfg),
        clocks=dict(d0=10.0, d1=14.0), scenario_args=dict(scheduler=scheduler),
        bfms=[dict(name='s0', kind='axi_master', endpoint='initiator:s0', clock='d1'),
              dict(name='t0', kind='axi_slave', endpoint='target:t0', clock='d0', salt=3)],
        model=dict(routes=nlconfig.routes(cfg), keeps_decerr=True,
                   targets=nlconfig.targets(cfg, error_data_zero=True)))


CONFIGS = [configuration(p, s) for p in ('axi3', 'axi4') for s in (False, True)]
CASE = dict(
    name='cdc_shaper', contracts=['AXI.ROUTE.LOCAL_OFFSET', 'AXI.ERR.READ_ZERO'],
    configs=CONFIGS, main_clock='d0', max_cycles=50000, negative_runs_all=True,
    faults=[dict(id='read_data_bit', op='text', contract='AXI.ROUTE.LOCAL_OFFSET',
                 subs=[(r'(assign nl_i_s0_rdata = )([^;]+);',
                        r"\g<1>(\g<2>) ^ 32'h1;", 1)], expect=['SOURCE_R_DATA'])])


def scenario(t, seed, scheduler=False):
    rng = random.Random(seed)
    t.ready('s0', 'b', 'modulo', 5, 0)
    t.ready('s0', 'r', 'modulo', 4, 0)
    for ch in ('aw', 'ar', 'w'):
        t.ready('t0', ch, 'modulo', 3, 0)
    t.set('t0', 'b_lat', 5)
    t.set('t0', 'r_lat', 7)
    t.reset(12)
    t.delay(40)
    t.write('s0', 0x10000, beats=16, id=1)
    t.drain(limit=10000)
    first = t.snap('t0', 'aw')
    t.write('s0', 0x10040, beats=16, id=2)
    if not scheduler:
        t.delay(8)
        t.expect_now('t0', 'aw', '==', 0, 'BUDGET_STALL', since=first)
    t.drain(limit=10000)
    for k in range(12):
        addr = 0x10200 + k * 64
        beats = rng.choice((1, 4, 8, 16))
        t.write('s0', addr, beats=beats, id=k % 4)
        t.drain(limit=10000)
        t.read('s0', addr, beats=beats, id=k % 4)
        t.drain(limit=10000)
    t.read('s0', 0x10000, beats=16, id=1)
    t.read('s0', 0x10040, beats=16, id=2)
    t.drain(limit=10000)
    t.end()
