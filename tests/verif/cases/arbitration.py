"""Schema 10 networks under each arbitration policy: AXI4 transactions and a
per-cycle replay of every arbitrated output.

Each network comes from the public CLI; the fixture recipe "arbitration"
builds the same definition byte for byte and names the nets of each
arbitrated switch output, which the bench logs every cycle. The offline
checker replays each log through tests/treadle_model.py under the policy
this file states for that output (independent of the generator: a per-output
override, else the written default, else round robin) and
requires every cycle to match. Controls: every policy other than those
configured disagrees with some output's log, each per-output override with
its own output's log (fixed priority also against the reversed order), and
enough contended decisions happened. The qos network has fixed levels (s0
writes 1, reads 7; s1 writes 7, reads 1): the highest valid level is
eligible, the policy chooses among it, and a level decision happens in each
graph.

Traffic: every source keeps write and read streams on AXI IDs 0 and 1 to
random targets with random backpressure, then (not in qos) directed
episodes: each source reads from every target at once with RREADY held low,
so its response output decides among several waiting packets after each
release. The checker judges the transactions end to end."""

import random

from nlverif import config as nlconfig

ENDPOINTS = {"hub": (["s0", "s1", "s2"], ["t0", "t1", "t2"]),
             "funnel": (["s0", "s1"], ["t0", "t1"]),
             "funnel_graph": (["s0", "s1"], ["t0", "t1"])}
NETWORKS = ["hub-auto", "hub-round_robin", "hub-fifo", "hub-rotating_priority", "hub-mixed", "funnel-auto",
            "funnel-round_robin", "funnel-rotating_priority", "funnel_graph-mixed", "qos"]
# Per-output overrides of the mixed networks (graph:node:output).
OVERRIDES = {
    "hub": {"request:hub:endpoint/t0": ("fixed_priority",
                                        ["endpoint/s2", "endpoint/s0", "endpoint/s1"]),
            "request:hub:endpoint/t1": ("rotating_priority", None),
            "response:back:endpoint/s1": ("fixed_priority",
                                          ["endpoint/t2", "endpoint/t0", "endpoint/t1"]),
            "response:back:endpoint/s2": ("rotating_priority", None)},
    "funnel_graph": {"request:m:endpoint/t0": ("fixed_priority", ["node/b", "node/a"]),
                     "request:m:endpoint/t1": ("rotating_priority", None)},
}
OUTPUTS = {"hub": 6, "funnel": 4, "funnel_graph": 4}
LEVELS = {0: 1, 1: 7, 2: 7, 3: 1}


