"""Large AXI ranges at a TL-UH target."""

def configuration(maximum, width, capacity, cdc=False, gated=False):
    cfg=dict(schema_version=10,name='uh_large',address_width=32,
        clock_gating=True if gated else False,
        reset_ports=[dict(name='reset_n')],test_ports=[dict(name='test')],
        clock_domains=[dict(name='main',clock_port='clk',reset_port='reset_n',test_port='test')],
        initiators=[dict(name='s',clock_domain='main',protocol='axi4',data_width=width,id_width=4,len_width=8,
                         max_outstanding=4,default_target='t')],
        targets=[dict(name='t',clock_domain='main',protocol='tl_uh',data_width=32,max_transfer_bytes=maximum,
                      max_outstanding=capacity,size_width=4,read_side_effects=True,
                      mappings=[dict(base='0x0',size='0x10000')])],
        connectivity='all',output=dict(backend='verilog',language='verilog_2001'))
    if cdc:
        cfg['clock_domains'][0]['period_ps']=10000
        cfg['clock_domains'].append(dict(name='far',clock_port='clk_far',reset_port='reset_n',test_port='test',period_ps=17000))
        cfg['targets'][0]['clock_domain']='far'
        cfg['topology']=dict(request=dict(nodes=[dict(name='q',clock_domain='main')],links=[],
            entries=[dict(endpoint='s',node='q')],exits=[dict(endpoint='t',node='q')]),
            response=dict(nodes=[dict(name='r',clock_domain='far')],links=[],
            entries=[dict(endpoint='t',node='r')],exits=[dict(endpoint='s',node='r')]))
    return cfg



CASE = dict(contracts=["TL.ACCESS"], dut=dict(source="cli", config=configuration(4096,128,3)))
