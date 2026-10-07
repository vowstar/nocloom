"""A dedicated 64-bit AXI4-Lite target through the public CLI: byte lanes,
packet words, held responses, both write orders and a reset that cancels
a waiting read.

Every source burst is one case, run alone. The case table and its oracle
(vectors(): the target accesses each case makes, with narrow beats of one
word packed into one access, and the response of every beat) come from
the bench; the offline checker judges every byte, strobe, read beat and
response, and each case's number of target accesses.

The configuration "exclusive" gives t0 a network exclusive monitor (one
reservation, 128-byte granule) behind a 64-bit AXI4 source with 4-bit
AxLEN and a 32-bit AXI4-Lite source. A 16-beat 64-bit exclusive read and
write (128 bytes, sixteen target words) pass the source admission and
answer EXOKAY with sixteen target accesses each; a Lite write over the
next reservation makes its exclusive write fail (OKAY, no byte changed).
The Lite source's writes come AW first, W first and both at once, and
one waits at the port while the B before it is held: the map names the
acquire and release nets of the source's write storage, and the oracle
requires each write to take it at its first AW or W handshake, keep it
through the other half, and give it back at its B handshake, one write
at a time. Both behaviours carry a designated fault on the netlist."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402


def configuration(clocked=False):
    d = {'schema_version': 10, 'name': 'lite_target64', 'address_width': 32,
         'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
         'initiators': [{'name': 's0', 'clock_domain': 'main', 'protocol': 'axi4',
                         'data_width': 32 if clocked else 64}],
         'targets': [{'name': 't0', 'clock_domain': 'far' if clocked else 'main',
                      'protocol': 'axi4_lite', 'data_width': 64, 'address_width': 8,
                      'mappings': [{'base': '0x1000', 'size': '0x100'}]}],
         'transport': {'transport_word_bytes': 4},
         'connectivity': 'all', 'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    if clocked:
        # A distinct 32-bit endpoint selects the per-endpoint width path.
        d['targets'].append({'name': 't1', 'clock_domain': 'main', 'protocol': 'axi4',
                             'data_width': 32, 'mappings': [{'base': '0x2000', 'size': '0x1000'}]})
        d['clock_domains'] = [{'name': name, 'clock_port': 'clk_' + name, 'reset_port': 'reset_n', 'test_port': 'test', 'period_ps': period}
                              for name, period in [('main', 10000), ('far', 14000)]]
        d['topology'] = {
            'request': {'nodes': [{'name': 'q0', 'clock_domain': 'main'}], 'links': [],
                        'entries': [{'endpoint': 's0', 'node': 'q0'}],
                        'exits': [{'endpoint': t, 'node': 'q0'} for t in ('t0', 't1')]},
            'response': {'nodes': [{'name': 'p0', 'clock_domain': 'far'}], 'links': [],
                         'entries': [{'endpoint': t, 'node': 'p0'} for t in ('t0', 't1')],
                         'exits': [{'endpoint': 's0', 'node': 'p0'}]}}
    return endpoint_ports(d, id_width=2, len_width=8)


def exclusive_configuration():
    d = {'schema_version': 10, 'name': 'lite_target64_exclusive', 'address_width': 32,
         'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}],
         'initiators': [{'name': 's0', 'clock_domain': 'main', 'protocol': 'axi4',
                         'data_width': 64, 'user_flags': {'owner': 0}},
                        {'name': 's1', 'clock_domain': 'main', 'protocol': 'axi4_lite',
                         'data_width': 32, 'user_flags': {'owner': 1}}],
         'targets': [{'name': 't0', 'clock_domain': 'main', 'protocol': 'axi4_lite',
                      'data_width': 64, 'address_width': 8, 'exclusive': 'network',
                      'exclusive_monitors': 1, 'exclusive_id_flags': ['owner'],
                      'mappings': [{'base': '0x1000', 'size': '0x100'}]}],
         'transport': {'exclusive_granule': 128, 'user_flags': ['owner']},
         'connectivity': 'all', 'output': {'backend': 'verilog', 'language': 'verilog_2001'}}
    return endpoint_ports(d, id_width=2, len_width=4)


def write_storage_probes(mapping):
    """The acquire and release nets of the Lite source's write storage."""
    store = next(s for s in mapping["network"]["storage"] if s["resource"] == "source/s1/write")
    return [dict(name="s1_write", kind="storage", nets=[
        [field, "nl_internal_v" + store[field].removeprefix("value/"), 1]
        for field in ("acquire", "release")])]


