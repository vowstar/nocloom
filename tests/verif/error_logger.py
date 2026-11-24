#!/usr/bin/env python3
"""An error logger on a service target over APB: first-error record, overflow, wait, alarm, clear.

The offsets and bit places come from the network's map. The unmodified netlist must pass; each
designated fault of the fault build (selection zero passes too) must fail on a check that names it.

  python3 tests/verif/error_logger.py --fixture FIXTURE --bench sv/nlv_logger_tb.sv
          --keep-bench sv/nlv_keep_logger_tb.sv --banks-bench sv/nlv_logger_banks_tb.sv
          --crossed-bench sv/nlv_crossed_logger_tb.sv --work DIR [--jobs N]
          [--sim ENGINE] [--sim-config JSON]

Exit code: 0 every run met its expectation, 1 a run did not, 2 infrastructure error.
"""

import argparse
import json
import subprocess
import sys
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor
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


def definition(compact):
    text = ["schema_version: 10", "name: logged", "address_width: 32",
            "reset_ports: [{name: reset_n}]", "test_ports: [{name: test}]",
            "clock_domains: [{name: main, clock_port: clk, reset_port: reset_n, test_port: test}]"]
    if compact:
        text.append("transport: {compact_failure_responses: true}")
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


def direct(at, **with_):
    return dict(component="network", op="replace", at={"binding": at}, **{"with": with_})


def latch_enable(at, **with_):
    """The condition under which a record latch takes its value."""
    return dict(component="network", op="set_operand", operand=0,
                at={"state_next": {"binding": KEY + at}}, **{"with": with_})


BANK = "burst_service/csr/bank/"
NOT_HELD = {"op": "BitNot", "args": [{"select": {"binding": KEY + "logger.valid"}}]}
NEXT_NOT_HELD = {"op": "BitNot", "args": [{"select": {"binding": KEY + "logger1.valid"}}]}

# Declared out of name order: a record's word stands as declared, zeta, Alpha, mid.
AUDIT = dict(
    flags=["zeta", "Alpha", "mid"],
    drivers=dict(cfg=dict(zeta="zero", Alpha="zero", mid="zero"),
                 data=dict(zeta="privileged", Alpha="~nonsecure", mid="instruction")),
    audit_match=dict(Alpha=1, zeta=0))

