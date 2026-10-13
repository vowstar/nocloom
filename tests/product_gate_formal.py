"""Generated two-domain outlet and merge gates against freely clocked hardware.

Only domain edge events and the initial common reset are constrained. The input
data, requests, target responses and backpressure remain free. The composition
itself supplies the two wakes, their activity registers and every connection.
GATE_EQUAL compares every product port on every tick, including ready and valid.
The implication checks are assertions, never assumptions. WAKE_* also require
the selected term to reach its actual wake. Restoring a narrower wake term tests
that contract separately from functional equivalence.

product_hold proves every generated gated register's next value equals its
current value while that register's gate is closed, in the ungated product.
The hold checks and their helper invariants form one conjunctive PDR target,
so each check participates in the common induction relation.
product_prove uses those discharged lemmas for induction over the paired
states. The runner requires product_hold to pass on the same generated input.
"""
import json
import hashlib
import re
import subprocess

from async_fifo_formal import cover_locations, finish


def cover_tasks(text, packet_steps=False, inline_steps=False):
    """Keep every cover, with packet witnesses separate from gate witnesses."""
    pattern = r"^\s*(COVER_\w+): cover[^\n]*\n"
    labels = set(re.findall(pattern, text, re.M))
    packet = {"COVER_DECODE_PACKET"} & labels
    groups = [("cover", labels - packet), ("cover_decode", packet)] if packet else [("cover", labels)]
    for name, selected in groups:
        assert selected
        model = re.sub(pattern, lambda m: m[0] if m[1] in selected else "", text, flags=re.M)
        if name == "cover_decode" and packet_steps:
            # A stronger existential goal: the original packet must appear on a
            # trace with both domains advancing. Safety inputs stay unconstrained.
            marker = "always @(posedge nl_tick) nl_f_init <= 1'b0;\n"
            assert model.count(marker) == 1
            if inline_steps:
                history = ("reg nl_cover_steps=1;\n"
                           "always @(posedge nl_tick) nl_cover_steps<=nl_cover_steps && nl_step0 && nl_step1;\n")
                predicate = "nl_cover_steps && nl_step0 && nl_step1"
            else:
                history = ("wire nl_decode_inputs=(nl_step0 == 1'b1) && (nl_step1 == 1'b1);\n"
                           "reg nl_decode_history=1;\n"
                           "always @(posedge nl_tick) nl_decode_history<=nl_decode_history && nl_decode_inputs;\n")
                predicate = "nl_decode_history && nl_decode_inputs"
            model = model.replace(marker, marker + history)
            goal = "COVER_DECODE_PACKET: cover(reset_n && nl_decode_packet);"
            assert model.count(goal) == 1
            model = model.replace(goal, "COVER_DECODE_PACKET: cover(reset_n && nl_decode_packet && "
                                  + predicate + ");")
        yield name, model, frozenset(selected)


def covers_reached(status, log, labels):
    expected = {"nocloom_product_gate_miter." + label for label in labels}
    return bool(expected) and status == "PASS" and cover_locations(log, True) == expected


def covers_unreached(status, log, labels):
    expected = {"nocloom_product_gate_miter." + label for label in labels}
    return bool(expected) and status == "FAIL" and cover_locations(log, False) == expected


