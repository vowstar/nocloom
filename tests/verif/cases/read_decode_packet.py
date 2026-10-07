"""Read responses preserve each source beat's own target contributions.

The source must answer its first beat once its sub-reads arrive while unrelated target suffixes
are withheld. Mixed errors, narrow reads, WRAP, same-ID traffic, R/B sharing and RUSER use the
independent event checker. A local miss supplies no target access and exposes no length data.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402

REGION = (0x2100, 0x2a00)
BEATS = (2, 3, 4, 8, 16)
READY_PHASE = 2  # the phase whose source RREADY pauses


def config(bursts, source_bits=32):
    return endpoint_ports({
        'schema_version': 10, 'name': 'decode_packet', 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}],
        'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n',
                           'test_port': 'test'}],
        'transport': {},
        'initiators': [{'name': 's0', 'clock_domain': 'main', 'protocol': 'axi4',
                        'data_width': source_bits, 'max_outstanding': 4}],
        'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4', 'data_width': 32,
                     'attributes': 'amba', 'max_outstanding': bursts,
                     'mappings': [{'base': '0x0', 'size': '0x8000'}]}],
        'connectivity': 'all',
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=2, len_width=8)



def okay_rresp(rtl, _map):
    """Every use of t0's RRESP input reads OKAY."""
    uses = [m for m in re.finditer(r'^(?!\s*input\b)(.*)\bnl_t_t0_rresp\b', rtl, re.M)]
    return [(r'^(?!\s*input\b)(.*)\bnl_t_t0_rresp\b', r"\g<1>2'd0", len(uses))]


def source_output(signal, value, source="s0"):
    def change(rtl, _map):
        pattern = rf"(?m)^(\s*assign nl_i_{source}_{signal} = )([^;]+);"
        return [(pattern, lambda m: m[1] + value(m[2]) + ";", 1)]
    return change


def recipe(bursts, source_bits=32, policy=None, protocol="axi4", target_bits=32):
    cfg = config(bursts, source_bits)
    cfg['transport']['response_user_bits'] = 2
    if policy is not None:
        cfg['transport']['response_codes'] = policy
    cfg['initiators'][0]['response_user_width'] = 2
    cfg['targets'][0]['response_user_width'] = 2
    cfg['targets'][0]['data_width'] = target_bits
    cfg['targets'][0]['protocol'] = protocol
    if protocol == "axi3":
        cfg['targets'][0]['len_width'] = 4
    return dict(dut=dict(source="cli", config=cfg),
                bfms=[dict(name='s0', kind="axi_master", endpoint="initiator:s0"),
                      dict(name='t0', kind="axi_slave", endpoint="target:t0", salt=0)],
                model=dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg),
                            response_user_bits=2,
                           targets=nlconfig.targets(cfg, cache="amba", prot=True)))


def exclusive_recipe(source_bits=32):
    entry = recipe(4, source_bits)
    cfg = entry["dut"]["config"]
    cfg["transport"]["user_flags"] = ["owner"]
    cfg["initiators"][0]["user_flags"] = dict(owner=0)
    cfg["targets"][0].update(exclusive="target", exclusive_id_flags=["owner"])
    entry["model"]["targets"]["t0"]["lock"] = True
    entry["scenario_args"] = dict(exclusive=True)
    entry["oracle"] = exclusive_responses
    if source_bits > 32:
        entry["faults"] = [dict(
            id="exclusive_mixed_success", op="text",
            subs=source_output("rresp", lambda old:
                f"(nl_i_s0_arlock && nl_i_s0_araddr == 32'h4700 && ({old}) == 2'd0"
                f" ? 2'd1 : ({old}))"),
            contract="AXI.ERR.READ_PACKET", expect=["SOURCE_R_RESP"],
            note="a prior OKAY sub-read must prevent a final EXOKAY from succeeding")]
    return entry


