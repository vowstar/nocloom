"""Clock gating and long-run retirement of a TL-UH target in generated networks."""
import argparse
import json
from pathlib import Path
import re
import runpy
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_engine

HERE = Path(__file__).resolve().parent
MASTER_IN = dict(
    awvalid="m_awvalid[{k}] & reset_n", awid="m_awid[{k}*4+:4]", awaddr="m_awaddr[{k}*32+:32]",
    awlen="m_awlen[{k}*8+:8]", awsize="m_awsize[{k}*3+:3]", awburst="2'd1", awlock="1'b0",
    awcache="4'd0", awprot="3'd0", wdata="m_wdata[{k}*DW+:DW]", wstrb="m_wstrb[{k}*LANES+:LANES]",
    wlast="m_wlast[{k}]", wvalid="m_wvalid[{k}] & reset_n", bready="m_bready[{k}]",
    arvalid="m_arvalid[{k}] & reset_n", arid="m_arid[{k}*4+:4]", araddr="m_araddr[{k}*32+:32]",
    arlen="m_arlen[{k}*8+:8]", arsize="m_arsize[{k}*3+:3]", arburst="2'd1", arlock="1'b0",
    arcache="4'd0", arprot="3'd0", rready="m_rready[{k}]")
MASTER_OUT = dict(
    awready="m_awready[{k}]", wready="m_wready[{k}]", arready="m_arready[{k}]",
    bvalid="m_bvalid[{k}]", bid="m_bid[{k}*4+:4]", bresp="m_bresp[{k}*2+:2]",
    rvalid="m_rvalid[{k}]", rid="m_rid[{k}*4+:4]", rresp="m_rresp[{k}*2+:2]",
    rlast="m_rlast[{k}]", rdata="m_rdata[{k}*DW+:DW]")
TARGET_IN = dict(a_ready="t_a_ready", d_valid="t_d_valid & reset_n", d_opcode="t_d_opcode",
                 d_param="t_d_param", d_size="t_d_size", d_source="t_d_source",
                 d_data="t_d_data", d_denied="t_d_denied", d_corrupt="t_d_corrupt")
TARGET_OUT = ("a_valid", "a_opcode", "a_param", "a_size", "a_source", "a_address", "a_mask",
              "a_data", "a_corrupt", "d_ready", "timed_out")
AXI_FIELDS = ("awaddr araddr awid arid bid rid awlen arlen awsize arsize awprot arprot awburst "
              "arburst bresp rresp awcache arcache awlock arlock awvalid awready arvalid arready "
              "wdata rdata wstrb wlast wvalid wready bvalid bready rlast rvalid rready").split()
TL_FIELDS = ("a_opcode a_param a_size a_source a_address a_mask a_data a_corrupt a_valid a_ready "
             "d_opcode d_param d_size d_source d_data d_corrupt d_denied d_valid d_ready").split()


def probe(value):
    assert value.startswith("value/"), value
    return "dut.nl_internal_v" + value.split("/")[1]


def gates(mapping, cfg):
    """Gate records of the map: control nets, the clock domain and the focus gates."""
    records = mapping["network"].get("clock_gates", [])
    far = cfg["targets"][0]["clock_domain"]
    cdc = far != "main"
    kinds, initiators, target = [], {}, {}
    for index, gate in enumerate(records):
        zone = gate.get("zone")
        if "initiator" in gate:
            kinds.append(2 if zone == "transport" else 1)
            initiators.setdefault(gate["initiator"], {})[zone] = index
        elif "target" in gate:
            kinds.append(4 if zone == "transport" else 3)
            target[zone] = index
        else:
            kinds.append(0)
    first = initiators.get("s0", {})

    def focus(zones):
        return next((zones[z] for z in ("protocol", "shared", "transport") if z in zones), 0)

    def protocol(zones):
        return next((zones[z] for z in ("protocol", "shared") if z in zones), 255)

    count = max(1, len(records))

    def bus(field, width=1):
        nets = [probe(g[field]) for g in records] or [f"{width}'d0"]
        return f"wire [{width*count-1}:0] g_{field}={{" + ",".join(reversed(nets)) + "};"

    names = [s["name"] for s in cfg["initiators"]]
    lines = [bus("enable").replace("g_enable", "g_en"), bus("idle"), bus("wake"),
             bus("remaining", 2).replace("g_remaining", "g_rem"),
             f"localparam NG={count}, GATED={int(bool(records))}, IG={focus(first)}, "
             f"TG={focus(target)}, TGP={protocol(target)};",
             f"localparam [{count-1}:0] G_FAR={count}'b"
             + "".join(str(int(cdc and g["clock_domain"] == far)) for g in reversed(records))
             + ("0" if not records else "") + ";",
             f"localparam [{8*len(names)-1}:0] IGP={{"
             + ",".join(f"8'd{protocol(initiators.get(n, {}))}" for n in reversed(names)) + "};",
             f"integer g_kind[0:{count-1}];", "initial begin"]
    lines += [f"  g_kind[{i}]={k};" for i, k in enumerate(kinds or [0])]
    return lines + ["end"]


