"""Check the target response path for sources excluded from local completion."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import mkdtemp

TESTS = Path(__file__).resolve().parents[2]
CASE = dict(contracts=["QOS.SHAPER.DECODE_MISS"])


# Digests cover file names and exact Verilog bytes on the target response path.
RTL_DIGESTS = {
    "multiple": "ec3ff955667e65766482fd9eec18d51724b27f822d9bad4ab96b24901530e621",
    "named": "df7d52fea5fb7474373f79e0c49f435a81618887af0551102c75e62fd50f85de",
    "log": "8ca3ffbd7add276933c8ae3b24955c8c220e3793b6f30db2ca04f18ac78f528f",
    "selector": "fa4cc78ad289f1d09cf68dd0ab440b5cd78c8e6693acd635dc7e2dfb0682abd3",
    "subpage": "5ec928a95ec750092b640661fe72a585ed659ca83c2104268118ed2fa752f323",
    "tl_uh": "32526131498917b32c81f85046fa00a9d655d1318eadcf0b990422431f3f6855",
    "shaper": "f0740d9e337d5e7a36e77a3e42503a9bba5b0d23a7c0b199b9faf7deb2a471c7",
    "scheduler": "cf39ce5a5b37a183d7f13e20e7d1536b72ac6fc030117b8252d70c04dfa2863b",
}

PATH_GROUPS = {
    "target_choice": ("local", "mixed", "multiple", "named"),
    "address_map": ("log", "selector", "subpage"),
    "protocol_and_qos": ("tl_uh", "shaper", "scheduler"),
}


def configuration(kind):
    source = dict(name="s", clock_domain="main", protocol="apb4", data_width=32,
                  max_outstanding=1)
    target = dict(name="t", clock_domain="main", protocol="apb4", data_width=32,
                  attributes="amba", mappings=[dict(base="0x0", size="0x1000")])
    cfg = dict(schema_version=10, name="source_miss_fallback", address_width=32,
               clock_gating=False, reset_ports=[dict(name="reset_n")],
               clock_domains=[dict(name="main", clock_port="clk", reset_port="reset_n")],
               initiators=[source], targets=[target], connectivity="all",
               output=dict(backend="verilog", language="verilog_2001"))
    if kind == "multiple":
        source.update(protocol="axi4", id_width=2, len_width=8, max_outstanding=2)
    elif kind == "named":
        source["default_target"] = "t"
    elif kind == "mixed":
        cfg["initiators"].append(dict(source, name="named", default_target="t"))
    elif kind == "log":
        # A given revision keeps the generator version out of the bank and of the digest.
        cfg["error_logging"] = dict(
            registers=dict(name="csr", base="0x10000", size="0x1000", clock_domain="main"),
            revision=0x01020304,
            logs=[dict(name="errors", watch=["t"], loggers=[dict(offset="0x100")])])
    elif kind == "selector":
        cfg["address_inputs"] = [dict(name="boot", width=1, clock_domain="main")]
        cfg["address_selectors"] = [dict(name="rom", equals=dict(boot=1))]
        target["mappings"][0]["when"] = dict(rom=True)
    elif kind == "subpage":
        target["mappings"][0]["size"] = "0x800"
    elif kind == "tl_uh":
        source.update(protocol="tl_uh", source_width=2)
    elif kind in ("shaper", "scheduler", "zero"):
        source["qos"] = dict(shaper=dict(bytes_per_1024_cycles=0 if kind == "zero" else 64,
                                        burst_bytes=32 if kind == "scheduler" else 0))
        if kind == "scheduler":
            source["qos"]["scheduler"] = dict(under_level=3, over_level=1, threshold_percent=0)
    return cfg


def generate(cli, work, kind):
    work.mkdir(parents=True, exist_ok=True)
    path = work / "input.yaml"
    path.write_text(json.dumps(configuration(kind)))
    gen = work / "gen"
    subprocess.run([cli, "generate", str(path), "--out-dir", str(gen)], check=True)
    mapping = json.loads((gen / "nocloom_source_miss_fallback.map.json").read_text())
    return gen, mapping


def rtl_digest(gen):
    return sha256(json.dumps({p.name: sha256(p.read_bytes()).hexdigest()
                              for p in sorted(gen.glob("*.v"))}).encode()).hexdigest()


def check_paths(cli, work, group=None):
    kinds = PATH_GROUPS[group] if group is not None else ("local", "mixed", *RTL_DIGESTS)
    key = "unconnected_pair_or_address_outside_routing_windows"
    local_rule = "DECERR_at_the_initiators_listed_in_local_decode_misses_without_a_network_request"
    target_rule = ("DECERR_from_the_reached_target_with_the_smallest_window_by_error_request_"
                   "without_target_handshake")
    for kind in kinds:
        gen, mapping = generate(cli, work / kind, kind)
        definition = mapping["definition"]
        rules = definition["contract"]["connectivity"]
        if kind in ("local", "mixed"):
            assert definition["local_decode_misses"] == ["s"]
            assert rules[key] == local_rule
            if kind == "mixed":
                assert rules["target_fallback"] == target_rule
                assert definition["default_targets"] == {"named": "t"}
            else:
                assert "target_fallback" not in rules
            continue
        local = definition.get("local_decode_misses", [])
        assert local == [], (kind, local)
        assert rules[key] == target_rule and "target_fallback" not in rules, (kind, rules)
        if kind == "named":
            assert definition["default_targets"] == {"s": "t"}
        digest = rtl_digest(gen)
        expected = RTL_DIGESTS[kind]
        assert digest == expected, (kind, digest, expected)
        print(f"{kind}: target response path and matching Verilog")


def internal(name):
    return "nl_internal_v" + name.removeprefix("value/")


def skip_miss_charge(rtl, charge, use_scheduler):
    miss = "(nl_i_s_paddr == 32'h2000)"
    if use_scheduler:
        pattern = rf"^    {charge} <= (nl_internal_v\d+);$"
        replacement = rf"    {charge} <= \1 & ~{miss};"
    else:
        pattern = rf"^assign {charge} = ([^;]+);$"
        replacement = rf"assign {charge} = (\1) & ~{miss};"
    rtl, count = re.subn(pattern, replacement, rtl, flags=re.M)
    assert count == 1, (charge, count)
    return rtl


def check_rate(cli, work, kind, variant):
    """Simulate one network: the generated one, or one whose miss takes no charge, which the
    bench must refuse."""
    from sim_engine import Simulation, generated_rtl

    gen, mapping = generate(cli, work, kind)
    use_scheduler = kind == "scheduler"
    resource = "source/s/" + ("scheduler" if use_scheduler else "shaper")
    storage = next(entry for entry in mapping["network"]["storage"]
                   if entry["resource"] == resource)
    charge = internal(storage["acquire"])
    counter = internal(storage["control"][0])
    rtl = generated_rtl(gen / "nocloom_source_miss_fallback.v")
    tb = (TESTS / "source_miss_fallback.sv").read_text()
    tb = (tb.replace("@ZERO@", str(int(kind == "zero")))
          .replace("@SCHEDULER@", str(int(use_scheduler)))
          .replace("@CHARGE@", "dut." + charge).replace("@COUNTER@", "dut." + counter))
    run = work / variant
    run.mkdir(exist_ok=True)
    (run / "tb.sv").write_text(tb)
    (run / "network.v").write_text(
        rtl if variant == "normal" else skip_miss_charge(rtl, charge, use_scheduler))
    result = Simulation(run, "network.v", success=("CHECK_PASS source_miss_rate",)).run()
    print(f"{kind}/{variant}\n{result.stdout}", end="")
    if variant == "normal":
        assert result.returncode == 0, (kind, variant)
    else:
        failure = "COMPLETION_CHARGE" if use_scheduler else "EARLY_ADMISSION"
        first = re.findall(r"^ASSERT_FAIL:SOURCE_MISS_RATE\.(\w+)$", result.stdout, re.M)
        assert result.returncode != 0 and first == [failure], (kind, first)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--case", choices=("paths", "zero", "shaper", "scheduler"), required=True)
    parser.add_argument("--path-group", choices=PATH_GROUPS)
    parser.add_argument("--variant", choices=("normal", "skip_miss_charge"))
    args = parser.parse_args()
    if args.path_group is not None and args.case != "paths":
        parser.error("--path-group requires --case paths")
    if (args.variant is None) != (args.case == "paths"):
        parser.error("--variant goes with every case but paths")
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(mkdtemp(dir=args.work.resolve(), prefix="run-"))
    if args.case == "paths":
        check_paths(args.cli, work, args.path_group)
    else:
        check_rate(args.cli, work / args.case, args.case, args.variant)


if __name__ == "__main__":
    sys.path.insert(0, str(TESTS))
    main()
