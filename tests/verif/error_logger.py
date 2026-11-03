#!/usr/bin/env python3
"""An error logger on a service target over APB: first-error record, stall, fault and clear.

The offsets come from the network's map. The unmodified netlist must pass; each designated fault of
the fault build (selection zero passes too) must fail on a check that names it.

  python3 tests/verif/error_logger.py --fixture FIXTURE --bench sv/nlv_logger_tb.sv
          --keep-bench sv/nlv_keep_logger_tb.sv --banks-bench sv/nlv_logger_banks_tb.sv
          --crossed-bench sv/nlv_crossed_logger_tb.sv --work DIR [--sim ENGINE] [--sim-config JSON]

Exit code: 0 every run met its expectation, 1 a run did not, 2 infrastructure error.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.dont_write_bytecode = True

import sim_engine  # noqa: E402
from nlverif import faults as fl  # noqa: E402

KEY = "observe/errors/"


def replace(at, **with_):
    return dict(component="network", op="replace", at={"binding": KEY + at}, **{"with": with_})


def definition(codes):
    text = ["schema_version: 10", "name: logged", "address_width: 32",
            "reset_ports: [{name: reset_n}]", "test_ports: [{name: test}]",
            "clock_domains: [{name: main, clock_port: clk, reset_port: reset_n, test_port: test}]"]
    text.append("transport: {response_codes: " + ("distinct" if codes else "uniform") + "}")
    text += ["initiators:",
             "  - {name: cfg, clock_domain: main, protocol: apb4, data_width: 32}",
             "  - {name: data, clock_domain: main, protocol: apb4, data_width: 32}",
             "targets:",
             "  - {name: csr, clock_domain: main, protocol: apb4, data_width: 32, address_width: 20, "
             "attributes: amba, mappings: [{base: '0x100000', size: '0x100000'}]}",
             "  - {name: ram, clock_domain: main, protocol: apb4, data_width: 32, address_width: 16, "
             "attributes: amba, mappings: [{base: '0x0', size: '0x1000'}]}",
             "connectivity:", "  cfg: [csr]", "  data: [ram]",
             "output: {backend: verilog, language: verilog_2001}"]
    return "\n".join(text) + "\n"


# Declared out of name order: a record's word stands as declared, zeta, Alpha, mid.
SECURITY = dict(
    flags=["zeta", "Alpha", "mid"],
    drivers=dict(cfg=dict(zeta="zero", Alpha="zero", mid="zero"),
                 data=dict(zeta="privileged", Alpha="~nonsecure", mid="instruction")),
    security_filter=dict(Alpha=1, zeta=0))

CONFIGS = [
    dict(label="plain", codes=False, ignored="", define="PLAIN", faults=[
        dict(id="overwrite_first", expect=("kept_first",),
             **replace("logger.capture", select={"binding": KEY + "logger.offered"})),
        dict(id="clear_loses", expect=("clear_capture",), component="network", op="set_next",
             at={"binding": KEY + "logger.valid"},
             **{"with": dict(op="BitOr", args=[
                 {"select": {"binding": KEY + "logger.capture"}},
                 {"op": "BitAnd", "args": [{"self": True},
                     {"op": "BitNot", "args": [{"select": {"binding": KEY + "logger.clear"}}]}]}])}),
        dict(id="capture_missing", expect=("first_record",),
             **replace("logger.capture", const=0)),
        dict(id="fault_early", expect=("fault_cycle",),
             **replace("logger.alarm", select={"binding": KEY + "logger.valid"})),
        dict(id="stall_off", expect=("stall_kept",), **replace("logger.hold", const=0)),
        dict(id="stall_always", expect=("timeout", "first_record", "kept_first"),
             **replace("logger.hold", const=1)),
    ]),
    dict(label="ignored", codes=True, ignored="2", define="IGNORED", faults=[
        dict(id="stall_after_filter", expect=("ignored_stalls",),
             **replace("logger.hold", op="BitAnd",
                       args=[{"self": True}, {"select": {"binding": KEY + "logger.accept"}}])),
        dict(id="ignored_logged", expect=("ignored_dropped",), **replace("logger.accept", const=1)),
    ]),
    dict(label="security", codes=True, ignored="", define="SECURITY", security=SECURITY, faults=[
        dict(id="flag_lost", expect=("security_record", "security_name_order"),
             **replace("record.security", const=0)),
        dict(id="flag_lost_at_target", expect=("security_record", "security_name_order"),
             component="network", op="replace", at={"binding": "observe/ram/security"},
             **{"with": dict(const=0)}),
        dict(id="filter_inverted", expect=("filter_accepts", "filter_rejects_one", "filter_rejects_zero"),
             **replace("logger.filter_match", op="BitNot", args=[{"self": True}])),
        dict(id="filter_ignored", expect=("filter_rejects_one", "filter_rejects_zero"),
             **replace("logger.filter_match", const=1)),
        dict(id="stall_after_flag_filter", expect=("filter_stalls",),
             **replace("logger.hold", op="BitAnd",
                       args=[{"self": True}, {"select": {"binding": KEY + "logger.accept"}}])),
    ]),
]


KEEP_FLAGS = [f"f{k:02d}" for k in range(39, -1, -1)]
# Four security flags above the user flags on the source's AxUSER.
KEEP_SECURITY = dict(
    flags=[f"c{k}" for k in range(3, -1, -1)],
    drivers=dict(cfg={f"c{k}": "zero" for k in range(4)},
                 data={f"c{3 - k}": f"user:{40 + k}" for k in range(4)}))


def keep_definition():
    flags = ", ".join(KEEP_FLAGS)
    driven = ", ".join(f"{flag}: 'user[{k}]'" for k, flag in enumerate(KEEP_FLAGS))
    zero = ", ".join(f"{flag}: 0" for flag in KEEP_FLAGS)
    text = ["schema_version: 10", "name: kept", "address_width: 48",
            f"transport: {{response_codes: distinct, user_flags: [{flags}]}}",
            "reset_ports: [{name: reset_n}]", "test_ports: [{name: test}]",
            "clock_domains: [{name: main, clock_port: clk, reset_port: reset_n, test_port: test}]",
            "initiators:",
            "  - {name: cfg, clock_domain: main, protocol: apb4, data_width: 32, "
            f"address_width: 32, user_flags: {{{zero}}}}}",
            "  - {name: data, clock_domain: main, protocol: axi4, data_width: 32, id_width: 1, "
            f"len_width: 8, max_outstanding: 2, user_width: 44, user_flags: {{{driven}}}}}",
            "targets:",
            "  - {name: csr, clock_domain: main, protocol: apb4, data_width: 32, address_width: 20, "
            "attributes: amba, mappings: [{base: '0x100000', size: '0x100000'}]}",
            "  - {name: ram, clock_domain: main, protocol: axi4, data_width: 32, id_width: 2, "
            f"len_width: 8, max_outstanding: 2, address_width: 48, attributes: amba, user: [{flags}], "
            "mappings: [{base: '0x0', size: '0x400000000000'}]}",
            "  - {name: rom, clock_domain: main, protocol: axi4, data_width: 32, id_width: 2, "
            f"len_width: 8, max_outstanding: 2, address_width: 48, attributes: amba, user: [{flags}], "
            "mappings: [{base: '0x400000000000', size: '0x400000000000'}]}",
            "connectivity:", "  cfg: [csr]", "  data: [ram, rom]",
            "output: {backend: verilog, language: verilog_2001}"]
    return "\n".join(text) + "\n"


def context_replace(at, **with_):
    return dict(component="network", op="replace", at={"binding": "observe/ram/" + at},
                **{"with": with_})


CONFIGS.append(dict(
    label="keep", codes=True, ignored="", define="KEEP", keep=True, top="kept",
    security=KEEP_SECURITY, faults=[
        dict(id="security_lost", expect=("whole_security_write", "slot_security"),
             **replace("record.security", const=0)),
        dict(id="security_of_other_context", expect=("slot_security",),
             **context_replace("context1.valid", const=0)),
        dict(id="address_cut", expect=("whole_address_write", "slot_address", "oldest_address",
                                       "context_free_address", "touching_word_address",
                                       "short_gap_address", "miss_address"),
             **replace("record.address", op="BitAnd",
                       args=[{"self": True}, {"const": 0xffffffff}])),
        dict(id="user_cut", expect=("whole_user_write", "slot_user", "context_free_user",
                                    "miss_user"),
             **replace("record.user", op="BitAnd",
                       args=[{"self": True}, {"const": 0xffffffff}])),
        dict(id="length_of_target", expect=("narrow_length", "fixed_length", "miss_length"),
             **replace("record.length", const=3)),
        dict(id="lock_lost", expect=("miss_lock",), **replace("record.lock", const=0)),
        dict(id="always_context_zero", expect=("slot_address", "slot_user"),
             **context_replace("context1.valid", const=0)),
        dict(id="newest_of_key", expect=("oldest_address",),
             **context_replace("context0.older1", op="BitNot", args=[{"self": True}])),
        dict(id="fixed_address_cut", expect=("fixed_address", "fixed_write_address"),
             **replace("record.address", op="BitAnd",
                       args=[{"self": True}, {"op": "BitNot", "args": [{"const": 0x7f}]}])),
        dict(id="gap_ignored", expect=("gap_low_bits", "gap_marker"),
             **context_replace("marked", const=0)),
        dict(id="logging_blocks_return", expect=("data_goes_on",),
             component="network", op="replace", at={"driver": "nl_t_ram_rready"},
             **{"with": dict(op="BitAnd", args=[{"self": True},
                 {"op": "BitNot", "args": [{"select": {"binding": KEY + "logger.hold"}}]}])}),
        dict(id="gap_of_one", expect=("short_gap_address", "touching_word_address"),
             **context_replace("quiet", const=2)),
    ]))


CONFIGS.append(dict(
    label="fixed_trimmed", codes=True, ignored="", define="KEEP", keep=True, trimmed=True,
    top="kept", security=KEEP_SECURITY, faults=[
        dict(id="fixed_address_cut", expect=("fixed_address", "fixed_write_address"),
             **replace("record.address", op="BitAnd",
                       args=[{"self": True}, {"op": "BitNot", "args": [{"const": 0x7f}]}]))]))


def banks_definition(codes):
    text = definition(codes).splitlines()
    at = text.index("connectivity:")
    text[at:at] = ["  - {name: rom, clock_domain: main, protocol: apb4, data_width: 32, "
                   "address_width: 16, attributes: amba, mappings: [{base: '0x10000', size: '0x1000'}]}"]
    text[text.index("  data: [ram]")] = "  data: [ram, rom]"
    return "\n".join(text).replace("name: logged", "name: banked") + "\n"


def direct(at, **with_):
    return dict(component="network", op="replace", at={"binding": at}, **{"with": with_})


BANKS = dict(sec=None, observers=[
    dict(name="errors", targets=["ram"], loggers=[dict(base=0x300, route=5, ignored=[])]),
    dict(name="others", targets=["rom"], loggers=[dict(base=0x100, route=2, ignored=[])])])
SERVICE = "burst_service/csr/"
# One error log with two loggers on ram. The first ignores decode codes and deletes what it logs.
CHAIN = dict(sec=None, observers=[
    dict(name="errors", targets=["ram"], loggers=[dict(base=0x300, route=5, ignored=[2]),
                                                  dict(base=0x100, route=2, ignored=[])])])
FORWARD = dict(sec=None, observers=[
    dict(name="errors", targets=["ram"], loggers=[dict(base=0x300, route=5, ignored=[2], forward=1),
                                                  dict(base=0x100, route=2, ignored=[])])])
CONFIGS.append(dict(
    label="banks", codes=False, ignored="", define="BANKS", top="banked", observers=BANKS, faults=[
        dict(id="branch_lost", expect=("rom_logged", "rom_record", "bank1_control", "clear_apart"),
             **direct(SERVICE + "bus.branch", const=0)),
        dict(id="key_lost", expect=("bank0_control", "ram_logged"),
             **direct(SERVICE + "bus.key", const=2)),
        dict(id="offset_lost", expect=("bank1_control",),
             **direct(SERVICE + "bus.in_bank", const=0)),
        dict(id="clear_of_other", expect=("clear_apart",),
             **direct("observe/others/logger.clear",
                      select={"binding": "observe/errors/logger.clear"})),
    ]))
CONFIGS.append(dict(
    label="chain", codes=True, ignored="", define="CHAIN", top="banked", observers=CHAIN, faults=[
        dict(id="forward_lost", expect=("forwarded",), **replace("logger.drop", const=1)),
        dict(id="delete_off", expect=("deleted",), **replace("logger.drop", const=0)),
        dict(id="next_room_ignored", expect=("stall_holds", "released"),
             **replace("logger.ready", op="BitNot",
                       args=[{"select": {"binding": KEY + "logger.hold"}}])),
        dict(id="clear_shared", expect=("deleted_again",),
             **replace("logger1.clear", select={"binding": KEY + "logger.clear"})),
        dict(id="key_lost", expect=("control_apart",), **direct(SERVICE + "bus.key", const=2)),
    ]))
CONFIGS.append(dict(
    label="forward", codes=True, ignored="", define="FORWARD", top="banked", observers=FORWARD,
    faults=[
        dict(id="forward_ignored", expect=("both_logged",),
             **replace("logger.drop", select={"binding": KEY + "logger.accept"})),
        dict(id="first_only", expect=("both_logged",), **replace("logger.drop", const=1)),
        dict(id="forward_before_accept", expect=("chain_waits_at_first",),
             **replace("logger1.offered", op="BitAnd", args=[
                 {"select": {"binding": KEY + "probe0.pending"}},
                 {"select": {"binding": KEY + "logger.serial_last"}}])),
    ]))


def crossed_definition():
    return """schema_version: 10
