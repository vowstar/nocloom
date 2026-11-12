"""Four corner AXI4 sources and nine AXI4 targets in a 3 by 3 mesh.

Every admitted pair runs writes and reads, including four diagonal directions
and local routes. All four sources queue bursts before any completion wait.
The checker judges target bytes, addresses, IDs, response data and completion.
"""
from nlverif import config as nlconfig

SOURCES = ('i00', 'i02', 'i20', 'i22')
TARGETS = tuple(f't{r}{c}' for r in range(3) for c in range(3))
# Independent address and connectivity oracle. The DUT comes from the C++ helper.
CONFIG = dict(
    schema_version=10,
    initiators=[dict(name=n, protocol='axi4', data_width=32, id_width=2) for n in SOURCES],
    targets=[dict(name=n, protocol='axi4', data_width=32, id_width=4,
                  attributes='amba', mappings=[dict(base=str(k * 4096), size='4096')])
             for k, n in enumerate(TARGETS)],
    connectivity='all')


def wrong_read_data(rtl, mapping):
    return [(r'^(\s*assign nl_i_i00_rdata = )[^;]+;', r"\g<1>32'd0;", 1)]


CASE = dict(
    name='mesh_topology',
    contracts=['AXI.ROUTE.LOCAL_OFFSET', 'AXI.W.UNSTROBED_ZERO'],
    dut=dict(source='generated', dir=PARAMS.get('generated')),
    bfms=[dict(name=n, kind='axi_master', endpoint=f'initiator:{n}') for n in SOURCES]
    + [dict(name=n, kind='axi_slave', endpoint=f'target:{n}', salt=k + 1)
       for k, n in enumerate(TARGETS)],
    model=dict(routes=nlconfig.routes(CONFIG), error_codes=nlconfig.error_codes(CONFIG),
               targets=nlconfig.targets(CONFIG, cache='amba', prot=True)),
    max_cycles=30000,
    faults=[dict(id='zero_read_data', op='text', subs=wrong_read_data,
                 contract='AXI.ROUTE.LOCAL_OFFSET', expect=['SOURCE_R_DATA'])])


def scenario(t, seed):
    t.reset(4)
    t.delay(2)
    for n in TARGETS:
        t.ready(n, 'w', 'modulo', 3, 1)
        t.set(n, 'b_lat', 5)
        t.set(n, 'r_lat', 5)
    for k, target in enumerate(TARGETS):
        t.set(target, 'gate_b', 0)
        for s, source in enumerate(SOURCES):
            # Disjoint bytes keep concurrent writes independent.
            t.write(source, k * 4096 + s * 256, beats=4, id=(s + k) % 4,
                    cache=3, prot=0, strobe='sparse')
        for source in SOURCES:
            t.wait(source, 'aw', '==', k + 1, limit=4000, label='MESH_CONCURRENT_AW')
            t.expect_now(source, 'b', '==', k, 'MESH_RESPONSES_HELD')
        t.set(target, 'gate_b', 1)
        t.drain(limit=4000, label='MESH_WRITES')
        for s, source in enumerate(SOURCES):
            t.read(source, k * 4096 + s * 256, beats=4, id=(s + k) % 4, cache=3)
        t.drain(limit=4000, label='MESH_READS')
    t.end()
