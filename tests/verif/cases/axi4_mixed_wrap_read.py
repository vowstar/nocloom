"""Mixed-width sources retain their own narrow WRAP response shape."""

from nlverif import config as nlconfig


def companion(capacity):
    cfg = dict(
        schema_version=10, name=f"mixed_wrap_read_{capacity}", address_width=16,
        clock_gating=False, reset_ports=[dict(name="reset_n")],
        clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n")],
        initiators=[dict(name=name, clock_domain="main", protocol="axi4", data_width=bits,
                         id_width=2, len_width=8, max_outstanding=4)
                    for name, bits in (("s0", 64), ("s1", 32))],
        targets=[dict(name="t0", clock_domain="main", protocol="axi4", data_width=32,
                      id_width=3, len_width=8, max_outstanding=capacity, attributes="amba",
                      mappings=[dict(base="0x0", size="0x1000")])],
        connectivity="all", output=dict(backend="verilog", language="verilog_2001"))
    return dict(label=f"capacity{capacity}", dut=dict(source="cli", config=cfg),
                bfms=[dict(name=s, kind="axi_master", endpoint="initiator:" + s)
                      for s in ("s0", "s1")] +
                     [dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=7)],
                model=dict(routes=nlconfig.routes(cfg), keeps_decerr=True,
                           targets=nlconfig.targets(cfg, cache="amba", prot=True)))


CASE = dict(name="axi4_mixed_wrap_read", contracts=["AXI.OUTLET.FRAMING"],
            configs=[companion(k) for k in (1, 3, 4)], max_cycles=30000,
            negative_runs_all=True,
            faults=[dict(id="missing_last", op="text", contract="AXI.OUTLET.FRAMING",
                         subs=[(r"^assign nl_i_s0_rlast = .*;$",
                                "assign nl_i_s0_rlast = 1'b0;", 1)],
                         expect=["AXI.R.LAST", "SOURCE_R_LAST"])])


def scenario(t, seed):
    t.reset(8)
    t.delay(8)
    t.set("t0", "r_ilv", 1)
    t.ready("s0", "r", "modulo", 3, 0)
    for source in ("s0", "s1"):
        for size in (0, 1, 2):
            for beats in (2, 4, 8, 16):
                for offset in range(0, 4, 1 << size):
                    t.read(source, 0x100 + offset, size=size, beats=beats, burst="wrap",
                           id=1, cache=3)
                    t.read(source, 0x300, beats=2, id=1, cache=3)
                    t.drain(limit=1000, label="WRAP_AND_SAME_ID_FOLLOWER")
    for k in range(8):
        for source in ("s0", "s1"):
            t.read(source, 0x202, size=1, beats=16, burst="wrap", id=k % 4, cache=3)
    t.drain(limit=4000, label="MIXED_CONCURRENT")
    t.end()