def configuration(bits, contexts, codes="distinct", write_only=False, protocol="axi4"):
    return f"""schema_version: 10
name: product_gates
address_width: 32
reset_ports: [{{name: reset_n}}]
test_ports: [{{name: test}}]
clock_domains:
  - {{name: source, clock_port: clk_source, reset_port: reset_n, test_port: test}}
  - {{name: target, clock_port: clk_target, reset_port: reset_n, test_port: test}}
synchronizer_stages: 2
initiators:
  - {{name: cpu, clock_domain: source, protocol: axi4, data_width: 32, id_width: 1,
      len_width: 8, max_outstanding: {contexts}}}
targets:
  - {{name: mem, clock_domain: target, protocol: {protocol}, data_width: {bits},
      len_width: {4 if protocol == 'axi3' else 8}, max_outstanding: {contexts}, attributes: amba, access: {'write_only' if write_only else 'read_write'},
      mappings: [{{base: '0x0', size: '0x1000'}}]}}
transport: {{response_codes: {codes}, data_width: 32}}
connectivity: all
topology:
  request:
    nodes: [{{name: q, clock_domain: source}}]
    links: []
    entries: [{{endpoint: cpu, node: q}}]
    exits: [{{endpoint: mem, node: q}}]
  response:
    nodes: [{{name: p, clock_domain: source}}]
    links: []
    entries: [{{endpoint: mem, node: p}}]
    exits: [{{endpoint: cpu, node: p}}]
output: {{backend: verilog, language: verilog_2001}}
"""


def wrap_traffic(rtl, bits):
    """A legal WRAP read stream; target responses follow the ungated copy's AR.

    Equality is checked before a differing output affects the target environment.
    The first R response waits until tick 50, filling the response crossing.
    A second request follows the first completion after eight idle ticks.
    This stimulus restricts fault witnesses only.
    """
    rid = re.search(r"input wire (\[\d+:0\] )?nl_t_mem_rid,", rtl).group(1) or ""
    constants = {"nl_step0": 1, "nl_step1": 1, "nl_i_cpu_awvalid": 0,
                 "nl_i_cpu_wvalid": 0, "nl_i_cpu_bready": 1,
                 "nl_i_cpu_araddr": bits // 8 - 4, "nl_i_cpu_arlen": 2 * (bits // 32) - 1,
                 "nl_i_cpu_arsize": 2, "nl_i_cpu_arburst": 2, "nl_i_cpu_arid": 0,
                 "nl_i_cpu_arcache": 0, "nl_i_cpu_arprot": 0, "nl_i_cpu_arlock": 0,
                 "nl_i_cpu_arqos": 0, "nl_t_mem_awready": 1, "nl_t_mem_wready": 1,
                 "nl_t_mem_bvalid": 0, "nl_t_mem_rresp": 0, "nl_t_mem_rdata": 0}
    return [f"reg {rid}nl_env_id = 0;",
            "reg nl_env_pending = 0; reg [8:0] nl_env_left = 0; reg [1:0] nl_env_wait = 0;",
            "reg [7:0] nl_env_time = 0; reg [2:0] nl_env_phase = 0; reg [3:0] nl_env_gap = 0;",
            "always @(posedge nl_tick) begin",
            "  if (!reset_n) begin nl_env_pending <= 0; nl_env_left <= 0; nl_env_wait <= 0; nl_env_id <= 0; end",
            "  else begin",
            "    if (!nl_env_pending && nl_env_arvalid) begin",
            "      nl_env_pending <= 1; nl_env_left <= {1'b0, nl_env_arlen} + 9'd1;",
            "      nl_env_id <= nl_env_arid; nl_env_wait <= 3;",
            "    end else if (nl_env_pending && nl_env_wait != 0) nl_env_wait <= nl_env_wait - 1'b1;",
            "    else if (nl_env_pending && nl_env_rready) begin",
            "      nl_env_left <= nl_env_left - 1'b1; if (nl_env_left == 1) nl_env_pending <= 0;",
            "    end",
            "  end",
            "end",
            "always @(posedge nl_tick) begin",
            "  if (!reset_n) begin nl_env_time <= 0; nl_env_phase <= 0; nl_env_gap <= 0; end",
            "  else begin",
            "    if (nl_env_time != 255) nl_env_time <= nl_env_time + 1'b1;",
            "    case (nl_env_phase)",
            "      0: if (nl_env_source_arready) nl_env_phase <= 1;",
            "      1: if (nl_env_source_rvalid && nl_i_cpu_rready && nl_env_source_rlast) begin nl_env_phase <= 2; nl_env_gap <= 8; end",
            "      2: if (nl_env_gap == 0) nl_env_phase <= 3; else nl_env_gap <= nl_env_gap - 1'b1;",
            "      3: if (nl_env_source_arready) nl_env_phase <= 4;",
            "    endcase",
            "  end",
            "end",
            "always @* if (!nl_f_init) begin",
            "  assume(reset_n);",
            "  assume(nl_i_cpu_arvalid == (nl_env_phase == 0 || nl_env_phase == 3));",
            "  assume(nl_i_cpu_rready == (nl_env_time >= 50));",
            "  assume(nl_t_mem_arready == !nl_env_pending);",
            "  assume(nl_t_mem_rvalid == (nl_env_pending && nl_env_wait == 0));",
            "  assume(nl_t_mem_rlast == (nl_env_left == 1));",
            "  assume(nl_t_mem_rid == nl_env_id);"] + [
                f"  assume({name} == {value});" for name, value in constants.items()
                if re.search(r"input wire (\[\d+:0\] )?" + name + r"[,\n]", rtl)
            ] + ["end"]


