"""Read a bank while a watched AXI or APB target returns an error."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile

from log_bank_access import Run, definition as apb_definition, ports
from log_bank_layout import ID
from sim_engine import Simulation, generated_rtl


TOP = "nocloom_atomic_log"
WINDOW = 0x100000
ADDRESS = 0xABCD5678
AXID = 0xA35C


def definition():
    flags = [f"flag{k}" for k in range(256)]
    audits = [f"audit{k}" for k in range(32)]
    domain = dict(clock_domain="main", data_width=32)
    config = dict(name="cfg", protocol="apb4", **domain)
    data = dict(name="data", protocol="axi4", id_width=16, len_width=8, **domain)
    for key, names in (("user_flags", flags), ("audit_flags", audits)):
        config[key] = dict.fromkeys(names, "0")
        data[key] = dict.fromkeys(names, "user[0]")
    data["user_width"] = 1
    memory = dict(name="ram", protocol="axi4", id_width=16, len_width=8,
                  address_width=32, attributes="amba", **domain)
    memory["mappings"] = [dict(base=hex(base), size="0x1000")
                          for base in (0x2000, ADDRESS & ~0xfff)]
    clock = dict(name="main", clock_port="clk", reset_port="reset_n", test_port="test")
    return dict(schema_version=10, name="atomic_log", address_width=32,
                reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
                transport=dict(user_flags=flags),
                clock_domains=[clock], initiators=[config, data], targets=[memory],
                connectivity=dict(cfg=["csr"], data=["ram"]),
                error_logging=dict(
                    registers=dict(name="csr", base=hex(WINDOW), size="0x1000",
                                   clock_domain="main"),
                    audit_flags=audits, logs=[dict(name="log", watch=["ram"], whole_record=True)]),
                output=dict(backend="verilog", language="verilog_2001"))


def connections(dut, crossed):
    apb = "psel penable pwrite paddr pready pslverr".split()
    bound = {f"nl_i_cfg_{name}": name for name in apb + ["pwdata", "pstrb", "prdata"]}
    bound.update(clk="clk", reset_n="reset_n")
    if crossed:
        bound.update({f"nl_i_data_{name}": f"data_{name}" for name in apb})
        bound.update(clk_slow="clk_slow", nl_t_ram0_pready="1'b1", nl_t_ram0_pslverr="1'b1",
                     nl_i_data_pprot="3'b101", nl_o_log0_alarm1="alarm")
    else:
        source = "arvalid arready araddr arid arsize aruser rvalid rid rresp rlast".split()
        target = "arvalid arready arid rvalid rready rid rresp rlast".split()
        bound.update({f"nl_i_data_{name}": name for name in source})
        bound.update({f"nl_t_ram_{name}": f"t_{name}" for name in target})
        bound.update(nl_i_data_rready="1'b1", nl_o_log_alarm="alarm")
    lines = []
    for direction, width, name in ports(dut, TOP):
        idle = "" if direction == "output" else f"{width}'d0"
        lines.append(f"    .{name}({bound.pop(name, idle)})")
    assert not bound, bound
    return ",\n".join(lines)


def bank_probes(dut, crossed):
    """Find the bank head acceptance and the logger's admitted record in the generated RTL."""
    equations = dict(re.findall(r"^assign (\w+) = (.*);$", dut, re.M))
    updates = dict(re.findall(r"^    (\w+) <= (\w+);$", dut, re.M))

    def operands(wire, operator):
        expression = equations[wire]
        match = re.fullmatch(rf"\((\w+) {re.escape(operator)} (\w+)\)", expression)
        assert match, (wire, operator, expression)
        return match.groups()

    # The bank latches the low four bits of the head under the acceptance
    # condition. Match the key in the same head to select the watched bank.
    # This uses the request path independently of the returned word.
    heads = []
    key = int(crossed)
    for state, update in updates.items():
        mux = re.fullmatch(r"\((\w+) \? (\w+) : (\w+)\)", equations.get(update, ""))
        if not mux or mux[3] != state:
            continue
        start, index, _ = mux.groups()
        head = re.fullmatch(r"(\w+)\[3:0\]", equations.get(index, ""))
        if not head:
            continue
        own, _ = operands(start, "&")
        _, match = operands(own, "&")
        selection, value = operands(match, "==")
        if equations[selection] == head[1] + "[8:4]" and equations[value] == f"5'd{key}":
            heads.append(start)
    assert len(heads) == 1, (key, heads)
    read_head, = heads

    # The held bit is the state that feeds the alarm. Its next value contains
    # capture, which takes an offered record whenever there is room.
    if crossed:
        alarm = equations["nl_o_log0_alarm1"]
        status, = re.fullmatch(r"(\w+)\[1:1\]", equations[alarm]).groups()
        _, next_held = re.fullmatch(r"\{(\w+), (\w+)\}", equations[updates[status]]).groups()
    else:
        alarm = equations["nl_o_log_alarm"]
        held, _ = operands(updates[alarm], "&")
        next_held = updates[held]
    capture, _ = operands(next_held, "|")
    offered, _ = operands(capture, "&")
    return read_head, offered