CONFIGS = [
    dict(label="plain", codes=False, skipped="", define="PLAIN", faults=[
        dict(id="overwrite_first", expect=("kept_first",),
             **replace("logger.capture", select={"binding": KEY + "logger.offered"})),
        # The cycle of a clear of record_held with a record arriving: the clear wins and the record
        # is gone, the held record stays and the arriving one is gone without a trace, one field
        # of the held record stays, record_overflow sets, the alarm falls.
        dict(id="clear_wins", expect=("clear_capture_held",), component="network", op="set_next",
             at={"binding": KEY + "logger.valid"},
             **{"with": dict(op="BitAnd", args=[
                 {"op": "BitNot", "args": [{"select": {"binding": KEY + "logger.clear"}}]},
                 {"op": "BitOr", "args": [{"select": {"binding": KEY + "logger.capture"}},
                                          {"self": True}]}])}),
        dict(id="arrival_dropped", expect=("clear_capture_trace",),
             **replace("logger.capture", op="BitAnd", args=[
                 {"select": {"binding": KEY + "logger.offered"}}, NOT_HELD])),
        dict(id="arrival_keeps_address", expect=("clear_capture_record",),
             **latch_enable("record.address", op="BitAnd", args=[{"self": True}, NOT_HELD])),
        dict(id="arrival_keeps_direction", expect=("clear_capture_direction",),
             **latch_enable("record.write", op="BitAnd", args=[{"self": True}, NOT_HELD])),
        dict(id="arrival_overflows", expect=("clear_capture_overflow",),
             **replace("logger.lost", op="BitAnd", args=[
                 {"select": {"binding": KEY + "logger.offered"}},
                 {"select": {"binding": KEY + "logger.valid"}}])),
        dict(id="alarm_falls_on_clear", expect=("clear_capture_alarm",), component="network",
             op="set_next", at={"binding": KEY + "logger.alarm"},
             **{"with": dict(op="BitAnd", args=[
                 {"op": "BitAnd", "args": [{"select": {"binding": KEY + "logger.valid"}},
                                           {"select": {"binding": BANK + "field.alarm_enable"}}]},
                 {"op": "BitNot", "args": [{"select": {"binding": KEY + "logger.clear"}}]}])}),
        dict(id="wait_yields_to_clear", expect=("wait_enters_later",),
             **replace("logger.hold", op="BitAnd", args=[
                 {"self": True},
                 {"op": "BitNot", "args": [{"select": {"binding": KEY + "logger.clear"}}]}])),
        dict(id="capture_missing", expect=("first_record",),
             **replace("logger.capture", const=0)),
        dict(id="alarm_early", expect=("alarm_cycle",),
             **replace("logger.alarm", select={"binding": KEY + "logger.valid"})),
        dict(id="wait_off", expect=("wait_kept",), **replace("logger.hold", const=0)),
        dict(id="wait_always", expect=("timeout", "first_record", "kept_first"),
             **replace("logger.hold", const=1)),
        dict(id="overflow_lost", expect=("overflow_sets",), **replace("logger.lost", const=0)),
        dict(id="overflow_on_capture", expect=("overflow_clean",),
             **replace("logger.lost", select={"binding": KEY + "logger.offered"})),
        dict(id="overflow_when_waiting", expect=("wait_no_overflow",),
             **replace("logger.lost", op="BitAnd", args=[
                 {"select": {"binding": KEY + "probe0.pending"}},
                 {"select": {"binding": KEY + "logger.valid"}}])),
        dict(id="held_clear_takes_overflow", expect=("held_clear_alone",),
             **direct(BANK + "field.record_overflow_clear",
                      select={"binding": BANK + "field.record_held_clear"})),
        dict(id="overflow_clear_takes_held", expect=("overflow_clear_alone",),
             **replace("logger.clear", op="BitOr", args=[
                 {"self": True}, {"select": {"binding": BANK + "field.record_overflow_clear"}}])),
        dict(id="overflow_clear_wins", expect=("overflow_set_wins",), component="network",
             op="set_next", at={"binding": KEY + "logger.overflow"},
             **{"with": dict(op="BitAnd", args=[
                 {"op": "BitOr", "args": [{"select": {"binding": KEY + "logger.lost"}},
                                          {"self": True}]},
                 {"op": "BitNot", "args": [
                     {"select": {"binding": BANK + "field.record_overflow_clear"}}]}])}),
        dict(id="overflow_alarm", expect=("overflow_no_alarm",),
             **replace("logger.alarm", op="BitOr", args=[
                 {"self": True}, {"select": {"binding": KEY + "logger.overflow"}}])),
        # The probe's drops while its record waits: never marked, marked but not counted at the
        # logger, a mark that stays after its record, a drop in the cycle the waiting record
        # leaves, and a clear of record_overflow that wins over a marked record.
        dict(id="probe_drop_unmarked", expect=("probe_drop_reported",),
             **replace("probe0.overflow", const=0)),
        dict(id="probe_drop_ignored", expect=("probe_drop_reported",),
             **replace("logger.probe_lost", const=0)),
        dict(id="probe_drop_stays", expect=("probe_drop_once",), component="network",
             op="set_next", at={"binding": KEY + "probe0.overflow"},
             **{"with": dict(op="BitOr", args=[
                 {"self": True}, {"select": {"binding": KEY + "event0.dropped"}}])}),
        dict(id="probe_drop_on_leave", expect=("probe_race_trace",),
             **replace("event0.dropped", op="BitAnd", args=[
                 {"select": {"binding": KEY + "event0.event"}},
                 {"select": {"binding": KEY + "probe0.pending"}}])),
        dict(id="probe_clear_wins", expect=("probe_clear_race",),
             **replace("logger.probe_lost", op="BitAnd", args=[
                 {"self": True},
                 {"op": "BitNot", "args": [
                     {"select": {"binding": BANK + "field.record_overflow_clear"}}]}])),
    ]),
    # The logger skips the decode class.
    dict(label="skipped", codes=True, skipped="1", define="SKIPPED", faults=[
        dict(id="wait_after_filter", expect=("skipped_waits",),
             **replace("logger.hold", op="BitAnd",
                       args=[{"self": True}, {"select": {"binding": KEY + "logger.accept"}}])),
        dict(id="skipped_logged", expect=("skipped_dropped",), **replace("logger.accept", const=1)),
        dict(id="overflow_of_skipped", expect=("skipped_no_overflow",),
             **replace("logger.lost", op="BitAnd", args=[
                 {"select": {"binding": KEY + "logger.taken"}},
                 {"select": {"binding": KEY + "logger.valid"}}])),
        dict(id="class_lost", expect=("target_class_logged",),
             **replace("record.class", const=0)),
    ]),
    dict(label="audit", codes=True, skipped="", define="AUDIT", audit=AUDIT, faults=[
        dict(id="flag_lost", expect=("audit_record", "audit_order"),
             **replace("record.audit_flags", const=0)),
        dict(id="flag_lost_at_target", expect=("audit_record", "audit_order"),
             component="network", op="replace", at={"binding": "observe/ram/audit_flags"},
             **{"with": dict(const=0)}),
        dict(id="filter_inverted", expect=("filter_accepts", "filter_rejects_one", "filter_rejects_zero"),
             **replace("logger.filter_match", op="BitNot", args=[{"self": True}])),
        dict(id="filter_bypassed", expect=("filter_rejects_one", "filter_rejects_zero"),
             **replace("logger.filter_match", const=1)),
        dict(id="wait_after_flag_filter", expect=("filter_waits",),
             **replace("logger.hold", op="BitAnd",
                       args=[{"self": True}, {"select": {"binding": KEY + "logger.accept"}}])),
        dict(id="overflow_of_filtered", expect=("filter_no_overflow",),
             **replace("logger.lost", op="BitAnd", args=[
                 {"select": {"binding": KEY + "logger.taken"}},
                 {"select": {"binding": KEY + "logger.valid"}}])),
    ]),
]


