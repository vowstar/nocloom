"""A two-flow AXI4 target with a folded ID or an ID carrying only the flow.

The addresses select mappings whose flow order differs from their source address order.
The mappings also give each flow a distinct target-local range. Check the low bit against
that range even when an ID fault prevents the checker from assigning the request an owner.
Check the full ID against the map model using the initiating flow and source address.
"""

from nlverif import config as nlconfig
from target_id_model import target_id


def configuration(width):
    return dict(
        schema_version=10, name="target_id_flows", address_width=16,
        reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
        clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n",
                            test_port="test")],
        initiators=[dict(name="s0", clock_domain="main", protocol="axi4", data_width=32,
                         id_width=2, len_width=8, max_outstanding=4,
                         route_classes=["first", "second"], default_class="first",
                         id_ranges=[{"first": 1, "last": 1, "class": "second"}])],
        targets=[dict(name="t0", clock_domain="main", protocol="axi4", data_width=32,
                      attributes="amba", id_width=width, len_width=8, flows=2, max_outstanding=4,
                      mappings=[dict(base="0x1000", size="0x1000", local_address="0x1000", flow=1),
                                dict(base="0x3000", size="0x1000", flow=0)])],
        connectivity="all",
        output=dict(backend="verilog", language="verilog_2001"))


def flow_at(addr, field):
    """The configured flow at a source or target address in this fixture."""
    for mapping in CFG["targets"][0]["mappings"]:
        base = nlconfig.number(mapping.get(field, 0))
        if base <= addr < base + nlconfig.number(mapping["size"]):
            return mapping["flow"]
    return None


def exact_id(checker, events, sources, targets):
    numbers = checker.bench["target_ids"]
    for burst in targets:
        flow = flow_at(burst.addr, "local_address")
        if burst.id & 1 != flow:
            yield dict(check="flow", addr=burst.addr, id=burst.id, want_flow=flow)
        owner = burst.owner
        if owner is not None:
            # Only AxID 1 uses the second source flow.
            expected = target_id(numbers, owner.port, burst.port, owner.id, write=owner.dir == "w",
                                 flow=flow_at(owner.addr, "base"), source_path=int(owner.id == 1))
            if burst.id != expected:
                yield dict(check="id", addr=owner.addr, id=burst.id, want_id=expected)


def flip_read_flow():
    # Undo the bit flip on RID to keep the DUT's response matching intact.
    return dict(id="read_flow_flipped", op="text", component="dut",
                subs=[(r"^assign nl_t_t0_arid = (\S+);",
                       r"assign nl_t_t0_arid = \1 ^ 1'b1;", 1),
                      (r"^assign (\w+) = nl_t_t0_rid;",
                       r"assign \1 = nl_t_t0_rid ^ 1'b1;", 1)],
                expect=["ORACLE"], contract="AXI.TARGET.ID_RULE",
                note="ARID carries the other valid target flow")


def reverse_read_sequence():
    return dict(id="read_sequence_reversed", op="text", component="dut",
                subs=[(r"^assign nl_t_t0_arid = (\S+);",
                       r"wire [2:0] nl_fault_arid = \1;" "\n"
                       r"assign nl_t_t0_arid = {nl_fault_arid[1], nl_fault_arid[2], nl_fault_arid[0]};",
                       1)],
                expect=["TARGET_UNATTRIBUTED"], contract="AXI.TARGET.ID_RULE",
                note="ARID carries the sequence bits in reverse order")


WIDTH = int(PARAMS.get("width", 3))
CFG = configuration(WIDTH)
CASE = dict(
    name="target_id_flows",
    contracts=["AXI.TARGET.ID_RULE"],
    dut=dict(source="cli", config=CFG),
    bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
          dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=1)],
    model=dict(routes=nlconfig.routes(CFG), keeps_decerr=True,
               targets=nlconfig.targets(CFG, cache="amba", prot=True)),
    oracle=exact_id,
    faults=[flip_read_flow()] + ([reverse_read_sequence()] if WIDTH == 3 else []),
    max_cycles=2000,
)


def scenario(t, seed):
    t.reset(8)
    t.delay(4)
    for mapping in CFG["targets"][0]["mappings"]:
        base = nlconfig.number(mapping["base"])
        for axid in range(4):
            addr = base + 0x40 + 4 * axid
            t.write("s0", addr, id=axid)
            t.drain(limit=200, label="WRITE")
            t.read("s0", addr, id=axid)
            t.drain(limit=200, label="READ")
    t.end()
