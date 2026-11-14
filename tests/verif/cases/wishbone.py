"""Classic Wishbone over the shared transaction drivers and byte scoreboard."""
from nlverif import config as nlconfig
from nlverif import model


def configuration(source, target, unit="byte", clocked=False, retry=False):
    clocks = [dict(name="main", clock_port="clk_main", reset_port="reset_n", test_port="test")]
    if clocked:
        clocks.append(dict(name="io", clock_port="clk_io", reset_port="reset_n", test_port="test"))
    src = dict(name="s0", clock_domain="main", protocol=source, data_width=32)
    tgt = dict(name="t0", clock_domain="io" if clocked else "main", protocol=target,
               data_width=32, mappings=[dict(base="0x1000", size="0x1000")])
    for endpoint in (src, tgt):
        if endpoint["protocol"] == "wishbone":
            endpoint["address_unit"] = unit
        else:
            if endpoint is tgt:
                endpoint["attributes"] = "amba"
            if endpoint["protocol"] == "axi4":
                endpoint.update(id_width=2, len_width=8)
    if retry:
        tgt["retry"] = "error"
    cfg = dict(schema_version=10, name="wb_classic", address_width=32,
               reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
               clock_domains=clocks, initiators=[src], targets=[tgt], connectivity="all",
               output=dict(backend="verilog", language="verilog_2001"))
    if clocked:
        cfg["topology"] = dict(
            request=dict(nodes=[dict(name="q", clock_domain="main")], links=[],
                         entries=[dict(endpoint="s0", node="q")], exits=[dict(endpoint="t0", node="q")]),
            response=dict(nodes=[dict(name="p", clock_domain="main")], links=[],
                          entries=[dict(endpoint="t0", node="p")], exits=[dict(endpoint="s0", node="p")]))
    return cfg


def held_cyc(rtl, mapping):
    import re
    name, = re.findall(r"^assign nl_t_t0_cyc = (nl_internal_v\d+);", rtl, re.M)
    return [(rf"^(\s*{name} <= )nl_internal_v\d+;", r"\g<1>1'd1;", 1)]


def ungated_termination(rtl, mapping):
    import re
    subs = []
    for port in ("ack", "err"):
        gate, = re.findall(rf"^assign nl_i_s0_{port} = (\w+);$", rtl, re.M)
        registered, = re.findall(rf"^assign {gate} = \((\w+) & \w+\);$", rtl, re.M)
        subs.append((rf"^assign nl_i_s0_{port} = \w+;$",
                     f"assign nl_i_s0_{port} = {registered};", 1))
    return subs


