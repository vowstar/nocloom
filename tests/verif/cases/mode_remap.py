"""Mode-dependent mappings (docs/address-map.md#address-selectors) through the public CLI:
two address inputs select which target answers the shared windows. An AXI4 source with one
shared burst slot, an AXI4 source with four, an AXI4-Lite and an APB4 source reach an AXI4 RAM, an
APB4 ROM and an AXI4-Lite device; each also has an unconditional alias.

Every mode combination runs reads and writes from every source with the network drained
around each switch. A burst decodes in the modes of its first request, held until it leaves:
a write whose AW waits for W while the mode changes goes to the new target, and a write or a
read whose first beats were taken stays at its target when the mode changes mid-burst. The
checker uses the expected admission modes for requests held behind full capacity, and the
address input values of the AR or first W handshake for the drained traffic."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

SOURCES = [("a0", "axi4", 1), ("m0", "axi4", 4), ("l0", "axi4_lite", 1), ("p0", "apb4", 1)]
# A shared window answered by one target per mode, and an unconditional alias of each target.
ROM, RAM, DEV = 0x0, 0x0, 0x4000
ALIAS = dict(ram=0x10000, rom=0x20000, dev=0x30000)


def configuration(packet_bytes=None):
    d = {'schema_version': 10, 'name': 'mode_remap', 'address_width': 32,
         'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
         'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                            'test_port': 'test'}],
         'address_inputs': [{'name': 'boot', 'width': 1, 'clock_domain': 'main'},
                        {'name': 'bank', 'width': 2, 'clock_domain': 'main'}],
         'address_selectors': [{'name': 'rom', 'equals': {'boot': 1}},
                        {'name': 'high', 'equals': {'bank': 2}}],
         'initiators': [dict(name=n, protocol=p, data_width=32, clock_domain='main',
                             **({'max_outstanding': k} if k > 1 else {}))
                        for n, p, k in SOURCES],
         'targets': [
             dict(name='ram', protocol='axi4', data_width=32, clock_domain='main',
                  attributes='amba', address_width=16,
                  mappings=[{'base': '0x0', 'size': '0x2000', 'when': {'rom': False}},
                            {'base': hex(ALIAS['ram']), 'size': '0x2000'}]),
             dict(name='rom', protocol='apb4', data_width=32, clock_domain='main',
                  attributes='amba', address_width=16,
                  mappings=[{'base': '0x0', 'size': '0x1000', 'when': {'rom': True}},
                            {'base': hex(ALIAS['rom']), 'size': '0x1000'}]),
             dict(name='dev', protocol='axi4_lite', data_width=32, clock_domain='main',
                  attributes='amba', address_width=16,
                  mappings=[{'base': hex(DEV), 'size': '0x1000',
                             'when': {'rom': True, 'high': True}},
                            {'base': hex(ALIAS['dev']), 'size': '0x1000'}])],
         'connectivity': 'all', 'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    if packet_bytes:
        d['transport'] = dict(packet_limits=[dict(source='a0', target=name, max_bytes=packet_bytes)
                                              for name in ('ram', 'rom', 'dev')])
    return endpoint_ports(d, id_width=2, len_width=8)


CFG = configuration()
MODES = [(boot, bank) for boot in (0, 1) for bank in (0, 2)]


def bfms():
    out = []
    for name, protocol, _ in SOURCES:
        if protocol == "apb4":
            out.append(dict(name=name, kind="apb_master", endpoint=f"initiator:{name}", dialect=4))
        else:
            out.append(dict(name=name, kind="axi_master", endpoint=f"initiator:{name}",
                            lite=protocol == "axi4_lite"))
    out += [dict(name="ram", kind="axi_slave", endpoint="target:ram", salt=1),
            dict(name="rom", kind="apb_slave", endpoint="target:rom", salt=2),
            dict(name="dev", kind="axi_slave", endpoint="target:dev", lite=True, salt=3)]
    return out


def model(cfg):
    return dict(mode_routes=nlconfig.mode_routes(cfg, MODES), error_codes=nlconfig.error_codes(cfg),
                targets=nlconfig.targets(cfg, error_data_zero=True))


def plane_held_text(source):
    """Fault: the write plane of a source always follows the address selectors (never held), anchored
    on the net the map names."""
    def subs(rtl, mapping):
        row = next(a for a in mapping['network']['admission'] if a['source'] == source)
        net = 'nl_internal_v' + row['write_mode_plane'].split('/')[1]
        return [(r'^(assign ' + net + r' = )\(?[^?;]+\? [^:;]+ : ([^;]+?)\)?;', r'\g<1>\g<2>;', 1)]
    return subs


def flag_stuck(flag):
    """Fault: an address selector stuck low (its net named by the map)."""
    def subs(rtl, mapping):
        net = 'nl_internal_v' + mapping['network']['address_selectors'][flag].split('/')[1]
        return [(r'^(assign ' + net + r' = )([^;]+);', r"\g<1>1'b0;", 1)]
    return subs


CASE = dict(
    name="mode_remap",
    contracts=["AXI.ROUTE.MODE", "AXI.ROUTE.LOCAL_OFFSET", "AXI.ERR.UNMAPPED_DECERR"],
    dut=dict(source="cli", config=CFG),
    modes=["boot", "bank"],
    bfms=bfms(),
    model=model(CFG),
    configs=[dict(label=""),
             dict(label="split16", dut=dict(source="cli", config=configuration(16)),
                  negative_run=True)],
    max_cycles=60000,
    faults=[
        dict(id="flag_stuck", op="text", subs=flag_stuck("rom"), contract="AXI.ROUTE.MODE",
             expect=["TARGET_UNATTRIBUTED", "SOURCE_R_DATA", "TARGET_FOOTPRINT",
                     "TARGET_WRITE_BYTES", "SOURCE_B_RESP", "SOURCE_R_RESP"]),
        dict(id="plane_unheld", op="text", subs=plane_held_text("a0"), contract="AXI.ROUTE.MODE",
             expect=["TARGET_UNATTRIBUTED", "TARGET_FOOTPRINT", "TARGET_WRITE_BYTES",
                     "SOURCE_B_RESP", "TAPE.WAIT_TIMEOUT:IN_FLIGHT_WRITE"]),
    ],
)


def traffic(t, sources, tag):
    """Reads and writes of every source to each shared window and each alias."""
    for k, (name, _, _) in enumerate(sources):
        for base in (RAM, DEV, ALIAS['ram'], ALIAS['rom'], ALIAS['dev']):
            address = base + 0x40 * (k + 1) + 0x100 * tag
            if name in ("a0", "m0"):
                t.write(name, address, beats=4)
                t.read(name, address, beats=4)
            else:
                t.write(name, address)
                t.read(name, address)


def scenario(t, seed):
    t.mode(0, 0)
    t.mode(1, 0)
    t.reset(10)
    t.delay(16)
    for tag, (boot, bank) in enumerate(MODES):
        t.mark("case", tag)
        t.mode(0, boot)
        t.mode(1, bank)
        t.delay(4)
        traffic(t, SOURCES, tag)
        t.drain(limit=4000, label=f"MODES_{boot}_{bank}")
        t.delay(4)
    # A write whose AW waits for W across a mode switch goes to the new mode's target.
    t.mark("case", 100)
    t.mode(0, 0)
    t.delay(4)
    t.gate("a0", "w", False)
    t.write("a0", 0x200, beats=4)
    t.wait("a0", "aw", ">=", 1, label="AW_TAKEN", since=t.snap("a0", "aw"))
    t.delay(12)
    t.mode(0, 1)
    t.delay(12)
    t.gate("a0", "w", True)
    t.drain(limit=4000, label="AW_BEFORE_SWITCH")
    # A read and a write whose first beat the ROM took stay there across the switch.
    t.set("rom", "wait", 6)
    for tag, start in ((101, t.read), (102, t.write)):
        t.mark("case", tag)
        t.mode(0, 1)
        t.delay(4)
        m = t.snap("rom", "access")
        start("a0", 0x400 + 0x100 * (tag - 101), beats=16)
        t.wait("rom", "access", ">=", 1, label=f"FIRST_ACCESS_{tag}", since=m)
        t.mode(0, 0)
        t.drain(limit=6000, label="IN_FLIGHT_READ" if tag == 101 else "IN_FLIGHT_WRITE")
    t.set("rom", "wait", 0)
    # Capacity one: a completed request packet still occupies capacity until its response.
    # Keep the APB target stalled so AR can arrive at the port before the mode changes.
    t.mark("case", 103)
    t.mode(0, 1)
    t.mode(1, 0)
    t.set("rom", "ready_mode", 1)
    t.delay(4)
    selected = t.snap("rom", "psel")
    completed = t.snap("rom", "access")
    ar = t.snap("a0", "ar")
    t.write("a0", 0x900)
    t.wait("rom", "psel", ">=", 1, since=selected, label="CAPACITY_OCCUPIED")
    t.read("a0", 0x940, id=1, modes=[0, 0])
    t.wait("a0", "ar", ">=", 1, since=ar, label="QUEUED_AR")
    t.delay(40)
    t.expect_now("rom", "access", "==", 0, since=completed, label="CAPACITY_STILL_FULL")
    t.mode(0, 0)
    t.delay(20)
    t.set("rom", "ready_mode", 0)
    t.drain(limit=4000, label="MODE_AFTER_CAPACITY_WAIT")
    t.end()
