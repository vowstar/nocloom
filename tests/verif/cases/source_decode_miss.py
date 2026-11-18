"""Check source responses around mapped traffic and with a timed-out target."""

import re

from nlverif import config as nlconfig

PROTOCOL = PARAMS.get("protocol", "axi4")
CLOCKED = PARAMS.get("clocked", "0") == "1"
UNIFORM = PARAMS.get("uniform", "0") == "1"
MASTER = {
    "axi4": "axi_master", "axi3": "axi_master", "axi4_lite": "axi_master",
    "apb4": "apb_master", "ahb_lite": "ahb_master", "wishbone": "wishbone_master",
    "tl_ul": "tl_master",
}[PROTOCOL]
BURST = PROTOCOL in ("axi4", "axi3")
ID = 1 if BURST or PROTOCOL == "tl_ul" else 0
PROT = 2 if PROTOCOL in ("wishbone", "tl_ul") else 0
FAULT_RULE = ("TAPE.WAIT_TIMEOUT:ORDERED_READS" if PROTOCOL == "wishbone"
              else "SOURCE_B_RESP")
HIT = 0x2020
MISS = 0x4040
CONTRACT = "FABRIC.ERR.DECODE_MISS"


def configuration():
    clock = dict(name="main", clock_port="clk_main", reset_port="reset_n", test_port="test")
    source = dict(name="s", clock_domain="main", protocol=PROTOCOL, data_width=32,
                  max_outstanding=1)
    if BURST or PROTOCOL == "axi4_lite":
        source["slverr_only"] = UNIFORM
    if BURST:
        source.update(id_width=2, len_width=4 if PROTOCOL == "axi3" else 8)
    if PROTOCOL == "wishbone":
        source["address_unit"] = "byte"
    targets = [dict(name="good", clock_domain="main", protocol="axi4", data_width=32,
                    len_width=8, attributes="amba",
                    mappings=[dict(base="0x2000", size="0x1000")])]
    cfg = dict(schema_version=10, name="source_miss", address_width=32,
               clock_domains=[clock], reset_ports=[dict(name="reset_n")],
               test_ports=[dict(name="test")], initiators=[source], targets=targets,
               connectivity="all", transport=dict(max_outstanding=1),
               output=dict(backend="verilog", language="verilog_2001"))
    if UNIFORM:
        cfg["transport"]["compact_failure_responses"] = True
    if CLOCKED:
        cfg["clock_domains"].append(dict(clock, name="far", clock_port="clk_far"))
        cfg["initiators"].append(dict(source, name="blocker"))
        targets.append(dict(targets[0], name="hung", clock_domain="far", timeout=dict(cycles=16),
                            mappings=[dict(base="0x0", size="0x1000")]))
        cfg["topology"] = dict(
            request=dict(nodes=[dict(name="q", clock_domain="main")], links=[],
                         entries=[dict(endpoint=s, node="q") for s in ("s", "blocker")],
                         exits=[dict(endpoint=t, node="q") for t in ("good", "hung")]),
            response="mirror")
    return cfg


def wrong_response():
    port = {
        "axi4": "bresp", "axi3": "bresp", "axi4_lite": "bresp", "apb4": "pslverr",
        "ahb_lite": "hresp", "wishbone": "err", "tl_ul": "d_denied",
    }[PROTOCOL]
    return [(rf"^assign nl_i_s_{port} = [^;]+;$", f"assign nl_i_s_{port} = 0;", 1)]


def wrong_read_tail(_rtl, _mapping):
    code = 2 if UNIFORM else 3
    return [(r"^assign nl_i_s_rlast = ([^;]+);$",
             rf"assign nl_i_s_rlast = (nl_i_s_rresp == 2'd{code}) ? 1'b0 : \1;", 1)]


def wrong_read_id(_rtl, mapping):
    address = mapping["network"]["formal"]["sources"]["s"]["read_desc_addr"]
    address = "nl_internal_v" + address.removeprefix("value/")
    return [(r"^assign nl_i_s_rid = ([^;]+);$",
             rf"assign nl_i_s_rid = \1 ^ (({address} == 32'd{MISS + 8}) & nl_i_s_rresp[1]);", 1)]