def assigns(rtl):
    return dict(re.findall(r"^assign (nl_internal_v\d+) = (.+);$", rtl, flags=re.M))


def exclusive_limit_subs(rtl, mapping):
    """The 4-bit bounds of an exclusive access's log2 total bytes at the
    source (log2 of 128 plus one): each feeds a compare whose left side is
    log2(beats) - (0 - AxSIZE). Lowered by one, the 128-byte bound becomes
    64 bytes."""
    rhs = assigns(rtl)
    binary = re.compile(r"^\((nl_internal_v\d+) ([-<]) (nl_internal_v\d+)\)$")
    subs = []
    for name, expr in rhs.items():
        m = binary.match(expr)
        if not m or m[2] != "<" or rhs.get(m[3]) != "4'd8":
            continue
        total = binary.match(rhs.get(m[1], ""))
        negated = total and total[2] == "-" and binary.match(rhs.get(total[3], ""))
        if negated and negated[2] == "-" and rhs.get(negated[1]) == "4'd0":
            subs.append((rf"^assign {m[3]} = 4'd8;$", f"assign {m[3]} = 4'd7;", 1))
    # The read and the write admission of s0, nothing else.
    return subs if len(subs) == 2 else [("^NO_EXCLUSIVE_BOUND$", "", 1)]


def write_capture_subs(rtl, mapping):
    """The Lite source's write storage is acquired on AW (W not held) or on
    W (no write pending): keep only the AW term."""
    store = next(s for s in mapping["network"]["storage"] if s["resource"] == "source/s1/write")
    net = "nl_internal_v" + store["acquire"].removeprefix("value/")
    return [(rf"^assign {net} = \((nl_internal_v\d+) \| nl_internal_v\d+\);$",
             f"assign {net} = (\\1);", 1)]


CASES =[(1, 0x1000, 1, 3, 1, 1 << i) for i in range(8)]
CASES += [(1, 0x1008, 1, 3, 1, m) for m in (0, 0xa5, 0xff)]
CASES += [(direction, 0x1010 + i, 1, 0, 1, 255) for direction in (1, 0) for i in range(8)]
CASES += [(direction, addr, beats, size, burst, 255)
          for direction in (1, 0)
          for addr, beats, size, burst in ((0x1024, 2, 2, 1), (0x1026, 2, 1, 1),
                                           (0x1033, 3, 2, 1), (0x1038, 4, 3, 2),
                                           (0x1078, 3, 3, 1), (0x10f8, 3, 3, 1),
                                           (0x1100, 2, 3, 1), (0x1050, 2, 3, 0))]
CASES += [(0, 0x1040, 4, 3, 1, 255), (1, 0x1040, 4, 3, 1, 0x5a)]
CASES += [(1, address, beats, 2, 1, keep)
          for address, beats, keep in ((0x1008, 4, 0xff), (0x1014, 3, 0xff),
                                      (0x1028, 4, 0), (0x1034, 3, 0x5))]


