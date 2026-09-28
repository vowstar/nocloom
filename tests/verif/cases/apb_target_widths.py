"""Physical APB subwords through the public CLI: APB4, APB3 and APB2
targets of 8 and 16 bits behind a 32-bit AXI4 source, every one with
fixed_bursts single, t0 with a 1024-cycle timeout that completes its
access.

The case table and its oracle (expected(): the physical write and read
accesses of every case, the APB3/APB2 rejection of each partial physical word,
the merged write response and every read beat's
response) are the scenario's; the offline checker judges bytes, strobes,
read data (a failed physical word zeroes the beat) and the access counts.
Before the cases: a common reset in the middle of a gathered read on the
8- and 16-bit APB4 targets; the t0 timeout in the middle of a gather (SLVERR
with zero data, the late completion still reading the word's remaining
bytes, the failed target answered without access). Back-to-back SETUP
happens exactly in "clocked"."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from endpoint_ports import endpoint_ports  # noqa: E402

from nlverif import config as nlconfig  # noqa: E402


def configuration(clocked):
    clocks = [{'name': 'main', 'clock_port': 'clk_main', 'reset_port': 'reset_n', 'test_port': 'test'}]
    if clocked:
        clocks += [{'name': 'io', 'clock_port': 'clk_io', 'reset_port': 'reset_n', 'test_port': 'test'}]
    targets = [{'name': f't{i}', 'clock_domain': 'io' if clocked else 'main',
                'protocol': protocol, 'data_width': bits, 'fixed_bursts': 'single',
                'mappings': [{'base': hex((i + 1) * 0x1000), 'size': '0x1000'}]}
               for i, (protocol, bits) in enumerate((p, w) for p in ('apb4', 'apb3', 'apb2')
                                                   for w in (8, 16))]
    targets[0]['timeout'] = {'cycles': 1024, 'completion': True}
    if clocked:
        for target in targets:
            target['apb_back_to_back'] = True
    names = [t['name'] for t in targets]
    topology = {
        'request': {'nodes': [{'name': 'q', 'clock_domain': 'main'}], 'links': [],
                    'entries': [{'endpoint': 's0', 'node': 'q'}],
                    'exits': [{'endpoint': n, 'node': 'q'} for n in names]},
        'response': {'nodes': [{'name': 'p', 'clock_domain': 'main'}], 'links': [],
                     'entries': [{'endpoint': n, 'node': 'p'} for n in names],
                     'exits': [{'endpoint': 's0', 'node': 'p'}]}}
    return endpoint_ports({
        'schema_version': 10, 'name': 'apb_words', 'address_width': 32,
        'reset_ports': [{'name': 'reset_n'}], 'test_ports': [{'name': 'test'}], 'clock_domains': clocks, 'arbitration': {'policy': 'round_robin'},
        **({'synchronizer_stages': 3} if clocked else {}),
        'initiators': [{'name': 's0', 'clock_domain': 'main', 'protocol': 'axi4',
                        'data_width': 32}],
        'targets': targets, 'connectivity': 'all', 'topology': topology,
        'transport': {'max_outstanding': 1},
        'output': {'backend': 'verilog', 'language': 'verilog_2001'}}, id_width=3, len_width=8)


def cases():
    out = []
    for target in range(6):
        for name, beats, size, burst, offset, masks, error in [
                ('full', 8, 2, 1, 0, [15], False),
                ('sparse', 16, 2, 1, 0, list(range(16)), False),
                ('groups', 8, 2, 1, 0, [3, 12, 15, 0], False),
                ('late_partial', 4, 2, 1, 0, [7, 15], False),
                ('early_partial', 4, 2, 1, 0, [13, 15], False),
                ('fixed_recover', 4, 2, 0, 0, [7, 15, 13, 15], False),
                ('byte', 8, 0, 1, 1, [15], False),
                ('half', 8, 1, 1, 1, [15], False),
                ('unaligned', 4, 2, 1, 1, [15], False),
                ('wrap', 8, 2, 2, 4, [15], False),
                ('fixed', 4, 2, 0, 0, [15, 5, 0, 15], False),
                ('error', 8, 2, 1, 0, [15], True),
                ('zero', 4, 2, 1, 0, [0], False)]:
            out.append(dict(target=target, name=name, beats=beats, size=size, burst=burst,
                            offset=offset, masks=masks, error=error))
    return out


def address(base, beat, size, burst, beats):
    step = 1 << size
    if beat == 0 or burst == 0:
        return base
    next_ = base // step * step + beat * step
    if burst == 2:
        span = beats * step
        return base // span * span + next_ % span
    return next_


def expected(case):
    """(physical writes, physical reads, write response, read response per
    beat) of one case: the scenario's oracle."""
    target = case['target']
    lanes = 1 if target % 2 == 0 else 2
    dialect = ('apb4', 'apb3', 'apb2')[target // 2]
    writes, reads, responses, bresp = 0, 0, [], 0
    step = 1 << case['size']

    def mask(a):
        end = (a // step + 1) * step
        return sum(1 << j for j in range(4) if a <= a // 4 * 4 + j < end)
    packed = case['burst'] == 1 and case['size'] < 2
    groups = []
    for beat in range(case['beats']):
        wa = address(0x40 + case['offset'], beat, case['size'], case['burst'], case['beats'])
        ra = address(0x180 + case['offset'], beat, case['size'], case['burst'], case['beats'])
        if packed and groups and wa // 4 == groups[-1][1] // 4:
            groups[-1][0].append(beat)
        else:
            groups.append(([beat], wa, ra))
    for beats_of, wa, ra in groups:
        strobes = 0
        for beat in beats_of:
            a = address(0x40 + case['offset'], beat, case['size'], case['burst'], case['beats'])
            strobes |= mask(a) & case['masks'][beat % len(case['masks'])]
        for lane in range(0, 4, lanes):
            strobe = (strobes >> lane) & ((1 << lanes) - 1)
            if not strobe:
                continue
            if dialect != 'apb4' and strobe != (1 << lanes) - 1:
                bresp = 2
                continue
            local = wa // 4 * 4 + lane
            error = dialect != 'apb2' and case['error'] and local % 64 == lanes
            writes += 1
            bresp = max(bresp, 2 if error else 0)
    # Reads share only a physical APB word. A byte or halfword target must
    # not read unused words merely because writes pack into a 4-byte container.
    previous = None
    for beat in range(case['beats']):
        a = address(0x180 + case['offset'], beat, case['size'], case['burst'], case['beats'])
        end = (a // step + 1) * step
        response = 0
        for local in range(a // step * step // lanes * lanes, end, lanes):
            if case['burst'] != 1 or local != previous:
                reads += 1
            previous = local
            error = dialect != 'apb2' and case['error'] and local % 64 == lanes
            response = max(response, 2 if error else 0)
        responses.append(response)
    return writes, reads, bresp, responses


def model(cfg):
    return dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg),
                targets=nlconfig.targets(cfg, error_data_zero=True))


def bfms(clocked):
    return [dict(name="s0", kind="axi_master", endpoint="initiator:s0")] + [
        dict(name=f"t{i}", kind="apb_slave", endpoint=f"target:t{i}", salt=i + 1,
             dialect=(4, 4, 3, 3, 2, 2)[i], clock="io" if clocked else "main",
             **({"sideband": ("dut", "target:t0", "timed_out")} if i == 0 else {}))
        for i in range(6)]


PLAIN, CLOCKED = configuration(False), configuration(True)
CASE = dict(
    name="apb_target_widths",
    contracts=["APB.TARGET.ACCESS", "AXI.ERR.READ_ZERO", "AXI.RESET.CANCEL",
               "AXI.TIMEOUT.EXACT"],
    dut=dict(source="cli", config=PLAIN),
    bfms=bfms(False),
    model=model(PLAIN),
    configs=[dict(label=""),
             dict(label="clocked", dut=dict(source="cli", config=CLOCKED), model=model(CLOCKED),
                  bfms=bfms(True), clocks=dict(main=10.0, io=14.0),
                  scenario_args=dict(clocked=True))],
    max_cycles=400000,
    faults=[],
)


def scenario(t, seed, clocked=False):
    t.ready("s0", "b", "delay", 7)
    t.ready("s0", "r", "delay", 5)
    for i in range(6):
        t.set(f"t{i}", "wait", 1)
    t.reset(8)
    t.delay(12)
    # A common reset in the middle of a gathered read (t0 8 bits, t1 16).
    for target, lanes in ((0, 1), (1, 2)):
        t.mark("gather_reset", target)
        m = t.snap(f"t{target}", "access")
        t.read("s0", (target + 1) * 4096 + 384, size=2, id=5)
        t.wait(f"t{target}", "access", "==", 1, since=m, label="RESET_GATHER_REACHED")
        t.wait(f"t{target}", "psel", "==", 1, label="RESET_GATHER_REACHED")
        t.reset(8)
        t.delay(12)
    # The t0 timeout in the middle of a gather: the word at local 385 waits
    # 1100 cycles, the target times out after 1024 and answers SLVERR with
    # zero data, and the late completion still reads the word's remaining
    # bytes (declared late accesses: four physical reads in all), while the
    # failed target answers a later read without access.
    t.mark("timeout", 0)
    t.set("t0", "slow_mask", 0xfff)
    t.set("t0", "slow_match", 385)
    t.set("t0", "slow_wait", 1100)
    t.read("s0", 4096 + 384, size=2, id=5, resp=2, target="any", late_access=True, targets=4)
    t.drain(limit=3000, label="TIMEOUT_ANSWER")
    t.delay(1200)
    t.read("s0", 4096 + 400, size=2, id=5, resp=2, target="none")
    t.drain(limit=3000, label="FAILED_TARGET_QUIET")
    t.reset(8)
    t.set("t0", "slow_mask", 0)
    t.delay(12)
    for k, case in enumerate(cases()):
        target = case['target']
        name = f"t{target}"
        t.mark("case", k)
        lanes = 1 if target % 2 == 0 else 2
        t.set(name, "errm_mask", 63 if case['error'] else 0)
        t.set(name, "errm_match", lanes)
        writes, reads, bresp, responses = expected(case)
        base = (target + 1) * 4096
        burst = ("fixed", "incr", "wrap")[case['burst']]
        t.strobes("s0", case['masks'])
        t.write("s0", base + 0x40 + case['offset'], beats=case['beats'], size=case['size'],
                burst=burst, id=3, strobe="table", targets=writes, resp=bresp)
        t.read("s0", base + 0x180 + case['offset'], beats=case['beats'], size=case['size'],
               burst=burst, id=5, targets=reads, beat_resps=responses)
        t.drain(limit=20000, label=f"CASE_{k}")
        t.delay(5)
    t.end()


def back_to_back(checker, events, sources, targets):
    """SETUP right after a completion (PSEL kept high between two accesses)
    happens on some target exactly with apb_back_to_back."""
    pairs = 0
    for i in range(6):
        name, last = f"t{i}", None
        for e in events:
            if e.get("p") != name:
                continue
            if e.get("k") == "hs" and e.get("ch") == "apb":
                if last == "hs":
                    pairs += 1
                last = "hs"
            elif e.get("k") in ("lv", "rst") and not e.get("v"):
                last = None
    clocked = checker.case.get("scenario_args", {}).get("clocked", False)
    if (pairs > 0) != clocked:
        return [dict(detail="back-to-back APB accesses", pairs=pairs, clocked=clocked)]
    return []


CASE["oracle"] = back_to_back
