"""Pipelined Wishbone transactions use the classic bus drivers and scoreboard."""
from cases.wishbone import variant, oracle as classic_oracle, response_aborts, ungated_termination
from nlverif import config as nlconfig
from copy import deepcopy
import re


def pipeline(source, target, source_pipe=True, target_pipe=True, clocked=False,
             two=False, timer=False, timer_wrapper=False, retry=False):
    entry = variant(source, target, unit="word" if clocked else "byte", clocked=clocked, retry=retry)
    entry["label"] += f"_pipe_{int(source_pipe)}_{int(target_pipe)}"
    cfg = entry["dut"]["config"]
    for endpoint, bfm, enabled in zip(
            (cfg["initiators"][0], cfg["targets"][0]), entry["bfms"],
            (source_pipe, target_pipe)):
        if endpoint["protocol"] == "wishbone" and enabled:
            endpoint["cycles"] = "pipelined"
            bfm["pipelined"] = True
            if endpoint is cfg["initiators"][0]:
                endpoint["max_outstanding"] = 4
    if two:
        other = deepcopy(cfg["targets"][0])
        other.update(name="t1", mappings=[dict(base="0x2000", size="0x1000")])
        cfg["targets"].append(other)
        bfm = deepcopy(entry["bfms"][1])
        bfm.update(name="t1", endpoint="target:t1", salt=5)
        entry["bfms"].append(bfm)
        entry["label"] += "_two"
    if timer:
        cfg["targets"][0]["timeout"] = dict(cycles=64, completion=True)
        entry["bfms"][1]["sideband"] = ("dut", "target:t0", "timed_out")
        entry["label"] += "_timer" + ("_wrapper" if timer_wrapper else "")
    entry["model"]["routes"] = nlconfig.routes(cfg)
    entry["model"]["targets"] = nlconfig.targets(cfg, error_data_zero=True)
    entry["scenario_args"] = dict(source=source, target=target, source_pipe=source_pipe,
                                  target_pipe=target_pipe, two=two, timer=timer,
                                  timer_wrapper=timer_wrapper, retry=retry)
    entry["faults"] = entry["faults"][:1]
    if source == "wishbone" and source_pipe:
        entry["faults"].append(dict(
            id="ungated_termination", component="dut", op="text",
            contract="AXI.ROUTE.LOCAL_OFFSET", expect=["WB.RULE_3_30"],
            subs=ungated_termination))
        entry["faults"].append(dict(
            id="orphan_response", component="dut", op="text",
            contract="AXI.ROUTE.LOCAL_OFFSET", expect=["WB.RULE_3_59"],
            subs=[(r"^(assign nl_i_s0_ack = )([^;]+);$",
                   r"\1(\2 | (nl_i_s0_cyc && nl_i_s0_stall && "
                   r"tb.bfm_s0.driver.issued == tb.bfm_s0.driver.head));", 1)]))
        if target == "wishbone" and target_pipe and not clocked and not two:
            entry["faults"].append(dict(
                id="inactive_response", component="dut", op="text",
                contract="AXI.ROUTE.LOCAL_OFFSET", expect=["WB.RULE_3_30"],
                subs=[(r"^assign nl_i_s0_ack = [^;]+;$",
                       "assign nl_i_s0_ack = 1'b1;", 1)]))
        entry["faults"].append(dict(
            id="single_outstanding", component="dut", op="text",
            contract="AXI.ROUTE.LOCAL_OFFSET", expect=["TAPE.EXPECT:WB_MULTIPLE", "ORACLE"],
            subs=[(r"^(assign (\w+) = )7'd4;\n(assign \w+ = \(\w+ < \2\);)$",
                   r"\g<1>7'd1;\n\3", 1)]))
    if retry:
        entry["faults"].append(dict(
            id="lost_retry", component="dut", op="text",
            contract="AXI.ROUTE.LOCAL_OFFSET", expect=["SOURCE_UNANSWERED"],
            subs=lost_retry))
    return entry


