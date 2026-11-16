"""A failed AXI4 target consumes every W beat before returning one B."""

from nlverif import config as nlconfig


def configuration(capacity):
    return dict(
        schema_version=10, name=f"timeout_write_drain_{capacity}", address_width=16,
        clock_gating=False, reset_ports=[dict(name="reset_n")],
        clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n")],
        initiators=[dict(name="s0", clock_domain="main", protocol="axi4", data_width=32,
                         id_width=2, len_width=4, max_outstanding=4)],
        targets=[dict(name="t0", clock_domain="main", protocol="axi4", data_width=32,
                      id_width=3, len_width=4, max_outstanding=capacity, attributes="amba",
                      timeout=dict(cycles=16, completion=True),
                      mappings=[dict(base="0x0", size="0x1000")])],
        connectivity="all", output=dict(backend="verilog", language="verilog_2001"))


def companion(capacity):
    cfg = configuration(capacity)
    return dict(label=f"capacity{capacity}", dut=dict(source="cli", config=cfg),
                bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
                      dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=7,
                           sideband=("dut", "target:t0", "timed_out"))],
                model=dict(routes=nlconfig.routes(cfg), keeps_decerr=True,
                           targets=nlconfig.targets(cfg, error_data_zero=True)))


CASE = dict(name="axi4_timeout_write_drain", contracts=["AXI.TIMEOUT.EXACT"],
            configs=[companion(k) for k in (1, 3, 4)], max_cycles=10000,
            faults=[dict(id="error_as_okay", op="text", contract="AXI.TIMEOUT.EXACT",
                         subs=[(r"^assign nl_i_s0_bresp = .*;$",
                                "assign nl_i_s0_bresp = 2'd0;", 1)],
                         expect=["SOURCE_B_RESP"])])


def scenario(t, seed):
    t.reset(8)
    t.delay(8)
    # A completed single beat leaves the internal W stage marked last.
    t.write("s0", 0x100, id=1)
    t.drain(limit=200, label="PRIME_LAST")
    t.gate("t0", "r", False)
    t.read("s0", 0x200, resp=2, target="any", late_access=True)
    t.wait("t0", "sideband", "==", 1, limit=100, label="TARGET_FAILED")
    t.drain(limit=200, label="EXPIRY_ANSWER")
    t.set("s0", "w_gap", 7)
    for beats in (2, 4, 16):
        before = t.snap("s0", "b")
        t.write("s0", 0x300, beats=beats, id=1, resp=2, target="none")
        t.write("s0", 0x400, beats=3, id=1, resp=2, target="none")
        t.drain(limit=1000, label="ALL_W_BEFORE_B")
        t.expect_now("s0", "b", "==", 2, "ONE_B_PER_WRITE", since=before)
    t.delay(24)
    t.reset(8)
    t.end()