transport: {response_codes: uniform}
name: crossed
address_width: 32
reset_ports: [{name: reset_n}]
test_ports: [{name: test}]
clock_domains:
  - {name: main, clock_port: clk, reset_port: reset_n, test_port: test, period_ps: 1250}
  - {name: slow, clock_port: clk_slow, reset_port: reset_n, test_port: test, period_ps: 3333}
initiators:
  - {name: cfg, clock_domain: main, protocol: apb4, data_width: 32}
  - {name: cfg2, clock_domain: main, protocol: apb4, data_width: 32}
  - {name: data, clock_domain: main, protocol: apb4, data_width: 32}
targets:
  - {name: csr, clock_domain: main, protocol: apb4, data_width: 32, address_width: 20, attributes: amba, mappings: [{base: '0x100000', size: '0x100000'}]}
  - {name: ram, clock_domain: main, protocol: apb4, data_width: 32, address_width: 16, attributes: amba, mappings: [{base: '0x0', size: '0x1000'}]}
connectivity:
  cfg: [csr]
  cfg2: [csr]
  data: [ram]
topology:
  request:
    nodes:
      - {name: cq, clock_domain: main}
      - {name: dq, clock_domain: main}
    links: []
    entries:
      - {endpoint: cfg, node: cq}
      - {endpoint: cfg2, node: cq}
      - {endpoint: data, node: dq}
    exits:
      - {endpoint: csr, node: cq}
      - {endpoint: ram, node: dq}
  response:
    nodes:
      - {name: cp, clock_domain: main}
      - {name: dp, clock_domain: main}
    links: []
    entries:
      - {endpoint: csr, node: cp}
      - {endpoint: ram, node: dp}
    exits:
      - {endpoint: cfg, node: cp}
      - {endpoint: cfg2, node: cp}
      - {endpoint: data, node: dp}
