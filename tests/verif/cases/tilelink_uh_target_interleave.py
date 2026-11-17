"""AXI4 traffic of several IDs at a TL-UH target with packet contexts."""


def configuration(width=128, capacity=3, maximum=1024, other=False, default="t"):
    """One AXI4 initiator and a TL-UH target. `other` adds an AXI4 target.

    `default` names the default target, or is None for the automatic choice.
    """
    initiator = dict(name="s", clock_domain="main", protocol="axi4", data_width=width,
                     id_width=5, len_width=8, max_outstanding=32)
    if default:
        initiator["default_target"] = default
    targets = [dict(name="t", clock_domain="main", protocol="tl_uh", data_width=32,
                    size_width=4, max_transfer_bytes=maximum, max_outstanding=capacity,
                    read_side_effects=True, mappings=[dict(base="0x0", size="0x10000")])]
    if other:
        targets.append(dict(name="q", clock_domain="main", protocol="axi4", data_width=32,
                            id_width=4, len_width=8, attributes="amba", max_outstanding=4,
                            mappings=[dict(base="0x20000", size="0x1000")]))
    return dict(schema_version=10, name="uh_interleave", address_width=32, clock_gating=False,
                reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
                clock_domains=[dict(name="main", clock_port="clk", reset_port="reset_n",
                                    test_port="test")],
                initiators=[initiator], targets=targets, connectivity="all",
                output=dict(backend="verilog", language="verilog_2001"))


CASE = dict(contracts=["TL.ACCESS"], dut=dict(source="cli", config=configuration()))
