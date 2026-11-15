"""Packet based TL-UH target configuration and byte-footprint contract."""

def configuration(maximum):
    return dict(schema_version=10, name="uh_target", address_width=32,
                clock_gating=False, reset_ports=[dict(name="reset_n")],
                test_ports=[dict(name="test")],
                clock_domains=[dict(name="main", clock_port="clk",
                                    reset_port="reset_n", test_port="test")],
                initiators=[dict(name="s", clock_domain="main", protocol="axi4",
                                 data_width=32, id_width=4, len_width=8, default_target="t")],
                targets=[dict(name="t", clock_domain="main", protocol="tl_uh",
                              data_width=32, size_width=4, max_transfer_bytes=maximum,
                              read_side_effects=True, fixed_bursts="single",
                              timeout=dict(cycles=16, completion=False),
                              mappings=[dict(base="0x0", size="0x10000")]),
                         dict(name="q", clock_domain="main", protocol="axi4",
                              data_width=32, id_width=4, len_width=8, attributes="amba",
                              mappings=[dict(base="0x20000", size="0x1000")])],
                connectivity="all", output=dict(backend="verilog", language="verilog_2001"))


CASE = dict(contracts=["TL.ACCESS"], dut=dict(source="cli", config=configuration(1024)))
