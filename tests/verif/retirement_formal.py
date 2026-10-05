"""Check generated read replay and AHB cancellation with bounded RTL models.

The drivers use complete packets and two cycle AHB responses. Symbolic payloads
and response codes remain free. Every safety check has a reachable completion
cover. Fault runs must produce an assertion counterexample, including a trace.
"""

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path


def run(argv, cwd, log, limit=300):
    with (cwd / log).open("w") as out:
        return subprocess.run([str(x) for x in argv], cwd=cwd, stdout=out,
                              stderr=subprocess.STDOUT, timeout=limit).returncode


def instance(component, expressions):
    declarations, connections = [], []
    for p in component["ports"]:
        name, width = p["name"], p["width"]
        span = f"[{width - 1}:0] " if width > 1 else ""
        value = expressions.get(name, f"{width}'d0")
        declarations.append(f"wire {span}{name}" + (f" = {value};" if p["input"] else ";"))
        connections.append(f".{name}({name})")
    return "\n".join(declarations) + "\n" + component["module"] + " dut(" + ",".join(connections) + ");\n"


def replay(component, kind, k, shared, within=False):
    merge = kind == "merge"
    request = "nl_up_rreq_" if merge else "nl_rreq_"
    target = "nl_down_rrsp_" if merge else "nl_axi_r"
    result = "nl_up_rrsp_" if merge else "nl_rrsp_"
    ready = "nl_down_rreq_ready" if merge else "nl_axi_arready"
    # Reads have two physical words and start one byte above their container base.
    # Distinct IDs return in reverse order. A shared ID must return in issue order.
    values = {"nl_clk": "clk", "nl_reset_n": "tick != 0 && tick != 12", ready: "1'b1",
              request + "valid": f"sent < {k}", request + "id": "shared ? 0 : sent",
              request + "address": "sent * 16", request + "start_address": "sent * 16 + 1",
              request + "length": "1" if within else "7" if merge else "1", request + "size": "0" if merge else "2",
              request + "burst": "2", request + "count": "1" if merge else "4",
              request + "source": "1",
              request + "first": "1", request + "last": "1", result + "ready": "tick[1:0] != 2",
              target + "valid": f"sent == {k} && returned < {k} && response_delay == 3",
              target + ("id" if merge else "id"): "shared ? 0 : " + (
                  "transaction" if merge else "{transaction[0],transaction[1]}"),
              target + ("data" if merge else "data"): "word_data",
              target + ("last" if merge else "last"): "1'b1" if within else "word_index",
              target + "first": "!word_index", target + "address": "transaction * 16 + word_index * 4",
              target + "count": "4", target + ("result" if merge else "resp"): "error_packet ? 2 : 0"}
    last_beat = 1 if within else 7 if merge else 2
    pre = f"""
module tb;
(* gclk *) reg clk;
reg [7:0] tick = 0;
reg [5:0] sent = 0, returned = 0, output_transaction = 0, output_beat = 0;
reg word_index = 0;
reg [2:0] response_delay = 0;
localparam shared = {int(shared)};
wire [1:0] transaction = shared ? returned : {k - 1} - returned;
(* anyconst *) reg [31:0] salt;
(* anyconst *) reg error_packet;
wire [31:0] word_data = salt ^ (32'h04040404 * transaction) ^ (word_index ? 32'hf0e0d0c0 : 0);
wire [1:0] expected_transaction = shared ? output_transaction : {k - 1} - output_transaction;
wire expected_word = {"1'b0" if within else "output_beat >= 3 && output_beat < 7" if merge else "output_beat == 1"};
wire [31:0] expected_data = salt ^ (32'h04040404 * expected_transaction)
                           ^ (expected_word ? 32'hf0e0d0c0 : 0);
"""
    body = f"""
always @(posedge clk) begin
  if (tick != 127) tick <= tick + 1;
  if (tick == 0 || tick == 12) begin
    sent <= 0; returned <= 0; word_index <= 0; output_transaction <= 0; output_beat <= 0;
    response_delay <= 0;
  end
  else begin
    if (sent == {k} && response_delay != 3) response_delay <= response_delay + 1;
    if ({request}valid && {request}ready) sent <= sent + 1;
    if ({target}valid && {target}ready) begin
      word_index <= {"1'b0" if within else "!word_index"};
      if ({"1'b1" if within else "word_index"}) returned <= returned + 1;
    end
    if ({result}valid && {result}ready) begin
      REPLAY_NO_DUP: assert(output_transaction < {k});
      REPLAY_IDENTITY: assert({result}id == (shared ? 0 : expected_transaction));
      REPLAY_ORDER: assert({result}first == (output_beat == 0));
      REPLAY_LAST: assert({result}last == (output_beat == {last_beat}));
      REPLAY_RESULT: assert({result}result == (error_packet ? 2 : 0));
      {('REPLAY_ADDRESS: assert(' + result + 'address == expected_transaction * 16 + (output_beat + 1) % ' + ('2' if within else '8') + ');') if merge else ''}
      {('REPLAY_ADDRESS: assert(' + result + 'address == expected_transaction * 16 + (output_beat == 1 ? 4 : 0));') if not merge else ''}
      if (error_packet) REPLAY_ERROR_ZERO: assert({result}data == 0);
      else {('REPLAY_BYTES: assert(((' + result + 'data ^ expected_data) >> (((output_beat + 1) % ' + ('2' if within else '4') + ') * 8) & 255) == 0);') if merge else ('REPLAY_BYTES: assert(' + result + 'data == (output_beat == 0 ? expected_data & 32\'hffffff00 : output_beat == 2 ? expected_data & 255 : expected_data));')}
      output_beat <= output_beat == {last_beat} ? 0 : output_beat + 1;
      if (output_beat == {last_beat}) output_transaction <= output_transaction + 1;
    end
    REPLAY_CAPACITY: assert(sent <= {k});
    if (tick == 60) REPLAY_PROGRESS_BOUND: assert(output_transaction == {k});
    REPLAY_REACH_ALL: cover(sent == {k} && returned == 0);
    REPLAY_COMPLETE: cover(output_transaction == {k});
  end
end
"""
    bindings = {p["signal"]: p["name"] for p in component["ports"] if p["endpoint"] == "probe"}
    for n in range(k):
        stem = f"r.context{n}." if merge else f"r.slot{n}."
        valid = bindings[stem + "saved_valid"]
        live = bindings[stem + ("used" if merge else "valid")]
        body += f"always @(posedge clk) if (tick > 1) REPLAY_OWNERSHIP_{n}: assert(!{valid} || {live});\n"
        body += f"always @(posedge clk) if (tick == 1 || tick == 13) REPLAY_RESET_{n}: assert(!{valid});\n"
        done = bindings["r.complete" if merge else "r.pop"]
        selection = bindings["r.slot" if merge else "r.join_index"]
        drain = bindings["r.drain" if merge else "r.align_purge"]
        selected = bindings["r.slot" if merge else "r.align_index"]
        body += f"always @(posedge clk) if (tick > 1 && {drain} && {selected} == {n}) REPLAY_NEEDS_SAVED_{n}: assert({valid});\n"
        if within:
            body += f"always @(posedge clk) if (tick > 1) REPLAY_NO_SMALL_RESERVATION_{n}: assert(!{valid});\n"
        else:
            body += f"always @(posedge clk) REPLAY_SAVE_REACHED_{n}: cover({valid});\n"
        body += f"""always @(posedge clk) if (tick > 2 && tick != 12 && tick != 13 && $past({valid}) &&
            !$past({done} && {selection} == {n})) REPLAY_RELEASE_{n}: assert({valid});\n"""
        data_key = stem + ("saved_data" if merge else "saved_data")
        data = bindings[data_key]
        body += f"""always @(posedge clk) if (tick > 2 && tick != 13 &&
          {valid} && $past({valid})) REPLAY_SLOT_STABLE_{n}: assert({data} == $past({data}));\n"""
    payload = "{" + ",".join(result + n for n in ("data", "result", "id", "first", "last")) + "}"
    body += f"""always @(posedge clk) if (tick > 2 && tick != 12 && tick != 13 && $past({result}valid && !{result}ready)) begin
  REPLAY_STABLE: assert({result}valid && {payload} == $past({payload}));
end
endmodule
"""
    return pre + instance(component, values) + body