def variant(source, target, unit="byte", clocked=False, retry=False):
    cfg = configuration(source, target, unit, clocked, retry)
    kinds = dict(wishbone="wishbone", axi4="axi", apb4="apb")
    strobe = dict(wishbone="sel", axi4="wstrb", apb4="pstrb")[target]
    faults = [dict(id="sel_swap", component="dut", op="text",
                   contract="AXI.W.UNSTROBED_ZERO", expect=["TARGET_WRITE_BYTES", "SOURCE_R_DATA", "TARGET_UNSTROBED_DATA"],
                   subs=[(rf"^(assign nl_t_t0_{strobe} = )([^;]+);$",
                          r"\1((\2 & 4'hc) | ((\2 & 4'h1) << 1) | ((\2 & 4'h2) >> 1));", 1)])]
    if target == "wishbone":
        faults.append(dict(id="held_cyc", component="dut", op="text",
                           contract="AXI.ROUTE.LOCAL_OFFSET", expect=["ORACLE"],
                           subs=held_cyc))
    if source == target == "wishbone":
        for name, rule, port, value in [
                ("reset_cyc", "WB.RULE_3_20", "nl_t_t0_cyc", "1'b1"),
                ("cyc_low", "WB.RULE_3_25", "nl_t_t0_cyc", "1'b0"),
                ("orphan_ack", "WB.RULE_3_30", "nl_i_s0_ack", "1'b1"),
                ("both_terms", "WB.RULE_3_45", "nl_i_s0_err", "nl_i_s0_ack"),
                ("moving_adr", "WB.RULE_3_60", "nl_t_t0_adr", "")]:
            if name == "moving_adr":
                subs = [(r"^(assign nl_t_t0_adr = )([^;]+);$", r"\1(\2 ^ {31'd0, nl_t_t0_ack});", 1)]
            else:
                subs = [(rf"^assign {port} = [^;]+;$", f"assign {port} = {value};", 1)]
            faults.append(dict(id=name, component="dut", op="text", contract="AXI.ROUTE.LOCAL_OFFSET",
                               expect=[rule], subs=subs))
    if source == "wishbone":
        faults.append(dict(id="ungated_termination", component="dut", op="text",
                           contract="AXI.ROUTE.LOCAL_OFFSET",
                           expect=["WB.RULE_3_30", "WB.RULE_3_50"],
                           subs=ungated_termination))
    return dict(label=f"{source}_{target}_{unit}_{int(clocked)}_{int(retry)}",
                dut=dict(source="cli", config=cfg),
                bfms=[dict(name="s0", kind=kinds[source] + "_master", endpoint="initiator:s0",
                           **(dict(word=unit == "word") if source == "wishbone" else {})),
                      dict(name="t0", kind=kinds[target] + "_slave", endpoint="target:t0", salt=3,
                           clock="io" if clocked else "main",
                           **(dict(word=unit == "word", retry=retry) if target == "wishbone" else {}))],
                clocks=dict(main=10.0, io=17.0),
                model=dict(routes=nlconfig.routes(cfg), targets=nlconfig.targets(cfg, error_data_zero=True),
                           keeps_decerr=nlconfig.keeps_decerr(cfg)),
                scenario_args=dict(source=source, target=target), faults=faults)


CASE = dict(name="wishbone_classic", negative_faults_all=True, contracts=["AXI.ROUTE.LOCAL_OFFSET", "AXI.ERR.READ_ZERO", "AXI.W.UNSTROBED_ZERO",
                       "AXI.TIMEOUT.EXACT"],
            configs=[variant(s, t, u, c, r) for s, t, u, c, r in [
                ("wishbone", "wishbone", "byte", False, False),
                ("wishbone", "wishbone", "word", True, True),
                ("wishbone", "axi4", "byte", False, False),
                ("axi4", "wishbone", "word", True, False),
                ("wishbone", "apb4", "word", False, False),
                ("apb4", "wishbone", "byte", False, False)]])


