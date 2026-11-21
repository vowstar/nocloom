"""Drops at the sender of an error log in a clock domain of its own, while the log's clock stops."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

from log_bank_access import ports
import sim_engine


TOP = "nocloom_probe_drops"


def definition(axi_source):
    """The log watches APB4 target t from its own clock domain. The window and t are in main."""
    domain = dict(clock_domain="main", data_width=32)
    if axi_source:
        initiators = [dict(name="cfg", protocol="apb4", **domain),
                      dict(name="m", protocol="axi4", id_width=2, len_width=4, **domain)]
        connectivity = dict(cfg=["csr", "t"], m=["t"])
    else:
        initiators = [dict(name="s", protocol="apb4", **domain)]
        connectivity = "all"
    sources = [i["name"] for i in initiators]
    hub = [dict(name="hub", clock_domain="main")]

    def attach(names):
        return [dict(endpoint=name, node="hub") for name in names]

    return dict(
        schema_version=10, name="probe_drops", address_width=32, clock_gating=False,
        reset_ports=[dict(name="reset_n")],
        clock_domains=[dict(name="main", clock_port="clk", reset_port="reset_n", period_ps=10000),
                       dict(name="log", clock_port="clk_log", reset_port="reset_n",
                            period_ps=20000)],
        initiators=initiators,
        targets=[dict(name="t", protocol="apb4", attributes="amba",
                      mappings=[dict(base="0x0", size="0x1000")], **domain)],
        error_logging=dict(
            registers=dict(name="csr", base="0x100000", size="0x1000", clock_domain="main"),
            logs=[dict(name="errors", watch=["t"], clock_domain="log", loggers=[{}])]),
        connectivity=connectivity,
        topology=dict(
            request=dict(nodes=hub, links=[], entries=attach(sources),
                         exits=attach(["t", "csr"])),
            response=dict(nodes=hub, links=[], entries=attach(["t", "csr"]),
                          exits=attach(sources))),
        output=dict(backend="verilog", language="verilog_2001"))


def connections(dut, axi_source):
    apb = "psel penable pwrite paddr pwdata pready prdata pslverr".split()
    source = "cfg" if axi_source else "s"
    bound = dict(clk="clk", clk_log="clk_log", reset_n="reset_n", nl_o_errors_alarm="alarm",
                 nl_t_t_prdata="32'h0")
    bound.update({f"nl_i_{source}_{name}": f"c_{name}" for name in apb})
    bound[f"nl_i_{source}_pstrb"] = "4'hf"
    bound.update({f"nl_t_t_{name}": f"t_{name}" for name in
                  "psel penable pwrite paddr pready pslverr".split()})
    if axi_source:
        bound.update({f"nl_i_m_{name}": f"m_{name}" for name in
                      "arvalid arid araddr arlen arready rvalid rlast rresp rid rdata".split()})
        bound.update(nl_i_m_arsize="3'd2", nl_i_m_arburst="2'd1", nl_i_m_rready="1'b1",
                     nl_i_m_bready="1'b1")
    lines = []
    for direction, width, name in ports(dut, TOP):
        idle = "" if direction == "output" else f"{width}'d0"
        lines.append(f"    .{name}({bound.pop(name, idle)})")
    assert not bound, bound
    return ",\n".join(lines)


def records_held(mapping):
    """Records that enter while the log's clock stops: the crossing's whole frames and the record
    the sender keeps."""
    log = mapping["definition"]["error_logs"]["errors"]
    cut, = (c for c in mapping["network"]["cdc"]["cuts"]
            if c["name"] == "cut/service/observe/errors/t")
    assert log["record_fields"][-1]["name"] == "probe_overflow", log["record_fields"][-1]
    return cut["depth"] // log["serial_words"] + 1


def run(args, work, axi_source):
    work.mkdir()
    config = work / "network.yaml"
    config.write_text(json.dumps(definition(axi_source)))
    rtl = work / "rtl"
    subprocess.run([args.cli, "generate", str(config), "--out-dir", str(rtl)],
                   check=True, timeout=60, stdout=subprocess.PIPE)
    mapping = json.loads((rtl / f"{TOP}.map.json").read_text())
    held = records_held(mapping)
    dut = sim_engine.generated_rtl(rtl / f"{TOP}.v")
    (work / "dut.v").write_text(dut)
    bench = args.bench.read_text().replace("@PORTS@", connections(dut, axi_source))
    (work / "tb.sv").write_text(("`define AXI_SOURCE\n" if axi_source else "") + bench)
    simulation = sim_engine.Simulation(work, "dut.v", engine=args.sim, config=args.sim_config)
    result = simulation.run((f"+EXPECT={held}",))
    print(result.stdout, end="")
    assert result.returncode == 0 and re.search(r"^CHECK_PASS ", result.stdout, re.M), work
    print(f"{work.name}: {held} records enter while the log clock stops", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=args.work))
    run(args, root / "apb_source", False)
    run(args, root / "axi_source", True)


if __name__ == "__main__":
    main()