KEEP_FLAGS = [f"f{k:02d}" for k in range(39, -1, -1)]
# Four audit flags above the user flags on the source's AxUSER.
KEEP_AUDIT = dict(
    flags=[f"c{k}" for k in range(3, -1, -1)],
    drivers=dict(cfg={f"c{k}": "zero" for k in range(4)},
                 data={f"c{3 - k}": f"user:{40 + k}" for k in range(4)}))


def keep_definition():
    flags = ", ".join(KEEP_FLAGS)
    driven = ", ".join(f"{flag}: 'user[{k}]'" for k, flag in enumerate(KEEP_FLAGS))
    zero = ", ".join(f"{flag}: 0" for flag in KEEP_FLAGS)
    text = ["schema_version: 10", "name: kept", "address_width: 48",
            f"transport: {{user_flags: [{flags}]}}",
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
    label="keep", codes=True, skipped="", define="KEEP", keep=True, top="kept",
    audit=KEEP_AUDIT, faults=[
        dict(id="audit_lost", expect=("whole_audit_write", "slot_audit"),
             **replace("record.audit_flags", const=0)),
        dict(id="audit_of_other_context", expect=("slot_audit",),
             **context_replace("context1.valid", const=0)),
        dict(id="address_cut", expect=("whole_address_write", "slot_address", "oldest_address",
                                       "context_free_address", "touching_word_address",
                                       "short_gap_address", "miss_address"),
             **replace("record.address", op="BitAnd",
                       args=[{"self": True}, {"const": 0xffffffff}])),
        dict(id="user_cut", expect=("whole_user_write", "slot_user", "context_free_user",
                                    "miss_user"),
             **replace("record.user_flags", op="BitAnd",
                       args=[{"self": True}, {"const": 0xffffffff}])),
        dict(id="length_of_target", expect=("narrow_length", "fixed_length", "miss_length"),
             **replace("record.bytes", const=4)),
        dict(id="lock_lost", expect=("miss_lock",), **replace("record.exclusive", const=0)),
        dict(id="class_zero", expect=("whole_class_write", "miss_class"),
             **replace("record.class", const=0)),
        # The route of this network, lowest first: region_index one bit, target_path two,
        # source_path one, sequence one, direction one.
        dict(id="path_lost", expect=("whole_path_write", "slot_path_read"),
             **replace("record.route", op="BitAnd", args=[{"self": True}, {"const": 0x31}])),
        dict(id="sequence_lost", expect=("whole_origin_write", "slot_origin_read"),
             **replace("record.route", op="BitAnd", args=[{"self": True}, {"const": 0x2f}])),
        dict(id="always_context_zero", expect=("slot_address", "slot_user"),
             **context_replace("context1.valid", const=0)),
        dict(id="newest_of_key", expect=("oldest_address",),
             **context_replace("context0.older1", op="BitNot", args=[{"self": True}])),
        dict(id="fixed_address_cut", expect=("fixed_address", "fixed_write_address"),
             **replace("record.address", op="BitAnd",
                       args=[{"self": True}, {"op": "BitNot", "args": [{"const": 0x7f}]}])),
        dict(id="gap_marker_lost", expect=("gap_low_bits", "gap_marker"),
             **context_replace("marked", const=0)),
        dict(id="logging_blocks_return", expect=("data_goes_on",),
             component="network", op="replace", at={"driver": "nl_t_ram_rready"},
             **{"with": dict(op="BitAnd", args=[{"self": True},
                 {"op": "BitNot", "args": [{"select": {"binding": KEY + "logger.hold"}}]}])}),
        dict(id="gap_of_one", expect=("short_gap_address", "touching_word_address"),
             **context_replace("quiet", const=2)),
    ]))


