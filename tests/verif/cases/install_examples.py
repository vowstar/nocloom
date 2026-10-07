"""The installed package against the shipped examples (one installation).



- tree_topology: the tree helper compiled against the installed Core
  component gives the same four artifacts as the explicit definition.
- api_<example>: the feature-definition consumer runs against the relocated
  Core component for mode mappings, explicit routes and flow-pair connectivity,
  QoS, AxQOS, ID-table capacity and WRAP alignment; its artifacts equal the
  installed CLI's byte for byte.
- tree_<mode> (apb, axi, apb16, axi64): examples/generate_tree.cpp built
  against the installed Core and the installed CLI on examples/tree_<mode>.yaml
  give byte-identical artifacts; the map, bundle and RTL ports pass the
  literal oracle (tree_example_checks.py) and four metadata corruptions fail
  it with their named cause; the RTL passes Verilog-2001 syntax, a Yosys
  synthesis check and the verif stack.
- cross_tree_<mode> (apb, axi): the clocked tree consumer and the CLI agree
  byte for byte; the clock placement oracle passes and two metadata
  corruptions fail it; the RTL passes the verif stack, which also samples
  both reset release chains the placement oracle found.
- basic_<example> (apb_simple, apb_shared, axi_lite_apb, apb_domains): the
  installed CLI's RTL passes the verif stack.

The verif stack (install.Context.verify) drives the installed CLI's RTL with
the APB and AXI4-Lite BFMs on each endpoint's domain clock and judges the
event log with the offline checker against an independent endpoint table
(literal windows, below). The tape of every example: each source, one at a
time, writes every strobe mask (every lane, alternating lanes, zero and full
on 64 bits) and reads offsets 0, 1, 0x38 and the last byte of each window,
with PPROT/AxPROT stepping through 0 to 7 (AXI4-Lite writes alternate AW
before W and W before AW); the word at 0x38 of every target answers SLVERR;
unmapped reads and writes answer SLVERR without a target access; then every
source at once (the sources must overlap); then a read held at the last
target is cut by the common reset and a fresh read completes alone. Target
wait states differ per target. Negative control: four text faults on the
port assignments (target write data, source read data, source error
response, target address) must each be caught; on the clocked trees also
a release chain one stage longer.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from install import cancel_and_resume, concurrent, controls, endpoints, error_word  # noqa: E402
from install import port_faults  # noqa: E402

from cross_tree_example_checks import check as cross_check  # noqa: E402
from tree_example_checks import check as tree_check  # noqa: E402
from tree_example_checks import specification  # noqa: E402


def table(name, sources, targets, domains=("main",)):
    """An independent endpoint table: sources (name, protocol, width, domain), targets
    (name, protocol, width, base, size, domain)."""
    return dict(
        schema_version=10, name=name, address_width=32,
        clock_domains=[dict(name=n) for n in domains],
        initiators=[dict(name=n, protocol=p, data_width=w, clock_domain=c)
                    for n, p, w, c in sources],
        targets=[dict(name=n, protocol=p, data_width=w, clock_domain=c, attributes="amba",
                      mappings=[dict(base=hex(b), size=hex(z))])
                 for n, p, w, b, z, c in targets],
        connectivity="all")


def tree_table(mode):
    bits, source, target, _, windows = specification(mode)
    return table("tree_" + mode, [(n, source, bits, "main") for n in ("cpu", "dma")],
                 [(f"bank{i:02}", target, bits, b, z, "main")
                  for i, (b, z) in enumerate(windows)])


CROSS = {mode: table("cross_tree_" + mode,
                     [(h, "apb4" if mode == "apb" else "axi4_lite", 32, "alpha")
                      for h in ("host0", "host1")],
                     [("bank0", "apb4", 32, 0, 4096, "alpha"),
                      ("bank1", "apb4", 32, 4096, 4096, "beta")], ("alpha", "beta"))
         for mode in ("apb", "axi")}
TWO = [("bank0", "apb4", 32, 0, 4096), ("bank1", "apb4", 32, 4096, 4096)]
BASIC = {
    "apb_simple": table("apb_simple", [("control", "apb4", 32, "main")], [
        ("registers", "apb4", 32, 0x1000, 0x100, "main"),
        ("counters", "apb4", 32, 0x2000, 0x100, "main"),
        ("scratch", "apb4", 32, 0xfffff000, 0x1000, "main")]),
    "apb_shared": table("apb_shared", [(n, "apb4", 32, "main") for n in ("cpu", "dma")],
                        [t + ("main",) for t in TWO]),
    "axi_lite_apb": table("axi_lite_apb", [(n, "axi4_lite", 32, "main") for n in ("cpu", "dma")],
                          [t + ("main",) for t in TWO]),
    "apb_domains": table("apb_domains", [(n, "apb4", 32, "source") for n in ("cpu", "dma")],
                         [t + ("target",) for t in TWO], ("source", "target")),
}
UNMAPPED = dict(tree=(0x3000, 0x80000001), cross=(0x2000, 0x80000001),
                apb_simple=(0, 0x1190, 0x80000001), basic=(0x2000, 0x80000001))
# Domain clocks: the first at 10 ns; the second at 14 ns with a phase that never meets an
# edge of the first.
CLOCKS = dict(alpha=10.0, beta=dict(period=14.0, phase=2.5), main=10.0, source=10.0,
              target=dict(period=14.0, phase=2.5))


def traffic(cfg, unmapped):
    """The scenario of one example table (module docstring)."""
    stack = endpoints(cfg)
    kinds = {b["name"]: b["kind"] for b in stack["bfms"]}

    def scenario(t, seed):
        for k, target in enumerate(cfg["targets"]):
            name = target["name"]
            error_word(t, name, kinds[name])
            if kinds[name] == "apb_slave":
                t.set(name, "wait", k % 3)
            else:
                for channel in ("aw", "w", "ar"):
                    t.ready(name, channel, "delay", k % 3)
        t.reset(10)
        t.delay(16)
        case = 0

        def step(source, write, address, mask=None):
            nonlocal case
            t.mark("case", case)
            lite = kinds[source] == "axi_master"
            if write:
                if lite and case % 2:
                    t.gate(source, "aw", False)
                t.strobes(source, [mask])
                t.write(source, address, prot=case % 8, strobe="table")
                if lite and case % 2:
                    t.delay(4)
                    t.gate(source, "aw", True)
            else:
                t.read(source, address, prot=case % 8)
            t.drain(limit=4000, label=f"CASE_{case}")
            case += 1

        for s in cfg["initiators"]:
            lanes = s["data_width"] // 8
            full = (1 << lanes) - 1
            masks = (list(range(1 << lanes)) if lanes <= 4
                     else [0, 0xFF, 0x55, 0xAA] + [1 << b for b in range(8)])
            for target in cfg["targets"]:
                base, size = (int(target["mappings"][0][k], 0) for k in ("base", "size"))
                for mask in masks:
                    step(s["name"], 1, base + 0x80, mask)
                for offset in (0, 1, 0x38, size - 1):
                    step(s["name"], 0, base + offset)
                step(s["name"], 1, base + 0x38, full)
                step(s["name"], 1, base + size - 1, full)
            for address in unmapped:
                step(s["name"], 0, address)
                step(s["name"], 1, address, full)
        # Every source at once, each on another target.
        t.mark("phase", 1)
        targets = cfg["targets"]
        for i, s in enumerate(cfg["initiators"]):
            base = int(targets[i % len(targets)]["mappings"][0]["base"], 0)
            t.write(s["name"], base + 0x100 + 4 * i, prot=i)
            t.read(s["name"], base + 0x100 + 4 * i, prot=i)
        t.drain(limit=4000, label="CONCURRENT")
        last = targets[-1]
        cancel_and_resume(t, cfg["initiators"][0]["name"], last["name"], kinds[last["name"]],
                          int(last["mappings"][0]["base"], 0))
        t.end()
    return stack, scenario


def stack_case(cfg, unmapped, out, main="main", extra=None):
    """The verif case of one example on the installed CLI's artifacts in `out`."""
    stack, scenario = traffic(cfg, unmapped)
    first = next(b for b in stack["bfms"] if b["kind"].endswith("master"))
    last = stack["bfms"][-1]
    case = dict(name=cfg["name"], dut=dict(source="generated", dir=str(out)),
                bfms=stack["bfms"], model=stack["model"], main_clock=main,
                clocks={d["name"]: CLOCKS[d["name"]] for d in cfg["clock_domains"]},
                max_cycles=200000,
                faults=port_faults(first["name"], first["kind"], last["name"], last["kind"],
                                   cfg["targets"][-1]["data_width"]))
    if len(cfg["initiators"]) > 1:
        case["oracle"] = concurrent()
    case.update(extra or {})
    return case, scenario