def hold_properties(rtl):
    checks = {f"HOLD_A{q}": f"nl_hold_a{q}"
              for q in re.findall(r"output wire nl_hold_a(\d+)[,\n]", rtl)}
    if not checks:
        raise AssertionError("no generated gate hold assertions")
    checks.update(AW_PENDING_BUSY="nl_aw_pending_busy",
                  W_VALID_FULL="nl_w_valid_full",
                  TAIL_REST="nl_tail_rest_a", FACE_REST="nl_face_rest_a")
    checks.update((f"READ_{name.upper()}", f"nl_read_{name}") for name in (
        "count_slots", "unissued_slots", "one_unissued", "issue_owner", "main_owner", "spare_owner")
        if f"output wire nl_read_{name}," in rtl)
    return checks


def product_model(rtl, contract=True, extra=(True, True), helpers=False, wake_only=False,
                  hold_only=False, assume_hold=False, traffic_bits=0):
    if hold_only:
        checks = hold_properties(rtl)
        # Keep every property as a named wire for counterexamples. A single conjunction
        # lets PDR use the entire relation, rather than rediscover it per output.
        wires = [f"wire {name} = {signal};" for name, signal in checks.items()]
        return finish(rtl, wires + ["reg nl_f_init = 1'b1;",
                           "always @(posedge nl_tick) nl_f_init <= 1'b0;",
                           "always @* if (nl_f_init) assume(!reset_n && nl_init_equal);",
                           "always @* if (!nl_f_init) begin",
                           "  HOLD_ALL: assert(&{" + ", ".join(checks) + "});", "end"])
    checks = [] if wake_only else ["  GATE_EQUAL: assert(nl_gate_equal);",
                                  "  CONTEXT_EQUAL: assert(nl_context_equal);"]
    for copy in (0, 1):
        checks += [f"  TAIL_BUSY_{copy}: assert(nl_tail_implies_busy_{copy});",
                   f"  VALID_FULL_{copy}: assert(nl_valid_implies_full_{copy});"]
    if contract:
        checks += ["  WAKE_TAIL: assert(nl_tail_covered);",
                   "  WAKE_FACE: assert(nl_face_covered);"]
    if helpers:
        checks += [f"  Q{q}_EQUAL: assert(nl_q{q}_equal);"
                   for q in re.findall(r"output wire nl_q(\d+)_equal", rtl)]
    if assume_hold:
        checks += [f"  assume(nl_hold_a{q});"
                   for q in re.findall(r"output wire nl_hold_a(\d+)[,\n]", rtl)]
    covers = [f"  COVER_{side.upper()}_{kind.upper()}: cover(reset_n && nl_{side}_{kind});"
              for i, side in enumerate(("tail", "face")) for kind in ("closed", "wake", "extra")
              if kind != "extra" or extra[i]]
    covers += ["  COVER_READ_DELIVERED: cover(reset_n && nl_read_delivered);"]
    if extra[1]:
        covers += ["  COVER_DECODE_PACKET: cover(reset_n && nl_decode_packet);"]
    return finish(rtl, [
        "reg nl_f_init = 1'b1;",
        "always @(posedge nl_tick) nl_f_init <= 1'b0;",
        "always @* if (nl_f_init) assume(!reset_n && nl_init_equal);",
        "always @* if (!nl_f_init) begin",
    ] + checks + covers + ["end"] + (wrap_traffic(rtl, traffic_bits) if traffic_bits else []))


