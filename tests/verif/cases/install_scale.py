"""The installed package against the schema 10 scale examples of one
protocol (--param protocol=apb|axi), every shape: 12 and 32 sources on a
balanced 16-node tree, 64 sources on a 32-node chain to one target (64h1),
on a balanced tree to 64 targets (64d64), 256 sources on the root of a
balanced 64-node tree to 256 targets (256), and 32 sources and 32 targets
each in a clock domain of their own on a balanced 16-node tree whose nodes
run in endpoint domains (c64: 64 domains).

Per shape: the installed CLI on examples/scale_<protocol>_<shape>.yaml (the
256 shape, not shipped, on its definition), the
installed Core consumer examples/generate_large_tree.cpp (direct, through a
plan, from permuted definitions) and the CLI on a permuted YAML give five
byte-identical artifact sets; dropping or changing one artifact fails that
comparison (controls); seven invalid definitions are rejected with their codes
and no output; the map passes the independent route and domain oracle
(scale_example_checks.py); the RTL passes Verilog-2001 syntax and the verif
stack on the selected simulator engine (install.Context.verify: the APB or AXI4-Lite
initiator BFM of every source and the APB target BFM of every target, each on
its domain clock, judged by the offline checker against the scale
configuration's windows).

The tape: every source at once writes (strobe mask source index mod 16) and
reads its own word of target source mod targets, and the sources must
overlap; source 0 reads every target no source reached, and (with two
transfers in flight) targets 64, 128, 192 and the last back to back; the
word at 0x38 of the last target answers SLVERR to a read and a write; a raw
read of the last byte of the last window; an unmapped read and write answer
SLVERR at APB sources and DECERR at AXI sources without access; a read held
at bank00 is cut by the common reset and a fresh read completes alone.
Every target must see an access. Negative
control: every shape runs an oracle control (the tape expects OKAY for the
unmapped read; the checker must report the response), and the 12 shape four
text faults on its port assignments (target write data, source read data,
source error response, target address) that must each be caught."""

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from install import cancel_and_resume, concurrent, controls, digests, endpoints  # noqa: E402
from install import error_word, port_faults  # noqa: E402

from scale_example_checks import verify  # noqa: E402
from scale_example_config import configuration  # noqa: E402

PARAMS = globals().get("PARAMS", {})
PROTOCOL = PARAMS.get("protocol", "apb")
if PROTOCOL not in ("apb", "axi"):
    raise ValueError("protocol must be apb or axi")


def equivalent(outputs):
    assert len(outputs["cli"]) == 6 and all(
        v == outputs["cli"] for v in outputs.values()), "PUBLIC_ARTIFACT_EQUIVALENCE"


def extra_sources(c):
    c["initiators"].extend(
        dict(name=f"src{i:02}", clock_domain="main", protocol="apb4", data_width=32)
        for i in range(len(c["initiators"]), 257))


def placed(c, node):
    """A node of a clocked graph names a domain."""
    return dict(node, clock_domain="main") if len(c["clock_domains"]) > 1 else node


def extra_nodes(c):
    c["topology"]["request"]["nodes"].extend(
        placed(c, dict(name=f"node{i:02}"))
        for i in range(len(c["topology"]["request"]["nodes"]), 129))


def extra_domains(c):
    c["clock_domains"].extend(
        dict(name=f"extra{i:02}", clock_port=f"clk_extra{i:02}", reset_port="reset_n",
             test_port="test") for i in range(len(c["clock_domains"]), 65))


INVALID = [
    ("too_many_sources", extra_sources, "NCL_UNSUPPORTED_TOPOLOGY"),
    ("too_many_nodes", extra_nodes, "NCL_AXI4_TOPOLOGY"),
    ("invalid_endpoint_width", lambda c: c["initiators"][0].update(data_width=17),
     "NCL_ENDPOINT_DATA_WIDTH"),
    ("global_width", lambda c: c.update(data_width=64), "NCL_SCHEMA_UNKNOWN_FIELD"),
    ("unknown", lambda c: c["topology"].update(credit=1), "NCL_SCHEMA_UNKNOWN_FIELD"),
    ("unused_node",
     lambda c: c["topology"]["request"]["nodes"].append(placed(c, dict(name="unused"))),
     "NCL_SWITCH_UNUSED_OBJECT"),
    ("too_many_domains", extra_domains, "NCL_UNSUPPORTED_TOPOLOGY"),
]


def clocks(cfg):
    """Domain clocks: main at 10 ns (the tape's clock); with several domains each other
    domain has its own period and phase."""
    names = [d["name"] for d in cfg["clock_domains"]]
    return {n: 10.0 if n == "main" else dict(period=8.0 + i * 7 % 19, phase=round(i * 0.137, 3))
            for i, n in enumerate(names)}


def reached(checker, events, sources, targets):
    """A case oracle: every target saw an access."""
    seen = {t.port for t in targets}
    missing = [n for n, b in checker.bench["bfms"].items()
               if b["kind"] == "apb_slave" and n not in seen]
    return [dict(detail="targets never reached", targets=missing[:8])] if missing else []


