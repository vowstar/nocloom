"""Registered feedback target sequences with a tag-consuming memory peer."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wishbone import configuration, scenario as classic_scenario, ungated_termination
from nlverif import config as nlconfig


def variant(bits=32, clocked=False, unit="byte", ack=1):
    cfg = configuration("axi4", "wishbone", unit, clocked)
    cfg["initiators"][0]["data_width"] = bits
    cfg["targets"][0].update(burst_tags=True, fixed_bursts="single")
    return dict(label=f"w{bits}_{unit}_{int(clocked)}_{ack}",
                dut=dict(source="cli", config=cfg),
                bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
                      dict(name="t0", kind="wishbone_slave", endpoint="target:t0",
                           clock="io" if clocked else "main", word=unit == "word")],
                clocks=dict(main=10.0, io=17.0),
                model=dict(routes=nlconfig.routes(cfg),
                           targets=nlconfig.targets(cfg, error_data_zero=True),
                           keeps_decerr=nlconfig.keeps_decerr(cfg)),
                scenario_args=dict(ack=ack))


CASE = dict(name="wishbone_bursts", contracts=["AXI.ROUTE.LOCAL_OFFSET", "AXI.ERR.READ_ZERO"],
            configs=[variant()])


def target_scenario(tape, seed, ack, family="shapes", bits=32):
    tape.reset(8)
    tape.delay(10)
    tape.set("t0", "ack_mode", ack)
    if family == "pressure":
        tape.ready("s0", "r", "never")
        tape.read("s0", 0x1100, beats=32, size=2, prot=2)
        tape.delay(100)
        tape.ready("s0", "r", "always")
        tape.drain()
    elif family == "banks":
        for j in range(6):
            tape.write("s0", 0x1100 + 128 * j, beats=32, size=2, salt=73 + j, prot=2)
            if j == 2:
                tape.read("s0", 0x1100, beats=32, size=2, prot=2)
        tape.drain()
    elif family == "width":
        size = (bits // 8).bit_length() - 1
        for burst, n, addr in (("incr", 32, 0x1100), ("wrap", 4, 0x1200 + bits // 8),
                                ("fixed", 4, 0x1300)):
            tape.write("s0", addr, beats=n, size=size, burst=burst, prot=2)
            tape.drain()
            tape.read("s0", addr, beats=n, size=size, burst=burst, prot=2)
            tape.drain()
        for size in (0, 1):
            # Start in the second half of a source word, with a 256-beat narrow stream.
            addr = 0x1400 + bits // 16 + 2
            addr &= ~((1 << size) - 1)
            tape.write("s0", addr, beats=256, size=size, prot=2)
            tape.drain()
            tape.read("s0", addr, beats=256, size=size, prot=2)
            tape.drain()
    else:
        size = (bits // 8).bit_length() - 1
        for addr in (0x1100 + bits // 8, 0x1100 + bits // 4):
            tape.write("s0", addr, beats=40, size=size, prot=2)
            tape.drain()
            tape.read("s0", addr, beats=40, size=size, prot=2)
            tape.drain()
        if bits != 32:
            tape.end()
            return
        for n in (1, 2, 3, 4, 7, 8, 16, 32, 64, 256):
            tape.write("s0", 0x1100, beats=n, size=2, prot=2)
            tape.drain()
            tape.read("s0", 0x1100, beats=n, size=2, prot=2)
            tape.drain()
        for n in (2, 4, 8, 16):
            for start in range(n):
                addr = 0x1800 + start * 4
                tape.write("s0", addr, beats=n, size=2, burst="wrap", prot=2)
                tape.drain()
                tape.read("s0", addr, beats=n, size=2, burst="wrap", prot=2)
                tape.drain()
        for mask in range(16):
            for burst in ("incr", "fixed"):
                tape.write("s0", 0x1300, beats=3, size=2, burst=burst,
                           strobe="keep", keep=mask, prot=2)
                tape.drain()
        for masks in ([15, 15, 5, 15, 0, 15], [0, 15, 15, 15], [15, 15, 0],
                      [15] * 8, [5] * 8, [15, 3, 15, 3], [0] * 8):
            tape.strobes("s0", masks)
            for burst in (("incr", "fixed", "wrap") if len(masks) in (2, 4, 8, 16)
                          else ("incr", "fixed")):
                tape.write("s0", 0x1300, beats=len(masks), size=2, burst=burst,
                           strobe="table", prot=2)
                tape.drain()
        tape.set("t0", "ack_mode", 4)
        for index in (0, 3, 7):
            tape.set("t0", "reg0_lo", 0x400 + 4 * index)
            tape.set("t0", "reg0_hi", 0x404 + 4 * index)
            tape.set("t0", "reg0_c", 1)
            tape.write("s0", 0x1400, beats=8, size=2, prot=2)
            tape.drain()
            tape.read("s0", 0x1400, beats=8, size=2, prot=2)
            tape.drain()
        tape.set("t0", "reg0_hi", 0)
        tape.read("s0", 0x1400, beats=8, size=2, prot=2)
        tape.drain()
    tape.end()


def source_scenario(tape, seed, ack, family="source", bits=32):
    tape.reset(8)
    tape.delay(10)
    tape.set("s0", "continuous", 1)
    for bte, n in ((0, 7), (1, 4), (2, 8), (3, 16)):
        tape.set("s0", "bte", bte)
        for k in range(n):
            tape.set("s0", "cti", 7 if k == n - 1 else 2)
            addr = 0x1100 + ((3 + k) % n if bte else k) * 4
            tape.write("s0", addr, prot=2)
            tape.drain()
        for k in range(n):
            tape.set("s0", "cti", 7 if k == n - 1 else 2)
            addr = 0x1100 + ((3 + k) % n if bte else k) * 4
            tape.read("s0", addr, prot=2)
            tape.drain()
    for cti in (0, 1, 3, 4, 5, 6, 7):
        tape.set("s0", "cti", cti)
        tape.set("s0", "bte", 0)
        tape.read("s0", 0x1200, prot=2)
        tape.drain()
        if cti == 1:
            tape.set("s0", "cti", 7)
            tape.read("s0", 0x1200, prot=2)
            tape.drain()
    tape.set("s0", "cti", 0)
    tape.set("s0", "bte", 0)
    tape.set("s0", "continuous", 0)
    classic_scenario(tape, seed, source="wishbone", target="axi4")


def burst_oracle(checker, events, sources, targets):
    tags = {e["t"]: e for e in events if e.get("k") == "wb_tag"}
    if checker.case.get("scenario_args", {}).get("family") == "source":
        return []
    out = []
    for s in sources:
        selected = [tags[t.t] for t in s.targets if t.t in tags]
        source_bytes = checker.bench["bfms"]["s0"]["data_bits"] // 8
        if s.burst == 1 and (1 << s.size) == source_bytes and selected:
            # Include zero-SEL words when reconstructing packets from source byte masks.
            physical_masks = []
            for i in range(s.length):
                mask = s.beats[i][1] if s.dir == "w" else (1 << source_bytes) - 1
                for lane in range(0, source_bytes, 4):
                    physical_masks.append((mask >> lane) & 15)
            expected = []
            # The 32-word capacity counts from the packet's first physical word.
            for start in range(0, len(physical_masks), 32):
                masks = physical_masks[start:start + 32]
                prefix = 0
                for mask in masks:
                    if mask != 15:
                        break
                    prefix += 1
                for i, mask in enumerate(masks):
                    if mask:
                        expected.append(2 if prefix > 1 and i < prefix - 1 else
                                        7 if prefix > 1 and i == prefix - 1 else 0)
            if [e["cti"] for e in selected] != expected:
                out.append(dict(detail="INCR prefix", got=[e["cti"] for e in selected], want=expected))
            continue
        if s.burst == 2 and (1 << s.size) == checker.bench["bfms"]["s0"]["data_bits"] // 8:
            physical = s.length * (1 << s.size) // 4
            full = s.dir == "r" or all(mask == (1 << (1 << s.size)) - 1 for _, mask in s.beats)
            if full:
                if physical in (4, 8, 16):
                    expected = [2] * (physical - 1) + [7]
                    if [e["cti"] for e in selected] != expected:
                        out.append(dict(detail="physical WRAP CTI", words=physical))
                    if any(e["bte"] != {4: 1, 8: 2, 16: 3}[physical] for e in selected):
                        out.append(dict(detail="physical WRAP BTE", words=physical))
                elif physical == 2:
                    if any(e["cti"] != 0 or e["bte"] != 0 for e in selected):
                        out.append(dict(detail="WRAP2 classic fallback"))
                elif any(e["bte"] != 0 for e in selected):
                    out.append(dict(detail="decomposed WRAP BTE", words=physical))
                continue
            if physical <= 16:
                if any(e["cti"] != 0 or e["bte"] != 0 for e in selected):
                    out.append(dict(detail="partial WRAP classic fallback"))
            elif any(e["bte"] != 0 for e in selected):
                out.append(dict(detail="partial decomposed WRAP BTE", words=physical))
            continue
        if s.size == 2 and checker.bench["bfms"]["s0"]["data_bits"] == 32:
            if s.burst == 0:
                masks = [mask for _, mask in s.beats] if s.dir == "w" else [15] * s.length
                fixed = len(masks) > 1 and len(set(masks)) == 1 and masks[0] != 0
                expected = ([1] * (len(selected) - 1) + [7]) if fixed else [0] * len(selected)
                if [e["cti"] for e in selected] != expected:
                    out.append(dict(detail="FIXED CTI", got=[e["cti"] for e in selected], want=expected))
            elif s.burst == 2 and s.length in (4, 8, 16):
                expected = [2] * (len(selected) - 1) + [7]
                if [e["cti"] for e in selected] != expected:
                    out.append(dict(detail="WRAP CTI", got=[e["cti"] for e in selected]))
                if any(e["bte"] != {4: 1, 8: 2, 16: 3}[s.length] for e in selected):
                    out.append(dict(detail="WRAP BTE"))
    return out


family = globals().get("PARAMS", {}).get("family", "shapes")
if family == "source":
    configs = []
    for unit in ("byte", "word"):
        cfg = configuration("wishbone", "axi4", unit)
        cfg["initiators"][0]["burst_tags"] = True
        configs.append(dict(label=unit, dut=dict(source="cli", config=cfg),
                            bfms=[dict(name="s0", kind="wishbone_master", endpoint="initiator:s0",
                                       word=unit == "word"),
                                  dict(name="t0", kind="axi_slave", endpoint="target:t0")],
                            model=dict(routes=nlconfig.routes(cfg), targets=nlconfig.targets(cfg)),
                            scenario_args=dict(ack=1, family=family)))
    scenario = source_scenario
else:
    configurations = [(32, False, "byte", 1), (32, False, "word", 4), (32, False, "byte", 5)]
    if family == "width":
        configurations = [(n, True, "word", 4) for n in (64, 128, 512, 1024)]
    elif family in ("pressure", "banks"):
        configurations = [(32, False, "byte", 4)]
    configs = [variant(n, c, u, a) for n, c, u, a in configurations]
    for entry, (n, _, _, _) in zip(configs, configurations):
        entry["scenario_args"].update(family=family, bits=n)
    scenario = target_scenario
CASE["configs"] = configs
CASE["oracle"] = burst_oracle



def last_tag(rtl, mapping):
    return [(r"^(assign nl_t_t0_cti = )([^;]+);$",
             r"\1((\2 == 3'd7) ? 3'd2 : \2);", 1)]


CASE["faults"] = [
    dict(id="missing_final_tag", component="dut", op="text",
         contract="WB.BURST.SEQUENCE", expect=["WB.RULE_4_30", "ORACLE"], subs=last_tag),
    dict(id="bank_data_bit", component="dut", op="text",
         contract="AXI.ROUTE.LOCAL_OFFSET", expect=["TARGET_WRITE_BYTES", "SOURCE_R_DATA"],
         subs=[(r"^(assign nl_t_t0_dat_w = )([^;]+);$", r"\1(\2 ^ 32'd1);", 1)]),
]
CASE["contracts"].append("WB.BURST.SEQUENCE")
if family == "shapes":
    import copy
    retry = copy.deepcopy(configs[1])
    retry["label"] += "_retry"
    retry["dut"]["config"]["targets"][0]["retry"] = "error"
    retry["bfms"][1]["retry"] = True
    configs.append(retry)
    for bits in (64, 128):
        entry = variant(bits)
        entry["scenario_args"].update(family=family, bits=bits)
        configs.append(entry)

if family == "source":
    CASE["faults"] = [dict(id="ungated_termination", component="dut", op="text",
                           contract="WB.BURST.SEQUENCE",
                           expect=["WB.RULE_3_30", "WB.RULE_3_50"], subs=ungated_termination)]
    cfg = configuration("wishbone", "axi4", "word", True)
    cfg["initiators"][0]["burst_tags"] = True
    cfg["targets"][0]["data_width"] = 64
    configs.append(dict(label="word_axi64_cdc", dut=dict(source="cli", config=cfg),
                        bfms=[dict(name="s0", kind="wishbone_master", endpoint="initiator:s0", word=True),
                              dict(name="t0", kind="axi_slave", endpoint="target:t0", clock="io")],
                        clocks=dict(main=10.0, io=17.0),
                        model=dict(routes=nlconfig.routes(cfg), targets=nlconfig.targets(cfg)),
                        scenario_args=dict(ack=1, family=family)))