def exclusive_responses(checker, events, sources, targets):
    out = []
    for source in sources:
        if source.port != "s0" or source.dir != "r" or source.addr not in (0x4600, 0x4700):
            continue
        count = source.length * (checker.bench["bfms"][source.port]["data_bits"] // 32)
        want = [1] * count if source.addr == 0x4600 else [0] * count
        want[2 if source.addr == 0x4600 else 3] = 3 if source.addr == 0x4600 else 1
        got = [code for target in source.targets for code in target.resps]
        if got != want:
            out.append(dict(rule="EXCLUSIVE_TARGET_RESPONSES", tag=source.tag, got=got, want=want))
    return out


def logging_recipe(bursts):
    entry = recipe(bursts)
    cfg = entry["dut"]["config"]
    cfg["initiators"].append(dict(name="cfg", clock_domain="main", protocol="apb4", data_width=32))
    cfg["connectivity"] = {"s0": ["t0"], "cfg": ["csr"]}
    cfg["error_logging"] = dict(
        registers=dict(name="csr", base="0x100000", size="0x1000", clock_domain="main"),
        logs=[dict(name="errors", watch=["t0"])])
    entry["bfms"].append(dict(name="cfg", kind="apb_master", endpoint="initiator:cfg"))
    entry["model"]["routes"]["cfg"] = [("csr", 0x100000, 0x1000)]
    entry["model"]["targets"]["csr"] = dict(kind="apb", bus_bytes=4, dedicated=False)
    entry["scenario_args"] = dict(logging=True)
    entry["oracle"] = logger_records
    entry["faults"] = [dict(
        id="logger_category", op="text",
        subs=source_output("prdata", lambda old: f"({old}) & 32'hfff8ffff", source="cfg"),
        contract="AXI.ERR.READ_PACKET", expect=["ORACLE"],
        note="clear the recorded error category while preserving the pending bit")]
    return entry


def logger_records(checker, events, sources, targets):
    out = []
    for source in sources:
        if source.port != "cfg" or source.dir != "r":
            continue
        want, mask = source.expect["logger_bits"]
        if len(source.beats) != 1 or source.beats[0][1] != 0 or source.beats[0][0] & mask != want:
            out.append(dict(rule="LOGGER_READ_BEAT", tag=source.tag, want=want, mask=mask,
                            got=source.beats))
    return out


def logging_scenario(t):
    def csr_read(offset, want, mask):
        before = t.snap("cfg", "r")
        t.read("cfg", 0x100000 + offset, target="none", skip=True, logger_bits=[want, mask])
        t.wait("cfg", "r", ">=", 1, since=before, limit=1000, label="LOGGER_REGISTER")

    for error_word, response in ((0, 3), (2, 3), (1, 2)):
        t.set("t0", "reg0_lo", 0x2400 + 4 * error_word)
        t.set("t0", "reg0_hi", 0x2404 + 4 * error_word)
        t.set("t0", "reg0_r", response)
        before = t.snap("t0", "r")
        tail = t.snap("t0", "rlast")
        t.set("t0", "r_stop", error_word + 2)
        t.read("s0", 0x2400, beats=8, id=1, cache=3)
        t.wait("t0", "r", ">=", error_word + 1, since=before, limit=1000,
               label="LOGGER_ERROR_WORD")
        t.delay(16)
        csr_read(0x10, 2, 2)
        csr_read(0x14, (2 if response == 3 else 1) << 16, 7 << 16)
        t.expect_now("t0", "rlast", "==", 0, since=tail, label="LOGGER_BEFORE_TAIL")
        t.set("t0", "r_stop", 0)
        t.drain(limit=4000, label="LOGGER_RESPONSE_COMPLETE")
        before = t.snap("cfg", "b")
        # The generated low write byte with salt zero has bit zero set at this address.
        t.write("cfg", 0x100010, salt=0, target="none", skip=True)
        t.wait("cfg", "b", ">=", 1, since=before, limit=1000, label="LOGGER_CLEAR")
        csr_read(0x10, 0, 2)
    t.end()


CASE = dict(
    name="read_decode_packet",
    contracts=["AXI.ERR.READ_PACKET"],
    negative_faults_all=True,
    max_cycles=40000,
    faults=[
        dict(id="first_error_suffix", op="text",
             subs=source_output("rresp", lambda _old: "2'd3"),
             contract="AXI.ERR.READ_PACKET", expect=["SOURCE_R_RESP"],
             note="the successful suffix must keep OKAY after a first DECERR"),
        dict(id="missing_beats", op="text",
             subs=source_output("rvalid", lambda old: f"({old}) && (nl_i_s0_rresp != 2'd0)"),
             contract="AXI.ERR.READ_PACKET", expect=["TAPE.EXPECT:READ_BEAT_COUNT"],
             note="drop successful suffix beats after the target completes"),
        dict(id="length_as_data", op="text",
             subs=source_output("rdata", lambda _old: "7"),
             contract="AXI.ERR.READ_PACKET", expect=["SOURCE_R_DATA"],
             note="expose the initial local miss length as response data"),
        dict(id="ruser_lost", op="text",
             subs=source_output("ruser", lambda _old: "2'd0"),
             contract="AXI.ERR.READ_PACKET", expect=["SOURCE_R_USER"]),
        dict(id="rresp_okay", op="text", subs=okay_rresp,
             contract="AXI.ERR.READ_PACKET", expect=["SOURCE_R_RESP"]),
    ],
    **recipe(1),
    configs=[dict(label=""), dict(label="four_bursts", **recipe(4)),
             dict(label="wide", **recipe(1, 64)),
             dict(label="wide_four_bursts", **recipe(4, 128)),
             dict(label="uniform", **recipe(4, 128, "uniform")),
             dict(label="axi3_target", **recipe(4, 128, protocol="axi3")),
             dict(label="upsize", **recipe(1, 32, target_bits=128)),
             dict(label="upsize_four", **recipe(4, 32, target_bits=128)),
             dict(label="exclusive", **exclusive_recipe()),
             dict(label="exclusive_wide", **exclusive_recipe(128)),
             dict(label="logger_one", **logging_recipe(1)),
             dict(label="logger_four", **logging_recipe(4))],
)


def scenario(t, seed, logging=False, exclusive=False):
    t.set("t0", "seed", 0x1357 * seed + 1)
    t.set("t0", "r_user_base", 3)
    t.set("t0", "r_user_step", 1)
    t.reset(4)
    t.delay(2)
    if logging:
        logging_scenario(t)
        return
    # The descriptor length is seven. No target access supplies this local error.
    t.read("s0", 0xf000, beats=8, id=0)
    t.drain(limit=2000, label="LOCAL_MISS")
    # One first-beat DECERR followed by successful target words. Withhold the suffix only
    # after enough sub-reads for the first source beat have arrived.
    t.set("t0", "reg0_lo", REGION[0])
    t.set("t0", "reg0_hi", REGION[0] + 4)
    t.set("t0", "reg0_r", 3)
    source_bytes = t.bfms['s0']['data_bits'] // 8
    contributions = max(1, source_bytes // (t.bfms['t0']['data_bits'] // 8))
    target = t.snap("t0", "r")
    source = t.snap("s0", "r")
    tail = t.snap("t0", "rlast")
    t.set("t0", "r_stop", contributions + 1)
    t.read("s0", REGION[0], beats=8, id=1, cache=3)
    t.wait("t0", "r", ">=", contributions, since=target, limit=1000,
           label="READ_CONTRIBUTIONS")
    t.wait("s0", "r", ">=", 1, since=source, limit=128, label="READ_BEAT_PROGRESS")
    t.expect_now("t0", "rlast", "==", 0, since=tail, label="READ_SUFFIX_WITHHELD")
    t.set("t0", "r_stop", 0)
    t.wait("t0", "rlast", ">=", 1, since=tail, limit=2000, label="TARGET_READ_COMPLETE")
    t.delay(64)
    t.expect_now("s0", "r", "==", 8, since=source, label="READ_BEAT_COUNT")
    t.drain(limit=2000, label="STREAMED_READ")
    # EXOKAY without an exclusive target access is a target SLVERR, not a length tag.
    t.set("t0", "reg0_r", 1)
    t.read("s0", REGION[0], beats=4, cache=3)
    t.drain(limit=4000, label="ORDINARY_EXOKAY")
    t.set("t0", "reg0_r", 3)
    # Mixed error words in wide beats, narrow INCR and both WRAP replay paths.
    t.set("t0", "reg1_lo", REGION[0] + 0x20)
    t.set("t0", "reg1_hi", REGION[0] + 0x24)
    t.set("t0", "reg1_r", 2)
    # DECERR and SLVERR within the same wide source beat use the exact documented priority.
    t.set("t0", "reg1_lo", REGION[0] + 4)
    t.set("t0", "reg1_hi", REGION[0] + 8)
    t.read("s0", REGION[0], cache=3)
    t.drain(limit=4000, label="PARTIAL_WIDE_PRIORITY")
    t.set("t0", "reg1_lo", REGION[0] + 0x20)
    t.set("t0", "reg1_hi", REGION[0] + 0x24)
    for ready in (False, True):
        if ready:
            t.ready("s0", "r", "modulo", 3, 0)
            t.set("t0", "r_rate", 50)
        for burst in ("incr", "wrap"):
            for at in (REGION[0], REGION[0] + 4, REGION[0] + 0x1c):
                for beats in (2, 4, 8):
                    t.read("s0", at, beats=beats, size=2, burst=burst, id=2, cache=3)
                    t.drain(limit=4000, label="MIXED_READ")
        t.read("s0", REGION[0], beats=8, id=3, cache=3)
        t.write("s0", 0x600, beats=4, id=3, cache=3)
        t.read("s0", 0x400, beats=8, id=3, cache=3)
        t.read("s0", REGION[0] + 0x20, beats=8, id=3, cache=3)
        t.drain(limit=6000, label="SAME_ID_READS_AND_B")
    if exclusive:
        t.set("t0", "lock_ok", 1)
        t.read("s0", 0x4500, beats=4, id=0, lock=1, cache=3)
        t.drain(limit=4000, label="EXCLUSIVE_EXOKAY")
        t.set("t0", "lock_ok", 0)
        t.set("t0", "reg0_lo", 0x4608)
        t.set("t0", "reg0_hi", 0x460c)
        t.set("t0", "reg0_r", 3)
        t.set("t0", "reg1_lo", 0x4600)
        t.set("t0", "reg1_hi", 0x4600 + 4 * source_bytes)
        t.set("t0", "reg1_r", 1)
        t.read("s0", 0x4600, beats=4, id=0, lock=1, cache=3)
        t.drain(limit=4000, label="EXCLUSIVE_MIXED_BEATS")
        t.set("t0", "reg1_lo", 0x470c)
        t.set("t0", "reg1_hi", 0x4710)
        t.read("s0", 0x4700, beats=4, id=0, lock=1, cache=3)
        t.drain(limit=4000, label="EXCLUSIVE_MIXED_SUCCESS")
    t.end()