def scenario(tape, seed, source, target, timer=False, exclusive=False):
    tape.reset(8)
    tape.delay(10)
    if target in ("wishbone", "apb4"):
        tape.set("t0", "wait", 3)
    else:
        tape.set("t0", "b_lat", 3)
        tape.set("t0", "r_lat", 4)
    for mask in range(16):
        addr = 0x1100 + 4 * mask
        tape.write("s0", addr, strobe="keep", keep=mask, prot=2)
        tape.drain()
        tape.read("s0", addr, prot=2)
        tape.drain()
    if source == "wishbone":
        tape.write("s0", 0x1183, strobe="keep", keep=6, prot=2)
        tape.drain()
        tape.read("s0", 0x1181, prot=2)
        tape.drain()
        # Each entry gives the selected lanes and the required aligned read block.
        shapes = [(0, 0, 2, 15), (1, 0, 0, 1), (2, 1, 0, 2), (3, 0, 1, 3),
                  (4, 2, 0, 4), (5, 0, 2, 15), (6, 0, 2, 15), (7, 0, 2, 15),
                  (8, 3, 0, 8), (9, 0, 2, 15), (10, 0, 2, 15), (11, 0, 2, 15),
                  (12, 2, 1, 12), (13, 0, 2, 15), (14, 0, 2, 15), (15, 0, 2, 15)]
        for selected, offset, size, read_mask in shapes:
            tape.set("s0", "read_sel", selected)
            tape.read("s0", 0x1120 + offset, size=size, prot=2, read_mask=read_mask)
            tape.drain()
        tape.set("s0", "read_sel", 0)
        tape.set("s0", "continuous", 1)
        for _ in range(4):
            tape.read("s0", 0x1120, prot=2)
        tape.drain()
        tape.set("s0", "continuous", 0)
        if target == "wishbone":
            tape.set("t0", "wait", 40)
            tape.read("s0", 0x1120, prot=2)
            tape.delay(16)
            tape.set("s0", "abort", 1)
            tape.delay(3)
            tape.set("s0", "abort", 0)
            tape.delay(3)
            tape.read("s0", 0x1124, prot=2)
            tape.drain()
            tape.set("t0", "wait", 3)
        response_aborts(tape, target, (1, 2))
    if target == "wishbone":
        for mode in (1, 2, 0):
            tape.set("t0", "ack_mode", mode)
            tape.write("s0", 0x1280 + 4 * mode, prot=2)
            tape.drain()
            tape.read("s0", 0x1280 + 4 * mode, prot=2)
            tape.drain()
    if exclusive:
        tape.read("s0", 0x1300, lock=1, id=1, prot=2, resp=1)
        tape.drain()
        tape.write("s0", 0x1300, lock=1, id=1, prot=2, resp=1)
        tape.drain()
        tape.write("s0", 0x1300, lock=1, id=1, prot=2, resp=0, target="none")
        tape.drain()
    if source == "axi4":
        for burst in ("fixed", "wrap"):
            tape.write("s0", 0x1344, beats=4, burst=burst, prot=2)
            tape.drain()
            tape.read("s0", 0x1344, beats=4, burst=burst, prot=2)
            tape.drain()
        tape.write("s0", 0x1400, beats=256, size=2, strobe="mix", prot=2)
        tape.drain()
        tape.read("s0", 0x1400, beats=256, size=2, prot=2)
        tape.drain()
        for size in (0, 1):
            tape.read("s0", 0x1180 + size, size=size, prot=2)
            tape.drain()
    if target in ("wishbone", "apb4"):
        tape.set("t0", "err_default", 1)
    else:
        tape.set("t0", "b_default", 2)
        tape.set("t0", "r_default", 2)
    tape.write("s0", 0x1200, prot=2)
    tape.drain()
    tape.read("s0", 0x1200, prot=2)
    tape.drain()
    if timer:
        tape.set("t0", "err_default", 0)
        tape.set("t0", "wait", 8192)
        tape.read("s0", 0x1300, resp=2, target="any", late_access=True)
        tape.drain()
        tape.expect_now("t0", "sideband", "==", 1, "WB_TIMEOUT")
        tape.write("s0", 0x1304, resp=2, target="none")
        tape.drain()
        tape.delay(8300)
    if target == "wishbone":
        # Source completion after timeout precedes the late target termination.
        # Sample idle only after that termination has drained.
        tape.delay(10)
        tape.mark("target_drained")
    tape.end()


def response_aborts(tape, target, modes, queued=1):
    count = 0
    for mode in modes:
        for failed in (0, 1):
            tape.set("t0", "b_default" if target == "axi4" else "err_default",
                     2 * failed if target == "axi4" else failed)
            tape.set("t0", "r_default" if target == "axi4" else "err_default",
                     2 * failed if target == "axi4" else failed)
            tape.set("s0", "abort_response", mode)
            for i in range(queued):
                tape.read("s0", 0x1160 + 4 * i, prot=2)
            tape.delay(400)
            count += 1
            tape.expect_now("s0", "response_aborts", "==", count, "WB_RESPONSE_ABORT")
            tape.set("s0", "abort_response", 0)
            tape.set("t0", "b_default" if target == "axi4" else "err_default", 0)
            tape.set("t0", "r_default" if target == "axi4" else "err_default", 0)
            tape.delay(3)
            tape.write("s0", 0x1164, prot=2)
            tape.read("s0", 0x1164, prot=2)
            tape.drain()