class Network:
    def __init__(self, name):
        self.name = name
        self.qos = name == "qos"
        self.shape, self.policy = ("funnel_graph", "fifo") if self.qos else name.split("-", 1)
        self.sources, self.targets = ENDPOINTS[self.shape]
        self.transfers = 48 if self.qos else 300
        self.episodes = 0 if self.qos else 30
        self.config = self.configuration()

    def overrides(self):
        return OVERRIDES.get(self.shape, {}) if self.policy == "mixed" else {}

    def override_list(self, graph):
        """The topology arbitration entries of a mixed network for one graph."""
        out = []
        for key, (policy, order) in self.overrides().items():
            g, _, output = key.split(":")
            if g == graph:
                out.append(dict(output=output, policy=policy,
                                **({"priority": order} if order else {})))
        return out

    def node(self, name, graph, arbitrated):
        entries = self.override_list(graph) if arbitrated else []
        return dict(name=name, **({"arbitration": entries} if entries else {}))

    def configuration(self):
        cfg = dict(schema_version=10, name=f"arbitration_{self.shape}", address_width=32,
                   reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")], clock_domains=[dict(name="main", clock_port="clk_main", reset_port="reset_n", test_port="test")])
        if self.policy not in ("auto", "mixed"):
            cfg["arbitration"] = dict(policy=self.policy)
        cfg["initiators"] = [dict(name=s, clock_domain="main", protocol="axi4", data_width=32,
                                  id_width=2, len_width=8) for s in self.sources]
        cfg["targets"] = [dict(name=t, clock_domain="main", protocol="axi4", data_width=32,
                               len_width=8, attributes="amba",
                               mappings=[dict(base=hex(i * 0x1000), size="0x1000")])
                          for i, t in enumerate(self.targets)]
        if self.qos:
            for i, s in enumerate(cfg["initiators"]):
                s["qos"] = dict(levels=dict(write=LEVELS[2 * i], read=LEVELS[2 * i + 1]))
            for t in cfg["targets"]:
                t["id_width"] = 2
            cfg["transport"] = dict(max_outstanding=1)
        else:
            for t in cfg["targets"]:
                t["max_outstanding"] = 2
            cfg["transport"] = dict(max_outstanding=4)
        cfg["connectivity"] = "all"
        s, t = self.sources, self.targets
        if self.shape == "hub":
            cfg["topology"] = dict(
                request=dict(nodes=[self.node("hub", "request", True)], links=[],
                             entries=[dict(endpoint=x, node="hub") for x in s],
                             exits=[dict(endpoint=x, node="hub") for x in t]),
                response=dict(nodes=[self.node("back", "response", True)], links=[],
                              entries=[dict(endpoint=x, node="back") for x in t],
                              exits=[dict(endpoint=x, node="back") for x in s]))
        elif self.shape == "funnel_graph":
            if self.qos:
                response = dict(nodes=[dict(name="n"), dict(name="x")],
                                links=[{"from": "x", "to": "n"}],
                                entries=[dict(endpoint=x, node="x") for x in t],
                                exits=[dict(endpoint=x, node="n") for x in s])
            else:
                response = dict(nodes=[dict(name="x"), dict(name="y"), dict(name="n")],
                                links=[{"from": "x", "to": "n"}, {"from": "y", "to": "n"}],
                                entries=[dict(endpoint="t0", node="x"),
                                         dict(endpoint="t1", node="y")],
                                exits=[dict(endpoint=x, node="n") for x in s])
            cfg["topology"] = dict(
                request=dict(nodes=[dict(name="a"), dict(name="b"),
                                    self.node("m", "request", True)],
                             links=[{"from": "a", "to": "m"}, {"from": "b", "to": "m"}],
                             entries=[dict(endpoint="s0", node="a"),
                                      dict(endpoint="s1", node="b")],
                             exits=[dict(endpoint=x, node="m") for x in t]),
                response=response)
        cfg["output"] = dict(backend="verilog", language="verilog_2001")
        return cfg

    def case(self):
        """The run.py configuration of this network."""
        checks = dict(policy_rule=dict(
            default=self.policy if self.policy not in ("auto", "mixed") else "auto",
            overrides=self.overrides()), outputs=3 if self.qos else OUTPUTS[self.shape])
        if self.qos:
            checks.update(flow_levels=LEVELS, reach=("contended",))
        else:
            checks.update(controls=True, min_contended=100 if self.shape == "hub" else 15,
                          overrides=sorted(self.overrides()))
        recipe = dict(recipe="arbitration", params=dict(
            shape=self.shape, policy=self.policy, qos="fixed" if self.qos else "off"))
        return dict(
            label=self.name, dut=dict(source="cli", config=self.config), fault_recipe=recipe,
            probe_checks=checks, scenario_args=dict(network=self.name),
            bfms=[dict(name=x, kind="axi_master", endpoint=f"initiator:{x}")
                  for x in self.sources]
            + [dict(name=x, kind="axi_slave", endpoint=f"target:{x}", salt=i)
               for i, x in enumerate(self.targets)],
            model=dict(routes=nlconfig.routes(self.config), error_codes=nlconfig.error_codes(self.config),
                       targets=nlconfig.targets(self.config, cache="amba", prot=True)))


CASE = dict(
    name="arbitration",
    contracts=["AXI.ARB.POLICY", "AXI.QOS.LEVEL"],
    probes=dict(instance={"network": "dut"}),
    configs=[Network(name).case() for name in NETWORKS],
    seed=20261002,
    max_cycles=400000,
    run_timeout=600,
    # On the first network (hub, round robin everywhere): the cursor of
    # one output never updates, and the grant of that
    # output is not held through a packet. Checks also require the other
    # arbitration policies to disagree on these traffic patterns.
    faults=[
        dict(id="rr_cursor_frozen", component="network", op="set_next",
             at={"binding": "graph/req/req_node0>endpoint/t0.cursor"},
             **{"with": {"self": True}}, expect=["PROBE_GRANT"], contract="AXI.ARB.POLICY"),
        dict(id="grant_not_held", component="network", op="replace",
             at={"binding": "graph/req/req_node0>endpoint/t0.held"},
             **{"with": {"const": 0}}, expect=["PROBE_GRANT"], contract="AXI.ARB.POLICY"),
    ],
)
CASE.update({k: v for k, v in CASE["configs"][0].items() if k != "label"})


def scenario(t, seed, network="hub-auto"):
    net = Network(network)
    rnd = random.Random(seed)
    for k, s in enumerate(net.sources):
        t.set(s, "seed", 7 * seed + k + 1)
        t.ready(s, "b", "random", 75)
        t.ready(s, "r", "random", 75)
        t.set(s, "vrate_w", 50)
    for k, tg in enumerate(net.targets):
        t.set(tg, "seed", 11 * seed + k + 1)
        for ch in ("aw", "w", "ar"):
            t.ready(tg, ch, "random", 75)
        t.set(tg, "b_rate", 33)
        t.set(tg, "r_rate", 67)
    t.reset(4)
    t.delay(2)
    if net.qos:
        qos_contention(t, net)
    # Random phase: write and read streams on IDs 0 and 1 of every source to
    # random targets, pushed eight at a time so the queues stay short.
    t.mark("phase", 1)
    nt = len(net.targets)
    for step in range(0, net.transfers, 8):
        for si, s in enumerate(net.sources):
            for k in range(step, min(step + 8, net.transfers)):
                ident = k % 2
                beats = rnd.randrange(8) + 1
                t.write(s, 0x1000 * rnd.randrange(nt) + 0x400 * si + 0x100 * ident
                        + 4 * rnd.randrange(64 - beats + 1), beats=beats, id=ident,
                        strobe="mix")
                beats = rnd.randrange(16) + 1
                t.read(s, 0x1000 * rnd.randrange(nt) + 0x400 * si + 0x200 + 0x100 * ident
                       + 4 * rnd.randrange(64 - beats + 1), beats=beats, id=ident)
        for s in net.sources:
            t.wait(s, "w_out", "<=", 8, limit=100000, label="RANDOM_PHASE")
            t.wait(s, "r_out", "<=", 8, limit=100000, label="RANDOM_PHASE")
    t.drain(limit=200000, label="RANDOM_PHASE")
    # Directed episodes: each source reads from every target at once with
    # RREADY low, so its response output decides among waiting packets.
    t.mark("phase", 2)
    for _ in range(net.episodes):
        for si, s in enumerate(net.sources):
            t.ready(s, "r", "never")
            first = rnd.randrange(nt)
            for r in range(nt + rnd.randrange(2)):
                i = (first + r) % nt
                beats = 4 + rnd.randrange(12)
                t.read(s, 0x1000 * i + 0x400 * si + 0x200 + 0x100 * (i % 2)
                       + 4 * rnd.randrange(64 - beats + 1), beats=beats, id=i)
        t.delay(150 + rnd.randrange(50))
        for s in net.sources:
            t.ready(s, "r", "random", 75)
        t.drain(limit=100000, label="EPISODE")
    t.end()


def qos_contention(t, net):
    """The first writes of both sources start together on one target (an
    unheld level decision after equal-depth request links); then writes to
    different targets whose B responses contend at the shared response
    output; the same for reads."""
    t.mark("phase", 0)
    for direction in ("w", "r"):
        channel, counter = ("b", "applied") if direction == "w" else ("r", "ar")
        for tg in net.targets:
            t.gate(tg, channel, False)
        for si, s in enumerate(net.sources):
            if direction == "w":
                t.write(s, 0x400 * si, beats=1, id=0)
            else:
                t.read(s, 0x400 * si + 0x200, beats=1, id=0)
        for tg in net.targets:
            t.gate(tg, channel, True)
        t.drain(limit=10000, label="QOS_FIRST")
        for tg in net.targets:
            t.gate(tg, channel, False)
        marks = {tg: t.snap(tg, counter) for tg in net.targets}
        for si, s in enumerate(net.sources):
            if direction == "w":
                t.write(s, 0x1000 * si + 0x400 * si, beats=1, id=1)
            else:
                t.read(s, 0x1000 * si + 0x400 * si + 0x200, beats=1, id=1)
        for tg in net.targets:
            t.wait(tg, counter, ">=", 1, since=marks[tg], limit=10000, label="QOS_BOTH_TARGETS")
        t.delay(4)
        for tg in net.targets:
            t.gate(tg, channel, True)
        t.drain(limit=10000, label="QOS_SECOND")
