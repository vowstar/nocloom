"""Target IDs follow the route sum on the public schema 10 path (CLI), at a target answering across IDs.

Two sources (AxID 2 bits) reach a target of two ID bits and four bursts in flight that answers the
youngest ready response first. The offline checker pairs every target access by the ID the rule
gives from the map's numbers (check.py _route_id_match). Reads and writes that take one target ID
wait for each other; those of different IDs overtake. With `exclusive: target` an exclusive access
takes the value of its owner flags and a normal access keeps the sum; either waits for an earlier
access of its source and AxID when one of them is exclusive. A service target numbers like any other. At a target answering in order nothing
overtakes.

Negative controls, one per rule: a target AR ID with its bits inverted loses the pairing
(TARGET_UNATTRIBUTED); the overtaking reversed fails its expectation for shared IDs and for
different IDs; an exclusive read's ID with its low bit flipped (target mode) loses the pairing; the
wait expected to have issued the second access fails. Two APB sources and a target of one burst
leave no direction bit: an inverted read ID loses the pairing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

# On t1 (global target index 1), the two-bit slices include source flow above AxID.
# Reads: s0 AxID 1 and s1 AxID 0 share port ID 3. AxID 2 at s0 gives port ID 1.
# Writes: s0 AxID 0 and s1 AxID 1 share port ID 1. AxID 1 at s0 gives port ID 0.
SHARED = dict(r=(1, 0), w=(0, 1))
APART = dict(r=(2, 0), w=(1, 1))
BASE = 0x1000


def config(exclusive=False, apertures=1):
    flags = {'user_flags': ['src', 'idb']} if exclusive else {}
    source = lambda index: dict(  # noqa: E731
        name=f's{index}', clock_domain='main', protocol='axi4', data_width=32, max_outstanding=4,
        **({'user_flags': {'src': index, 'idb': 0}} if exclusive else {}))
    target = dict(name='t1', clock_domain='main', protocol='axi4', data_width=32, id_width=2,
                  attributes='amba', max_outstanding=4,
                  mappings=[{'base': hex(BASE), 'size': '0x1000'}])
    if exclusive:
        target.update(exclusive='target', exclusive_id_flags=['src', 'idb'])
    cfg = endpoint_ports({
        'schema_version': 10, 'name': 'target_id_rule', 'address_width': 16,
        'transport': dict(response_codes='distinct', **flags),
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'initiators': [source(0), source(1)],
        'targets': [target,
                    dict(name='memoryTargetPort0', clock_domain='main', protocol='axi4', data_width=32,
                         id_width=3, attributes='amba', max_outstanding=4,
                         mappings=[{'base': '0x0', 'size': '0x1000'}])],
        'connectivity': 'all',
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)
    # A second mapping widens the observation region index without changing transaction IDs.
    if apertures > 1:
        cfg['targets'][0]['mappings'].append({'base': '0x2000', 'size': '0x1000'})
    cfg['targets'][0]['id_width'] = 2
    cfg['targets'][1]['id_width'] = 3
    return cfg


def case_for(exclusive=False, apertures=1):
    cfg = config(exclusive, apertures)
    targets = nlconfig.targets(cfg, cache="amba", prot=True)
    targets['t1']['lock'] = exclusive
    return dict(
        dut=dict(source="cli", config=cfg),
        bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
              dict(name="s1", kind="axi_master", endpoint="initiator:s1"),
              dict(name="t1", kind="axi_slave", endpoint="target:t1", salt=1),
              dict(name="memoryTargetPort0", kind="axi_slave", endpoint="target:memoryTargetPort0", salt=2)],
        model=dict(routes=nlconfig.routes(cfg), error_codes=True, targets=targets),
        scenario_args=dict(exclusive=exclusive, ids_only=apertures > 1),
        max_cycles=20000,
    )


def apb_case(bursts=1):
    """APB sources need no direction or sequence field, at any target capacity.
    The checker pairs target accesses by the compact source and target indices."""
    cfg = config()
    cfg['address_width'] = 16
    for index, source in enumerate(cfg['initiators']):
        cfg['initiators'][index] = dict(name=source['name'], clock_domain='main', protocol='apb4',
                                        data_width=32)
    cfg['targets'][0].update(max_outstanding=bursts)
    cfg['targets'][1] = dict(name='memoryTargetPort0', clock_domain='main', protocol='apb4', data_width=32,
                             attributes='amba', mappings=[{'base': '0x0', 'size': '0x1000'}])
    targets = nlconfig.targets(cfg, cache="amba", prot=True)
    targets['t1']['lock'] = False
    return dict(
        dut=dict(source="cli", config=cfg),
        bfms=[dict(name="s0", kind="apb_master", endpoint="initiator:s0", dialect=4),
              dict(name="s1", kind="apb_master", endpoint="initiator:s1", dialect=4),
              dict(name="t1", kind="axi_slave", endpoint="target:t1", salt=1),
              dict(name="memoryTargetPort0", kind="apb_slave", endpoint="target:memoryTargetPort0", dialect=4, salt=2)],
        model=dict(routes=nlconfig.routes(cfg), error_codes=True, targets=targets),
        scenario_args=dict(apb=True),
        max_cycles=20000,
    )


def service_case():
    """A service target takes a target number like any other: a third target beside t1 and
    memoryTargetPort0, reached only by a configuration source that adds to the source numbers and leaves
    three data windows. Its bank has no ports, so the bench has no target to drive for it."""
    cfg = config()
    cfg['address_width'] = 32
    cfg['targets'][0]['mappings'].append({'base': '0x30000', 'size': '0x1000'})
    cfg['initiators'].append(dict(name='control_bus', clock_domain='main', protocol='apb4', data_width=32))
    cfg['targets'].append(dict(name='controlRegisterPort', clock_domain='main', protocol='apb4',
                               data_width=32, address_width=16, attributes='amba',
                               mappings=[{'base': '0x800000', 'size': '0x10000'}]))
    cfg['connectivity'] = {'s0': ['t1', 'memoryTargetPort0'], 's1': ['t1', 'memoryTargetPort0'],
                           'control_bus': ['controlRegisterPort']}
    targets = nlconfig.targets(cfg, cache="amba", prot=True)
    targets['t1']['lock'] = False
    return dict(
        dut=dict(source="cli", config=cfg, service='controlRegisterPort'),
        bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
              dict(name="s1", kind="axi_master", endpoint="initiator:s1"),
              dict(name="control_bus", kind="apb_master", endpoint="initiator:control_bus", dialect=4),
              dict(name="t1", kind="axi_slave", endpoint="target:t1", salt=1),
              dict(name="memoryTargetPort0", kind="axi_slave", endpoint="target:memoryTargetPort0", salt=2)],
        model=dict(routes=nlconfig.routes(cfg), error_codes=True, targets=targets),
        scenario_args=dict(ids_only=True, bases=(BASE, 0x0, 0x30000)),
        max_cycles=20000,
    )


CASE = dict(
    name="target_id_rule",
    contracts=["AXI.TARGET.ID_RULE"],
    negative_faults_all=True,
    configs=[dict(label="plain", **case_for()),
             dict(label="apertures", **case_for(apertures=2), faults=[]),
             dict(label="register_window_ids", **service_case(), faults=[
                 dict(id="service_route_id", op="text", component="dut",
                      subs=[(r"^assign nl_t_t1_arid = (\S+);", r"assign nl_t_t1_arid = ~\1;", 1)],
                      expect=["TARGET_UNATTRIBUTED"], contract="AXI.TARGET.ID_RULE",
                      note="a read not carrying the sum of the numbers with the service target")]),
             dict(label="apb_several_bursts", **apb_case(4), faults=[]),
             dict(label="apb_no_direction", **apb_case(), faults=[
                 dict(id="apb_route_id", op="text", component="dut",
                      subs=[(r"^assign nl_t_t1_arid = (\S+);", r"assign nl_t_t1_arid = ~\1;", 1)],
                      expect=["TARGET_UNATTRIBUTED"], contract="AXI.TARGET.ID_RULE",
                      note="an APB read not carrying the sum without the direction bit")]),
             dict(label="exclusive", **case_for(True), faults=[
                 dict(id="owner_id", op="text", component="dut",
                      subs=[(r"^assign nl_t_t1_arid = (\S+);", r"assign nl_t_t1_arid = \1 ^ nl_t_t1_arlock;", 1)],
                      expect=["TARGET_UNATTRIBUTED"], contract="AXI.TARGET.ID_RULE",
                      note="an exclusive read not carrying its owner flags"),
                 dict(id="owner_wait", op="scenario", params=dict(flip="owner_wait"),
                      expect=["TAPE.EXPECT:OWNER_ORDER_WAIT"], contract="AXI.TARGET.ID_RULE",
                      note="the second access of one source and AxID issued while the first is out")])],
    faults=[dict(id="route_id", op="text", component="dut",
                 subs=[(r"^assign nl_t_t1_arid = (\S+);", r"assign nl_t_t1_arid = ~\1;", 1)],
                 expect=["TARGET_UNATTRIBUTED"], contract="AXI.TARGET.ID_RULE",
                 note="a read not carrying the sum"),
            dict(id="shared_overtakes", op="scenario", params=dict(flip="shared"),
                 expect=["TAPE.EXPECT:ORDER_SHARED_BEHIND"], contract="AXI.TARGET.ID_RULE",
                 note="a read or write of a shared ID overtaking the older one"),
            dict(id="apart_in_order", op="scenario", params=dict(flip="apart"),
                 expect=["TAPE.EXPECT:ORDER_APART_BEHIND"], contract="AXI.TARGET.ID_RULE",
                 note="reads and writes of different IDs not overtaking")],
)


def pair(t, kind, ids, label, overtakes, flip):
    """The first of s0 and the second of s1 reach t1 with responses held, then the target answers
    the youngest ready first: s1 finishes first only when the IDs differ."""
    issue = (lambda s, i, a: t.read(s, a, size=2, id=i)) if kind == "r" else \
        (lambda s, i, a: t.write(s, a, size=2, id=i))
    channel = "ar" if kind == "r" else "aw"
    done = "r" if kind == "r" else "b"
    t.gate("t1", done, False)
    target = t.snap("t1", channel)
    first, second = t.snap("s0", done), t.snap("s1", done)
    issue("s0", ids[0], BASE + 0x40)
    t.wait("t1", channel, "==", 1, since=target, label="ISSUE_FIRST")
    issue("s1", ids[1], BASE + 0x80)
    t.wait("t1", channel, "==", 2, since=target, label="ISSUE_SECOND")
    t.delay(8)
    t.gate("t1", done, True)
    # Reversed, the wanted winner really finishes last, so the other has finished already.
    ahead = ("s1", second) if overtakes != flip else ("s0", first)
    behind = ("s0", first) if overtakes != flip else ("s1", second)
    t.wait(ahead[0], done, "==", 1, limit=30, label=label, since=ahead[1])
    t.expect_now(behind[0], done, "==", 0, label + "_BEHIND", since=behind[1])
    t.drain(limit=500, label="drain")
    t.delay(8)


def scenario(t, seed, exclusive=False, flip=None, ids_only=False, apb=False, bases=(BASE,)):
    t.reset(4)
    t.delay(4)
    if apb:
        for source in ("s0", "s1"):
            t.read(source, BASE + 0x40, size=2)
            t.drain(limit=200, label="drain")
            t.write(source, BASE + 0x80, size=2)
            t.drain(limit=200, label="drain")
        t.end()
        return
    if ids_only:
        for base in bases:
            for source in ("s0", "s1"):
                for axid in range(4):
                    t.read(source, base + 0x40, size=2, id=axid)
                    t.write(source, base + 0x40, size=2, id=axid)
                    t.drain(limit=200, label="drain")
        t.end()
        return
    if exclusive:
        t.set("t1", "lock_ok", 1)
    for order in (1, 0):
        t.set("t1", "r_order", order)
        t.set("t1", "b_order", order)
        for kind in ("r", "w"):
            pair(t, kind, SHARED[kind], "ORDER_SHARED", False, flip == "shared" and order == 1)
            if order:
                pair(t, kind, APART[kind], "ORDER_APART", True, flip == "apart")
            else:
                # In order, nothing overtakes.
                pair(t, kind, APART[kind], "ORDER_KEPT", False, False)
    # A slot of a finished write takes a younger write of a shared ID: the B still completes the older.
    t.set("t1", "b_order", 0)
    t.set("t1", "b_lat", 20)
    first, second = t.snap("s0", "b"), t.snap("s1", "b")
    t.write("s0", BASE + 0x40, size=2, id=APART["w"][0])
    t.delay(12)
    t.write("s0", BASE + 0x80, size=2, id=SHARED["w"][0])
    t.wait("s0", "b", "==", 1, since=first, label="SLOT_FREED")
    t.write("s1", BASE + 0xc0, size=2, id=SHARED["w"][1])
    t.wait("s0", "b", "==", 2, limit=60, since=first, label="SLOT_REUSE_OLDER")
    t.expect_now("s1", "b", "==", 0, "SLOT_REUSE_YOUNGER", since=second)
    t.drain(limit=500, label="drain")
    t.set("t1", "b_lat", 0)
    if exclusive:
        # An access meeting an earlier one of its source and AxID, either exclusive, waits for it.
        for first, second in ((1, 0), (0, 1)):
            for kind, channel, done, issue in (("r", "ar", "r", t.read), ("w", "aw", "b", t.write)):
                t.gate("t1", done, False)
                target = t.snap("t1", channel)
                issue("s0", BASE + 0x40, size=2, id=1, lock=first)
                issue("s0", BASE + 0x80, size=2, id=1, lock=second)
                t.wait("t1", channel, "==", 1, since=target, label="WAIT_FIRST")
                t.delay(12)
                t.expect_now("t1", channel, "==", 2 if flip == "owner_wait" else 1, "OWNER_ORDER_WAIT",
                             since=target)
                t.gate("t1", done, True)
                t.drain(limit=500, label="drain")
                t.delay(8)
        # An exclusive read and write of each owner and a normal access: one target ID per owner.
        for source, axid in (("s0", 1), ("s0", 2), ("s1", 3)):
            t.read(source, BASE + 0x100, size=2, id=axid, lock=1)
            t.drain(limit=200, label="drain_exclusive")
            t.write(source, BASE + 0x100, size=2, id=axid, lock=1)
            t.drain(limit=200, label="drain_exclusive")
            t.read(source, BASE + 0x104, size=2, id=axid)
            t.drain(limit=200, label="drain_exclusive")
    t.end()
