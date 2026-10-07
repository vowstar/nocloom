"""Small single-transfer address ports through the public CLI: APB4, APB3
and AXI4-Lite initiators of 8 to 64 bits with 1- to 11-bit address ports
reach an APB4 target; the source alignment is kept and the transport
decodes the full address.

The case table and the access count of every transfer are the scenario's;
the offline checker judges bytes, strobes, read data and responses, and a
target beyond bit 31 (t1, the mixed configuration) must never be selected.
A read waiting at the target is cancelled by the common reset, then a
fresh read completes. The mixed configuration gates its clocks (the default)
and also runs with the test port high; "ungated" runs it without clock gates."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402


def configuration(tiny=False):
    sources = [('a8', 'apb4', 8, 1), ('a16', 'apb3', 16, 1),
               ('a32', 'apb4', 32, 1), ('l32', 'axi4_lite', 32, 2)]
    if not tiny:
        sources += [('a11', 'apb4', 8, 11), ('l2', 'axi4_lite', 64, 2),
                    ('l11', 'axi4_lite', 64, 11)]
    d = {'schema_version': 10, 'name': 'small_address', 'address_width': 2 if tiny else 40,
         'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
         'initiators': [dict(name=n, protocol=p, data_width=w, address_width=a,
                             clock_domain='main') for n, p, w, a in sources],
         'targets': [dict(name='t0', protocol='apb4', data_width=32,
                          address_width=2 if tiny else 10, clock_domain='far' if tiny else 'main',
                          mappings=[{'base': '0x0', 'size': '0x4' if tiny else '0x400'}])],
         'transport': {'transport_word_bytes': 4},
         'connectivity': 'all', 'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    if tiny:
        # One source inherits the global two-bit width; the others override it.
        d['initiators'][-1].pop('address_width')
        d['clock_domains'] = [dict(name=n, clock_port='clk_' + n, reset_port='reset_n', test_port='test', period_ps=period)
                              for n, period in [('main', 10000), ('far', 14000)]]
        d['topology'] = {
            'request': {'nodes': [dict(name='q', clock_domain='main')], 'links': [],
                        'entries': [dict(endpoint=n, node='q') for n, _, _, _ in sources],
                        'exits': [dict(endpoint='t0', node='q')]},
            'response': {'nodes': [dict(name='r', clock_domain='far')], 'links': [],
                         'entries': [dict(endpoint='t0', node='r')],
                         'exits': [dict(endpoint=n, node='r') for n, _, _, _ in sources]}}
    else:
        # A low-port address must never alias a target beyond bit 31. No low port can connect that
        # target: an idle source of the whole address space reaches it.
        d['targets'].append(dict(name='t1', protocol='apb4', data_width=32, address_width=12,
                                 clock_domain='main',
                                 mappings=[{'base': '0x100000000', 'size': '0x1000'}]))
        d['initiators'].append(dict(name='wide', protocol='axi4_lite', data_width=32,
                                    clock_domain='main'))
        d['connectivity'] = {n: ['t0'] for n, _, _, _ in sources}
        d['connectivity']['wide'] = ['t1']
    return endpoint_ports(d, id_width=0, len_width=8), sources


def transfers(sources, tiny):
    """(source, protocol, write, address, strobe mask, target accesses) of
    the scenario, in its order."""
    size = 4 if tiny else 1024
    out = []
    for name, protocol, width, ab in sources:
        addresses = [0, (1 << ab) - 1]
        if ab > 2:
            addresses += [1, 3, 7, 0x3ff, 0x400]
        for address in addresses:
            for wr in (0, 1):
                full = (1 << (width // 8)) - 1
                masks = [full] if not wr or protocol == 'apb3' else [1, 0, full]
                for mask in masks:
                    aligned = address // (width // 8) * (width // 8)
                    accesses = 0
                    if aligned < size:
                        for part in range(0, width // 8, 4):
                            if not wr or (mask >> part) & 15:
                                accesses += 1
                    out.append((name, protocol, wr, address, mask, accesses))
    return out


def bfms(sources, tiny):
    out = []
    for name, protocol, width, ab in sources:
        if protocol.startswith('apb'):
            out.append(dict(name=name, kind="apb_master", endpoint=f"initiator:{name}",
                            dialect=int(protocol[-1])))
        else:
            out.append(dict(name=name, kind="axi_master", endpoint=f"initiator:{name}", lite=True))
    out.append(dict(name="t0", kind="apb_slave", endpoint="target:t0", salt=1,
                    clock="far" if tiny else "main"))
    if not tiny:
        out.append(dict(name="wide", kind="axi_master", endpoint="initiator:wide", lite=True))
        out.append(dict(name="t1", kind="apb_slave", endpoint="target:t1", salt=2))
    return out


def model(cfg):
    return dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg),
                targets=nlconfig.targets(cfg, error_data_zero=True))


MIXED, MIXED_SOURCES = configuration(False)
TINY, TINY_SOURCES = configuration(True)
# Clock gating (the default): the mixed tape runs with the test port low and high; "ungated"
# runs it without clock gates.
UNGATED = nlconfig.ungated(MIXED)
CASE = dict(
    name="small_address",
    contracts=["AXI.ROUTE.LOCAL_OFFSET", "AXI.ERR.READ_ZERO", "AXI.RESET.CANCEL",
               "AXI.W.UNSTROBED_ZERO", "APB.INITIATOR.TRANSFER"],
    dut=dict(source="cli", config=MIXED),
    bfms=bfms(MIXED_SOURCES, False),
    model=model(MIXED),
    scenario_args=dict(tiny=False),
    configs=[dict(label="", test_lockstep=True),
             dict(label="tiny", dut=dict(source="cli", config=TINY), model=model(TINY),
                  bfms=bfms(TINY_SOURCES, True), clocks=dict(main=10.0, far=14.0),
                  scenario_args=dict(tiny=True)),
             dict(label="ungated", dut=dict(source="cli", config=UNGATED), model=model(UNGATED))],
    max_cycles=40000,
    faults=[],
)


def scenario(t, seed, tiny=False):
    sources = TINY_SOURCES if tiny else MIXED_SOURCES
    if not tiny:
        t.set("t1", "quiet", 1)
    t.set("t0", "wait", 2)
    for name, protocol, _, _ in sources:
        if protocol == "axi4_lite":
            t.ready(name, "b", "delay", 4)
            t.ready(name, "r", "delay", 4)
    t.reset(10)
    t.delay(16)
    for k, (name, protocol, wr, address, mask, accesses) in enumerate(transfers(sources, tiny)):
        t.mark("case", k)
        if wr:
            if protocol == "axi4_lite" and k % 2:
                # W before AW at the source (both orders, AXI A3.3.1).
                t.gate(name, "aw", False)
            t.strobes(name, [mask])
            t.write(name, address, strobe="table", targets=accesses)
            if protocol == "axi4_lite" and k % 2:
                t.delay(4)
                t.gate(name, "aw", True)
        else:
            t.read(name, address, targets=accesses)
        t.drain(limit=2000, label=f"CASE_{k}")
    # Cancel an APB read waiting at the target, then start fresh.
    t.mark("case", 9999)
    m = t.snap("t0", "access")
    t.set("t0", "wait", 100000)
    t.read("a8", 1)
    t.wait("t0", "psel", "==", 1, label="TARGET_WAITING")
    t.delay(6)
    t.reset(10)
    t.set("t0", "wait", 2)
    t.delay(16)
    t.read("a8", 1, targets=1)
    t.drain(limit=2000, label="AFTER_RESET")
    t.expect_now("t0", "access", "==", 1, "FRESH_READ_ONLY", since=m)
    t.end()