def lost_retry(rtl, mapping):
    signal, = re.findall(r"^assign (\w+) = nl_t_t0_rty;$", rtl, re.M)
    return [(rf"^(assign \w+ = )\(\w+ & {signal}\);$", r"\g<1>1'b0;", 1)]


CASE = dict(name="wishbone_pipelined", negative_faults_all=True,
            contracts=["AXI.ROUTE.LOCAL_OFFSET", "AXI.ERR.READ_ZERO", "AXI.W.UNSTROBED_ZERO"],
            configs=[pipeline(s, t, sp, tp, c) for s, t, sp, tp, c in [
                ("wishbone", "wishbone", True, True, False),
                ("wishbone", "wishbone", True, True, True),
                ("wishbone", "wishbone", True, False, False),
                ("wishbone", "wishbone", False, True, False),
                ("wishbone", "axi4", True, False, False),
                ("axi4", "wishbone", False, True, False),
                ("wishbone", "apb4", True, False, False),
                ("apb4", "wishbone", False, True, False)]])


CASE["configs"] += [pipeline("wishbone", "wishbone", retry=True),
                    pipeline("wishbone", "wishbone", two=True),
                    pipeline("axi4", "wishbone", source_pipe=False, timer=True),
                    pipeline("axi4", "wishbone", source_pipe=False, timer=True, timer_wrapper=True)]

# Exercise all fault types on one pipelined Wishbone pair, including clock crossing and retry.
if globals().get("PARAMS", {}).get("short") == "true":
    CASE["configs"] = [entry for entry in CASE["configs"]
                       if entry["scenario_args"]["source_pipe"]
                       and entry["scenario_args"]["target_pipe"]
                       and not entry["scenario_args"]["two"]]


def scenario(tape, seed, source, target, source_pipe, target_pipe, two, timer,
             timer_wrapper, retry):
    tape.reset(8)
    tape.delay(10)
    if target == "axi4":
        tape.set("t0", "b_lat", 20)
        tape.set("t0", "r_lat", 20)
    else:
        tape.set("t0", "wait", 12)
    for i in range(8):
        tape.write("s0", 0x1100 + 4 * i, prot=2, strobe="keep", keep=1)
    tape.drain()
    if source == "wishbone" and source_pipe:
        tape.expect_now("s0", "pipe_peak", ">=", 2, "WB_MULTIPLE")
        # The old requests remain in the network when CYC restarts.
        tape.set("t0", "r_lat" if target == "axi4" else "wait", 50)
        for i in range(4):
            tape.read("s0", 0x1100 + 4 * i, prot=2)
        tape.delay(20)
        tape.expect_now("s0", "pipe_pending", ">=", 2, "WB_ABORT_PENDING")
        tape.set("s0", "abort", 1)
        tape.delay(3)
        tape.set("s0", "abort", 0)
        tape.delay(3)
        tape.read("s0", 0x1180, prot=2)
        tape.drain()
        tape.set("t0", "r_lat" if target == "axi4" else "wait", 20 if target == "axi4" else 12)
    for i in range(8):
        tape.read("s0", 0x1100 + 4 * i, prot=2)
    tape.drain()
    for i in range(8):
        tape.write("s0", 0x1180 + 4 * i, prot=2)
        tape.read("s0", 0x1180 + 4 * i, prot=2)
    tape.drain()
    if source == "wishbone":
        # Selected lanes and expected byte ranges are independent of the DUT.
        shapes = [(0, 0, 2, 15), (1, 0, 0, 1), (2, 1, 0, 2), (3, 0, 1, 3),
                  (4, 2, 0, 4), (5, 0, 2, 15), (6, 0, 2, 15), (7, 0, 2, 15),
                  (8, 3, 0, 8), (9, 0, 2, 15), (10, 0, 2, 15), (11, 0, 2, 15),
                  (12, 2, 1, 12), (13, 0, 2, 15), (14, 0, 2, 15), (15, 0, 2, 15)]
        for selected, offset, size, read_mask in shapes:
            tape.set("s0", "read_sel", selected)
            tape.read("s0", 0x1100 + offset, size=size, prot=2, read_mask=read_mask)
            tape.drain()
        tape.set("s0", "read_sel", 0)
        response_aborts(tape, target, (1,) if source_pipe else (1, 2),
                        queued=4 if source_pipe else 1)
    if target != "axi4":
        tape.set("t0", "errm_mask", 0xfff)
        tape.set("t0", "errm_match", 0x204)
        for i in range(4):
            tape.read("s0", 0x1200 + 4 * i, prot=2)
        tape.drain()
        if source == "axi4":
            tape.read("s0", 0x1200, beats=4, prot=2)
            tape.drain()
    if target == "wishbone" and target_pipe:
        tape.set("t0", "errm_mask", 0)
        for mode in (3, 2):
            tape.set("t0", "ack_mode", mode)
            tape.write("s0", 0x1240 + 4 * mode, prot=2)
            tape.drain()
            tape.read("s0", 0x1240 + 4 * mode, prot=2)
            tape.drain()
    if retry:
        tape.set("t0", "ack_mode", 3)
        tape.set("t0", "wait", 4)
        tape.set("t0", "err_default", 1)
        tape.write("s0", 0x1280, prot=2, strobe="keep", keep=1)
        tape.read("s0", 0x1280, prot=2)
        tape.drain()
        tape.set("t0", "err_default", 0)
        tape.write("s0", 0x1284, prot=2)
        tape.read("s0", 0x1284, prot=2)
        tape.drain()
    if two:
        for i in range(8):
            tape.write("s0", 0x1300 + 4 * i, prot=2)
            tape.write("s0", 0x2300 + 4 * i, prot=2)
            tape.read("s0", 0x1300 + 4 * i, prot=2)
            tape.read("s0", 0x2300 + 4 * i, prot=2)
        tape.drain()
    if timer:
        tape.set("t0", "ack_mode", 3 if timer_wrapper else 0)
        tape.set("t0", "wait", 8192)
        tape.read("s0", 0x1400, prot=2, resp=2, target="any", late_access=True)
        tape.drain()
        tape.expect_now("t0", "sideband", "==", 1, "WB_TIMEOUT")
        tape.write("s0", 0x1404, prot=2, resp=2, target="none")
        tape.drain()
        tape.delay(8300)
    tape.end()