CONFIGS.append(dict(
    label="fixed_trimmed", codes=True, skipped="", define="KEEP", keep=True, trimmed=True,
    top="kept", audit=KEEP_AUDIT, faults=[
        dict(id="fixed_address_cut", expect=("fixed_address", "fixed_write_address"),
             **replace("record.address", op="BitAnd",
                       args=[{"self": True}, {"op": "BitNot", "args": [{"const": 0x7f}]}]))]))


def banks_definition(compact):
    text = definition(compact).splitlines()
    at = text.index("connectivity:")
    text[at:at] = ["  - {name: rom, clock_domain: main, protocol: apb4, data_width: 32, "
                   "address_width: 16, attributes: amba, mappings: [{base: '0x10000', size: '0x1000'}]}"]
    text[text.index("  data: [ram]")] = "  data: [ram, rom]"
    return "\n".join(text).replace("name: logged", "name: banked") + "\n"


# Two error logs of one logger each: a log's place among them is its branch of the window's
# switch, and each bank has the first key of its chain.
BANKS = dict(audit=None, error_logs=[
    dict(name="errors", targets=["ram"], loggers=[dict(base=0x300, skipped=[])]),
    dict(name="others", targets=["rom"], loggers=[dict(base=0x100, skipped=[])])])
SERVICE = "burst_service/csr/"
# One error log with two loggers on ram: a logger's place in the chain is its bank's key. The
# first skips the decode class and deletes what it logs.
CHAIN = dict(audit=None, error_logs=[
    dict(name="errors", targets=["ram"], loggers=[dict(base=0x300, skipped=[1]),
                                                  dict(base=0x100, skipped=[])])])
PASS_ALWAYS = dict(audit=None, error_logs=[
    dict(name="errors", targets=["ram"], loggers=[dict(base=0x300, skipped=[1], pass_always=1),
                                                  dict(base=0x100, skipped=[])])])
# The header of a frame with the index of its register cleared.
INDEX_LOST = dict(op="Select", args=[{"select": {"binding": SERVICE + "bus.at_header"}},
                                     {"op": "BitAnd", "args": [{"self": True}, {"const": 0x1f0}]},
                                     {"self": True}])
CONFIGS.append(dict(
    label="banks", codes=False, skipped="", define="BANKS", top="banked", error_logs=BANKS, faults=[
        dict(id="branch_lost", expect=("rom_logged", "rom_record", "bank1_control", "clear_apart"),
             **direct(SERVICE + "bus.branch", const=0)),
        dict(id="key_lost", expect=("bank0_control", "ram_logged"),
             **direct(SERVICE + "bus.key", const=2)),
        dict(id="offset_lost", expect=("bank1_control",),
             **direct(SERVICE + "bus.word", **INDEX_LOST)),
        dict(id="clear_of_other", expect=("clear_apart",),
             **direct("observe/others/logger.clear",
                      select={"binding": "observe/errors/logger.clear"})),
        dict(id="overflow_of_other", expect=("overflow_apart",),
             **direct("observe/others/logger.lost",
                      select={"binding": "observe/errors/logger.lost"})),
    ]))