def cyc_oracle(checker, events, sources):
    levels = [e for e in events if e.get("p") == "t0" and e.get("k") == "lv"
              and e.get("ch") == "cyc"]
    falls = [e["t"] for e in levels if e["v"] == 0]
    period = checker.bench["bfms"]["t0"]["period_ps"]
    out = []
    for e in events:
        if e.get("k") == "mk" and checker.tape["marks"][e["m"]] == "target_drained":
            value = next((v["v"] for v in reversed(levels) if v["t"] <= e["t"]), 0)
            if value != 0:
                out.append(dict(detail="CYC remains high after drain", t=e["t"]))
    for s in sources:
        if s.cancelled:
            continue
        # INCR bursts split at 128-byte boundaries. FIXED and WRAP here fit
        # in one packet. Zero-strobe words produce no target transfer.
        groups = {}
        for t in s.targets:
            groups.setdefault(t.addr // 128 if s.burst == 1 else 0, []).append(t.t)
        packets = sorted(groups.values(), key=min)
        for times in packets:
            if any(min(times) < fall < max(times) for fall in falls):
                out.append(dict(detail="CYC dropped inside a packet", addr=s.addr))
        for before, after in zip(packets, packets[1:]):
            if not any(max(before) < fall < min(after) and
                       next((e["t"] for e in levels if e["t"] > fall and e["v"] == 1),
                            min(after)) - fall >= period for fall in falls):
                out.append(dict(detail="CYC has no low cycle between packets", addr=s.addr,
                                t=min(after)))
    return out


def oracle(checker, events, sources, targets):
    if checker.case.get("scenario_args", {}).get("target") != "wishbone":
        return []
    masks = {e["t"]: int(e["s"], 16) for e in events
             if e.get("p") == "t0" and e.get("ch") == "apb" and e.get("k") == "hs"}
    out = cyc_oracle(checker, events, sources)
    for s in sources:
        if s.dir != "r" or s.cancelled:
            continue
        wanted = set()
        expected = checker.tape["expect"].get(str(s.tag), {})
        if "read_mask" in expected:
            base = s.addr & ~3
            wanted = {base + lane for lane in range(4) if expected["read_mask"] & (1 << lane)}
        else:
            for beat in range(s.length):
                addr = model.beat_addr(s.addr, s.length, s.size, s.burst, beat)
                wanted.update(model.beat_bytes(addr, s.size, 4))
        observed = set()
        for t in s.targets:
            mask = sum(1 << lane for lane in range(4) if 0x1000 + t.addr + lane in wanted)
            observed.update(0x1000 + t.addr + lane for lane in range(4)
                            if masks.get(t.t, 0) & (1 << lane))
            if masks.get(t.t) != mask:
                out.append(dict(detail="read SEL", got=masks.get(t.t), want=mask))
        if "read_mask" in expected and observed != wanted:
            out.append(dict(detail="read byte range", got=sorted(observed), want=sorted(wanted)))
    return out


CASE["oracle"] = oracle


def option_variant(option):
    entry = variant("axi4", "wishbone")
    entry["label"] += "_" + option
    cfg = entry["dut"]["config"]
    if option == "timer":
        cfg["targets"][0]["timeout"] = dict(cycles=4096, completion=True)
        entry["scenario_args"]["timer"] = True
        entry["bfms"][1]["sideband"] = ("dut", "target:t0", "timed_out")
        entry["faults"].append(dict(id="stopped_timer", component="dut", op="text",
                                    contract="AXI.TIMEOUT.EXACT", expect=["TAPE.EXPECT:WB_TIMEOUT", "SOURCE_R_RESP"],
                                    subs=stopped_timer))
    elif option == "exclusive":
        cfg["targets"][0]["exclusive"] = "network"
        cfg["targets"][0]["exclusive_owner_flags"] = ["owner"]
        cfg["initiators"][0]["user_flags"] = dict(owner=0)
        cfg["transport"] = dict(user_flags=["owner"])
        entry["scenario_args"]["exclusive"] = True
    else:
        cfg["targets"][0]["fixed_bursts"] = "single"
    entry["model"]["targets"] = nlconfig.targets(cfg, error_data_zero=True)
    return entry


def stopped_timer(rtl, mapping):
    import re
    name, = re.findall(r"^assign nl_t_t0_timed_out = (nl_internal_v\d+);", rtl, re.M)
    return [(rf"^(\s*{name} <= )nl_internal_v\d+;", r"\g<1>1'd0;", 1)]


CASE["configs"] += [option_variant(o) for o in ("single", "timer", "exclusive")]