def bench(mapping, cfg, body):
    ports = {p["name"]: p for p in mapping["ports"]}
    target = mapping["definition"]["tilelink"]["targets"]["t"]
    count = len(cfg["initiators"])
    width = cfg["initiators"][0]["data_width"]
    lanes = width // 8
    source = max(1, target["source_width"])
    timeout = cfg["targets"][0].get("timeout", {}).get("cycles", 0)
    cdc = cfg["targets"][0]["clock_domain"] != "main"
    text = ["`timescale 1ps/1ps", "module tb;", "reg clk=0, clk_far=0, reset_n=0, test=0;",
            f"localparam NS={count}, DW={width}, LANES={lanes}, LSZ={lanes.bit_length()-1}, "
            f"K={target['max_outstanding']}, MAXB={target['max_transfer_bytes']}, "
            f"SRCW={source}, TIMEOUT={timeout}, CDC={int(cdc)}, "
            f"DUT_OUTSTANDING={cfg['initiators'][0]['max_outstanding']};",
            "reg [NS-1:0] m_arvalid=0, m_awvalid=0, m_wvalid=0, m_wlast=0, m_rready=0, m_bready=0;",
            "reg [NS*4-1:0] m_arid=0, m_awid=0;", "reg [NS*32-1:0] m_araddr=0, m_awaddr=0;",
            "reg [NS*8-1:0] m_arlen=0, m_awlen=0;", "reg [NS*3-1:0] m_arsize=0, m_awsize=0;",
            "reg [NS*DW-1:0] m_wdata=0;", "reg [NS*LANES-1:0] m_wstrb=0;",
            "wire [NS-1:0] m_arready, m_awready, m_wready, m_rvalid, m_rlast, m_bvalid;",
            "wire [NS*4-1:0] m_rid, m_bid;", "wire [NS*2-1:0] m_rresp, m_bresp;",
            "wire [NS*DW-1:0] m_rdata;",
            "wire t_a_valid, t_a_corrupt, t_d_ready, t_timed_out;",
            "wire [2:0] t_a_opcode, t_a_param;", "wire [3:0] t_a_size, t_a_mask;",
            f"wire [{source-1}:0] t_a_source;", "wire [31:0] t_a_address, t_a_data;",
            "reg t_a_ready=0, t_d_valid=0, t_d_denied=0, t_d_corrupt=0;",
            "reg [2:0] t_d_opcode=0;", "reg [1:0] t_d_param=0;", "reg [3:0] t_d_size=0;",
            f"reg [{source-1}:0] t_d_source=0;", "reg [31:0] t_d_data=0;"]
    connections, used = [], set()
    for name, port in ports.items():
        if name in ("clk", "clk_far", "reset_n", "test"):
            connections.append(f".{name}({name})")
            continue
        text.append(f"wire [{port['width']-1}:0] {name};")
        connections.append(f".{name}({name})")
    for k, initiator in enumerate(cfg["initiators"]):
        prefix = f"nl_i_{initiator['name']}_"
        for signal, value in MASTER_IN.items():
            assert ports[prefix + signal]["direction"] == "input", signal
            text.append(f"assign {prefix}{signal}={value.format(k=k)};")
            used.add(prefix + signal)
        for signal, value in MASTER_OUT.items():
            assert ports[prefix + signal]["direction"] == "output", signal
            text.append(f"assign {value.format(k=k)}={prefix}{signal};")
            used.add(prefix + signal)
        text.append(
            f'nlv_tl_uh_axi_check #(.NAME("{initiator["name"]}"),.DATA_W(DW)) checker_s{k}('
            ".clk(clk),.rst_n(reset_n)," + ",".join(f".{f}({prefix}{f})" for f in AXI_FIELDS) + ");")
    for signal, value in TARGET_IN.items():
        if "nl_t_t_" + signal in ports:
            text.append(f"assign nl_t_t_{signal}={value};")
            used.add("nl_t_t_" + signal)
    for signal in TARGET_OUT:
        name = "nl_t_t_" + signal
        if name in ports:
            text.append(f"assign t_{signal}={name};")
            used.add(name)
        else:
            assert signal in ("a_source", "timed_out"), signal
            text.append(f"assign t_{signal}=0;")
    missing = set(ports) - used - {"clk", "clk_far", "reset_n", "test"}
    assert not missing, missing
    text.append("nocloom_uh_gating dut(" + ",".join(connections) + ");")
    text += gates(mapping, cfg)
    # The map names the nets that take and return a packet context of the target.
    pool = next((s for s in mapping["network"]["storage"]
                 if s["resource"] == "burst_service/t/tilelink"), None)
    text.append(f"localparam POOL_PROBE={int(pool is not None)};")
    text.append("wire pool_acquire=" + (probe(pool["acquire"]) if pool else "1'b0") + ";")
    text.append("wire pool_release=" + (probe(pool["release"]) if pool else "1'b0") + ";")
    text.append(body)
    text.append(
        f'nlv_tl_checker #(.NAME("target"),.SOURCE_W({source}),.SIZE_W(4),.UH(1),.SOURCE_LIMIT(K)) '
        "checker_t(.clk(fclk),.rst_n(reset_n),.allow_reject(1'b0),"
        + ",".join(f".{f}(t_{f})" if f not in ("d_valid",) else ".d_valid(t_d_valid & reset_n)"
                   for f in TL_FIELDS) + ");")
    text.append("endmodule")
    return "\n".join(text) + "\n"