def capacity(component, kind, k):
    merge = kind == "merge"
    req = "nl_up_rreq_" if merge else "nl_rreq_"
    values = {"nl_clk": "clk", "nl_reset_n": "tick != 0",
              "nl_down_rreq_ready" if merge else "nl_axi_arready": "1",
              req + "valid": f"sent < {k + 1}", req + "id": "0", req + "source": "1",
              req + "address": "0", req + "start_address": "1",
              req + "length": "7" if merge else "1", req + "size": "0" if merge else "2",
              req + "burst": "2", req + "count": "1" if merge else "4",
              req + "first": "1", req + "last": "1"}
    return """module tb;
(* gclk *) reg clk;
reg [7:0] tick = 0;
reg [5:0] sent = 0;
""" + instance(component, values) + f"""
always @(posedge clk) begin
  if (tick != 63) tick <= tick + 1;
  if (tick != 0 && {req}valid && {req}ready) sent <= sent + 1;
  REPLAY_CAPACITY: assert(sent <= {k});
  if (tick == {k + 5}) REPLAY_ALL_K: assert(sent == {k});
  REPLAY_REACH_ALL: cover(sent == {k});
end
endmodule
"""


def ahb(component, direction, fault_at):
    values = {"nl_clk": "clk", "nl_reset_n": "tick != 0",
              "nl_ahb_hreadyout": "ready", "nl_ahb_hresp": "response", "nl_ahb_hrdata": "32'h87654321",
              "nl_wrsp_ready": "tick >= 25 && tick[1:0] != 2", "nl_rrsp_ready": "tick >= 25 && tick[1:0] != 2",
              "nl_wreq_valid": "write_words < 5" if direction == "write" else "0",
              "nl_wreq_data": "32'h12345678 + write_words", "nl_wreq_be": "15",
              "nl_wreq_first": "write_words == 0 || write_words == 4", "nl_wreq_last": "write_words >= 3",
              "nl_wreq_length": "write_words < 4 ? 3 : 0", "nl_wreq_size": "2",
              "nl_wreq_count": "4", "nl_wreq_burst": "1", "nl_wreq_id": "write_words < 4 ? 1 : 2",
              "nl_wreq_address": "32'h100 + write_words * 4", "nl_wreq_start_address": "32'h100",
              "nl_rreq_valid": "read_packets < 2" if direction == "read" else "0",
              "nl_rreq_address": "32'h200 + read_packets * 64",
              "nl_rreq_start_address": "32'h200 + read_packets * 64", "nl_rreq_length": "3",
              "nl_rreq_size": "2", "nl_rreq_count": "4", "nl_rreq_burst": "1",
              "nl_rreq_id": "read_packets + 1", "nl_rreq_first": "1", "nl_rreq_last": "1"}
    pre = f"""module tb;
(* gclk *) reg clk;
reg [7:0] tick = 0;
reg [3:0] write_words = 0, read_packets = 0, reads = 0, writes = 0;
reg pending = 0, bad = 0;
reg [31:0] accepted_address = 0;
reg [1:0] phase = 0;
(* anyconst *) reg split;
wire ready = !pending || (bad ? phase == 2 : phase >= 1);
wire [1:0] response = pending && bad && phase >= 1 ? (split ? 3 : 2) : 0;
reg was_abort = 0;
"""
    base = "32'h100" if direction == "write" else "32'h200"
    body = f"""
always @(posedge clk) begin
  if (tick != 127) tick <= tick + 1;
  if (tick == 0) begin pending <= 0; phase <= 0; end
  else begin
    if (nl_wreq_valid && nl_wreq_ready) write_words <= write_words + 1;
    if (nl_rreq_valid && nl_rreq_ready) read_packets <= read_packets + 1;
    if (pending && !ready) phase <= phase + 1;
    if (ready) begin
      pending <= nl_ahb_hsel && nl_ahb_htrans[1];
      if (nl_ahb_hsel && nl_ahb_htrans[1]) accepted_address <= nl_ahb_haddr;
      phase <= 0;
      bad <= nl_ahb_haddr == {base} + {fault_at * 4};
    end
    if (was_abort) AHB_CANCEL_ADDRESS: assert(nl_ahb_htrans == 0 && !nl_ahb_hsel);
    was_abort <= pending && !ready && response >= 2;
    {('if (pending) AHB_WRITE_BYTES: assert(nl_ahb_hwdata == 32\'h12345678 + (accepted_address - 32\'h100) / 4);') if direction == 'write' else ''}
    if (nl_ahb_hsel && nl_ahb_htrans[1] && ready)
      AHB_NO_REISSUE: assert(!(nl_ahb_haddr > {base} + {fault_at * 4} && nl_ahb_haddr < {base} + 16));
    if (nl_wrsp_valid && nl_wrsp_ready) begin
      AHB_B_ONCE: assert(writes < 2);
      AHB_WRITE_ERROR: assert(nl_wrsp_result == (writes == 0 ? 2 : 0));
      AHB_B_ID: assert(nl_wrsp_id == writes + 1);
      writes <= writes + 1;
    end
    if (nl_rrsp_valid && nl_rrsp_ready) begin
      AHB_R_ONCE: assert(reads < 8);
      AHB_READ_ERROR: assert(nl_rrsp_result == (reads >= {fault_at} && reads < 4 ? 2 : 0));
      AHB_R_ID: assert(nl_rrsp_id == (reads < 4 ? 1 : 2));
      AHB_R_LAST: assert(nl_rrsp_last == (reads == 3 || reads == 7));
      if (nl_rrsp_result != 0) AHB_READ_ZERO: assert(nl_rrsp_data == 0);
      reads <= reads + 1;
    end
    AHB_COMPLETE: cover({"writes == 2" if direction == "write" else "reads == 8"});
    AHB_ABORT_REACHED: cover(was_abort);
    AHB_NO_OVERFLOW: assert(nl_probe_r_count + nl_probe_r_inflight <= 2);
    AHB_PHASE_ACCOUNT: assert(nl_probe_bus_data_phase == pending);
    if (tick == 60) AHB_DRAIN_BOUND: assert({"writes == 2" if direction == "write" else "reads == 8"});
    if (tick == 1) AHB_RESET: assert(!nl_probe_bus_abort_pending);
  end
end
always @(posedge clk) if (tick > 2 && $past(pending && !ready))
  AHB_DATA_HOLD: assert(nl_ahb_hwdata == $past(nl_ahb_hwdata));
always @(posedge clk) if (tick > 2 && $past(nl_rrsp_valid && !nl_rrsp_ready)) begin
  AHB_RESPONSE_HOLD: assert(nl_rrsp_valid &&
    {{nl_rrsp_data,nl_rrsp_result,nl_rrsp_id,nl_rrsp_last}} ==
    $past({{nl_rrsp_data,nl_rrsp_result,nl_rrsp_id,nl_rrsp_last}}));
end
endmodule
"""
    return pre + instance(component, values) + body