CONFIGS.append(dict(
    label="chain", codes=True, skipped="", define="CHAIN", top="banked", error_logs=CHAIN, faults=[
        dict(id="pass_dropped", expect=("next_record_held",), **replace("logger.drop", const=1)),
        dict(id="delete_off", expect=("deleted",), **replace("logger.drop", const=0)),
        dict(id="next_bank_overrun", expect=("wait_holds", "released"),
             **replace("logger.ready", op="BitNot",
                       args=[{"select": {"binding": KEY + "logger.hold"}}])),
        dict(id="clear_shared", expect=("deleted_again",),
             **replace("logger1.clear", select={"binding": KEY + "logger.clear"})),
        dict(id="key_lost", expect=("bank0_control", "next_record_held"),
             **direct(SERVICE + "bus.key", const=2)),
        dict(id="overflow_of_skipped", expect=("skipped_no_overflow",),
             **replace("logger.lost", op="BitAnd", args=[
                 {"select": {"binding": KEY + "logger.taken"}},
                 {"select": {"binding": KEY + "logger.valid"}}])),
        dict(id="class_of_first", expect=("next_record_class",),
             **replace("record1.class", select={"binding": KEY + "record.class"})),
    ]))
CONFIGS.append(dict(
    label="pass_always", codes=True, skipped="", define="PASS_ALWAYS", top="banked", error_logs=PASS_ALWAYS,
    faults=[
        dict(id="pass_always_disabled", expect=("both_logged",),
             **replace("logger.drop", select={"binding": KEY + "logger.accept"})),
        dict(id="first_only", expect=("both_logged",), **replace("logger.drop", const=1)),
        dict(id="pass_before_take", expect=("chain_waits_at_first",),
             **replace("logger1.offered", op="BitAnd", args=[
                 {"select": {"binding": KEY + "probe0.pending"}},
                 {"select": {"binding": KEY + "logger.serial_last"}}])),
        # The cycle of a clear of the second logger's record_held with a record arriving there.
        dict(id="next_arrival_dropped", expect=("chain_arrival_trace",),
             **replace("logger1.capture", op="BitAnd", args=[
                 {"select": {"binding": KEY + "logger1.offered"}}, NEXT_NOT_HELD])),
        dict(id="next_keeps_address", expect=("chain_arrival_record",),
             **latch_enable("record1.address", op="BitAnd", args=[{"self": True}, NEXT_NOT_HELD])),
        dict(id="next_arrival_overflows", expect=("chain_arrival_overflow",),
             **replace("logger1.lost", op="BitAnd", args=[
                 {"select": {"binding": KEY + "logger1.offered"}},
                 {"select": {"binding": KEY + "logger1.valid"}}])),
        dict(id="room_of_first", expect=("chain_arrival_held",),
             **replace("logger1.capture", op="BitAnd", args=[
                 {"select": {"binding": KEY + "logger1.offered"}},
                 {"op": "BitOr", "args": [NEXT_NOT_HELD,
                                          {"select": {"binding": KEY + "logger.clear"}}]}])),
    ]))