def vectors(source_bits):
    """Per case: (write, address, beats, size, burst, keep masks per beat,
    target accesses, write response or read responses per beat)."""
    source_bytes = source_bits // 8
    out = []
    for write, address, beats, size, burst, keep in CASES:
        size = min(size, source_bytes.bit_length() - 1)
        aligned = address >> size << size
        span = beats << size
        codes, masks, words = [], [], []
        for beat in range(beats):
            start = aligned if burst == 0 else (
                aligned // span * span + (aligned % span + beat * (1 << size)) % span
                if burst == 2 else aligned + beat * (1 << size))
            actual = address if beat == 0 else start
            masks.append(sum(1 << (b % source_bytes)
                             for b in range(actual, start + (1 << size))) & keep)
            # A window miss answers DECERR under the default policy.
            # FIXED bursts are refused (fixed_bursts reject).
            code = 3 if not 0x1000 <= actual < 0x1100 else 2 if burst == 0 else 0
            if code == 0:
                # INCR writes collect all beats in one target word, including
                # full-width beats of a narrower source and empty strobes.
                # Read accesses retain the source word boundary.
                packed = burst == 1 and (write or size < source_bytes.bit_length() - 1
                                        or beats == 1)
                word = actual // (8 if write else source_bytes)
                if not (packed and words and words[-1] == word):
                    words.append(word)
                if actual // 8 * 8 - 0x1000 == 0x80:
                    code = 2
            codes.append(code)
        out.append((write, address, beats, size, burst, masks, len(words), codes))
    return out


def model(cfg):
    return dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg),
                targets=nlconfig.targets(cfg, error_data_zero=True))


CONFIG = configuration(False)
CLOCKED = configuration(True)
EXCLUSIVE = exclusive_configuration()

CASE = dict(
    name="lite_target64",
    contracts=["AXI.LITE.WORD_ACCESS", "AXI.ERR.READ_ZERO", "AXI.ROUTE.LOCAL_OFFSET",
               "AXI.RESET.CANCEL", "AXI.W.UNSTROBED_ZERO", "AXI.EXCL.MONITOR",
               "AXI.LITE.SOURCE_WRITE"],
    dut=dict(source="cli", config=CONFIG),
    bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
          dict(name="t0", kind="axi_slave", endpoint="target:t0", lite=True, salt=1)],
    model=model(CONFIG),
    scenario_args=dict(source_bits=64),
    configs=[dict(label=""),
             dict(label="clocked", dut=dict(source="cli", config=CLOCKED), model=model(CLOCKED),
                  clocks=dict(main=10.0, far=14.0), scenario_args=dict(source_bits=32),
                  bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
                        dict(name="t0", kind="axi_slave", endpoint="target:t0", lite=True,
                             clock="far", salt=1),
                        dict(name="t1", kind="axi_slave", endpoint="target:t1", salt=2)]),
             dict(label="exclusive", dut=dict(source="cli", config=EXCLUSIVE),
                  model=model(EXCLUSIVE), scenario_args=dict(mode="exclusive"),
                  bfms=[dict(name="s0", kind="axi_master", endpoint="initiator:s0"),
                        dict(name="s1", kind="axi_master", endpoint="initiator:s1", lite=True),
                        dict(name="t0", kind="axi_slave", endpoint="target:t0", lite=True,
                             salt=1)],
                  map_probes=dict(groups=write_storage_probes),
                  faults=[
                      dict(id="exclusive_bound_64", op="text", subs=exclusive_limit_subs,
                           expect=["SOURCE_R_RESP", "SOURCE_B_RESP", "TARGET_FOOTPRINT"],
                           contract="AXI.EXCL.MONITOR",
                           note="the source admits exclusive totals up to 64 bytes"),
                      dict(id="lite_write_capture_aw_only", op="text", subs=write_capture_subs,
                           expect=["ORACLE"], contract="AXI.LITE.SOURCE_WRITE",
                           note="a W-first Lite write takes its storage only at AW")])],
    # The faults belong to the exclusive configuration.
    negative_faults_all=True,
    max_cycles=60000,
    faults=[],
)


