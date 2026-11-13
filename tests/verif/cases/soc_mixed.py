"""Read and write every connected pair of the two-domain SoC example.

The address oracle is independent of the generated input and map. Full
64-bit AXI beats exercise both halves of the 32-bit targets. The AHB driver
uses non-bufferable SINGLE transfers, so completion includes the write result.
"""

from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[3] / "examples/soc_mixed.yaml"
WINDOWS = {
    "ram": (0x80000000, 0x10000000),
    "rom": (0x0, 0x10000),
    "uart": (0x40000000, 0x1000),
    "timer": (0x40001000, 0x1000),
    "gpio": (0x40002000, 0x1000),
}
CONNECTED = {
    "cpu": list(WINDOWS),
    "dma": ["ram", "uart"],
    "dbg": list(WINDOWS),
}
ROUTES = {s: [(t, *WINDOWS[t]) for t in targets] for s, targets in CONNECTED.items()}


def oracle(checker, events, sources, targets):
    """Each pair must complete a read and a write, including each 64-bit half."""
    errors = []
    widths = dict(cpu=64, dma=64, dbg=32, **{name: 32 for name in WINDOWS})
    for name, want_bits in widths.items():
        actual = checker.bench["bfms"][name]["data_bits"]
        if actual != want_bits:
            errors.append(dict(detail="endpoint data width", port=name,
                               got=actual, want=want_bits))
    covered = {(t.port, t.window[0], t.dir) for t in sources
               if t.done and t.window is not None}
    want = {(s, t, d) for s, names in CONNECTED.items() for t in names for d in ("w", "r")}
    if not want <= covered:
        errors.append(dict(detail="connected pair coverage", missing=sorted(want - covered)))
    writes = {}
    for t in sources:
        if t.port not in ("cpu", "dma") or t.window is None:
            continue
        address_events = [e for e in events if e.get("p") == t.port and e.get("k") == "hs"
                          and e.get("ch") == ("aw" if t.dir == "w" else "ar")
                          and e.get("tag") == t.tag]
        if (not t.done or t.size != 3 or t.length != 1 or len(t.beats) != 1
                or len(address_events) != 1 or address_events[0]["sz"] != 3
                or address_events[0]["len"] != 0):
            errors.append(dict(detail="eight-byte AXI transfer", port=t.port, tag=t.tag))
            continue
        data = t.beats[0][0]
        halves = [data & 0xFFFFFFFF, data >> 32]
        if halves[0] == halves[1] or not all(halves):
            errors.append(dict(detail="distinct nonzero AXI halves", port=t.port, tag=t.tag))
        if t.dir == "w" and t.beats[0][1] != 0xFF:
            errors.append(dict(detail="eight-byte write strobes", port=t.port, tag=t.tag))
        local = t.addr - WINDOWS[t.window[0]][0]
        words = sorted((tt.addr + 4 * i, beat[0]) for tt in t.targets
                       for i, beat in enumerate(tt.beats) if tt.size == 2)
        if words != [(local, halves[0]), (local + 4, halves[1])]:
            errors.append(dict(detail="two target halves", port=t.port, tag=t.tag,
                               got=words, want=[(local, halves[0]), (local + 4, halves[1])]))
        key = (t.port, t.window[0], t.addr)
        if t.dir == "w":
            writes[key] = data
        elif writes.get(key) != data:
            errors.append(dict(detail="read back both written halves", port=t.port, tag=t.tag))
    return errors


CASE = dict(
    name="soc_mixed",
    contracts=["AXI.ROUTE.LOCAL_OFFSET", "AXI.WIDTH.DOWNSIZE.PIECES", "APB.TARGET.ACCESS",
               "AXI.CDC.CROSSING"],
    dut=dict(source="cli", path=str(EXAMPLE)),
    bfms=[dict(name=s, kind="ahb_master" if s == "dbg" else "axi_master",
               endpoint=f"initiator:{s}", clock="core") for s in CONNECTED]
    + [dict(name=t, kind="axi_slave" if t == "ram" else
            "ahb_slave" if t == "rom" else "apb_slave", endpoint=f"target:{t}",
            clock="core" if t in ("ram", "rom") else "periph",
            dialect=3 if t == "gpio" else 4, salt=i) for i, t in enumerate(WINDOWS)],
    clocks=dict(core=10.0, periph=17.0),
    main_clock="core",
    model=dict(routes=ROUTES, error_codes=True, targets={
        "ram": dict(kind="axi", bus_bytes=4),
        "rom": dict(kind="ahb", bus_bytes=4, engine_bytes=4, attributes="amba"),
        "uart": dict(kind="apb", bus_bytes=4, engine_bytes=4, attributes="amba"),
        "timer": dict(kind="apb", bus_bytes=4, engine_bytes=4, attributes="amba"),
        "gpio": dict(kind="apb", bus_bytes=4, engine_bytes=4, dialect=3),
    }),
    oracle=oracle,
    max_cycles=40000,
    faults=[dict(id=f"{source}_read_upper_zero", op="text", component="dut",
                 contract="AXI.WIDTH.DOWNSIZE.PIECES", expect=["SOURCE_R_DATA"],
                 subs=[(rf"^(assign nl_i_{source}_rdata = )([^;]+);$",
                        r"\g<1>(\g<2> & 64'h00000000ffffffff);", 1)])
            for source in ("cpu", "dma")]
    + [dict(id="dma_write_lower_zero", op="text", component="dut",
            contract="AXI.WIDTH.DOWNSIZE.PIECES", expect=["TARGET_WRITE_BYTES"],
            subs=[(r"^(assign \w+ = )(nl_i_dma_wdata);$",
                   r"\g<1>(\g<2> & 64'hffffffff00000000);", 1)])],
)


def scenario(t, seed):
    for s in ("cpu", "dma"):
        t.ready(s, "b", "modulo", 3, 1)
        t.ready(s, "r", "modulo", 4, 2)
    for name in ("uart", "timer", "gpio", "rom"):
        t.set(name, "wait", 2)
    t.ready("ram", "aw", "modulo", 3, 1)
    t.ready("ram", "w", "modulo", 4, 2)
    t.ready("ram", "ar", "modulo", 3, 2)
    t.reset(8)
    t.delay(20)
    for index, (source, names) in enumerate(CONNECTED.items()):
        for target in names:
            address = WINDOWS[target][0] + 0x100 + index * 0x100
            t.write(source, address, prot=2, cache=0)
            t.drain(limit=3000, label=f"{source}_{target}_WRITE")
            t.read(source, address, prot=2, cache=0)
            t.drain(limit=3000, label=f"{source}_{target}_READ")
    t.end()