def product_jobs(root, fixture, violated, bound=48, only=""):
    jobs = []
    # K=1 exercises busy-backed tail wake; K=2..4 exercise the joined fallback.
    # Write-only targets with K>1 use the queued, non-joined merge's r.busy.
    shapes = [(b, k, "distinct", False, "axi4") for b in (64, 128) for k in (1, 2, 3, 4)]
    shapes += [(64, k, "distinct", True, "axi4") for k in (2, 3, 4)]
    shapes += [(64, k, "uniform", False, "axi4") for k in (1, 4)]
    shapes += [(64, k, "distinct", False, "axi3") for k in (1, 4)]
    for bits, contexts, codes, write_only, protocol in shapes:
        label = f"product_{bits}_k{contexts}_{codes}" + ("_write_only" if write_only else "")
        if protocol != "axi4":
            label += "_" + protocol
        if only and label not in only.split(","):
            continue
        config = root / f"{label}.yaml"
        config.write_text(configuration(bits, contexts, codes, write_only, protocol))
        faults = ("none", "tail_zero", "face_zero", "tail_narrow", "face_narrow",
                  "tail_term_zero", "face_term_zero")
        if (bits, contexts, codes, write_only, protocol) == (64, 1, "distinct", False, "axi4"):
            faults += ("tail_or", "face_or")
        for fault in faults:
            work = root / f"{label}_{fault}"
            subprocess.run([fixture, str(work), "product_formal", str(config), fault],
                           check=True, timeout=120)
            meta = json.loads((work / "bindings.json").read_text())
            text = (work / "generated.v").read_text()
            for pattern in (r"output wire nl_q(\d+)_equal", r"output wire nl_hold_a(\d+)[,\n]"):
                if len(re.findall(pattern, text)) != meta["gated_states"]:
                    raise AssertionError("the miter must watch every generated gated state")
            row = dict(shape=[label], fault=fault, stage="product_bmc",
                       top="nocloom_product_gate_miter",
                       tail_busy_applicable=meta["has_busy"],
                       configuration_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
                       product_sha256=hashlib.sha256((work / "product.v").read_bytes()).hexdigest())
            if fault.endswith("or"):
                row.update(stage="product_or_prove", expected=["equivalent wake"])
                jobs.append((row, work / "or_prove", product_model(text, helpers=True, assume_hold=True),
                             "prove", "smtbmc z3", 4, lambda status, log: status == "PASS"))
                continue
            if fault.endswith("narrow") and not meta["occupancy_" + ("tail" if fault.startswith("tail") else "face")]:
                # This path already uses the narrower term.
                continue
            expected = "WAKE_TAIL" if fault.startswith("tail") else "WAKE_FACE"
            equivalent = write_only and fault == "face_narrow"
            whole_zero = fault in ("tail_zero", "face_zero")
            failures = {expected, "GATE_EQUAL", "CONTEXT_EQUAL"} if whole_zero else {expected}
            accept = (lambda status, log: status == "PASS") if fault == "none" or equivalent else (
                lambda status, log, failures=failures:
                status == "FAIL" and bool(failures & set(violated(log))))
            row["properties" if fault == "none" else "expected"] = (
                ["GATE_EQUAL", "TAIL_BUSY", "VALID_FULL", "WAKE_TAIL", "WAKE_FACE"]
                if fault == "none" else sorted(failures))
            if fault == "none":
                jobs.append((dict(row, stage="product_contracts"), work / "contracts",
                             product_model(text, wake_only=True), "prove", "abc pdr", 20, accept))
                # SBY adds the -l toggle. Cancel its anytime schedule so CTG
                # generalization has no per-output wall-clock deadline. The
                # enclosing SBY task still enforces the overall timeout.
                hold_engine = "abc pdr -l"
                if codes == "uniform" and contexts == 1:
                    hold_engine = "abc pdr -l -r -c -n"
                jobs.append((dict(row, stage="product_hold", properties=list(hold_properties(text))), work / "hold",
                             product_model(text, hold_only=True), "prove", hold_engine, 20, accept))
                row["stage"] = "product_prove"
                jobs.append((row, work / "prove", product_model(text, helpers=True, assume_hold=True),
                             "prove", "smtbmc z3", 4, accept))
            elif equivalent:
                row["stage"] = "product_equivalent"
                row["expected"] = ["equivalent wake"]
                jobs.append((row, work / "equivalent", product_model(text, helpers=True, assume_hold=True),
                             "prove", "smtbmc z3", 4, accept))
            else:
                if fault.endswith("term_zero"):
                    row["stage"] = "product_term_bmc"
                jobs.append((row, work / "bmc", product_model(text, wake_only=not whole_zero),
                             "bmc", "abc bmc3", bound, accept))
            if fault == "none":
                cover = product_model(text, extra=(meta["occupancy_tail"], meta["occupancy_face"] and not write_only))
                # These deep packet witnesses benefit from a fixed edge pattern.
                # Other shapes retain their unrestricted packet witnesses.
                packet_steps = (bits, contexts, protocol) in (
                    (64, 3, "axi4"), (64, 4, "axi4"), (128, 3, "axi4"), (64, 4, "axi3"))
                for name, cover_text, labels in cover_tasks(cover, packet_steps, inline_steps=protocol == "axi3"):
                    jobs.append((dict(row, stage="product_cover", task_variant=name),
                                 work / name, cover_text, "cover", "smtbmc z3", bound,
                                 lambda status, log, labels=labels: covers_reached(status, log, labels)))
                    if name == "cover_decode":
                        broken, hits = re.subn(r"^assign nl_decode_packet = [^\n]+;",
                                               "assign nl_decode_packet = 1'b0;", cover_text, flags=re.M)
                        assert hits == 1, "decode cover observer mutation did not bind"
                        negative = dict(row, fault="decode_observer_zero", stage="product_cover",
                                        task_variant="cover_decode_zero", expected=sorted(labels))
                        negative.pop("properties", None)
                        jobs.append((negative, work / "cover_decode_zero", broken, "cover", "smtbmc z3",
                                     bound, lambda status, log, labels=labels: covers_unreached(status, log, labels)))
            elif whole_zero and (bits, contexts, codes, write_only, protocol) in (
                    (64, 1, "distinct", False, "axi4"), (128, 1, "distinct", False, "axi4")):
                # Whole-wake faults must also break port equality on legal read traffic.
                full = work / "full"
                subprocess.run([fixture, str(full), "product_formal", str(config), fault, "full"],
                               check=True, timeout=120)
                full_text = (full / "generated.v").read_text()
                jobs.append((dict(shape=[label], fault=fault, stage="product_functional",
                                  top="nocloom_product_gate_miter", traffic="WRAP reads",
                                  context_shared=False, expected=["GATE_EQUAL"]),
                             work / "functional", product_model(full_text, False, traffic_bits=bits),
                             "bmc", "abc bmc3", 128,
                             lambda status, log: status == "FAIL" and "GATE_EQUAL" in violated(log)))
    return jobs