def missing_write_beat(_rtl, mapping):
    row = next(row for row in mapping["network"]["admission"] if row["source"] == "s")
    mapped = "nl_internal_v" + row["write_route_match"].removeprefix("value/")
    pass_beat = f"({mapped} | ~nl_i_s_wlast)"
    return [
        (r"^assign (nl_internal_v\d+) = nl_i_s_wvalid;$",
         rf"assign \1 = nl_i_s_wvalid & {pass_beat};", 1),
        (r"^assign nl_i_s_wready = ([^;]+);$",
         rf"assign nl_i_s_wready = \1 & {pass_beat};", 1),
    ]


def overwrite_held_b(rtl, mapping):
    # Follow the staged BRESP update to the local fill and remove its room check.
    stage = mapping["network"]["formal"]["sources"]["s"]["write_staged_resp"]
    stage = "nl_internal_v" + stage.removeprefix("value/")
    net = r"nl_internal_v\d+"

    def capture(pattern):
        matches = re.findall(pattern, rtl, re.M)
        if len(matches) != 1:
            raise ValueError(f"Expected one staged B anchor for {pattern}, found {len(matches)}")
        return matches[0]

    next_response = capture(rf"^    {stage} <= ({net});$")
    local_mux = capture(rf"^assign {next_response} = \({net} \? {net} : ({net})\);$")
    local_fill = capture(rf"^assign {local_mux} = \(({net}) \? {net} : {stage}\);$")
    reset, guard = capture(rf"^assign {local_fill} = \(({net}) & ({net})\);$")
    responding = capture(rf"^assign {guard} = \(({net}) & {net}\);$")
    return [(rf"^assign {local_fill} = \({reset} & {guard}\);$",
             f"assign {local_fill} = ({reset} & {responding});", 1)]


CFG = configuration()
BFMS = [dict(name="s", kind=MASTER, endpoint="initiator:s", lite=PROTOCOL == "axi4_lite"),
        dict(name="good", kind="axi_slave", endpoint="target:good", salt=3)]
if CLOCKED:
    BFMS += [dict(name="blocker", kind=MASTER, endpoint="initiator:blocker"),
             dict(name="hung", kind="axi_slave", endpoint="target:hung", clock="far",
                  sideband=("dut", "target:hung", "timed_out"), salt=4)]

CASE = dict(
    name="source_decode_miss",
    contracts=[CONTRACT],
    dut=dict(source="cli", config=CFG), bfms=BFMS,
    model=dict(routes=nlconfig.routes(CFG), targets=nlconfig.targets(CFG, error_data_zero=True),
               keeps_decerr=not UNIFORM),
    clocks=dict(main=10.0, far=17.0) if CLOCKED else dict(main=10.0),
    max_cycles=4000,
    faults=[dict(id="lost_error", op="text", subs=wrong_response(),
                 expect=[FAULT_RULE],
                 contract=CONTRACT)],
)
if BURST:
    CASE["faults"] += [
        dict(id="wrong_read_tail", op="text", subs=wrong_read_tail,
             expect=["AXI.R.LAST"], contract=CONTRACT),
        dict(id="wrong_read_id", op="text", subs=wrong_read_id,
             expect=["AXI.R.UNEXPECTED"], contract=CONTRACT),
        dict(id="missing_write_beat", op="text", subs=missing_write_beat,
             expect=["TAPE.WAIT_TIMEOUT:MISS_LAST_W"], contract=CONTRACT),
        dict(id="overwrite_held_b", op="text", subs=overwrite_held_b,
             expect=["AXI.B.HOLD", "TAPE.EXPECT:OLDER_B_UNCHANGED"], contract=CONTRACT),
    ]


def ordered_writes(t):
    t.ready("s", "b", "never")
    t.write("s", HIT, id=ID)
    t.wait("s", "bvalid", "==", 1, limit=40, label="OLDER_B_HELD")
    t.delay(1)
    t.expect_now("s", "bresp", "==", 0, "OLDER_B_OKAY")
    t.write("s", MISS + 0x40, beats=3, id=ID)
    t.wait("s", "w", "==", 7, limit=60, label="W_BEHIND_OLDER_B")
    t.delay(5)
    t.expect_now("s", "b", "==", 1, "NO_B_WHILE_STALLED")
    t.expect_now("s", "bresp", "==", 0, "OLDER_B_UNCHANGED")
    t.ready("s", "b", "always")
    t.wait("s", "b", "==", 3, limit=40, label="OLDER_B_FIRST")
    t.expect_now("s", "bresp", "==", 2 if UNIFORM else 3, "MISS_B_SECOND")
    t.write("s", HIT + 4, id=ID)
    t.wait("s", "b", "==", 4, limit=80, label="MAPPED_WRITE")
    t.expect_now("good", "aw", "==", 2, "MISS_NO_TARGET_WRITE")
    t.expect_now("good", "w", "==", 2, "ONLY_MAPPED_DATA")


