"""Each initiator's read and write responses, and the class seen by a log."""

from nlverif import config as nlconfig

MODE = globals().get("PARAMS", {}).get("protocol", "axi4")
ALL_FOLDED = MODE in ("axi4_folded", "axi4_compact")
PROTOCOL = "axi4" if ALL_FOLDED else MODE
LOGGED = PROTOCOL == "logged"
FULL = PROTOCOL in ("axi4", "axi3")
APB = PROTOCOL == "apb4"
HIT, SLOW, EXCLUSIVE, MISS = 0x1000, 0x2000, 0x3000, 0x4000
STATE = 0x100004


def source(name, protocol, folded):
    out = dict(name=name, clock_domain="main", protocol=protocol, data_width=32)
    if protocol in ("axi4", "axi3", "axi4_lite"):
        out["slverr_only"] = folded
    if protocol in ("axi4", "axi3"):
        out.update(id_width=2, len_width=4)
    return out


def target(name, protocol, base, **fields):
    return dict(name=name, clock_domain="main", protocol=protocol, data_width=32,
                attributes="amba", mappings=[dict(base=hex(base), size="0x1000")], **fields)


def configuration():
    cfg = dict(schema_version=10, name="initiator_errors", address_width=32,
               reset_ports=[dict(name="reset_n")], test_ports=[dict(name="test")],
               clock_domains=[dict(name="main", clock_port="clk", reset_port="reset_n",
                                   test_port="test")],
               initiators=[source("folded", "axi4" if LOGGED else PROTOCOL, True)],
               targets=[target("mem", "axi4", HIT)], connectivity="all",
               output=dict(backend="verilog", language="verilog_2001"))
    if LOGGED:
        cfg["initiators"].append(source("csr", "apb4", True))
        cfg["connectivity"] = dict(folded=["mem"], csr=["regs"])
        cfg["error_logging"] = dict(
            registers=dict(name="regs", base="0x100000", size="0x1000", clock_domain="main"),
            logs=[dict(name="errors", watch=["mem"], loggers=[dict(skip=["decode"]), {}])])
    else:
        cfg["initiators"].append(source("standard", PROTOCOL, ALL_FOLDED))
        if MODE == "axi4_compact":
            cfg["transport"] = dict(compact_failure_responses=True)
        cfg["targets"].append(target("slow", "axi4", SLOW,
                                     timeout=dict(cycles=16, completion=True)))
        if FULL:
            cfg.setdefault("transport", {})["user_flags"] = ["owner"]
            for owner, initiator in enumerate(cfg["initiators"]):
                initiator["user_flags"] = {"owner": owner}
            cfg["targets"].append(target("monitor", "apb4", EXCLUSIVE, exclusive="network",
                                         exclusive_owner_flags=["owner"]))
    return cfg


def bench(cfg):
    out = [dict(name=s["name"], endpoint="initiator:" + s["name"],
                kind="apb_master" if s["protocol"] == "apb4" else "axi_master",
                **({"lite": True} if s["protocol"] == "axi4_lite" else {}))
           for s in cfg["initiators"]]
    for t in cfg["targets"]:
        name = t["name"]
        spec = dict(name=name, endpoint="target:" + name,
                    kind="apb_slave" if name == "monitor" else "axi_slave")
        if name == "slow":
            spec["sideband"] = ("dut", "target:slow", "timed_out")
        out.append(spec)
    return out


def completed(t, direction, port, address, code, **expected):
    count = "rlast" if direction == "r" else "b"
    before = t.snap(port, count)
    send = t.read if direction == "r" else t.write
    send(port, address, resp=code, **expected)
    t.wait(port, count, "==", 1, since=before, limit=180, label="SOURCE_RESPONSE")


def target_responses(t):
    for slot, code in enumerate((2, 3)):
        for field, value in (("lo", 4 * slot), ("hi", 4 * slot + 4),
                             ("r", code), ("w", code)):
            t.set("mem", f"reg{slot}_{field}", value)


def transfers(t, port):
    decerr = 2 if port == "folded" or APB or ALL_FOLDED else 3
    for direction in ("r", "w"):
        completed(t, direction, port, MISS, decerr)
        completed(t, direction, port, HIT, 2)
        completed(t, direction, port, HIT + 4, decerr)
        completed(t, direction, port, HIT + 8, 0)
    if FULL:
        completed(t, "w", port, EXCLUSIVE, 0, lock=1, target="none")
        completed(t, "r", port, EXCLUSIVE + 4, 1, lock=1)
        completed(t, "w", port, EXCLUSIVE + 4, 1, lock=1)