def oracle(checker, events, sources, targets):
    args = checker.case.get("scenario_args", {})
    out = [] if args.get("two") else classic_oracle(checker, events, sources, targets)
    if args.get("source") == "wishbone" and args.get("source_pipe"):
        accepted = [e["t"] for e in events if e.get("p") == "s0" and e.get("k") == "hs"
                    and e.get("ch") == "w" and 1 <= e.get("tag", 0) <= 8]
        completed = [e["t"] for e in events if e.get("p") == "s0" and e.get("k") == "hs"
                     and e.get("ch") == "b"]
        if len(accepted) < 2 or not completed or accepted[1] >= completed[0]:
            out.append(dict(detail="second acceptance must precede first response",
                            accepted=accepted[:2], completed=completed[:1]))
    if args.get("retry"):
        bus = [e for e in events if e.get("p") == "t0" and e.get("k") == "wb"]
        for detail, reached in [
                ("request held during STALL", any(e["stb"] and e["stall"] for e in bus)),
                ("RTY on acceptance edge", any(e["stb"] and not e["stall"] and e["rty"]
                                                for e in bus)),
                ("RTY after acceptance", any(not e["stb"] and e["rty"] for e in bus))]:
            if not reached:
                out.append(dict(detail=detail))
        retries = [e["t"] for e in bus if e["rty"]]
        accesses = [e for e in events if e.get("p") == "t0" and e.get("k") == "hs"
                    and e.get("ch") == "apb"]
        for write in (0, 1):
            if not retries or not any(e["t"] > max(retries) and e["w"] == write
                                      and e["r"] == 0 for e in accesses):
                out.append(dict(detail="successful request after RTY", write=write))
    return out


CASE["oracle"] = oracle