def both_orders(checker, events, sources, targets):
    """The target saw AW before W and W before AW."""
    seq = [e for e in events if e.get("p") == "t0" and e.get("k") == "hs"
           and e.get("ch") in ("aw", "w")]
    aw_first = sum(1 for a, b in zip(seq, seq[1:]) if a["ch"] == "aw" and b["ch"] == "w"
                   and a["t"] < b["t"])
    w_first = sum(1 for a, b in zip(seq, seq[1:]) if a["ch"] == "w" and b["ch"] == "aw"
                  and a["t"] < b["t"])
    if not aw_first or not w_first:
        return [dict(detail="both write orders at the target", aw_first=aw_first,
                     w_first=w_first)]
    return []


def write_hold(checker, events):
    """Each write of the Lite source s1 acquires its write storage at the
    first of its AW and W handshakes (both at once included), not at the
    other half, and releases it at its B handshake; acquires and releases
    alternate (one write at a time). Coverage: AW first, W first, both at
    once, and a write offered at the port while the one before held it."""
    group = next((g for g in checker.bench.get("probes", []) if g["name"] == "s1_write"), None)
    if group is None:
        return [dict(detail="no s1_write probes")]
    acquires, releases = [], []
    for e in events:
        if e.get("k") == "pr" and e.get("p") == group["port"]:
            row = checker._unpack(group, e)
            if row is None:
                return [dict(detail="s1_write probe X", t=e["t"])]
            if row["acquire"]:
                acquires.append(e["t"])
            if row["release"]:
                releases.append(e["t"])
    hs = {ch: [e["t"] for e in events if e.get("p") == "s1" and e.get("k") == "hs"
               and e.get("ch") == ch] for ch in ("aw", "w", "b")}
    offered = [e["t"] for e in events if e.get("p") == "s1" and e.get("k") == "lv"
               and e.get("ch") in ("aw", "w") and e.get("v") == 1]
    out = []
    if not len(hs["aw"]) == len(hs["w"]) == len(hs["b"]):
        return [dict(detail="unpaired Lite write handshakes",
                     **{ch: len(v) for ch, v in hs.items()})]
    firsts = [min(a, w) for a, w in zip(hs["aw"], hs["w"])]
    if acquires != firsts:
        out.append(dict(detail="write storage acquire not at the first AW or W handshake",
                        acquires=acquires[:6], firsts=firsts[:6]))
    if releases != hs["b"]:
        out.append(dict(detail="write storage release not at the B handshake",
                        releases=releases[:6], b=hs["b"][:6]))
    edges = sorted([(t, 0) for t in acquires] + [(t, 1) for t in releases])
    if [kind for _, kind in edges] != [k % 2 for k in range(len(edges))]:
        out.append(dict(detail="write storage acquired while held or released while free"))
    seen = dict(
        aw_first=sum(a < w for a, w in zip(hs["aw"], hs["w"])),
        w_first=sum(w < a for a, w in zip(hs["aw"], hs["w"])),
        together=sum(a == w for a, w in zip(hs["aw"], hs["w"])),
        held=sum(1 for k in range(1, len(firsts))
                 if any(hs["b"][k - 1] > t > firsts[k - 1] for t in offered)))
    if not all(seen.values()):
        out.append(dict(detail="Lite write coverage not reached", seen=seen))
    return out


def oracle(checker, events, sources, targets):
    if checker.case.get("scenario_args", {}).get("mode") == "exclusive":
        return write_hold(checker, events)
    return both_orders(checker, events, sources, targets)


CASE["oracle"] = oracle