def timeout(t, port, direction):
    t.reset(8)
    t.delay(8)
    channel = "r" if direction == "r" else "b"
    request = "ar" if direction == "r" else "aw"
    issued = t.snap("slow", request)
    t.gate("slow", channel, False)
    completed(t, direction, port, SLOW, 2, target="any", targets=1, late_access=True)
    t.expect_now("slow", request, "==", 1, "TARGET_REQUEST_ACCEPTED", since=issued)
    t.expect_now("slow", "sideband", "==", 1, "TIMEOUT_REPORTED")
    reply = "rlast" if direction == "r" else "b"
    before = t.snap("slow", reply)
    t.gate("slow", channel, True)
    t.wait("slow", reply, "==", 1, since=before, limit=80, label="LATE_REPLY_DRAINED")


def read_bank(t, state, code):
    completed(t, "r", "csr", state, 0, target="none", skip=True, record_state=code)


def logged_transfers(t):
    for direction in ("r", "w"):
        for address, category in ((HIT, 2), (HIT + 4, 1), (MISS, 1)):
            completed(t, direction, "folded", address, 2)
            t.delay(8)
            bank = STATE if category == 2 else STATE + 64
            read_bank(t, STATE, 0x21 if category == 2 else 0)
            read_bank(t, STATE + 64, 0x11 if category == 1 else 0)
            completed(t, "w", "csr", bank, 0, target="none", skip=True, salt=0, strobe="first")
    read_bank(t, STATE, 0)
    read_bank(t, STATE + 64, 0)


def log_oracle(_checker, _events, sources, _targets):
    return [dict(rule="RECORD_CLASS", tag=s.tag, expected=want, got=s.beats)
            for s in sources if "record_state" in s.expect
            for want in [s.expect["record_state"]]
            if len(s.beats) != 1 or s.beats[0][0] & (0xF1 if want else 1) != want]


def scenario(t, seed):
    t.reset(8)
    t.delay(8)
    target_responses(t)
    if LOGGED:
        logged_transfers(t)
    else:
        for port in ("folded", "standard"):
            transfers(t, port)
            for direction in ("r", "w"):
                timeout(t, port, direction)
    t.drain(limit=180, label="DONE")
    t.end()


def replace_port(port, transform):
    def subs(_rtl, _map):
        signal = f"nl_i_{port}"
        return [(rf"^assign {signal} = ([^;]+);$", lambda m:
                 f"assign {signal} = {transform(m[1])};", 1)]
    return subs


def native_decerr_unfolded(_rtl, _map):
    # Only the read of the target's DECERR word bypasses the fold.
    return [(r"^assign nl_i_folded_rresp = ([^;]+);$",
             r"reg bypass_fold;"
             "\nalways @(posedge clk) if (nl_i_folded_arvalid && nl_i_folded_arready)"
             f"\n  bypass_fold <= nl_i_folded_araddr == 32'd{HIT + 4};"
             r"\nassign nl_i_folded_rresp = bypass_fold && (\1) == 2'd2 ? 2'd3 : (\1);", 1)]


CONTRACT = "FABRIC.ERR.DECODE_MISS"
CFG = configuration()
MAPPED = dict(CFG, connectivity=dict(folded=["mem"])) if LOGGED else CFG
CASE = dict(
    name="initiator_error_responses", contracts=[CONTRACT],
    dut=dict(source="cli", config=CFG), bfms=bench(CFG), max_cycles=10000,
    model=dict(routes=nlconfig.routes(MAPPED), targets=nlconfig.targets(CFG, error_data_zero=True)),
)
if LOGGED:
    CASE["model"]["routes"]["csr"] = [("regs", STATE - 4, 0x1000)]
    CASE["model"]["targets"]["regs"] = dict(kind="apb", bus_bytes=4, dedicated=False)
    CASE["oracle"] = log_oracle
    CASE["faults"] = [dict(id="log_loses_decode", op="text", contract=CONTRACT, expect=["ORACLE"],
                           subs=replace_port("csr_prdata", lambda old: f"({old}) & 32'hffffffef"))]
elif APB:
    CASE["faults"] = [dict(id="apb_drops_error", op="text", contract=CONTRACT, expect=["SOURCE_R_RESP"],
                           subs=replace_port("folded_pslverr", lambda _old: "1'b0"))]
else:
    CASE["faults"] = [
        dict(id="read_target_error_unfolded", op="text", contract=CONTRACT,
             expect=["SOURCE_R_RESP"], subs=native_decerr_unfolded),
        dict(id="write_other_source_wrong_code", op="text", contract=CONTRACT,
             expect=["SOURCE_B_RESP"], subs=replace_port("standard_bresp",
                 lambda old: (f"({old}) == 2'd2 ? 2'd3 : ({old})" if ALL_FOLDED else
                              f"({old}) == 2'd3 ? 2'd2 : ({old})"))),
    ]
