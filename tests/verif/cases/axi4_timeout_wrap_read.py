"""Aligned WRAP reads replay the start word when timeout completes a read."""

from nlverif import config as nlconfig


def companion(capacity):
    cfg = dict(
        schema_version=10, name=f"timeout_wrap_read_{capacity}", address_width=16,
        clock_gating=False, reset_ports=[dict(name="reset_n")],
        clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n")],
        initiators=[dict(name="s0", clock_domain="main", protocol="axi4", data_width=32,
                         id_width=2, len_width=4, max_outstanding=4)],
        targets=[dict(name="t0", clock_domain="main", protocol="axi4", data_width=32,
                      id_width=3, len_width=4, max_outstanding=capacity, attributes="amba",
                      timeout=dict(cycles=16, completion=True),
                      mappings=[dict(base="0x0", size="0x1000")])],
        connectivity="all", output=dict(backend="verilog", language="verilog_2001"))
    return dict(label=f"capacity{capacity}", dut=dict(source="cli", config=cfg),
                bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
                      dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=7,
                           sideband=("dut", "target:t0", "timed_out"))],
                model=dict(routes=nlconfig.routes(cfg), keeps_decerr=True,
                           targets=nlconfig.targets(cfg, error_data_zero=True)))


CASE = dict(name="axi4_timeout_wrap_read", contracts=["AXI.TIMEOUT.EXACT"],
            configs=[companion(k) for k in (1, 3, 4)], max_cycles=40000,
            negative_runs_all=True,
            faults=[dict(id="missing_last", op="text", contract="AXI.TIMEOUT.EXACT",
                         subs=[(r"^assign nl_i_s0_rlast = .*;$",
                                "assign nl_i_s0_rlast = 1'b0;", 1)],
                         expect=["AXI.R.LAST", "SOURCE_R_LAST"])])


def scenario(t, seed):
    for delivered in (0, 1, 2):
        t.reset(8)
        t.delay(8)
        t.set("t0", "r_stop", delivered + 1)
        # The start word's low three bytes are held until the WRAP tail.
        good = 0 if delivered == 0 else 4 * delivered - 3
        codes = [0] * good + [2] * (13 - good) + ([0] * 3 if delivered else [2] * 3)
        t.read("s0", 0x103, size=0, beats=16, burst="wrap", id=1, cache=3,
               beat_resps=codes, target="any", late_access=True)
        t.wait("t0", "sideband", "==", 1, limit=200, label="WRAP_EXPIRED")
        t.drain(limit=1000, label="WRAP_REPLAY_COMPLETES")
        for size in (0, 1):
            for beats in (4, 8, 16):
                for offset in range(0, 4, 1 << size):
                    t.read("s0", 0x200 + offset, size=size, beats=beats, burst="wrap",
                           id=1, cache=3, resp=2, target="none")
                    t.read("s0", 0x300, id=1, resp=2, target="none")
                    t.drain(limit=500, label="FAILED_WRAP_AND_FOLLOWER")
        t.set("t0", "r_stop", 0)
        t.delay(32)
    t.reset(8)
    t.end()