def crossed_definition():
    return """schema_version: 10
transport: {compact_failure_responses: true}
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


CROSSED = dict(audit=None, error_logs=[
    dict(name="errors", targets=["ram"], domain="slow", loggers=[dict(base=0x300, skipped=[])])])
SERIAL = "cut/service/csr/"
RECORD = "cut/service/observe/errors/ram/"
CONFIGS.append(dict(
    label="crossed", codes=False, skipped="", define="CROSSED", top="crossed", error_logs=CROSSED,
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
        dict(id="sender_overrun", expect=("fourth_record", "fifth_record"),
             **replace("sender0/sender.moved", select={"binding": KEY + "sender0/sender.valid"})),
        dict(id="wait_off", expect=("released", "released_address"),
             **replace("logger.hold", const=0)),
        dict(id="clear_lost", expect=("cleared",), **replace("logger.clear", const=0)),
        dict(id="alarm_stuck", expect=("alarm_quiet",), **replace("logger.valid", const=1)),
        dict(id="overflow_lost", expect=("overflow_sets",), **replace("logger.lost", const=0)),
        # The cycle of a clear of record_held with a record arriving, on the bank's clock.
        dict(id="arrival_dropped", expect=("crossed_arrival_trace",),
             **replace("logger.capture", op="BitAnd", args=[
                 {"select": {"binding": KEY + "logger.offered"}}, NOT_HELD])),
        dict(id="arrival_keeps_address", expect=("crossed_arrival_record",),
             **latch_enable("record.address", op="BitAnd", args=[{"self": True}, NOT_HELD])),
        dict(id="arrival_overflows", expect=("crossed_arrival_overflow",),
             **replace("logger.lost", op="BitAnd", args=[
                 {"select": {"binding": KEY + "logger.offered"}},
                 {"select": {"binding": KEY + "logger.valid"}}])),
        # The sender's drops: never marked, marked but not counted at the logger, a mark that
        # stays after its record, a drop in the cycle the kept record leaves, a mark one cycle
        # late on the word, and a clear of record_overflow that wins over a marked record.
        dict(id="probe_drop_unmarked", expect=("probe_drop_reported",),
             **replace("sender0/probe.overflow", const=0)),
        dict(id="probe_drop_ignored", expect=("probe_drop_reported",),
             **replace("logger.probe_lost", const=0)),
        dict(id="probe_drop_stays", expect=("probe_drop_once",), component="network",
             op="set_next", at={"binding": KEY + "sender0/probe.overflow"},
             **{"with": dict(op="BitOr", args=[
                 {"self": True}, {"select": {"binding": KEY + "sender0/event.dropped"}}])}),
        dict(id="probe_drop_on_leave", expect=("probe_race_trace",),
             **replace("sender0/event.dropped", op="BitAnd", args=[
                 {"select": {"binding": KEY + "sender0/event.event"}},
                 {"select": {"binding": KEY + "sender0/sender.valid"}}])),
        dict(id="probe_mark_late", expect=("probe_race_trace",),
             **replace("sender0/probe.overflow", delay={"self": True})),
        dict(id="probe_clear_wins", expect=("probe_clear_race",),
             **replace("logger.probe_lost", op="BitAnd", args=[
                 {"self": True},
                 {"op": "BitNot", "args": [
                     {"select": {"binding": BANK + "field.record_overflow_clear"}}]}])),
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


# A word of the bank by one field of it, and the fields the table puts in the same word.
WORDS = dict(ID=("layout",),
             STATE=("record_held", "record_overflow", "alarm_enable", "wait_when_full",
                    "record_class", "record_write", "record_wrap", "record_exclusive",
                    "record_address_partial", "record_sequence"),
             ADDRESS=("record_address_low",),
             EXTENT=("record_bytes", "record_address_high"),
             PATH=("record_source_path", "record_target_path"),
             REGION=("record_region_index",),
             AUDIT=("record_audit_flags",),
             USER0=("record_user_flags0",), USER1=("record_user_flags1",))


def offsets(map_path):
    """The bank's size, the offset of each word and the lsb of each field, from the map."""
    network = json.loads(map_path.read_text())
    bank = network["definition"]["log_registers"]["csr"]
    # A bank's record is its name, its place in the window, its layout, its capabilities, the
    # places of its log and of its logger, and its registers. Its key on the serial path follows
    # from its place among the loggers and is no part of the map.
    for record in bank.get("banks", []):
        assert set(record) == {"bank", "base", "capabilities", "layout", "log_index",
                               "logger_index", "registers"}, sorted(record)
    assert set(bank) - {"banks"} == {"access_kinds", "bank", "bank_bytes", "base", "capabilities",
                                     "error_classes", "layout", "log_index", "logger_index",
                                     "registers", "unmapped"}, sorted(bank)
    fields = {r["name"]: r for r in bank["registers"]}
    lines = [f"`define SERIAL_WORD_BITS "
             f"{network['definition']['error_logs']['errors']['serial_word_bits']}\n",
             f"`define BANK_BYTES {bank['bank_bytes']}\n"]
    for word, names in WORDS.items():
        here = {fields[name]["offset"] for name in names if name in fields}
        assert len(here) <= 1, (word, here)
        lines += [f"`define OFFSET_{word} {offset}\n" for offset in here]
    lines += [f"`define {name.upper()}_lsb {field['lsb']}\n" for name, field in fields.items()]
    return "".join(lines)