def bench(dut, path, crossed):
    # The only AXI initiator follows cfg in name order. ram follows csr in target name order.
    # The captured address hits the second mapping.
    if crossed:
        bank = WINDOW + 0x40
        expected = {4: 0x8121, 8: 0x34, 12: 4 << 16, 16: 0x10001, 24: 0x66666666}
    else:
        bank = WINDOW
        expected = {4: AXID << 16 | 0x121, 8: ADDRESS,
                    12: 4 << 16, 16: 0x10001, 20: 1, 24: 0xffffffff}
        expected.update(dict.fromkeys(range(32, 64, 4), 0xffffffff))
    checks = [f"    sweep(32'h{bank + at:x}, 32'h{word:x});" for at, word in expected.items()]
    checks.append(f"    sweep(32'h{bank + 4:x}, 32'h{expected[4] | 2:x}, 1);")
    read_head, offered = bank_probes(dut, crossed)
    return (("`define CROSSED\n" if crossed else "") + path.read_text()
            .replace("@PORTS@", connections(dut, crossed)).replace("@CHECKS@", "\n".join(checks))
            .replace("@BANK_CLOCK@", "clk_slow" if crossed else "clk")
            .replace("@READ_HEAD@", read_head).replace("@RECORD_OFFERED@", offered)
            .replace("@BANK@", f"32'h{bank:x}")
            .replace("@AXID@", f"16'h{AXID:x}")
            .replace("@ADDRESS@", f"32'h{ADDRESS:x}"))


def run(args, work, crossed):
    work.mkdir()
    config = work / "network.yaml"
    if crossed:
        clocks = (("main", "clk", 10000), ("slow", "clk_slow", 27000))
        text, _ = apb_definition("atomic_log", [2], clocks=clocks, log_domain="slow",
                                 parts=("classes",), audits=32)
    else:
        text = json.dumps(definition())
    config.write_text(text)
    rtl = work / "rtl"
    subprocess.run([args.cli, "generate", str(config), "--out-dir", str(rtl)],
                   check=True, timeout=60)
    dut = generated_rtl(rtl / (TOP + ".v"))
    (work / "dut.v").write_text(dut)
    (work / "tb.sv").write_text(bench(dut, args.bench, crossed))
    print(f"Work directory: {work}", flush=True)
    result = Simulation(work, "dut.v").run()
    print(result.stdout, end="")
    assert result.returncode == 0, result.returncode


def check_read_wake(cli, work):
    """Read different bank IDs while the window clocks have time to stop between reads."""
    clocks = (("main", "clk", None),)
    # The first chain and the single-bank branch share the window's return path.
    text, banks = apb_definition("read_wake", [[0, 0x400, 0x1000], [0x8000]])
    run = Run(cli, work, "read_wake", text, banks, clocks, 24)
    for bank in banks:
        run.idle(8)
        run.read(f"gated bank ID {bank.name}", run.address(bank, ID), run.header(bank)[ID])
    checks = run.simulate()
    print(f"read_wake: {checks} checks", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--bench", required=True, type=Path)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work))
    run(args, work / "same_domain", crossed=False)
    run(args, work / "crossed", crossed=True)


if __name__ == "__main__":
    main()
