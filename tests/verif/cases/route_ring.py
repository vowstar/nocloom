"""Multihop routes around one-way rings that no route cycle closes
(docs/network.md#route-cycle-report).

Four AXI4 sources and four AXI4 targets attach to a ring of four switches,
endpoint i at switch i. A ring direction with every pair connected has one
cycle of waits. Each configuration leaves out every contributor of the wait
of n0 -> n1 for n1 -> n2, so the definition is accepted while routes still
take up to three links:

  both      request ring and response ring (the main configuration)
  request   request ring, one response switch
  response  one request switch, response ring

Every connected pair writes and reads three bursts of 8 to 16 beats. All
sources queue their bursts of every pair at once, two in flight each. Every
target accepts AW, W and AR at random and every source accepts B and R at
random, so bursts wait on the links they share. The checker judges target
bytes, addresses, IDs, response codes, read data and completion.

Designated fault: zero_read_data (the read data of s0 held at zero)."""

from nlverif import config as nlconfig

SIZE = 4
ALL = {(s, t) for s in range(SIZE) for t in range(SIZE)}
# The contributors of n0 -> n1 waits for n1 -> n2: the routes over n0, n1 and n2 in turn.
REQUEST_CONTRIBUTORS = {(0, 2), (0, 3), (3, 2)}
RESPONSE_CONTRIBUTORS = {(2, 0), (3, 0), (2, 3)}
PAIRS = {
    "both": ALL - REQUEST_CONTRIBUTORS - RESPONSE_CONTRIBUTORS,
    "request": ALL - REQUEST_CONTRIBUTORS,
    "response": ALL - RESPONSE_CONTRIBUTORS,
}


def graph(ring, response, size=SIZE):
    """One direction: the ring n0 -> n1 -> ... -> n0, or the switch hub."""
    node = (lambda i: f"n{i}") if ring else (lambda i: "hub")
    sources = [{"endpoint": f"s{i}", "node": node(i)} for i in range(size)]
    targets = [{"endpoint": f"t{i}", "node": node(i)} for i in range(size)]
    return {"nodes": [{"name": name} for name in sorted({node(i) for i in range(size)})],
            "links": [{"from": node(i), "to": node((i + 1) % size)} for i in range(size)]
            if ring else [],
            "entries": targets if response else sources,
            "exits": sources if response else targets}


def initiator(name, **fields):
    return {"name": name, "clock_domain": "main", "protocol": "axi4", "data_width": 32,
            "len_width": 8, "id_width": 2, "max_outstanding": 2, **fields}


def target(name, index, **fields):
    return {"name": name, "clock_domain": "main", "protocol": "axi4", "data_width": 32,
            "len_width": 8, "id_width": 8, "max_outstanding": 2, "attributes": "amba",
            "mappings": [{"base": hex(0x10000 * index), "size": "0x10000"}], **fields}


def definition(initiators, targets, connectivity, request, response, routes=None):
    topology = {"request": request, "response": response}
    if routes:
        topology["routes"] = routes
    return {
        "schema_version": 10, "name": "ring", "address_width": 32,
        "arbitration": {"policy": "round_robin"},
        "reset_ports": [{"name": "reset_n"}], "test_ports": [{"name": "test"}],
        "clock_domains": [{"name": "main", "clock_port": "clk_main", "reset_port": "reset_n",
                           "test_port": "test"}],
        "initiators": initiators, "targets": targets, "connectivity": connectivity,
        "topology": topology, "output": {"backend": "verilog", "language": "verilog_2001"}}


def configuration(pairs, request_ring=True, response_ring=True, size=SIZE):
    """Endpoint i at switch i of each ring; pairs holds the connected (source, target) indices."""
    connectivity = {f"s{s}": [f"t{t}" for t in range(size) if (s, t) in pairs]
                    for s in range(size)}
    return definition([initiator(f"s{i}") for i in range(size)],
                      [target(f"t{i}", i) for i in range(size)], connectivity,
                      graph(request_ring, False, size), graph(response_ring, True, size))


def config_of(name):
    cfg = configuration(PAIRS[name], name != "response", name != "request")
    return dict(label="" if name == "both" else name, dut=dict(source="cli", config=cfg),
                model=dict(routes=nlconfig.routes(cfg), keeps_decerr=nlconfig.keeps_decerr(cfg),
                           targets=nlconfig.targets(cfg, cache="amba", prot=True)),
                scenario_args=dict(pairs=sorted(PAIRS[name])))


def zero_read_data(rtl, mapping):
    return [(r"^(\s*assign nl_i_s0_rdata = )[^;]+;", r"\g<1>32'd0;", 1)]


MAIN = config_of("both")
CASE = dict(
    name="route_ring",
    contracts=["AXI.ROUTE.LOCAL_OFFSET", "AXI.W.UNSTROBED_ZERO"],
    dut=MAIN["dut"], model=MAIN["model"], scenario_args=MAIN["scenario_args"],
    bfms=[dict(name=f"s{i}", kind="axi_master", endpoint=f"initiator:s{i}") for i in range(SIZE)]
    + [dict(name=f"t{i}", kind="axi_slave", endpoint=f"target:t{i}", salt=i + 1)
       for i in range(SIZE)],
    configs=[config_of(name) for name in PAIRS],
    negative_runs_all=True,
    max_cycles=60000,
    faults=[dict(id="zero_read_data", op="text", subs=zero_read_data,
                 contract="AXI.ROUTE.LOCAL_OFFSET", expect=["SOURCE_R_DATA"])],
)


def scenario(t, seed, pairs=()):
    pairs = sorted(tuple(p) for p in pairs)
    t.reset(4)
    t.delay(2)
    for i in range(SIZE):
        for channel, percent in (("aw", 40), ("w", 50), ("ar", 40)):
            t.ready(f"t{i}", channel, "random", percent)
        for channel in ("b", "r"):
            t.ready(f"s{i}", channel, "random", 50)
    # Disjoint bytes for each pair keep the concurrent writes independent.
    for k in range(3):
        for s, target in pairs:
            t.write(f"s{s}", 0x10000 * target + 0x400 * s + 0x80 * k, beats=8 + 4 * k,
                    id=(s + target + k) % 4, cache=3, strobe="sparse")
        t.drain(limit=20000, label="RING_WRITES")
        for s, target in pairs:
            t.read(f"s{s}", 0x10000 * target + 0x400 * s + 0x80 * k, beats=8 + 4 * k,
                   id=(s + target + k) % 4, cache=3)
        t.drain(limit=20000, label="RING_READS")
    # Writes and reads of different pairs at once.
    for k, (s, target) in enumerate(pairs):
        address = 0x10000 * target + 0x400 * s + 0x200
        if k % 2:
            t.write(f"s{s}", address, beats=4, id=k % 4, cache=3)
        else:
            t.read(f"s{s}", address, beats=4, id=k % 4, cache=3)
    t.drain(limit=20000, label="RING_MIXED")
    t.end()
