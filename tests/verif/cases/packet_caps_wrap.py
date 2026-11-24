"""A pair packet limit or boundary below an initiator's largest WRAP holds at the target."""

from nlverif import config as nlconfig

# (initiator bits, limit field, limit bytes); every target is 32 bits wide.
LIMITS = ((256, "max_bytes", 64), (256, "boundary_bytes", 64), (128, "max_bytes", 64),
          (128, "boundary_bytes", 64), (64, "max_bytes", 64), (64, "boundary_bytes", 64),
          (32, "boundary_bytes", 16))


def span(t):
    """Bytes [lo, hi) of one target burst: a WRAP covers its container."""
    beat = 1 << t.size
    if t.burst == 2:
        lo = t.addr & ~(t.length * beat - 1)
        return lo, lo + t.length * beat
    lo = t.addr >> t.size << t.size
    return lo, lo + t.length * beat


def within(field, limit, align):
    """Every target burst ends within the limit from its alignment-word start, or stays in one
    block of the boundary; some source WRAP is larger than the limit."""
    def oracle(checker, events, sources, targets):
        out = []
        for t in targets:
            lo, hi = span(t)
            if field == "max_bytes":
                bad = hi - (t.addr // align * align) > limit
            else:
                bad = lo // limit != (hi - 1) // limit
            if bad:
                out.append(dict(detail=f"target burst breaks {field} {limit}", port=t.port,
                                addr=hex(t.addr), beats=t.length, size=t.size, burst=t.burst))
        if not any(s.burst == 2 and s.length << s.size > limit for s in sources):
            out.append(dict(detail=f"no WRAP above {field} {limit}"))
        return out[:8]
    return oracle


def companion(bits, field, limit):
    name = f"packet_caps_wrap_{bits}_{field.split('_')[0]}"
    cfg = dict(
        schema_version=10, name=name, address_width=16, clock_gating=False,
        reset_ports=[dict(name="reset_n")],
        clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n")],
        initiators=[dict(name="s0", clock_domain="main", protocol="axi4", data_width=bits,
                         len_width=8, id_width=2, max_outstanding=4)],
        targets=[dict(name="t0", clock_domain="main", protocol="axi4", data_width=32,
                      len_width=6, id_width=2, max_outstanding=4, attributes="amba",
                      mappings=[dict(base="0x0", size="0x4000")])],
        transport=dict(data_width=32,
                       packet_limits=[dict(source="s0", target="t0", **{field: limit})]),
        connectivity="all", output=dict(backend="verilog", language="verilog_2001"))
    return dict(label=f"{bits}_{field}", dut=dict(source="cli", config=cfg),
                bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
                      dict(name="t0", kind="axi_slave", endpoint="target:t0", salt=7)],
                model=dict(routes=nlconfig.routes(cfg), keeps_decerr=True,
                           targets=nlconfig.targets(cfg, error_data_zero=True)),
                oracle=within(field, limit, max(bits // 8, 4)),
                scenario_args=dict(bits=bits))


CASE = dict(name="packet_caps_wrap", contracts=["AXI.WIDTH.ENDPOINT"],
            configs=[companion(*limit) for limit in LIMITS], max_cycles=60000)


def scenario(t, seed, bits=256):
    t.reset(8)
    t.delay(8)
    top = (bits // 8).bit_length() - 1
    tag = 0
    # WRAP of every length and width from a container base and from its middle, then a long
    # INCR from an unaligned start. Each address block is used once.
    for size in sorted({top, max(top - 1, 0), 2}):
        for beats in (2, 4, 8, 16):
            container = beats << size
            for offset in (0, container // 2 + (1 << size)):
                base = 0x200 * tag
                tag += 1
                start = base + offset % container
                t.write("s0", start, size=size, beats=beats, burst="wrap", id=tag % 4, cache=2)
                t.read("s0", start, size=size, beats=beats, burst="wrap", id=tag % 4, cache=2)
                t.drain(limit=4000, label="WRAP")
    t.write("s0", 0x3c04, size=top, beats=8, id=1, cache=2)
    t.read("s0", 0x3c04, size=top, beats=8, id=2, cache=2)
    t.drain(limit=4000, label="INCR")
    t.end()