def run_config(work, args, config):
    """Generate, build and judge one configuration in its own directory."""
    runs, lines, good = [], [], True
    root = work / config["label"]
    gen = root / "gen"
    root.mkdir()
    (root / "network.yaml").write_text(
        keep_definition() if config.get("keep") else
        crossed_definition() if config.get("crossed") else
        banks_definition(not config["codes"]) if config.get("error_logs")
        else definition(not config["codes"]))
    spec = root / "faults.spec.json"
    spec.write_text(json.dumps(dict(faults=[{k: v for k, v in f.items() if k != "expect"}
                                            for f in config["faults"]])) + "\n")
    params = ["--param", f"config={root / 'network.yaml'}", "--param", "service=csr",
              "--param", f"capture_class={int(config['codes'])}",
              "--param", f"artifacts={root / 'artifacts'}"]
    if config.get("error_logs"):
        (root / "error_logs.json").write_text(json.dumps(config["error_logs"]) + "\n")
        params += ["--param", f"error_logs={root / 'error_logs.json'}"]
    else:
        params += ["--param", "watch=ram", "--param", "log_name=errors"]
    if config["skipped"]:
        params += ["--param", f"skipped={config['skipped']}"]
    if config.get("serial_word_bits"):
        params += ["--param", f"serial_word_bits={config['serial_word_bits']}"]
    if config.get("keep"):
        params += ["--param", "keep=0" if config.get("trimmed") else "keep=1"]
    if config.get("audit"):
        (root / "audit.json").write_text(json.dumps(config["audit"]) + "\n")
        params += ["--param", f"audit={root / 'audit.json'}"]
    subprocess.run([args.fixture, "service_network", str(gen), *params,
                    "--faults", str(spec), "--probes"], check=True)
    map_path = root / "artifacts" / f"nocloom_{config.get('top', 'logged')}.map.json"
    effective = json.loads(map_path.read_text())["definition"]["error_logs"]["errors"]
    assert effective["serial_word_bits"] == config.get("serial_word_bits", 32)
    source = Path(args.keep_bench if config.get("keep") else
                  args.crossed_bench if config.get("crossed") else
                  args.banks_bench if config.get("error_logs") else args.bench)
    bench = (f"`define {config['define']}\n"
             f"`define TRIMMED_RECORD {int(config.get('trimmed', False))}\n"
             + offsets(map_path) + source.read_text())
    cells = (gen / "cells.v").read_text() if (gen / "cells.v").exists() else ""
    plain = root / "plain"
    plain.mkdir()
    def observation(path, hierarchy):
        # The nets of the first logger as LOG_<NET>, of the next in a chain as LOG1_<NET>,
        # of a crossed log's first sender as SENDER_<NET>, of a log's first probe in its
        # domain as PROBE_<NET>.
        prefix = dict(record="LOG_", record1="LOG1_", sender="SENDER_", probe="PROBE_")
        groups = json.loads(path.read_text())["network"]["groups"]
        return "".join(f"`define {prefix[group['name']]}{name.upper()} {hierarchy}.{net}\n"
                       for group in groups if group["name"] in prefix
                       for name, net, _ in group["nets"])

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
        runs.append(dict(run=name, ok=ok, failed=failed[:12], exit=result.returncode))
        lines.append(f"{name}: {'OK' if ok else 'BAD'} failed={failed[:6]}")
        return ok

    label = config["label"]
    baseline = compile_(plain)
    for options in CROSSED_RATIOS if config.get("crossed") else [()]:
        good &= judge(baseline, f"{label}/baseline{''.join(options)}", options, None)
    sim = compile_(faulted)
    good &= judge(sim, f"{label}/fault_build_selection_0", ("+NLV_FAULT=0",), None)
    for k, fault in enumerate(config["faults"], 1):
        good &= judge(sim, f"{label}/{fault['id']}", (f"+NLV_FAULT={k}",), fault["expect"])
    return dict(runs=runs, lines=lines, good=good, engine=sim.engine)


def run(work, args):
    """Run the configurations, args.jobs at a time; report them in their listed order."""
    results = dict(schema=1, case="error_logger", runs=[], work=str(work))
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        outcomes = list(pool.map(lambda config: run_config(work, args, config), CONFIGS))
    good = True
    for outcome in outcomes:
        print("\n".join(outcome["lines"]), flush=True)
        results["runs"] += outcome["runs"]
        results["engine"] = outcome["engine"]
        good &= outcome["good"]
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
    p.add_argument("--jobs", type=int, default=1, help="configurations run at once")
    sim_engine.add_arguments(p)
    args = p.parse_args()
    if args.jobs < 1:
        p.error("--jobs must be positive")
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
