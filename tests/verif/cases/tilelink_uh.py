"""TileLink UH ingress message limits and transaction contract."""
def configuration(maximum, source_width, slots):
    return dict(schema_version=10, name="uh_ingress", address_width=32,
                reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
                clock_domains=[dict(name="main", clock_port="clk", reset_port="reset_n", test_port="test")],
                initiators=[dict(name="s", clock_domain="main", protocol="tl_uh", data_width=32,
                                 source_width=source_width, size_width=4,
                                 max_transfer_bytes=maximum, max_outstanding=slots)],
                targets=[dict(name="t", clock_domain="main", protocol="axi4", data_width=32,
                              id_width=4, len_width=8, max_outstanding=4, attributes="amba",
                              mappings=[dict(base="0x0", size="0x10000")])],
                connectivity="all", output=dict(backend="verilog", language="verilog_2001"))


CASE = dict(contracts=["TL.ACCESS"], dut=dict(source="cli", config=configuration(1024, 4, 4)))