def exclusive_scenario(t):
    t.ready("s0", "b", "delay", 3)
    t.ready("s0", "r", "delay", 3)
    t.ready("s1", "b", "delay", 3)
    t.reset(8)
    t.delay(12)
    # A 128-byte exclusive pair: sixteen 64-bit beats, sixteen target words.
    t.mark("case", 0)
    t.read("s0", 0x1000, beats=16, size=3, lock=1, id=1, prot=1, resp=1, targets=16)
    t.drain(limit=5000, label="EXCLUSIVE_READ")
    t.write("s0", 0x1000, beats=16, size=3, lock=1, id=1, prot=1, resp=1, targets=16)
    t.drain(limit=5000, label="EXCLUSIVE_WRITE")
    # A Lite write inside the next reservation's granule: the exclusive
    # write fails, one access per word with WSTRB zero, OKAY.
    t.mark("case", 1)
    t.read("s0", 0x1080, beats=16, size=3, lock=1, id=1, prot=1, resp=1, targets=16)
    t.drain(limit=5000, label="EXCLUSIVE_READ")
    t.write("s1", 0x10f4, targets=1)
    t.drain(limit=5000, label="LITE_WRITE")
    t.write("s0", 0x1080, beats=16, size=3, lock=1, id=1, prot=1, resp=0, bytes="none",
            targets=16)
    t.drain(limit=5000, label="EXCLUSIVE_FAILED")
    # Lite writes AW first, W first and both at once.
    for k, held in enumerate(("w", "aw", None, "w", "aw")):
        t.mark("case", 2 + k)
        if held:
            t.gate("s1", held, False)
        t.write("s1", 0x1010 + 4 * k, prot=2, targets=1)
        if held:
            t.delay(4 + k)
            t.gate("s1", held, True)
        t.drain(limit=5000, label=f"LITE_ORDER_{k}")
    # A second write offered while the first one's B is held.
    t.mark("case", 7)
    m = t.snap("s1", "b")
    t.ready("s1", "b", "never")
    t.write("s1", 0x1030, targets=1)
    t.wait("s1", "bvalid", "==", 1, label="LITE_B_HELD")
    t.write("s1", 0x1034, targets=1)
    t.delay(16)
    t.expect_now("s1", "b", "==", 0, "LITE_B_HELD", since=m)
    t.ready("s1", "b", "delay", 3)
    t.drain(limit=5000, label="LITE_HELD")
    t.end()


def scenario(t, seed, source_bits=64, mode="plain"):
    if mode == "exclusive":
        exclusive_scenario(t)
        return
    t.ready("s0", "b", "delay", 5)
    t.ready("s0", "r", "delay", 5)
    # Target local word 0x80 answers SLVERR in both directions.
    t.set("t0", "reg0_lo", 0x80)
    t.set("t0", "reg0_hi", 0x88)
    t.set("t0", "reg0_w", 2)
    t.set("t0", "reg0_r", 2)
    t.set("t0", "b_lat", 4)
    t.set("t0", "r_lat", 4)
    t.reset(8)
    t.delay(12)
    for k, (write, address, beats, size, burst, masks, accesses, codes) in enumerate(
            vectors(source_bits)):
        t.mark("case", k)
        # AW waits for W in even cases, W for AW in odd ones: both orders.
        t.ready("t0", "aw", "delay" if k % 2 else "always", 3)
        t.ready("t0", "w", "always" if k % 2 else "delay", 3)
        common = dict(beats=beats, size=size, burst=("fixed", "incr", "wrap")[burst],
                      targets=accesses)
        if write:
            t.strobes("s0", masks)
            t.write("s0", address, id=2, prot=3, strobe="table",
                    resp=max(codes), **common)
        else:
            t.read("s0", address, id=1, prot=5, **common)
        t.drain(limit=5000, label=f"CASE_{k}")
    # Cancel a target read waiting for R, then serve a fresh read.
    t.mark("case", len(CASES))
    m = t.snap("t0", "ar")
    t.gate("t0", "r", False)
    t.read("s0", 0x1000, size=(source_bits // 8).bit_length() - 1, id=1, prot=5)
    t.wait("t0", "ar", "==", 1, since=m, label="READ_AT_TARGET")
    t.delay(4)
    t.reset(8)
    t.gate("t0", "r", True)
    t.delay(12)
    t.read("s0", 0x1000, size=(source_bits // 8).bit_length() - 1, id=1, prot=5, targets=1)
    t.drain(limit=5000, label="AFTER_RESET")
    t.end()