output: {backend: verilog, language: verilog_2001}
"""


CROSSED = dict(sec=None, observers=[
    dict(name="errors", targets=["ram"], domain="slow", loggers=[dict(base=0x300, route=5, ignored=[])])])
SERIAL = "cut/service/csr/"
RECORD = "cut/service/observe/errors/ram/"
CONFIGS.append(dict(
    label="crossed", codes=False, ignored="", define="CROSSED", top="crossed", observers=CROSSED,
    crossed=True, faults=[
        dict(id="answer_lost", expect=("timeout",), **direct(SERIAL + "response/input.valid", const=0)),
        dict(id="request_word_lost", expect=("timeout", "control_kept"),
             **direct(SERIAL + "request/input.field0", const=0)),
        dict(id="owner_swapped", expect=("timeout", "own_data"),
             **direct("burst_service/csr/bus.source", add=1)),
        dict(id="record_lost", expect=("record_logged",), **direct(RECORD + "input.valid", const=0)),
        dict(id="gather_lost", expect=("record_address",), **replace("logger.gather0", const=0)),
        dict(id="probe_free_early", expect=("record_logged", "record_address"),
             **replace("sender0/sender.done", select={"binding": KEY + "sender0/sender.moved"})),
        dict(id="room_ignored", expect=("fourth_record", "fifth_record"),
             **replace("sender0/sender.moved", select={"binding": KEY + "sender0/sender.valid"})),
        dict(id="stall_off", expect=("released", "released_address"),
             **replace("logger.hold", const=0)),
        dict(id="clear_lost", expect=("cleared",), **replace("logger.clear", const=0)),
        dict(id="fault_stuck", expect=("fault_quiet",), **replace("logger.valid", const=1)),
    ]))
crossed_config = CONFIGS[-1]
for width in (8, 16):
    faults = [dict(f) for f in crossed_config["faults"]]
    if width == 8:
        next(f for f in faults if f["id"] == "gather_lost")["at"] = {
            "binding": KEY + "logger.gather1"}
    CONFIGS.append(dict(crossed_config, label=f"crossed{width}",
                        serial_word_bits=width, faults=faults))
# Clock ratios a crossed network runs the bench under, the source's period over the bank's.
CROSSED_RATIOS = [("+SLOW_NS=33",), ("+SLOW_NS=3.1",), ("+SLOW_NS=10.7",)]


def offsets(map_path):
    network = json.loads(map_path.read_text())
    registers = network["definition"]["log_registers"]["csr"]["registers"]
    words = {r["name"]: r["offset"] for r in registers}
    bits = dict(RECORD_BYTES_MINUS_ONE="record_bytes_minus_one",
                RECORD_PARTIAL="record_address_partial", RECORD_WRITE="record_write",
                RECORD_EXCLUSIVE="record_exclusive")
    lsbs = {r["name"]: r["lsb"] for r in registers}
    wanted = dict(ALARM_ENABLE="alarm_enable", RECORD_VALID="record_valid", RECORD_CLEAR="record_clear_valid",
                  RECORD_HEAD="record_write", RECORD_ROUTE="record_route_low",
                  RECORD_ADDRESS="record_address_low", HOLD_ENABLE="serial_hold_when_full",
                  RECORD_ADDRESS_LOW="record_address_low", RECORD_ADDRESS_HIGH="record_address_high",
                  RECORD_USER_LOW="record_user_word0", RECORD_USER_HIGH="record_user_word1",
                  RECORD_SECURITY="record_audit_flags")
    return (f"`define SERIAL_WORD_BITS {network['definition']['error_logs']['errors']['serial_word_bits']}\n"
            + "".join(f"`define {k} {words[v]}\n" for k, v in wanted.items() if v in words)
            + f"`define RECORD_VALID_lsb {lsbs['record_valid']}\n"
            + "".join(f"`define {k}_lsb {lsbs[v]}\n" for k, v in bits.items()))


def run(work, args):
    results = dict(schema=1, case="error_logger", runs=[], work=str(work))
    good = True
    for config in CONFIGS:
        root = work / config["label"]
        gen = root / "gen"
        root.mkdir()
        (root / "network.yaml").write_text(
            keep_definition() if config.get("keep") else
            crossed_definition() if config.get("crossed") else
            banks_definition(config["codes"]) if config.get("observers")
            else definition(config["codes"]))
        spec = root / "faults.spec.json"
        spec.write_text(json.dumps(dict(faults=[{k: v for k, v in f.items() if k != "expect"}
                                                for f in config["faults"]])) + "\n")
        params = ["--param", f"config={root / 'network.yaml'}", "--param", "service=csr",
                  "--param", f"artifacts={root / 'artifacts'}"]
        if config.get("observers"):
            (root / "observers.json").write_text(json.dumps(config["observers"]) + "\n")
            params += ["--param", f"observers={root / 'observers.json'}"]
        else:
            params += ["--param", "observe=ram", "--param", "observer=errors"]
        if config["ignored"]:
            params += ["--param", f"ignored={config['ignored']}"]
        if config.get("serial_word_bits"):
            params += ["--param", f"serial_word_bits={config['serial_word_bits']}"]
        if config.get("keep"):
            params += ["--param", "keep=0" if config.get("trimmed") else "keep=1"]
        if config.get("security"):
            (root / "security.json").write_text(json.dumps(config["security"]) + "\n")
            params += ["--param", f"security={root / 'security.json'}"]
        subprocess.run([args.fixture, "service_network", str(gen), *params,
                        "--faults", str(spec), "--probes"], check=True)
        map_path = root / "artifacts" / f"nocloom_{config.get('top', 'logged')}.map.json"
        effective = json.loads(map_path.read_text())["definition"]["error_logs"]["errors"]
        assert effective["serial_word_bits"] == config.get("serial_word_bits", 32)
        source = Path(args.keep_bench if config.get("keep") else
                      args.crossed_bench if config.get("crossed") else
                      args.banks_bench if config.get("observers") else args.bench)
        bench = (f"`define {config['define']}\n"
                 f"`define TRIMMED_RECORD {int(config.get('trimmed', False))}\n"
                 + offsets(map_path) + source.read_text())
        cells = (gen / "cells.v").read_text() if (gen / "cells.v").exists() else ""
        plain = root / "plain"
        plain.mkdir()
        def observation(path, hierarchy):
            if config["label"] != "plain":
                return ""
            groups = json.loads(path.read_text())["network"]["groups"]
            nets = next(g["nets"] for g in groups if g["name"] == "record")
            return "".join(f"`define LOG_{name.upper()} {hierarchy}.{net}\n"
                           for name, net, _ in nets)

        (plain / "tb.sv").write_text(observation(gen / "probes.json", "dut") + bench)
        (plain / "dut.v").write_text((gen / "network.v").read_text() + cells)
        faulted = root / "faulted"
        faulted.mkdir()
        (faulted / "tb.sv").write_text(observation(gen / "faulted_probes.json", "dut.core") + bench)
        components = json.loads((gen / "ports.json").read_text())
        (faulted / "dut.v").write_text(
            (gen / "faulted.v").read_text() + cells
            + fl.wrapper(components, json.loads((gen / "faults.json").read_text())))

        def compile_(where):
            return sim_engine.Simulation(where, "dut.v", engine=args.sim, config=args.sim_config,
                                       compile_timeout=300, run_timeout=120)

        def judge(sim, name, options, expect):
            result = sim.run(options)
            failed = sorted({line.split()[1] for line in result.stdout.splitlines()
                             if line.startswith("CHECK_FAIL ")})
            if expect is None:
                ok = result.returncode == 0 and not failed
            else:
                ok = result.returncode != 0 and bool(set(failed) & set(expect))
            results["runs"].append(dict(run=name, ok=ok, failed=failed[:12],
                                        exit=result.returncode))
            print(f"{name}: {'OK' if ok else 'BAD'} failed={failed[:6]}")
            return ok

        label = config["label"]
        baseline = compile_(plain)
        for options in CROSSED_RATIOS if config.get("crossed") else [()]:
            good &= judge(baseline, f"{label}/baseline{''.join(options)}", options, None)
        sim = compile_(faulted)
        results["engine"] = sim.engine
        good &= judge(sim, f"{label}/fault_build_selection_0", ("+NLV_FAULT=0",), None)
        for k, fault in enumerate(config["faults"], 1):
            good &= judge(sim, f"{label}/{fault['id']}", (f"+NLV_FAULT={k}",), fault["expect"])
    results["exit"] = 0 if good else 1
    return results


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fixture", required=True)
    p.add_argument("--bench", required=True)
    p.add_argument("--keep-bench", required=True)
    p.add_argument("--banks-bench", required=True)
    p.add_argument("--crossed-bench", required=True)
    p.add_argument("--work", required=True)
    sim_engine.add_arguments(p)
    args = p.parse_args()
    top = Path(args.work).resolve()
    top.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=top))
    try:
        results = run(work, args)
    except Exception:  # generation or compilation
        results = dict(schema=1, case="error_logger", exit=2, error=traceback.format_exc())
        print(results["error"])
    (top / "results.json").write_text(json.dumps(results, indent=1, default=str) + "\n")
    print("ERROR_LOGGER", "PASS" if results["exit"] == 0 else "FAIL")
    return results["exit"]


if __name__ == "__main__":
    sys.exit(main())
