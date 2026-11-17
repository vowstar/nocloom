"""A read beat gathered from several blocks returns zero data when one block fails.

A target with read side effects takes an unaligned first beat as naturally
aligned blocks. The target fails the reads of one address range, so a beat
can hold a failed block beside good ones. That beat must report the failure
and carry no data. Reads cover every start offset of the beats around the
failed range, full and narrow sizes, one to three beats, with AxCACHE 0
(blocks) and 2 (one packet).

Configurations: a 128-bit source with four bursts in flight and a 32-bit
AXI4 target, a 64-bit source and a 32-bit AHB target, and a 32-bit source
and a 32-bit AXI4 target whose failed range is one byte address inside a
word. Each must reach a minimum count of first beats that hold a failed
block beside a good one."""

from nlverif import config as nlconfig

BASE = 0x240


def companion(label, bits, protocol, low, high, reach, outstanding=1):
    source = dict(name="s0", clock_domain="main", protocol="axi4", data_width=bits, id_width=2,
                  len_width=8)
    if outstanding > 1:
        source["max_outstanding"] = outstanding
    target = dict(name="t0", clock_domain="main", protocol=protocol, data_width=32,
                  attributes="amba", read_side_effects=True,
                  mappings=[dict(base="0x0", size="0x1000")])
    if protocol == "axi4":
        target.update(id_width=2, len_width=8)
    cfg = dict(schema_version=10, name="read_block_error_" + label, address_width=16,
               clock_gating=False, reset_ports=[dict(name="reset_n")],
               clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n")],
               initiators=[source], targets=[target], connectivity="all",
               output=dict(backend="verilog", language="verilog_2001"))
    axi = protocol == "axi4"
    return dict(label=label, dut=dict(source="cli", config=cfg),
                bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
                      dict(name="t0", kind="axi_slave" if axi else "ahb_slave",
                           endpoint="target:t0", salt=5)],
                model=dict(routes=nlconfig.routes(cfg), keeps_decerr=nlconfig.keeps_decerr(cfg),
                           targets=nlconfig.targets(cfg, error_data_zero=True)),
                scenario_args=dict(bits=bits, low=low, high=high, axi=axi),
                oracle=gathered_failures(reach))


def gathered_failures(reach):
    """At least `reach` first beats hold a failed block beside a good one."""
    def oracle(checker, events, sources, targets):
        mixed = 0
        for s in sources:
            if s.dir != "r" or not s.done or s.cancelled:
                continue
            end = s.addr - s.addr % (1 << s.size) + (1 << s.size)
            first = [t.resps[0] for t in s.targets if s.addr // 4 * 4 <= t.addr < end and t.resps]
            mixed += len(first) > 1 and 0 in first and 2 in first
        return [] if mixed >= reach else [
            dict(detail="too few beats with a failed block beside a good one", got=mixed,
                 want=reach)]
    return oracle


CASE = dict(
    name="read_block_error", contracts=["AXI.ERR.READ_ZERO"],
    configs=[companion("wide", 128, "axi4", BASE + 4, BASE + 8, 30, outstanding=4),
             companion("ahb", 64, "ahb_lite", BASE + 4, BASE + 8, 24),
             companion("word", 32, "axi4", BASE + 1, BASE + 2, 3)],
    max_cycles=400000, negative_runs_all=True,
    faults=[dict(id="failed_beat_data", op="text", contract="AXI.ERR.READ_ZERO",
                 subs=[(r"^(assign nl_i_s0_rdata = )([^;]+);$",
                        r"\g<1>(\g<2>) | {127'd0, nl_i_s0_rresp[1]};", 1)],
                 expect=["SOURCE_R_DATA"])])


def scenario(t, seed, bits, low, high, axi):
    step = bits // 8
    top = step.bit_length() - 1
    t.set("t0", "reg0_lo", low)
    t.set("t0", "reg0_hi", high)
    t.set("t0", "reg0_r" if axi else "reg0_c", 2 if axi else 1)
    if axi:
        t.set("t0", "r_rate", 70)
    t.ready("s0", "r", "modulo", 3, 0)
    t.reset(8)
    t.delay(8)
    count = 0
    for size in sorted({top, max(top - 1, 0)}):
        for cache in (0, 2):
            for start in range(BASE - step, BASE + 2 * step):
                for beats in (1, 2, 3):
                    t.read("s0", start, size=size, beats=beats, id=count % 4, cache=cache)
                    count += 1
                    t.drain(limit=2000, label="READ")
    t.end()