def traffic(cfg):
    """The scenario of one scale configuration (module docstring)."""
    sources, targets = cfg["initiators"], cfg["targets"]
    ns, nt = len(sources), len(targets)
    base = [int(t["mappings"][0]["base"], 0) for t in targets]
    first = sources[0]["name"]
    settle = 16 if len(cfg["clock_domains"]) == 1 else 64

    def scenario(t, seed, oracle=False):
        for k, target in enumerate(targets):
            error_word(t, target["name"], "apb_slave")
            t.set(target["name"], "wait", k % 4)
        t.reset(10)
        t.delay(settle)
        t.mark("phase", 0)
        for i, s in enumerate(sources):
            address = base[i % nt] + 0x100 + 4 * i
            t.strobes(s["name"], [i % 16])
            t.write(s["name"], address, prot=i % 8, strobe="table")
            t.read(s["name"], address, prot=i % 8)
        t.drain(limit=100000, label="ALL_SOURCES")
        t.mark("phase", 1)
        for k in range(ns, nt):
            t.read(first, base[k] + 0x400)
        t.drain(limit=100000, label="REMAINING_TARGETS")
        if sources[0].get("max_outstanding", 1) > 1:
            # Several transfers in flight decode their destinations among every routing window.
            for k in (64, 128, 192, nt - 1):
                t.read(first, base[k] + 0x800)
            t.drain(limit=100000, label="IN_FLIGHT")
        t.mark("phase", 2)
        t.read(first, base[-1] + 0x38)
        t.write(first, base[-1] + 0x38)
        t.read(first, base[-1] + 4095)
        # The scale examples keep the default response codes: an unmapped access answers
        # DECERR at AXI sources and PSLVERR at APB sources (the control expects OKAY).
        t.read(first, 0x80000000, **(dict(resp=0) if oracle else {}))
        t.write(first, 0x80000000)
        t.drain(limit=100000, label="ERRORS")
        cancel_and_resume(t, first, targets[0]["name"], "apb_slave", base[0])
        t.end()
    return scenario


def verif_case(config, name, out):
    stack = endpoints(config, mem_bytes=4096)
    kind = stack["bfms"][0]["kind"]
    faults = [dict(id="oracle_unmapped_okay", op="scenario", params=dict(oracle=True),
                   expect=["SOURCE_R_RESP"])]
    if name == "12":
        faults += port_faults(config["initiators"][0]["name"], kind, "bank01", "apb_slave")
    big = name in ("c64", "256")

    def oracle(checker, events, sources, targets):
        return (concurrent()(checker, events, sources, targets)
                + reached(checker, events, sources, targets))
    return dict(name=config["name"], dut=dict(source="generated", dir=str(out)),
                bfms=stack["bfms"], model=stack["model"], clocks=clocks(config),
                oracle=oracle, max_cycles=600000, faults=faults, compile_timeout=1800,
                run_timeout=1800 if big else 600), traffic(config)


def shape(name):
    def entry(ctx, d):
        exe = ctx.consumer("scale", {"main.cpp": "examples/generate_large_tree.cpp"})
        config = configuration(PROTOCOL, name)
        module = "nocloom_" + config["name"]
        # The 256 shape is not shipped as an example: the CLI reads its definition dict.
        example = config if name == "256" else ctx.source / f"examples/scale_{PROTOCOL}_{name}.yaml"
        outputs = dict(cli=ctx.generate(example, d / "cli"))
        ctx.run([exe, PROTOCOL, name, d / "api"], "api-generate", cwd=d)
        for mode in ("plan", "permuted"):
            ctx.run([exe, PROTOCOL, name, d / mode, mode], mode, cwd=d)
        permuted = copy.deepcopy(config)
        permuted["initiators"].reverse()
        permuted["targets"].reverse()
        for graph in permuted["topology"].values():
            for rows in graph.values():
                rows.reverse()
        outputs["yaml-permuted"] = ctx.generate(permuted, d / "yaml-permuted", "yaml-permuted")
        for mode in ("api", "plan", "permuted"):
            outputs[mode] = digests(d / mode)
        equivalent(outputs)

        def drop(o):
            del o["api"][module + ".v"]

        def change(o):
            o["api"][module + ".v"] = "0" * 64
        witnesses = controls(equivalent, outputs, [
            ("missing_rtl", drop, "PUBLIC_ARTIFACT_EQUIVALENCE"),
            ("changed_byte", change, "PUBLIC_ARTIFACT_EQUIVALENCE")])
        rejected = []
        for label, edit, code in INVALID:
            invalid = copy.deepcopy(config)
            edit(invalid)
            path = d / (label + ".yaml")
            path.write_text(json.dumps(invalid) + "\n")
            r = ctx.run([ctx.cli, "generate", path, "--out-dir", d / label], label, cwd=d,
                        expected=1)
            assert code in r.stderr and not (d / label).exists(), f"REJECT {label}: {r.stderr}"
            rejected.append(label)
        directory = d / "cli"
        mapping = json.loads((directory / (module + ".map.json")).read_text())
        bundle = json.loads((directory / (module + ".bundle.json")).read_text())
        rtl = (directory / (module + ".v")).read_text()
        counts = verify(mapping, bundle, rtl, config, ctx.version)
        # The 256 shape (90 MB of RTL) uses a longer limit.
        ctx.run(["iverilog", "-g2001", "-s", module, "-o", directory / "syntax.vvp",
                 *(directory / s for s in bundle["simulation_sources"])], "verilog2001",
                cwd=directory, timeout=3600 if name == "256" else 300)
        case, scenario = verif_case(config, name, directory)
        verif = ctx.verify(d / "verif", case, scenario)
        return dict(counts=counts, artifacts=outputs["cli"], controls=witnesses,
                    rejected=rejected, verif=verif)
    return entry


ENTRIES = [(f"scale_{PROTOCOL}_{s}", shape(s))
           for s in ("12", "32", "64h1", "64d64", "256", "c64")]