def both(*oracles):
    def oracle(checker, events, sources, targets):
        return [x for o in oracles for x in o(checker, events, sources, targets)]
    return oracle


def rises(spec, after, count):
    """Time (ps) of the count-th rising edge of a bench clock (period in ns, or
    {period, phase}) strictly after `after` (ps)."""
    spec = spec if isinstance(spec, dict) else dict(period=spec)
    period, first = spec["period"] * 1000, (spec.get("phase", 0) + spec["period"] / 2) * 1000
    k = max(0, int((after - first) // period) + 1)
    while first + k * period <= after:
        k += 1
    return first + (k + count - 1) * period


def release(clocks, stages):
    """A case oracle on the release chain probes (one net per domain, sampled on the
    main clock while the common reset is released): each release is low until the
    stages-th rising edge of its domain clock after the reset release, then high."""
    def oracle(checker, events, sources, targets):
        group = next(g for g in checker.bench["probes"] if g["name"] == "release")
        out, released, rows = [], None, 0
        for e in events:
            if e.get("k") == "rst" and e.get("p") is None:
                released = e["t"] if e.get("v") == 1 else None
            elif e.get("k") == "pr" and e.get("p") == group["port"] and released is not None:
                row, rows = checker._unpack(group, e), rows + 1
                for domain, _ in group["fields"]:
                    want = int(e["t"] > rises(clocks[domain], released, stages))
                    if row is None or row[domain] != want:
                        out.append(dict(detail="release", domain=domain, t=e["t"],
                                        released=released, want=want))
        return out[:8] if rows else [dict(detail="no release samples")]
    return oracle


def cross_case(mode, releases, out):
    """The verif case of a clocked tree: its release chains (cell, net per domain, from the
    placement oracle) sampled every main clock and judged by their stage count."""
    nets = [[domain, releases[domain][5], 1] for domain in ("alpha", "beta")]
    probes = dict(groups=lambda _: [dict(name="release", kind="release", nets=nets)])
    case, scenario = stack_case(CROSS[mode], UNMAPPED["cross"], out, main="alpha",
                                extra=dict(map_probes=probes))
    case["oracle"] = both(case["oracle"], release(case["clocks"], 2))
    # A release chain one stage longer releases its domain an edge late.
    case["faults"].append(dict(id="release_stage", op="text", expect=["ORACLE"], subs=[(
        r"^(nocloom_synchronizer #\(\.STAGES\()2(\)\) " + releases["beta"][1] + r" )",
        r"\g<1>3\g<2>", 1)]))
    return case, scenario


def tree_topology(ctx, d):
    exe = ctx.consumer("tree_topology", {"main.cpp": "tests/tree_topology_consumer.cpp",
                                         "tree_topology_fixture.hpp":
                                         "tests/tree_topology_fixture.hpp"})
    assert (ctx.prefix / "include/nocloom/topology.hpp").is_file(), "TOPOLOGY_HEADER"
    out = ctx.run([exe, d / "artifacts"], "consumer", cwd=d).stdout
    assert "TREE_TOPOLOGY_CORE_PASS" in out, "TREE_TOPOLOGY_CORE_PASS"
    rows = []
    for artifact in sorted((d / "artifacts/helper").iterdir()):
        other = d / "artifacts/explicit" / artifact.name
        assert artifact.read_bytes() == other.read_bytes(), "HELPER_EXPLICIT " + artifact.name
        rows.append(artifact.name)
    assert len(rows) == 6, "ARTIFACT_COUNT"
    return dict(identical=rows)


API_FEATURES = ("mode_remap", "explicit_routes_conn_flows", "axi4_qos_profiles",
                "axi4_axqos_map", "axi4_id_table", "axi4_wrap_alignment")


def api_feature(example):
    def entry(ctx, d):
        exe = ctx.consumer("feature_definitions",
                           {"main.cpp": "examples/generate_feature_definitions.cpp"})
        cli = ctx.generate(ctx.source / f"examples/{example}.yaml", d / "cli")
        out = ctx.run([exe, example, d / "api"], "api-generate", cwd=d).stdout
        assert "API_FEATURE_CORE_PASS" in out, "API_FEATURE_CORE_PASS"
        api = {p.name: p.read_bytes() for p in (d / "api").iterdir()}
        assert {p.name: p.read_bytes() for p in (d / "cli").iterdir()} == api, \
            "CLI_CORE_BYTES"
        return dict(artifacts=cli)
    return entry


def get(value, path):
    for key in path:
        value = value[key]
    return value


def setter(path, value):
    def mutate(record):
        get(record, path[:-1])[path[-1]] = value
    return mutate


def tree(mode):
    def entry(ctx, d):
        exe = ctx.consumer("tree", {"main.cpp": "examples/generate_tree.cpp"})
        cli = ctx.generate(ctx.source / f"examples/tree_{mode}.yaml", d / "cli")
        ctx.run([exe, mode, d / "api"], "api-generate", cwd=d)
        api = {p.name: p.read_bytes() for p in (d / "api").iterdir()}
        assert {p.name: p.read_bytes() for p in (d / "cli").iterdir()} == api, \
            "ARTIFACT_DETERMINISM.REPEAT"
        module = "nocloom_control_tree_" + mode
        mapping = json.loads((d / "cli" / (module + ".map.json")).read_text())
        bundle = json.loads((d / "cli" / (module + ".bundle.json")).read_text())
        rtl = (d / "cli" / (module + ".v")).read_text()
        tree_check(mapping, bundle, rtl, mode, ctx.version)
        witnesses = controls(lambda m: tree_check(m, bundle, rtl, mode, ctx.version), mapping, [
            ("windows", setter(("definition", "targets", 0, "begin"), "1"), "WINDOWS"),
            ("routes", setter(("network", "request_routes", 0, "hardware_nodes"), []), "ROUTES"),
            ("rtl_port", setter(("ports", 2, "width"), 99), "RTL_PORT"),
            ("exits", setter(("definition", "topology", "response", "exits", 0, "node"),
                             "leaf"), "EXITS")])
        sources = bundle["synthesis_sources"]
        ctx.run(["iverilog", "-g2001", "-s", module, "-o", "syntax", *sources],
                "verilog2001", cwd=d / "cli")
        ctx.run(["yosys", "-p", f"read_verilog {' '.join(sources)}; hierarchy -check -top {module}; "
                 f"synth -top {module}; check -assert; stat"], "synthesis", cwd=d / "cli")
        case, scenario = stack_case(tree_table(mode), UNMAPPED["tree"], d / "cli")
        verif = ctx.verify(d / "verif", case, scenario)
        return dict(artifacts=cli, controls=witnesses, verif=verif)
    return entry


def cross_tree(mode):
    def entry(ctx, d):
        exe = ctx.consumer("cross_tree", {"main.cpp": "tests/cross_tree_example_consumer.cpp",
                                          "endpoint_ports.hpp": "tests/endpoint_ports.hpp"})
        cli = ctx.generate(ctx.source / f"examples/cross_tree_{mode}.yaml", d / "cli")
        ctx.run([exe, mode, d / "api"], "api-generate", cwd=d)
        api = {p.name: p.read_bytes() for p in (d / "api").iterdir()}
        assert {p.name: p.read_bytes() for p in (d / "cli").iterdir()} == api, "CLI_CORE_BYTES"
        module = "nocloom_cross_tree_" + mode
        mapping = json.loads((d / "cli" / f"{module}.map.json").read_text())
        bundle = json.loads((d / "cli" / f"{module}.bundle.json").read_text())
        rtl = (d / "cli" / f"{module}.v").read_text()
        releases = cross_check(mapping, bundle, rtl, mode)

        def drop_sync(m):
            m["network"]["cdc"]["cuts"][0]["write_count_sync"].pop()
        witnesses = controls(lambda m: cross_check(m, bundle, rtl, mode), mapping, [
            ("sync_stage", drop_sync, None),
            ("constraint_target", setter(("network", "cdc", "cuts", 0, "obligations", 0,
                                          "destination"), "value/absent"), None)])
        case, scenario = cross_case(mode, releases, d / "cli")
        verif = ctx.verify(d / "verif", case, scenario)
        return dict(artifacts=cli, controls=witnesses, verif=verif)
    return entry


def basic(example):
    def entry(ctx, d):
        cli = ctx.generate(ctx.source / f"examples/{example}.yaml", d / "generated")
        cfg = BASIC[example]
        case, scenario = stack_case(cfg, UNMAPPED.get(example, UNMAPPED["basic"]),
                                    d / "generated", main=cfg["clock_domains"][0]["name"])
        return dict(artifacts=cli, verif=ctx.verify(d / "verif", case, scenario))
    return entry


ENTRIES = ([("tree_topology", tree_topology)]
           + [(f"api_{e}", api_feature(e)) for e in API_FEATURES]
           + [(f"tree_{m}", tree(m)) for m in ("apb", "axi", "apb16", "axi64")]
           + [(f"cross_tree_{m}", cross_tree(m)) for m in ("apb", "axi")]
           + [(f"basic_{e}", basic(e))
              for e in ("apb_simple", "apb_shared", "axi_lite_apb", "apb_domains")])
assert len({n for n, _ in ENTRIES}) == len(ENTRIES) and not [
    n for n, _ in ENTRIES if not re.fullmatch(r"[a-z0-9_]+", n)]