def scenario(t, seed):
    t.reset(12)
    t.delay(12)
    if CLOCKED:
        t.gate("hung", "r", False)
        t.read("blocker", 0x20, id=ID)
        t.wait("hung", "sideband", "==", 1, limit=150, label="TARGET_TIMED_OUT")
    t.read("s", HIT, id=ID, prot=PROT)
    t.wait("s", "rlast", "==", 1, limit=100, label="FIRST_MAPPED")
    if BURST:
        t.ready("s", "r", "never")
        t.read("s", MISS, id=ID)
        t.read("s", HIT+4, id=ID)
        t.wait("good", "ar", "==", 2, limit=60, label="MAPPED_BEHIND_STALLED_MISS_TAIL")
        t.expect_now("s", "rlast", "==", 1, "HELD_MISS")
        t.ready("s", "r", "always")
        t.wait("s", "rlast", "==", 3, limit=80, label="ORDERED_READS")
        t.read("s", MISS + 0x40, beats=3, id=ID)
        t.wait("s", "rlast", "==", 4, limit=40, label="MISS_READ_LAST")
        t.expect_now("s", "r", "==", 6, "MISS_READ_BEATS")
        t.ready("s", "r", "never")
        t.read("s", HIT + 8, id=ID)
        t.read("s", MISS + 8, id=ID)
        t.wait("s", "rvalid", "==", 1, limit=40, label="OLDER_MAPPED_HELD")
        t.delay(8)
        t.ready("s", "r", "always")
        t.wait("s", "rlast", "==", 6, limit=40, label="OLDER_MAPPED_FIRST")
        t.set("s", "w_gap", 12)
        t.ready("s", "b", "never")
        t.write("s", MISS, beats=3, id=ID)
        t.wait("s", "w", "==", 1, limit=40, label="MISS_FIRST_W")
        t.delay(5)
        t.expect_now("s", "bvalid", "==", 0, "NO_B_BEFORE_LAST_W")
        t.wait("s", "w", "==", 3, limit=60, label="MISS_LAST_W")
        t.wait("s", "bvalid", "==", 1, limit=30, label="MISS_B_HELD")
        t.delay(5)
        t.ready("s", "b", "always")
        t.wait("s", "b", "==", 1, limit=30, label="MISS_B")
    else:
        external_error = dict(resp=2) if PROTOCOL == "tl_ul" else {}
        t.read("s", MISS, id=ID, prot=PROT, **external_error)
        if PROTOCOL == "tl_ul":
            t.wait("s", "rlast", "==", 2, limit=40, label="TL_MISS_READ")
        t.read("s", HIT+4, id=ID, prot=PROT)
        t.wait("s", "rlast", "==", 3, limit=100, label="ORDERED_READS")
        t.write("s", MISS, id=ID, prot=PROT, **external_error)
        t.wait("s", "b", "==", 1, limit=40, label="MISS_B")
    t.expect_now("good", "ar", "==", 3 if BURST else 2, "MISS_NO_TARGET_READ")
    t.expect_now("good", "aw", "==", 0, "MISS_NO_TARGET_WRITE")
    if BURST:
        ordered_writes(t)
    else:
        t.write("s", HIT, id=ID, prot=PROT)
        t.wait("s", "b", "==", 2, limit=80, label="MAPPED_WRITE")
    if CLOCKED:
        t.expect_now("hung", "ar", "==", 1, "ONLY_THE_BLOCKER_READ")
        t.expect_now("hung", "aw", "==", 0, "MISS_NO_HUNG_TARGET_WRITE")
        t.reset(12)
    t.drain(limit=150, label="COMPLETED")
    t.end()