def substitute(rtl, pattern, replacement):
    result, count = re.subn(pattern, replacement, rtl, flags=re.M)
    if count != 1:
        raise RuntimeError(f"fault anchor matched {count} times: {pattern}")
    return result


def faulted(rtl, bindings, edits, controls):
    """A copy of the network with every listed fault behind its +FAULT selector."""
    def net(name):
        return "nl_internal_v" + str(bindings[name]["index"])

    for index, (choice, kind, name, value) in enumerate(edits):
        if kind == "input":
            # Every internal copy of an input port.
            rtl, count = re.subn(rf"^(assign nl_internal_v\d+ = )({name});$",
                                 lambda m: f"{m[1]}({choice} ? ({value(m[2], net)}) : {m[2]});",
                                 rtl, flags=re.M)
            if not count:
                raise RuntimeError("fault anchor missing: " + name)
        elif kind == "wire":
            # nlv_kept carries the unchanged value for a fault that needs its history.
            rtl = substitute(rtl, rf"^(assign {net(name)} = )([^;]+);$",
                             lambda m: f"assign nlv_kept{index} = {m[2]};\n"
                                       f"{m[1]}({choice} ? ({value(m[2], net)}) : ({m[2]}));")
        elif kind == "state":
            rtl = substitute(rtl, rf"^(\s*{net(name)} <= )(nl_internal_v\d+);$",
                             lambda m: f"{m[1]}({choice} ? ({value(m[2], net)}) : {m[2]});")
        elif kind == "output":
            rtl = substitute(rtl, rf"^(assign {name} = )([^;]+);$",
                             lambda m: f"{m[1]}({choice} ? ({value(m[2], net)}) : ({m[2]}));")
        else:
            rtl = substitute(
                rtl, rf"^(nocloom_clock_gate {net(name)}_gate \(.*\.enable\()(nl_internal_v\d+)(\).*)$",
                lambda m: f"{m[1]}({choice} ? ({value(m[2], net)}) : {m[2]}){m[3]}")
    end = rtl.index("\n", rtl.index(");", rtl.index("module nocloom_uh_gating")))
    wires = "".join(f"wire nlv_kept{i};\n" for i, edit in enumerate(edits) if edit[1] == "wire")
    start = """
integer nlv_fault_sel=0;
initial if (!$value$plusargs("FAULT=%d",nlv_fault_sel)) nlv_fault_sel=0;
"""
    return rtl[:end] + start + wires + controls + rtl[end:]


