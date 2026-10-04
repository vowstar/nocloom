"""Literal two-domain placement, clock-chain and physical-intent checks."""
import re


def check(mapping, bundle, rtl, mode):
    d, net = mapping['definition'], mapping['network']
    assert mapping['module'] == bundle['top'] == 'nocloom_cross_tree_' + mode
    assert d == bundle['definition']
    assert d['clock_domains'] == [dict(name=n, clock_port='clk_' + n, reset_port='reset_n', test_port='test') for n in ('alpha','beta')]
    assert d['synchronizer_stages'] == 2
    assert d['clock_crossings'] == [dict(from_domain='beta', to_domain='alpha', synchronizer_stages=4)]
    assert d['endpoint_clock_domains'] == dict(initiators={'host0':'alpha','host1':'alpha'},
                                              targets={'bank0':'alpha','bank1':'beta'})
    assert d['endpoint_protocols'] == dict(initiators={h:'apb4' if mode=='apb' else 'axi4_lite' for h in ('host0','host1')},
                                         targets={b:'apb4' for b in ('bank0','bank1')})
    assert d['targets'] == [dict(name='bank0',begin='0',end='4096'),dict(name='bank1',begin='4096',end='8192')]
    assert d['reset_ports']==[dict(name='reset_n',release='resynchronized')]
    cdc=net['cdc'];assert cdc['physical_status']=='OPEN'
    objects={'source/host0':'alpha','source/host1':'alpha','target/bank0':'alpha','target/bank1':'beta'}
    for direction,key in [('request','req/'),('response','rsp/')]:
        nodes=d['topology'][direction]['nodes'];assert {x['name']:x['clock_domain'] for x in nodes}=={'root':'alpha','leaf':'beta'}
        hardware={x['name']:x['hardware_node'] for x in nodes}
        objects.update({key+hardware[n]:v for n,v in [('root','alpha'),('leaf','beta')]})
        expected=[]
        for h in ('host0','host1'):
            for b in ('bank0','bank1'):
                route=['root'] if b=='bank0' else ['root','leaf']
                if direction=='response':route.reverse()
                expected.append(dict(entry=h if direction=='request' else b,exit=b if direction=='request' else h,
                                     hardware_nodes=[hardware[n] for n in route]))
        assert net[direction+'_routes']==sorted(expected,key=lambda x:(x['entry'],x['exit']))
    assert cdc['objects']==objects
    regs={r['value']:r for r in cdc['registers']};assert len(regs)==len(cdc['registers'])
    aliases={port:value for value,port in re.findall(r'assign (nl_internal_v[0-9]+) = (clk_[A-Za-z0-9_]+|reset_n);',rtl)}
    # Clock gates: each gated clock comes from its domain clock; their own registers are not
    # release registers.
    gated={out:clock for clock,out in re.findall(
        r'^nocloom_clock_gate \w+ \(\.clock_in\((nl_internal_v[0-9]+)\), [^;]*\.clock_out\((nl_internal_v[0-9]+)\)\);',rtl,flags=re.M)}
    assert gated and len(gated)==len(net['clock_gates'])
    # Synchronizer cell instances by name: stages, clock, reset, d, q.
    cells={m[1]:m for m in re.findall(r'^nocloom_synchronizer #\(\.STAGES\((\d+)\)\) (\w+) \(\.clock\((\w+)\), '
                                       r'\.reset_n\((\w+)\), \.d\(([\w[\]]+)\), \.q\(([\w[\]]+)\)\);',rtl,flags=re.M)}
    # Find actual release chains by their constant-one input and domain: two stages from the port.
    releases={}
    for domain in ('alpha','beta'):
        found=[m for m in cells.values() if m[2]==aliases['clk_'+domain]
               and re.search(r'assign '+m[4]+r" = 1'[bd]1;",rtl)]
        assert len(found)==1 and found[0][0]=='2' and found[0][3]==aliases['reset_n']
        releases[domain]=found[0]
    # A domain's state resets from its release chain's output; the chain from the reset port.
    for r in regs.values():
        release=releases[r['clock'].removeprefix('clk_')]
        if 'synchronizer' in r:
            for name in r['synchronizer']['instances']:
                n,_,clock,reset,_,_=cells[name]
                assert clock==aliases[r['clock']] and r['synchronizer']['stage']<int(n) and r['async_reset']
                assert reset==(aliases['reset_n'] if name==release[1] else release[5])
            continue
        block=next(b for b in rtl.split('always @')[1:] if r['rtl']+' <=' in b)
        clock=re.match(r'\(posedge (nl_internal_v[0-9]+)',block)[1]
        assert gated.get(clock,clock)==aliases[r['clock']]
        assert block.startswith('(posedge '+clock+(' or negedge '+release[5]+')' if r['async_reset'] else ')'))
    sync=set(cdc['synchronizer_stages'])
    assert {v for v,r in regs.items() if 'synchronizer' in r}==sync
    assert {i for v in sync for i in regs[v]['synchronizer']['instances']}==set(cells)
    assert len(cdc['cuts'])==2
    assert {c['segment'].split('/')[0] for c in cdc['cuts']}=={'req','rsp'}
    for cut in cdc['cuts']:
        direction=cut['segment'].split('/')[0]
        a,z=('alpha','beta') if direction=='req' else ('beta','alpha')
        assert (cut['from_domain'],cut['to_domain'])==(a,z)
        # alpha to beta takes the global two stages, beta to alpha its own four.
        na=nb={'alpha':2,'beta':4}[a]
        assert (cut['depth'],cut['depth_source'],cut['synchronizer_stages'])==(6,'fallback',na)
        for key,count,clock,previous in [('write_count_sync',nb,z,cut['write_count']),('read_count_sync',na,a,cut['read_count'])]:
            assert len(cut[key])==count
            for value in cut[key]:
                r=regs[value];assert value in sync and r['clock']=='clk_'+clock and r['next']==previous and r['async_reset'];previous=value
        assert cut['obligations']==[
            dict(kind='gray_count_skew',source=cut['write_count'],destination=cut['write_count_sync'][0],
                 bound='bit_to_bit_skew_below_both_clock_periods'),
            dict(kind='gray_count_skew',source=cut['read_count'],destination=cut['read_count_sync'][0],
                 bound='bit_to_bit_skew_below_both_clock_periods'),
            dict(kind='slot_read_max_delay',sources=[v for f in cut['fields'] for v in f['storage']],
                 destinations=[f['received'] for f in cut['fields']],bound='below_write_count_synchronization_latency')]
        for f in cut['fields']:
            assert len(f['storage'])==6
            # The default reset_coverage all resets the payload slots with the sender domain.
            assert all(regs[v]['clock']=='clk_'+a and regs[v]['async_reset'] for v in f['storage'])
            assert regs[f['received']]['clock']=='clk_'+z
    assert {x['path'] for x in bundle['files']}=={mapping['module']+s for s in ('.v','.map.json','.dependency.json','.bundle.json')}|{'nocloom_clock_gate.v','nocloom_synchronizer.v'}
    return releases
