"""Each shared-target WRAP read retains its source beat count and byte order."""

import random

from nlverif import config as nlconfig


def companion(mask, timeout, capacity=3, bits=32):
    sources = [("a3", "axi3", bits), ("a4", "axi4", bits), ("wide", "axi4", bits * 2)]
    sources = [s for k, s in enumerate(sources) if mask & (1 << k)]
    cfg = dict(
        schema_version=10, name=f"shared_wrap_{mask}_{int(timeout)}_{capacity}_{bits}",
        address_width=16, clock_gating=False, reset_ports=[dict(name="reset_n")],
        clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n")],
        transport=dict(data_width=32),
        initiators=[dict(name=name, clock_domain="main", protocol=protocol, data_width=width,
                         id_width=2, len_width=4, max_outstanding=4)
                    for name, protocol, width in sources],
        targets=[dict(name="t0", clock_domain="main", protocol="axi4", data_width=bits,
                      id_width=4, len_width=8, max_outstanding=capacity, attributes="amba",
                      mappings=[dict(base="0x1000", size="0x4000")])],
        connectivity="all", output=dict(backend="verilog", language="verilog_2001"))
    if timeout:
        cfg["targets"][0]["timeout"] = dict(cycles=64, completion=True)
    return dict(label=cfg["name"], dut=dict(source="cli", config=cfg),
                bfms=[dict(name=s[0], kind="axi_master", endpoint="initiator:" + s[0])
                      for s in sources] +
                     [dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=7,
                           **(dict(sideband=("dut", "target:t0", "timed_out"))
                              if timeout else {}))],
                scenario_args=dict(sources=[(s[0], s[2]) for s in sources], timeout=timeout),
                model=dict(routes=nlconfig.routes(cfg), keeps_decerr=True,
                           targets=nlconfig.targets(cfg, cache="amba", prot=True,
                                                    error_data_zero=True)))


matrix = PARAMS.get("matrix", "0") == "1"
bits = int(PARAMS.get("bits", 32))
masks = range(1, 4 if bits == 1024 else 8)
configs = ([companion(mask, timeout, bits=bits) for mask in masks
            for timeout in (False, True)] if matrix else
           [companion(3, False, capacity=1, bits=bits), companion(7, False, bits=bits),
            companion(7, True, capacity=4, bits=bits)])
CASE = dict(name="axi3_shared_wrap_read", contracts=["AXI.OUTLET.FRAMING"],
            configs=configs, seeds=[17, 43, 91],
            # Wider ports serialize more network words per burst.
            max_cycles=100000 * max(1, bits // 128),
            negative_runs_all=True,
            faults=[dict(id="missing_last", op="text", contract="AXI.OUTLET.FRAMING",
                         subs=[(r"^assign nl_i_a3_rlast = .*;$",
                                "assign nl_i_a3_rlast = 1'b0;", 1)],
                         expect=["AXI.R.LAST", "SOURCE_R_LAST"])])


def scenario(t, seed, sources, timeout):
    rng = random.Random(seed)
    t.reset(8)
    t.delay(8)
    # A single AXI3 request with every other source idle checks the shared outlet.
    if any(name == "a3" for name, _ in sources):
        t.read("a3", 0x1002, size=1, beats=4, burst="wrap", id=0)
        t.drain(limit=1000, label="AXI3_WRAP_SINGLE")
    t.set("t0", "seed", seed)
    t.set("t0", "r_ilv", 1)
    for name, width in sources:
        t.set(name, "seed", seed + width)
        t.ready(name, "r", "random", 67, 0)
        shapes = [(size, beats, offset)
                  for size in range((width // 8).bit_length() - 1)
                  for beats in (2, 4, 8, 16)
                  for offset in range(0, beats << size, 1 << size)]
        rng.shuffle(shapes)
        for k, (size, beats, offset) in enumerate(shapes):
            t.read(name, 0x2000 + offset, size=size, beats=beats, burst="wrap",
                   id=k % 4, cache=3)
            t.read(name, 0x3000, beats=2, id=k % 4, cache=3)
            t.drain(limit=2000, label="WRAP_AND_SAME_ID_FOLLOWER")
    for k in range(12):
        for name, _ in sources:
            t.read(name, 0x4002, size=1, beats=16, burst="wrap", id=k % 4, cache=3)
    t.drain(limit=4000, label="SHARED_CONCURRENT_WRAP")
    if timeout:
        for name, width in sources:
            for size in range((width // 8).bit_length() - 1):
                for beats in (2, 4, 8, 16):
                    for offset in range(0, beats << size, 1 << size):
                        t.reset(8)
                        t.delay(8)
                        t.gate("t0", "r", False)
                        t.read(name, 0x2000 + offset, size=size, beats=beats, burst="wrap",
                               id=1, cache=3, resp=2, target="any", late_access=True)
                        t.wait("t0", "sideband", "==", 1, limit=200, label="WRAP_EXPIRED")
                        t.drain(limit=1000, label="FAILED_WRAP_COMPLETES")
                        t.gate("t0", "r", True)
                        t.delay(32)
        t.reset(8)
    t.delay(20)
    t.end()