def generate(args, work, cfg):
    (work / "input.yaml").write_text(json.dumps(cfg))
    generated = work / "generated"
    result = subprocess.run([str(args.cli.resolve()), "generate", str(work / "input.yaml"),
                             "--out-dir", str(generated)], capture_output=True, text=True,
                            timeout=120)
    (work / "generation.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError("generation failed:\n" + result.stderr)
    mapping = json.loads(next(generated.glob("*.map.json")).read_text())
    return mapping, sim_engine.generated_rtl(generated / "nocloom_uh_gating.v")


def checkers():
    return (HERE / "sv/nlv_tl_checker.sv").read_text() + (HERE / "sv/nlv_tl_uh_axi_check.sv").read_text()


def fields(output, tag):
    values = {}
    for line in output.splitlines():
        if line.startswith(tag + " "):
            values.update(item.split("=", 1) for item in line.split()[1:] if "=" in item)
    return {k: int(v) for k, v in values.items() if k not in ("seed", "pm", "pf")}


def network(args, case):
    cfg = dict(case["NETWORKS"][args.network])
    cfg.update(mode=args.mode, cdc=not args.single_domain)
    return case["configuration"](**cfg)


def run(args, case):
    root = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    cfg = network(args, case)
    mapping, rtl = generate(args, root, cfg)
    body = args.env.read_text() + args.scenario.read_text()
    (root / "tb.sv").write_text(bench(mapping, cfg, body))
    (root / "network.v").write_text(rtl + checkers())
    sim = sim_engine.Simulation(root, "network.v", engine=args.sim, config=args.sim_config,
                                compile_timeout=300, run_timeout=args.run_timeout,
                                success=("UH_PASS",))
    pairs = [case["PAIRS"][int(i)] for i in args.pairs.split(",")]
    seeds = [int(s) for s in args.seeds.split(",")]
    failed, reached = 0, set()
    for main, far, label in pairs:
        if args.single_domain:
            far, label = main, "single_clock"
        for seed in seeds:
            options = [f"+SEED={seed}", f"+PM={main}", f"+PF={far}"] + ["+" + o for o in args.plus]
            result = sim.run(options)
            (root / f"simulation-{main}-{far}-{seed}.log").write_text(result.stdout)
            row = dict(network=args.network, mode=args.mode, ratio=label, main_ps=main, far_ps=far,
                       seed=seed, status="PASS" if result.returncode == 0 else "FAIL")
            row.update(fields(result.stdout, "UHG_STAT"))
            row.update(fields(result.stdout, "UHG_COVER"))
            if result.returncode:
                failed += 1
                row["failure"] = next((line for line in result.stdout.splitlines()
                                       if "ASSERT_FAIL:" in line), result.stdout[-400:])
            else:
                reached.add(label)
            print(json.dumps(row), flush=True)
    print(root, flush=True)
    missing = set(case["CLASSES"]) - reached if args.all_classes else set()
    if missing:
        print("ratio classes without a passing run: " + ", ".join(sorted(missing)), flush=True)
    return 1 if failed or missing else 0


def negative(args, case, edits):
    root = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    cfg = network(args, case)
    mapping, rtl = generate(args, root, cfg)
    fixture = root / "fixture"
    subprocess.run([str(args.fixture.resolve()), "tilelink_uh_probe", str(fixture), "--param",
                    f"config={root / 'input.yaml'}", "--list-bindings"], check=True,
                   capture_output=True, timeout=120)
    if (fixture / "network.v").read_text() + (fixture / "cells.v").read_text() != rtl:
        raise RuntimeError("fixture differs from the CLI RTL")
    bindings = json.loads((fixture / "bindings.json").read_text())["dut"]
    body = args.env.read_text() + args.scenario.read_text()
    (root / "tb.sv").write_text(bench(mapping, cfg, body))
    (root / "network.v").write_text(faulted(rtl, bindings, *edits(cfg)) + checkers())
    sim = sim_engine.Simulation(root, "network.v", engine=args.sim, config=args.sim_config,
                                compile_timeout=300, run_timeout=args.run_timeout,
                                success=("UH_PASS",))
    main, far, _ = case["PAIRS"][int(args.pairs.split(",")[0])]
    options = [f"+SEED={args.seeds.split(',')[0]}", f"+PM={main}", f"+PF={far}"]
    options += ["+" + o for o in args.plus]
    clean = sim.run(options + ["+FAULT=0"])
    (root / "fault-0.log").write_text(clean.stdout)
    if clean.returncode:
        raise RuntimeError("the network without a fault failed:\n" + clean.stdout[-2000:])
    rows = [dict(selector=0, status="PASS")]
    for selector, (name, _, rules) in case["FAULTS"].items():
        result = sim.run(options + [f"+FAULT={selector}"])
        (root / f"fault-{selector}.log").write_text(result.stdout)
        line = next((x for x in result.stdout.splitlines() if "ASSERT_FAIL:" in x), "")
        caught = result.returncode != 0 and any("ASSERT_FAIL:" + rule in line for rule in rules)
        rows.append(dict(selector=selector, fault=name, status="CAUGHT" if caught else "SURVIVED",
                         assertion=line))
        print(json.dumps(rows[-1]), flush=True)
    (root / "results.json").write_text(json.dumps(rows, indent=2))
    print(root, flush=True)
    return 0 if all(row["status"] in ("PASS", "CAUGHT") for row in rows) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cli", "case", "env", "scenario", "work"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--network", default="k3")
    parser.add_argument("--mode", default="independent")
    parser.add_argument("--single-domain", action="store_true")
    parser.add_argument("--pairs", default="0")
    parser.add_argument("--seeds", default="1")
    parser.add_argument("--plus", action="append", default=[])
    parser.add_argument("--run-timeout", type=int, default=600)
    parser.add_argument("--all-classes", action="store_true",
                        help="Require a passing run in every clock ratio class")
    parser.add_argument("--negative", action="store_true")
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    case = runpy.run_path(str(args.case))
    if not args.negative:
        return run(args, case)
    return negative(args, case, case["fault_edits"])


if __name__ == "__main__":
    raise SystemExit(main())