def check(args, root, kind, shared=False, direction="read", at=0, fault=None, slots=2, fill=False, within=False):
    label = f"{kind}_{int(shared)}_{direction}_{at}_{slots}_{int(fill)}_{int(within)}" + ("_" + fault["id"] if fault else "")
    work = root / label
    work.mkdir()
    cmd = [args.fixture, "replay_safety", work, "--param", f"kind={kind}",
           "--param", f"capacity={slots}"]
    if fault:
        (work / "fault.json").write_text(json.dumps({"faults": [dict(fault, component="dut")]}))
        cmd += ["--faults", work / "fault.json"]
    rc = run(cmd, work, "generate.log")
    if rc:
        raise RuntimeError(f"fixture failed: {work}")
    component = json.loads((work / "ports.json").read_text())[0]
    harness = (ahb(component, direction, at) if kind == "ahb" else
               capacity(component, kind, slots) if fill else replay(component, kind, slots, shared, within))
    (work / "tb.sv").write_text(harness)
    rtl = "faulted.v" if fault else "network.v"
    # Fault copies add their selector to the component. Tie it high in the wrapper.
    if fault:
        original = component["module"]
        modules = json.loads((work / "faults.json").read_text())["modules"]
        (work / "tb.sv").write_text(harness.replace(original + " dut(",
            modules[0]["core"] + " dut(.nl_nlv_fault_sel(8'd1),"))
    tasks = "check" if fault else "check cover"
    (work / "safety.sby").write_text(f"""[tasks]
{tasks.replace(' ', chr(10))}
[options]
check: mode bmc
{'' if fault else 'cover: mode cover'}
depth 64
timeout 240
[engines]
smtbmc z3
[script]
read -formal {rtl} tb.sv
prep -top tb
async2sync
dffunmap
[files]
{rtl}
tb.sv
""")
    rc = run(["sby", "safety.sby"], work, "sby.log", 290)
    status = (work / "safety_check/status").read_text().split()[0]
    if fault:
        traces = list((work / "safety_check").rglob("*.vcd"))
        assert rc != 0 and status == "FAIL" and traces, (label, rc, status)
        names = re.findall(r"Assert failed in .*?: ([A-Za-z0-9_]+)", (work / "sby.log").read_text())
        expected = {
            "alias_last_slot": ("REPLAY_BYTES", "REPLAY_SLOT_STABLE"),
            "saved_word_corrupt": ("REPLAY_BYTES", "REPLAY_SLOT_STABLE"),
            "early_release": ("REPLAY_NEEDS_SAVED",), "missing_release": ("REPLAY_OWNERSHIP",),
            "reset_leak": ("REPLAY_RESET",), "wrong_id": ("REPLAY_IDENTITY",),
            "duplicate_tail": ("REPLAY_LAST",), "all_ones_limit": ("REPLAY_ALL_K",),
            "newest_same_id": ("REPLAY_ADDRESS", "REPLAY_BYTES", "REPLAY_LAST",),
            "replay_needs_ar": ("REPLAY_PROGRESS_BOUND", "REPLAY_ADDRESS", "REPLAY_IDENTITY",),
            "drain_never_releases": ("REPLAY_NO_DUP", "REPLAY_LAST", "REPLAY_PROGRESS_BOUND",
                                      "REPLAY_NEEDS_SAVED", "REPLAY_IDENTITY",),
            "save_second_word": ("REPLAY_BYTES", "REPLAY_SLOT_STABLE",),
            "small_word_reservation": ("REPLAY_NO_SMALL_RESERVATION",),
            "false_success": ("AHB_WRITE_ERROR",), "read_success": ("AHB_READ_ERROR",),
            "cancel_seq": ("AHB_CANCEL_ADDRESS",), "extra_last": ("AHB_R_LAST",),
            "overflow": ("AHB_NO_OVERFLOW",), "write_data_moves": ("AHB_DATA_HOLD", "AHB_WRITE_BYTES"),
            "reset_abort": ("AHB_RESET",), "reissue_failure": ("AHB_NO_REISSUE",),
        }[fault["id"]]
        assert any(n.startswith(expected) for n in names), (label, names, expected)
    else:
        assert rc == 0 and status == "PASS", (label, rc, status)
        assert (work / "safety_cover/status").read_text().split()[0] == "PASS", label
    return dict(label=label, status="counterexample" if fault else "bounded", depth=64,
                caught_by=names if fault else [],
                inputs={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in (work / rtl, work / "tb.sv")})


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fixture", required=True)
    p.add_argument("--work", required=True)
    args = p.parse_args()
    Path(args.work).mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="retirement-", dir=args.work))
    results = []
    for kind in ("merge", "outlet"):
        for shared in (False, True):
            results.append(check(args, root, kind, shared))
        for slots in (2, 3, 4, 16):
            results.append(check(args, root, kind, slots=slots, fill=True))
    results.append(check(args, root, "merge", within=True))
    for direction in ("read", "write"):
        for at in (0, 1, 3):
            results.append(check(args, root, "ahb", direction=direction, at=at))
    for kind in ("merge", "outlet"):
        prefix = "r.context" if kind == "merge" else "r.slot"
        data = prefix + "1.saved_data"
        valid = prefix + "0.saved_valid"
        result = "nl_up_rrsp_" if kind == "merge" else "nl_rrsp_"
        faults = [
            dict(id="alias_last_slot", op="replace", at={"binding": data},
                 **{"with": {"select": {"binding": prefix + "0.saved_data"}}}),
            dict(id="saved_word_corrupt", op="set_next", at={"binding": data},
                 **{"with": {"const": 0}}),
            dict(id="early_release", op="set_next", at={"binding": valid},
                 **{"with": {"const": 0}}),
            dict(id="missing_release", op="set_next", at={"binding": valid},
                 **{"with": {"op": "BitOr", "args": [
                     {"select": {"state_next": {"binding": valid}}}, {"self": 1}]}}),
            dict(id="reset_leak", op="remove_reset", at={"binding": valid}),
            dict(id="wrong_id", op="set_driver", port=result + "id", **{"with": {"add": 1}}),
            dict(id="duplicate_tail", op="set_driver", port=result + "last", **{"with": {"const": 1}}),
        ]
        for fault in faults:
            results.append(check(args, root, kind, fault=fault))
        live = prefix + "0." + ("used" if kind == "merge" else "valid")
        fault = dict(id="all_ones_limit", op="replace", at={"driver": "nl_up_rreq_ready" if kind == "merge" else "nl_rreq_ready"},
                     **{"with": {"op": "BitAnd", "args": [{"self": 1},
                         {"op": "BitNot", "args": [{"select": {"binding": live}}]}]}})
        results.append(check(args, root, kind, fault=fault, fill=True))
        fault = dict(id="newest_same_id", op="set_next", at={"binding": "r.age0_1"},
                     **{"with": {"const": 0}})
        results.append(check(args, root, kind, shared=True, fault=fault))
        drain = "r.drain" if kind == "merge" else "r.align_purge"
        request = "nl_up_rreq_valid" if kind == "merge" else "nl_rreq_valid"
        fault = dict(id="replay_needs_ar", op="set_driver", port=result + "valid",
                     **{"with": {"op": "BitAnd", "args": [{"self": 1},
                         {"op": "BitOr", "args": [
                             {"op": "BitNot", "args": [{"select": {"binding": drain}}]},
                             {"select": {"input": request}}]}]}})
        results.append(check(args, root, kind, fault=fault))
        fault = dict(id="drain_never_releases", op="set_next", at={"binding": drain},
                     **{"with": {"op": "BitOr", "args": [
                         {"select": {"state_next": {"binding": drain}}}, {"self": 1}]}})
        results.append(check(args, root, kind, fault=fault))
        target = "nl_down_rrsp_" if kind == "merge" else "nl_axi_r"
        fault = dict(id="save_second_word", op="set_next", at={"binding": prefix + "0.saved_data"},
                     **{"with": {"op": "Select", "args": [
                         {"op": "BitAnd", "args": [{"select": {"input": target + "valid"}},
                             {"select": {"input": target + "last"}}]},
                         {"select": {"input": target + "data"}}, {"self": 1}]}})
        results.append(check(args, root, kind, fault=fault))
    fault = dict(id="small_word_reservation", op="set_next", at={"binding": "r.context0.saved_valid"},
                 **{"with": {"op": "Select", "args": [{"select": {"binding": "r.accept"}}, {"const": 1}, {"self": 1}]}})
    results.append(check(args, root, "merge", within=True, fault=fault))
    faults = [
        ("write", 0, dict(id="false_success", op="set_driver", port="nl_wrsp_result", **{"with": {"const": 0}})),
        ("read", 0, dict(id="read_success", op="set_driver", port="nl_rrsp_result", **{"with": {"const": 0}})),
        ("read", 1, dict(id="cancel_seq", op="set_driver", port="nl_ahb_htrans", **{"with": {"select": {"binding": "bus.htrans"}}})),
        ("read", 3, dict(id="extra_last", op="set_driver", port="nl_rrsp_last", **{"with": {"const": 1}})),
        ("read", 3, dict(id="overflow", op="replace", at={"binding": "r.room"}, **{"with": {"const": 1}})),
        ("write", 1, dict(id="write_data_moves", op="set_driver", port="nl_ahb_hwdata", **{"with": {"select": {"binding": "w.data"}}})),
        ("read", 1, dict(id="reset_abort", op="remove_reset", at={"binding": "bus.abort_pending"})),
        ("read", 1, dict(id="reissue_failure", op="set_next", at={"binding": "r.packet1.abort"}, **{"with": {"const": 0}})),
    ]
    for direction, at, fault in faults:
        results.append(check(args, root, "ahb", direction=direction, at=at, fault=fault))
    # Repeat the unmodified models after the fault builds.
    results.append(check(args, root, "merge", shared=True, direction="restored"))
    results.append(check(args, root, "ahb", direction="write", at=2))
    (root / "results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
