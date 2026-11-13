"""Offline checker: rebuilds source and target transactions from the event
log and judges them against the independent model.

Judged here: routing (target local offsets), byte effects of writes, read
data, response codes (local errors, merged split responses), completion
accounting across the common reset, attribution of every target access to
a source transaction, scenario expectations and ordering checks. An event
with an unknown (X/Z) field is EVENT_X. Reset propagation to every BFM is a
testbench self-check: a miss lands in "infra", never in mismatches. Protocol
timing (holds, quiet windows, timeout gaps, throughput) is judged online by
the SV monitors; their failures arrive as "fail" events.
"""

import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

from . import contracts as C
from . import model
from .events import hexval

# The independent cycle-accurate arbiter policy model of the arbiter library
# tests (tests/treadle_model.py).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from target_id_model import from_port, number, on_port  # noqa: E402
from treadle_model import Arbiter  # noqa: E402


class LevelArbiter(Arbiter):
    """Fixed levels in front of the policy: only the highest valid level is
    eligible for an unheld choice (map contract arbitration.qos)."""

    levels = ()

    def choose(self, valid):
        high = max((self.levels[i] for i, v in enumerate(valid) if v), default=0)
        return super().choose([v and self.levels[i] == high for i, v in enumerate(valid)])


class Txn:
    def __init__(self, port, direction, push, epoch):
        self.port, self.dir, self.epoch = port, direction, epoch
        self.tag = push["tag"]
        self.addr = int(push["a"], 16)
        self.length = push["len"] + 1
        self.size, self.burst, self.id = push["sz"], push["bu"], push["id"]
        self.lock, self.cache, self.prot = push["lk"], push["ca"], push["pr"]
        self.issued_t = None       # address handshake time
        self.done_t = None         # B, or last R beat, at the source
        self.rusers = []
        self.beats = []            # writes: (data, strb); reads: (data, resp, last)
        self.resp = None           # write response
        self.done = False
        self.cancelled = False
        self.targets = []          # attributed target transactions
        self.footprint = set()     # (target, local word) pairs the transaction touches
        self.remaining = set()     # of those, the words no target access visited yet
        self.window = None
        self.local_error = None    # code the network answers without target access
        self.beat_local = {}       # beat -> code of beats a window decode answers itself
        self.skip = set()          # (beat, byte) a write never delivers (APB3/APB2 strobes)
        self.extra_codes = []      # failures the target outlet adds to a write response
        self.modes = ()            # address input values the burst decodes in


class TargetTxn:
    def __init__(self, port, direction, e, epoch):
        self.port, self.dir, self.epoch, self.t = port, direction, epoch, e["t"]
        self.addr = int(e["a"], 16)
        self.length = e.get("len", 0) + 1
        self.size, self.burst, self.id = e["sz"], e.get("bu", 1), e.get("id", 0)
        self.prot, self.cache, self.lock = e.get("pr", 0), e.get("ca", 0), e.get("lk", 0)
        self.beats = []
        self.users = []
        self.cycles = []           # cycle number of each data beat at the target
        self.resps = []
        self.owner = None
        self.end_t = None          # B, or last R beat


class Checker:
    def __init__(self, case, bench, tape_meta):
        self.case, self.bench, self.tape = case, bench, tape_meta
        self.mismatches = []
        self.infra = []
        self.coverage = Counter()
        self.rates = []
        self.reached = []
        self.limit = 200
        # Address input values by index (Tape.mode); a burst decodes in the values of its first
        # request at the source port (AR, or the first W beat).
        self.modes = {}

    def miss(self, kind, **detail):
        if len(self.mismatches) < self.limit:
            self.mismatches.append(dict(kind=kind, **detail))

    # ------------------------------------------------------------------
    def run(self, events):
        bfms = self.bench["bfms"]
        masters = [n for n, b in bfms.items() if b["kind"] in ("axi_master", "apb_master", "ahb_master")]
        # A case may name target BFMs whose buses carry no source traffic
        # (the rule stub drives its APB and AHB buses on its own).
        slaves = [n for n, b in bfms.items()
                  if b["kind"] in ("axi_slave", "apb_slave", "ahb_slave")
                  and n not in self.case.get("unjudged_targets", ())]
        epoch = 0
        src = {m: dict(push=dict(w=deque(), r=deque()), aw=deque(), ar=deque(),
                       wbeats=deque(), bq=defaultdict(deque), rq=defaultdict(deque),
                       all=[]) for m in masters}
        tgt = {s: dict(aw=[], wbursts=[], wcur=[], bq=defaultdict(deque),
                       rq=defaultdict(deque), ahb=deque(), all=[]) for s in slaves}
        streams = defaultdict(list)
        probes = defaultdict(list)
        for e in events:
            k, p = e.get("k"), e.get("p")
            if k == "bad" or e.get("_x"):
                e["_epoch"] = epoch
                self.miss("EVENT_X", port=p, event=e["_n"], fields=e.get("_x", "line"))
                continue
            e["_epoch"] = epoch + (1 if k == "rst" and p is None and e.get("v") == 0 else 0)
            if k == "rst" and p is None and e.get("v") == 0:
                epoch += 1
                for m in masters:
                    for t in src[m]["all"]:
                        if not t.done:
                            t.cancelled = True
                    s = src[m]
                    s["push"]["w"].clear(); s["push"]["r"].clear(); s["aw"].clear()
                    s["ar"].clear(); s["wbeats"].clear(); s["bq"].clear(); s["rq"].clear()
                for sl in slaves:
                    d = tgt[sl]
                    d["wcur"] = []
                    d["aw"] = []
                    d["wbursts"] = []
                    d["bq"].clear(); d["rq"].clear(); d["ahb"].clear()
                continue
            if k in ("push", "hs", "ahb_addr", "ahb_data") and any(
                    f in e and hexval(e, f) is None for f in ("a", "d", "s")):
                self.miss("EVENT_X", port=p, event=e["_n"])
                continue
            if k == "mk" and self.tape["marks"][e["m"]].startswith("mode"):
                self.modes[int(self.tape["marks"][e["m"]][4:])] = e["v"]
            if p in src:
                self._source_event(src[p], p, e, epoch)
            elif p in tgt:
                self._target_event(tgt[p], p, e, epoch)
            elif k == "hs" and e.get("ch") == "x":
                streams[p].append(e)
            elif k == "pr":
                probes[p].append(e)
        self.streams = streams
        self._check_reset_propagation(events)
        sources = [t for m in masters for t in src[m]["all"]]
        targets = [t for s in slaves for t in tgt[s]["all"]]
        self._expect_local(sources)
        self._attribute(sources, targets)
        for t in sources:
            self._judge(t)
        for check in self.tape.get("checks", []):
            getattr(self, "_check_" + check["kind"])(check, events, sources, targets)
        if "oracle" in self.case:
            # A case's own independent oracle (per-case event inventories).
            for detail in self.case["oracle"](self, events, sources, targets):
                self.miss("ORACLE", **detail)
        if self.bench.get("probes") and "probe_checks" in self.case:
            self.probe_records = self._probe_replay(probes)
        return dict(mismatches=self.mismatches, infra=self.infra,
                    issued=len(sources),
                    completed=sum(t.done for t in sources),
                    cancelled=sum(t.cancelled and not t.done for t in sources),
                    target_transactions=len(targets),
                    probes=getattr(self, "probe_records", None),
                    rates=self.rates, reached=self.reached,
                    coverage={"/".join(map(str, k)): v for k, v in sorted(self.coverage.items())})

    def _check_reset_propagation(self, events):
        """Each reset edge the tb drives reaches every protocol BFM at the
        first rising edge of its clock: T < t <= T + period."""
        bfms = {n: b for n, b in self.bench["bfms"].items() if b["kind"] != "stream"}
        seen = defaultdict(list)
        for e in events:
            if e.get("k") == "rst" and e.get("p") in bfms:
                seen[e["p"]].append((e["t"], e.get("v")))
        end = max((e.get("t", 0) for e in events), default=0)
        level = 0
        for e in events:
            if e.get("k") != "rst" or e.get("p") is not None or e.get("v") == level:
                continue
            level, edge = e.get("v"), e["t"]
            for name, b in bfms.items():
                period = b.get("period_ps", 10000)
                if edge + period > end:
                    continue
                if not any(v == level and edge < t <= edge + period for t, v in seen[name]):
                    self.infra.append(dict(kind="RESET_PROPAGATION", port=name, edge=edge,
                                           value=level, seen=[t for t, v in seen[name]
                                                              if v == level and t > edge][:1]))

    # ------------------------------------------------------------------
    def _source_event(self, s, port, e, epoch):
        k, ch = e["k"], e.get("ch")
        if k == "push":
            t = Txn(port, ch, e, epoch)
            if self.bench["bfms"][port].get("lite"):
                # An AXI4-Lite or APB initiator clears the address bits below
                # its bus width before any check; WSTRB or PSTRB as given.
                t.addr &= ~((1 << t.size) - 1)
            s["push"][ch].append(t)
            s["all"].append(t)
        elif k == "rst" and e.get("v") == 0:
            # This source reset alone (bfm "reset"): its open transactions are cancelled; a
            # response the network still returns for one of them is SOURCE_EXTRA or answers a
            # later transaction.
            for t in s["all"]:
                t.cancelled = t.cancelled or not t.done
            for q in (s["push"]["w"], s["push"]["r"], s["aw"], s["ar"], s["wbeats"], s["bq"],
                      s["rq"]):
                q.clear()
        elif k == "hs" and ch == "aw":
            t = self._pop_tag(s["push"]["w"], e, port)
            if t:
                t.issued_t = e["t"]
                s["aw"].append(t)
                s["bq"][e["id"]].append(t)
        elif k == "hs" and ch == "w":
            for t in s["all"]:
                if t.tag == e["tag"] and t.dir == "w" and not t.cancelled:
                    if not t.beats:
                        t.modes = tuple(sorted(self.modes.items()))
                    t.beats.append((hexval(e, "d"), hexval(e, "s")))
                    break
        elif k == "hs" and ch == "ar":
            t = self._pop_tag(s["push"]["r"], e, port)
            if t:
                t.issued_t = e["t"]
                t.modes = tuple(sorted(self.modes.items()))
                s["rq"][e["id"]].append(t)
        elif k == "hs" and ch == "b":
            q = s["bq"].get(e["id"])
            if not q:
                self.miss("SOURCE_EXTRA_B", port=port, id=e["id"], t=e["t"])
                return
            t = q.popleft()
            t.resp = e["r"]
            t.done = True
            t.done_t = e["t"]
        elif k == "hs" and ch == "r":
            q = s["rq"].get(e["id"])
            if not q:
                self.miss("SOURCE_EXTRA_R", port=port, id=e["id"], t=e["t"])
                return
            # A beat belongs to the oldest open burst of its ID: bursts of one
            # ID return in order, those of different IDs may interleave beat
            # by beat (docs/protocols.md). A same-ID beat out of place shows in the
            # burst's count, RLAST or data.
            t = q[0]
            t.beats.append((hexval(e, "d"), e["r"], e["l"]))
            t.rusers.append(hexval(e, "u") if "u" in e else 0)
            if len(t.beats) >= t.length or (e["l"] and not self.bench["bfms"][port]["lite"]):
                q.popleft()
                t.done = True
                t.done_t = e["t"]

    def _pop_tag(self, queue, e, port):
        while queue:
            t = queue.popleft()
            if t.tag == e["tag"]:
                return t
            self.miss("ACCOUNTING", port=port, detail="push order", tag=t.tag)
        self.miss("ACCOUNTING", port=port, detail="handshake without push", tag=e.get("tag"))
        return None

    def _target_event(self, d, port, e, epoch):
        k, ch = e["k"], e.get("ch")
        kind = self.bench["bfms"][port]["kind"]
        if k == "rst" and e.get("v") == 0:
            # This target reset alone (bfm "reset"): it forgets its open bursts, so a beat or
            # response the network still exchanges with it belongs to no earlier access.
            d["aw"], d["wbursts"], d["wcur"] = [], [], []
            d["bq"].clear(); d["rq"].clear(); d["ahb"].clear()
            return
        if kind == "apb_slave":
            self._apb_event(d, port, e, epoch)
            return
        if kind == "ahb_slave":
            self._ahb_event(d, port, e, epoch)
            return
        if k != "hs":
            return
        if ch in ("aw", "ar"):
            self._legal(port, e)
        if ch == "aw":
            t = TargetTxn(port, "w", e, epoch)
            d["aw"].append(t)
            d["all"].append(t)
            d["bq"][e["id"]].append(t)
            self._pair_w(d)
        elif ch == "w":
            d["wcur"].append((hexval(e, "d"), hexval(e, "s"), e.get("c")))
            if e["l"]:
                d["wbursts"].append(d["wcur"])
                d["wcur"] = []
                self._pair_w(d)
        elif ch == "b":
            q = d["bq"].get(e["id"])
            if q:
                done = q.popleft()
                done.resps.append(e["r"])
                done.end_t = e["t"]
            else:
                self.miss("TARGET_EXTRA_B", port=port, id=e["id"], t=e["t"])
        elif ch == "ar":
            t = TargetTxn(port, "r", e, epoch)
            d["all"].append(t)
            d["rq"][e["id"]].append(t)
        elif ch == "r":
            q = d["rq"].get(e["id"])
            if not q:
                self.miss("TARGET_EXTRA_R", port=port, id=e["id"], t=e["t"])
                return
            t = q[0]
            t.beats.append((hexval(e, "d"), None))
            t.users.append(hexval(e, "u") if "u" in e else 0)
            t.cycles.append(e.get("c"))
            t.resps.append(e["r"])
            if len(t.beats) >= t.length:
                t.end_t = e["t"]
                q.popleft()

    def _bus_size(self, port):
        return (self.bench["bfms"][port]["data_bits"] // 8).bit_length() - 1

    def _apb_event(self, d, port, e, epoch):
        """One completed APB access: a one-word target transaction (PSTRB
        lanes, PPROT, PSLVERR as SLVERR)."""
        if e["k"] != "hs" or e.get("ch") != "apb":
            return
        size = self._bus_size(port)
        nbus = 1 << size
        addr = int(e["a"], 16)
        t = TargetTxn(port, "w" if e["w"] else "r",
                      dict(t=e["t"], a="%x" % (addr - addr % nbus), sz=size, pr=e.get("pr", 0)),
                      epoch)
        code = C.SLVERR if e.get("r") else C.OKAY
        t.beats = [(hexval(e, "d"), hexval(e, "s") if e["w"] else None)]
        t.cycles = [e.get("c")]
        t.resps = [code]
        t.end_t = e["t"]
        d["all"].append(t)

    def _ahb_event(self, d, port, e, epoch):
        """AHB address phases queue in order; each data phase completes the
        oldest one as a one-beat target transaction (lanes of HSIZE, HPROT,
        each nonzero HRESP as SLVERR). HBURST and HTRANS stay with it for
        the case oracles."""
        if e["k"] == "ahb_addr":
            d["ahb"].append(e)
            return
        if e["k"] != "ahb_data":
            return
        if not d["ahb"]:
            self.miss("TARGET_EXTRA_R" if not e["w"] else "TARGET_EXTRA_B", port=port, t=e["t"])
            return
        a = d["ahb"].popleft()
        nbus = 1 << self._bus_size(port)
        addr, size = int(a["a"], 16), a["sz"]
        t = TargetTxn(port, "w" if a["w"] else "r",
                      dict(t=a["t"], a=a["a"], sz=size, pr=a.get("pr", 0), lk=a.get("lk", 0)),
                      epoch)
        lo, hi = model.lanes(addr, size, nbus)
        strb = sum(1 << j for j in range(lo, hi + 1)) if a["w"] else None
        t.beats = [(hexval(e, "d"), strb)]
        t.cycles = [e.get("c")]
        t.resps = [C.SLVERR if e.get("r") else C.OKAY]
        t.end_t = e["t"]
        t.hburst, t.htrans, t.hresp, t.addr_cycle = a.get("bu"), a.get("tr"), e.get("r"), a.get("c")
        d["all"].append(t)

    @staticmethod
    def _pair_w(d):
        waiting = [t for t in d["aw"] if not t.beats]
        while waiting and d["wbursts"]:
            t, burst = waiting.pop(0), d["wbursts"].pop(0)
            t.beats = [(data, strb) for data, strb, _ in burst]
            t.cycles = [c for _, _, c in burst]

    def _legal(self, port, e):  # noqa: C901
        """AXI A3.4.1 at a target port: no reserved burst type, a beat no
        wider than the bus, WRAP of 2, 4, 8 or 16 aligned beats, FIXED of at
        most 16, INCR within 4 KiB."""
        nbus = self.bench["bfms"][port]["data_bits"] // 8
        addr, n, size, burst = int(e["a"], 16), e["len"] + 1, e["sz"], e["bu"]
        bad = (burst == 3 or (1 << size) > nbus
               or burst == 2 and (n not in (2, 4, 8, 16) or addr % (1 << size))
               or burst == 0 and n > 16 or model.crosses_4k(addr, n, size, burst))
        if bad:
            self.miss("TARGET_ILLEGAL", port=port, ch=e["ch"], a=e["a"], len=n, sz=size, bu=burst)

    # ------------------------------------------------------------------
    def _mappings(self, t):
        """(target, base, size, local) of every mapping a source reaches, the
        smaller first: where mappings overlap the smaller decodes. A model with
        mode_routes takes the routes of the burst's modes (Txn.modes)."""
        model = self.case["model"]
        routes = model["mode_routes"][t.modes] if "mode_routes" in model else model["routes"]
        return sorted(((e[0], e[1], e[2], e[3] if len(e) > 3 else 0)
                       for e in routes[t.port]), key=lambda m: m[2])

    def _route(self, t):
        """The mapping of the start address; in a page of sub-page mappings
        the one routing it (docs/address-map.md#target-mappings): the last mapping at or
        below the address, else the first of the page."""
        maps = self._mappings(t)
        for m in maps:
            if m[1] <= t.addr < m[1] + m[2]:
                return m
        page = sorted((m for m in maps if m[2] < 4096 and m[1] // 4096 == t.addr // 4096),
                      key=lambda m: m[1])
        below = [m for m in page if m[1] <= t.addr]
        return below[-1] if below else page[0] if page else None

    def _local(self, t, b):
        """(target, local address) of global byte b in a mapping t's source
        reaches, or None outside them: a burst of a page shared by targets
        reaches each of them in its own mappings (docs/address-map.md#target-mappings)."""
        for target, base, size, local in self._mappings(t):
            if base <= b < base + size:
                return target, b - base + local
        return None

    def _word(self, at):
        """The target bus word holding a (target, local) byte."""
        tbytes = self._target_kind(at[0])["bus_bytes"]
        return at[0], at[1] // tbytes * tbytes

    def _absorbs(self, t, at):
        """A write byte a read-only target absorbs, or a read byte a
        write-only one answers, without access."""
        kind = self._target_kind(at[0])
        return kind.get("read_only" if t.dir == "w" else "write_only", False)

    def _subpage(self, t):
        return any(target == t.window[0] and size < 4096
                   for target, _, size, _ in self._mappings(t))

    def _error_codes(self):
        """transport.response_codes: absent selects distinct response classes."""
        return self.case["model"].get("error_codes", True)

    def _target_kind(self, name):
        return self.case["model"]["targets"][name]

    def _beat_locals(self, t, kind):
        """Beats a dedicated target's window decode answers itself: a beat
        with a byte outside the exact window (sub-page remainder) completes
        with DECERR (SLVERR under uniform); its words inside the window keep
        their accesses."""
        sbytes = self.bench["bfms"][t.port]["data_bits"] // 8
        out = {}
        for i in range(t.length):
            a = model.beat_addr(t.addr, t.length, t.size, t.burst, i)
            if any(self._local(t, b) is None for b in model.beat_bytes(a, t.size, sbytes)):
                out[i] = C.DECERR if self._error_codes() else C.SLVERR
        return out

    def _dedicated_local(self, t, kind):
        """Whole-transaction answers of the dedicated outlets (APB, AHB,
        AXI4-Lite): a write to a read-only target, a FIXED burst refused by
        fixed_bursts reject or wider than the engine word, a start in the
        sub-page remainder of a FIXED burst."""
        decerr = C.DECERR if self._error_codes() else C.SLVERR
        if kind.get("read_only" if t.dir == "w" else "write_only"):
            # Answered without access after the window decode (a write to a
            # read-only target, a read of a write-only one): a word in the
            # page remainder makes it DECERR, the window SLVERR.
            return decerr if self._beat_locals(t, kind) else C.SLVERR
        if t.burst == 0:
            # The window decode comes first: a start in the page remainder
            # answers DECERR on every beat without access.
            if self._local(t, t.addr) is None:
                return decerr
            engine = kind.get("engine_bytes", kind["bus_bytes"])
            if kind.get("fixed", "reject") != "single" or (1 << t.size) > engine:
                return C.SLVERR
        return None

    def _expect_local(self, sources):
        """Local answers: unmapped DECERR, 4 KiB crossing, refused FIXED."""
        for t in sources:
            exp = self.tape["expect"].get(str(t.tag), self.tape["expect"].get(t.tag, {}))
            t.expect = exp
            # Explicit modes describe admission after a controlled capacity wait.
            if "modes" in exp:
                t.modes = tuple(enumerate(exp["modes"]))
            t.window = self._route(t)
            if t.window is None:
                t.local_error = C.DECERR if self._error_codes() else C.SLVERR
                continue
            kind = self._target_kind(t.window[0])
            if model.crosses_4k(t.addr, t.length, t.size, t.burst):
                t.local_error = C.SLVERR
            elif (kind["kind"] in ("downsize", "axi") and t.burst == 0
                  and (1 << t.size) > kind["bus_bytes"] and kind.get("fixed_reject", True)):
                # A FIXED beat wider than the target is never cut (docs/protocols.md
                # width conversion): SLVERR without target access.
                t.local_error = C.SLVERR
            elif kind["kind"] in ("apb", "ahb", "lite") and kind.get("dedicated", True):
                t.local_error = self._dedicated_local(t, kind)
            elif kind["kind"] == "axi" and kind.get("read_only" if t.dir == "w" else "write_only"):
                t.local_error = C.SLVERR
            if exp.get("target") == "none":
                t.local_error = t.local_error if t.local_error is not None else -1
            t.beat_local = {}
            if t.local_error is None and self._subpage(t):
                t.beat_local = self._beat_locals(t, kind)
            if t.local_error is None:
                t.skip, t.extra_codes = self._apb_plan(t, kind)
                t.footprint = self._footprint(t, kind)
                t.remaining = set(t.footprint)

    def _apb_plan(self, t, kind):
        """APB3 and APB2 have no PSTRB (docs/protocols.md, Dedicated APB3, APB2 targets
        and Dedicated APB4 burst targets): each physical word of a write is
        checked in address order; no strobes skip it, all of its bytes
        strobed write it, a partial mask makes no access and adds SLVERR.
        Later physical words are checked independently. Returns the
        (beat, byte) pairs never delivered and the codes
        added to the write response."""
        if t.dir != "w" or kind.get("kind") != "apb" or kind.get("dialect", 4) == 4:
            return set(), []
        sbytes = self.bench["bfms"][t.port]["data_bits"] // 8
        wb = kind["bus_bytes"]
        # Packets of an INCR burst (docs/packets.md#bounded-packets): from the
        # alignment word (source bytes, at least 4) to 128 bytes, cut at the
        # window's own boundary; a split narrow Non-modifiable burst sends
        # each beat as its own packet.
        ends = []
        if t.burst == 1:
            align, cross = max(sbytes, 4), min(t.window[2], 4096)
            end = (t.addr >> t.size << t.size) + (t.length << t.size)
            at = t.addr
            while at < end:
                at = min(at // align * align + 128, at // cross * cross + cross, end)
                ends.append(at)
        split = (t.port in self.case["model"].get("split_narrow", ()) and t.burst == 1
                 and (1 << t.size) < sbytes and not t.cache & 2)
        # Internal beats: a narrow INCR packs the beats of one 32-bit word
        # into one internal beat (strobes merged); otherwise one per beat.
        groups = []
        for i, (_, strb) in enumerate(t.beats):
            a = model.beat_addr(t.addr, t.length, t.size, t.burst, i)
            on = {b for b in model.beat_bytes(a, t.size, sbytes) if strb >> (b % sbytes) & 1}
            every = set(model.beat_bytes(a, t.size, sbytes))
            packet = (i if t.burst == 0 or split
                      else sum(1 for e in ends if e <= a) if t.burst == 1 else 0)
            # A narrow WRAP whose container fits one source word goes out as
            # one beat at the container base (its words in address order).
            folded = (t.burst == 2 and (1 << t.size) < sbytes
                      and t.length << t.size <= sbytes)
            if folded and groups:
                groups[-1][0].append(i)
                groups[-1][1].update(on)
                groups[-1][4].update(every)
            elif (kind.get("pack_narrow", True) and t.burst == 1 and t.size < 2 and groups
                    and groups[-1][3] == a // 4 and groups[-1][2] == packet):
                groups[-1][0].append(i)
                groups[-1][1].update(on)
                groups[-1][4].update(every)
            else:
                groups.append(([i], set(on), packet, a // 4, set(every)))
        skip, codes = set(), []
        for beats, on, packet, _, every in groups:
            words = sorted({b // wb for b in every})
            for w in words:
                mine = [b for b in on if b // wb == w]
                if not mine or len(mine) == wb:
                    continue
                codes.append(C.SLVERR)
                skip.update((i, b) for i in beats for b in every if b // wb == w)
        return skip, codes

    def _footprint(self, t, kind=None):
        """(target, local word) pairs, of each target's bus, a source
        transaction touches."""
        sbytes = self.bench["bfms"][t.port]["data_bits"] // 8
        words = set()
        for i in range(min(t.length, t.expect.get("abort_after", t.length))):
            a = model.beat_addr(t.addr, t.length, t.size, t.burst, i)
            for b in model.beat_bytes(a, t.size, sbytes):
                at = self._local(t, b)
                if at is None or self._absorbs(t, at):
                    continue
                kind = self._target_kind(at[0])
                if t.dir == "r" and kind.get("kind") == "apb":
                    # APB reads pack only to a physical bus word. A single-transfer
                    # source gathers its own AxSIZE container without burst packing.
                    word_bytes = min(sbytes, kind["bus_bytes"])
                    packed = (t.burst == 1 and (1 << t.size) < word_bytes
                              and not self.bench["bfms"][t.port].get("lite"))
                    container = word_bytes if packed else 1 << t.size
                    low = b - b % container
                    words.update(self._word(x) for x in map(lambda y: self._local(t, y),
                                                            range(low, low + container))
                                 if x is not None and x[0] == at[0])
                    continue
                # APB and AHB make no access for a write byte without its strobe.
                strobed = t.dir == "w" and kind.get("kind") in ("apb", "ahb")
                strb = t.beats[i][1] if strobed and i < len(t.beats) else None
                if (i, b) not in t.skip and (not strobed or strb is not None
                                             and strb >> (b % sbytes) & 1):
                    words.add(self._word(at))
        return words

    def _id_match(self, s, tt, exact=False):
        """The target AxID carries the flow number and the sum of the ID-wide slices of the route
        word, rearranged (numbers from the map; a fixture DUT has no map, so no ID check).
        An exclusive access of a target monitor carries its owner flags."""
        ids = self.bench.get("target_ids")
        if ids:
            return self._route_id_match(ids, s, tt)
        # A lone outlet takes the source AxID as it is.
        return not exact or "outlet" not in self.case["dut"].get("recipe", "") or tt.id == on_port(0, s.id, self.bench["bfms"][tt.port].get("id_bits", 0), 0)

    def _route_id_match(self, ids, s, tt):
        config = self.case["dut"].get("config") or {}
        target = next((t for t in config.get("targets", []) if t["name"] == tt.port), None)
        source = next((i for i in config.get("initiators", []) if i["name"] == s.port), None)
        width = self.bench["bfms"][tt.port].get("id_bits", 0)
        if target is None or source is None or s.port not in ids["source_paths"]:
            return True
        if tt.lock:
            return self._owner_id_match(target, source, tt)
        flows = target.get("flows", 1)
        flow_bits = (flows - 1).bit_length()
        if width <= flow_bits:
            return tt.id < flows
        bits = ids["route_bits"]
        positions = {f["name"]: f["lsb"] for f in ids["route_fields"]}
        at_target = positions.get("target_path", 0)
        at_initiator = positions.get("source_path", 0)
        classes = source.get("route_classes", [])
        source_path = classes.index(source["default_class"]) if classes else 0
        for interval in source.get("id_ranges", []):
            if interval["first"] <= s.id <= interval["last"]:
                source_path = classes.index(interval["class"])
                break
        write = 1 if s.dir == "w" and bits["direction"] and source.get("protocol") not in ("apb4", "apb3") else 0
        width_seq = width - flow_bits
        mask = (1 << width_seq) - 1
        for g in range(flows):
            route = (number(ids["source_paths"], s.port, source_path) << at_initiator
                     | number(ids["target_paths"], tt.port, g) << at_target
                     | s.id << bits["direction"] | write)
            total = 0
            while route:
                total += route & mask
                route >>= width_seq
            if tt.id == on_port(g, total & mask, width, flow_bits):
                return True
        return False

    def _owner_id_match(self, target, source, tt):
        """An exclusive access takes the value of the owner flags, the name sorting first low.
        A flag from AxUSER has no value here."""
        value = 0
        for bit, flag in enumerate(sorted(target.get("exclusive_id_flags", []))):
            driver = source.get("user_flags", {}).get(flag)
            if driver not in (0, 1):
                return True
            value |= driver << bit
        flows_bits = (target.get("flows", 1) - 1).bit_length()
        width = self.bench["bfms"][tt.port].get("id_bits", 0)
        return from_port(tt.id, width, flows_bits)[1] == value

    def _attribute(self, sources, targets):
        """Owns every target transaction to one source transaction: same
        target, direction and reset epoch, address handshake before it,
        target ID matching the map's route encoding, every word it visits
        inside the source footprint; among several, the earliest issued one
        that still has unvisited words there, else the earliest still open
        (a FIXED or repeated beat visits a word again)."""
        for tt in sorted(targets, key=lambda x: x.t):
            kind = self._target_kind(tt.port)
            tbytes = kind["bus_bytes"]
            words = {(tt.port, model.beat_addr(tt.addr, tt.length, tt.size, tt.burst, i)
                      // tbytes * tbytes) for i in range(tt.length)}
            first = (tt.port,
                     model.beat_addr(tt.addr, tt.length, tt.size, tt.burst, 0) // tbytes * tbytes)
            near = [s for s in sources
                    if s.dir == tt.dir and s.window
                    and s.epoch == tt.epoch and s.issued_t is not None and s.issued_t <= tt.t
                    and s.local_error is None and self._id_match(s, tt)
                    and first in s.footprint]
            whole = [s for s in near if words <= s.footprint]
            fresh = [s for s in whole if s.remaining & words]
            open_ = [s for s in whole if s.done_t is None or s.done_t >= tt.t]
            # A closed owner is reported (SOURCE_EARLY); a target=any one
            # owns nothing after its answer unless it declares late_access
            # (a target timeout whose completion keeps visiting the word).
            pool = fresh or open_ or [s for s in (whole or near)
                                      if s.expect.get("target") != "any"]
            if not pool:
                # A source transaction whose target access is left open
                # (target=any) owns an access only inside its own footprint,
                # after its address handshake and before its completion.
                allowed = [s for s in sources if s.dir == tt.dir and s.epoch == tt.epoch
                           and s.expect.get("target") == "any" and s.window
                           and s.issued_t is not None
                           and s.issued_t <= tt.t and (s.done_t is None or tt.t <= s.done_t
                                                       or s.expect.get("late_access"))
                           and first in self._footprint(s, kind)]
                if allowed:
                    owner = min(allowed, key=lambda s: s.issued_t)
                    owner.targets.append(tt)
                    tt.owner = owner
                    continue
                # A transaction the network must answer itself, seen at its
                # target: owned by it so that the judge reports TARGET_ACCESS.
                local = [s for s in sources
                         if s.dir == tt.dir and s.window
                         and s.epoch == tt.epoch and s.issued_t is not None
                         and s.issued_t <= tt.t and s.local_error is not None
                         and first in self._footprint(s, kind)]
                if local:
                    owner = min(local, key=lambda s: s.issued_t)
                    owner.targets.append(tt)
                    tt.owner = owner
                    continue
                self.miss("TARGET_UNATTRIBUTED", port=tt.port, dir=tt.dir, addr=hex(tt.addr),
                          id=tt.id, t=tt.t)
                continue
            # Sources reading one word with one AxID stay apart by the access
            # they asked for; arrival order alone would swap them.
            same = [s for s in pool if (s.addr, s.length, s.size) == (tt.addr, tt.length, tt.size)]
            owner = min(same or pool, key=lambda s: s.issued_t)
            tt.owner = owner
            owner.targets.append(tt)
            outside = words - owner.footprint
            if outside and owner.expect.get("target") != "any":
                self.miss("TARGET_FOOTPRINT", port=tt.port, tag=owner.tag, dir=tt.dir,
                          word=hex(min(outside)[1]), detail="outside source footprint")
            owner.remaining -= words

    # ------------------------------------------------------------------
    def _judge(self, t):
        exp = t.expect
        self.coverage[(t.port, t.window[0] if t.window else "unmapped", t.dir, t.burst,
                       t.size, "local" if t.local_error is not None else "target")] += 1
        if t.cancelled and not t.done:
            return
        if not t.done:
            self.miss("SOURCE_UNANSWERED", port=t.port, tag=t.tag, dir=t.dir)
            return
        if t.local_error is not None and t.targets and exp.get("target") != "any":
            self.miss("TARGET_ACCESS", port=t.port, tag=t.tag, detail="local answer reached target")
        # Causality: the network answers a source only after the target
        # accesses it made for it (no posted write, no early read). A
        # target=any transaction may be answered first (target timeout).
        for tt in t.targets if exp.get("target") != "any" else ():
            if tt.t > t.done_t or (tt.end_t is not None and tt.end_t > t.done_t):
                self.miss("SOURCE_EARLY", port=t.port, tag=t.tag, dir=t.dir, target=tt.port,
                          t=t.done_t)
                break
        if t.local_error is None and t.remaining and exp.get("target") != "any":
            self.miss("TARGET_FOOTPRINT", port=t.port, tag=t.tag, dir=t.dir,
                      detail="words not reached",
                      words=[f"{p}:{hex(w)}" for p, w in sorted(t.remaining)[:8]])
        kind = self._target_kind(t.window[0]) if t.window else {}
        if t.dir == "w":
            self._judge_write(t, kind, exp)
        else:
            self._judge_read(t, kind, exp)
        for tt in t.targets:
            # Each target's own attribute map (docs/user-signals.md#target-attributes).
            tkind = self._target_kind(tt.port)
            mapped = (model.target_attributes(tkind["attributes"], tkind["kind"], t.dir == "w",
                                              t.cache, t.prot) if "attributes" in tkind else None)
            if mapped is not None:
                cache, prot = mapped
                if prot is not None and tt.prot != prot:
                    self.miss("TARGET_PROT", port=tt.port, tag=t.tag, got=tt.prot, want=prot)
                if cache is not None and tt.cache != cache:
                    self.miss("TARGET_PROT", port=tt.port, tag=t.tag, detail="cache",
                              got=tt.cache, want=cache)
            elif ("attributes" not in tkind and tkind.get("prot", True)
                  and tkind.get("kind") not in ("apb", "ahb") and tt.prot != t.prot):
                self.miss("TARGET_PROT", port=tt.port, tag=t.tag, got=tt.prot, want=t.prot)
            want = (model.amba_cache(t.cache, t.dir == "w") if tkind.get("cache") == "amba"
                    else t.cache)
            if tkind.get("cache") and tt.cache != want:
                self.miss("TARGET_PROT", port=tt.port, tag=t.tag, detail="cache", got=tt.cache,
                          want=want)
            lock = exp.get("target_lock", t.lock)
            if (tkind.get("lock") or "target_lock" in exp) and tt.lock != lock:
                self.miss("TARGET_PROT", port=tt.port, tag=t.tag, detail="lock", got=tt.lock)
            if tkind.get("ids") and not self._id_match(t, tt, exact=True):
                self.miss("TARGET_PROT", port=tt.port, tag=t.tag, detail="id", got=tt.id)
        # Raw AxADDR preserved: the first target access of a burst starts at
        # the source's own (unaligned) address, local to the window.
        if kind.get("raw_address") and t.targets and t.local_error is None:
            first = min(t.targets, key=lambda x: x.t)
            if first.addr != self._local(t, t.addr)[1]:
                self.miss("TARGET_DESCRIPTOR", port=first.port, tag=t.tag, detail="raw address",
                          got=hex(first.addr), want=hex(self._local(t, t.addr)[1]))
        if "pieces" in exp and t.local_error is None:
            # The exact target bursts of an independent cut and merge model:
            # (local AxADDR, AxLEN, AxSIZE, AxBURST[, AxCACHE, AxPROT]) in order.
            want = [list(p) for p in exp["pieces"]]
            fields = len(want[0]) if want else 4
            got = [[tt.addr, tt.length - 1, tt.size, tt.burst, tt.cache, tt.prot][:fields]
                   for tt in t.targets]
            if got != want:
                self.miss("TARGET_DESCRIPTOR", port=t.port, tag=t.tag, detail="pieces",
                          got=got[:4], want=want[:4])
        if exp.get("one_target") and len(t.targets) != 1:
            self.miss("TARGET_DESCRIPTOR", port=t.port, tag=t.tag,
                      detail=f"{len(t.targets)} target transactions, want one")
        if "descriptors" in exp:
            got = [(tt.addr, tt.length - 1, tt.size, tt.burst) for tt in t.targets]
            want = [tuple(d) for d in exp["descriptors"]]
            if got != want:
                self.miss("TARGET_DESCRIPTOR", port=t.port, tag=t.tag, got=got[:4], want=want[:4])
        if "targets" in exp and len(t.targets) != exp["targets"]:
            self.miss("TARGET_DESCRIPTOR", port=t.port, tag=t.tag,
                      detail=f"{len(t.targets)} target transactions, want {exp['targets']}")
        # Bounded packets split narrow (non-modifiable) bursts into one beat each.
        if kind.get("narrow_single"):
            for tt in t.targets:
                if tt.size < 2 and (tt.length != 1 or tt.burst != 1):
                    self.miss("TARGET_DESCRIPTOR", port=tt.port, tag=t.tag,
                              detail="narrow packet not single beat INCR")
        # Pieces of one split burst are serial: each starts after the last ends.
        if kind.get("kind") == "downsize":
            for before, after in zip(t.targets, t.targets[1:]):
                if before.end_t is None or after.t <= before.end_t:
                    self.miss("TARGET_ORDER", port=after.port, tag=t.tag, t=after.t)
        if kind.get("kind") == "downsize" and t.local_error is None and exp.get("target") != "any":
            pieces, _ = model.downsize_pieces(t.length, t.size, t.burst, t.addr)
            got = [(tt.addr, tt.length - 1, tt.size, tt.burst) for tt in t.targets]
            want = [(self._local(t, a)[1], n, s, b) for a, n, s, b in pieces]
            if got != want:
                self.miss("TARGET_DESCRIPTOR", port=t.port, tag=t.tag, got=got[:4], want=want[:4])

    def _expected_code(self, t, codes):
        exp = t.expect
        if "resp" in exp:
            return {exp["resp"]}
        if "resp_in" in exp:
            return set(exp["resp_in"])
        code = (t.local_error if t.local_error is not None and t.local_error >= 0
                else model.merge(codes))
        # APB PSLVERR and AHB-Lite HRESP cannot distinguish decode errors.
        simple = self.bench["bfms"][t.port]["kind"] in ("apb_master", "ahb_master")
        return {C.SLVERR if code == C.DECERR and (simple or not self._error_codes()) else code}

    def _judge_write(self, t, kind, exp):
        sbytes = self.bench["bfms"][t.port]["data_bits"] // 8
        codes = ([C.SLVERR if r == C.EXOKAY and not tt.lock else r
                  for tt in t.targets for r in tt.resps] + list(t.beat_local.values())
                 + t.extra_codes)
        want = self._expected_code(t, codes)
        if t.resp not in want:
            self.miss("SOURCE_B_RESP", port=t.port, tag=t.tag, got=t.resp, want=sorted(want))
        if t.local_error is not None or exp.get("target") == "any":
            return
        # Every strobed source byte reaches the target exactly once with its
        # value: the (address, value) multisets of source and target match.
        expected = Counter()
        dropped = set(exp.get("drop_beats", ()))
        for i, (data, strb) in enumerate(t.beats):
            if i in dropped or i >= exp.get("abort_after", t.length):
                continue  # a beat the network marked failed reaches no byte
            a = model.beat_addr(t.addr, t.length, t.size, t.burst, i)
            for b in model.beat_bytes(a, t.size, sbytes):
                lane = b % sbytes
                at = self._local(t, b)
                if (strb >> lane) & 1 and at is not None and (i, b) not in t.skip \
                        and not self._absorbs(t, at):
                    expected[(at, (data >> (8 * lane)) & 0xFF)] += 1
        if exp.get("bytes") == "none":
            expected = Counter()  # a failed exclusive write reaches the target without strobes
        if len(t.beats) != t.length:
            self.miss("SOURCE_R_COUNT", port=t.port, tag=t.tag, detail="write beats")
        actual = Counter()
        for tt in t.targets:
            tkind = self._target_kind(tt.port)
            tbytes = tkind["bus_bytes"]
            for i, (data, strb) in enumerate(tt.beats):
                a = model.beat_addr(tt.addr, tt.length, tt.size, tt.burst, i)
                word = a // tbytes * tbytes
                for lane in range(tbytes):
                    if (strb >> lane) & 1:
                        actual[((tt.port, word + lane), (data >> (8 * lane)) & 0xFF)] += 1
                    elif ((data >> (8 * lane)) & 0xFF and tkind.get("unstrobed_zero", True)
                          and tkind.get("kind") != "ahb"):
                        # (AHB lanes outside HSIZE belong to no transfer.)
                        # Map contract: unstrobed write bytes are zero at every target.
                        self.miss("TARGET_UNSTROBED_DATA", port=tt.port, tag=t.tag, beat=i,
                                  lane=lane)
        if actual != expected:
            diff = sorted(((actual - expected) + (expected - actual)).items())[:6]
            self.miss("TARGET_WRITE_BYTES", port=t.port, tag=t.tag,
                      diff=[(f"{a[0]}:{hex(a[1])}", v, n) for (a, v), n in diff])

    def _judge_read(self, t, kind, exp):
        sbytes = self.bench["bfms"][t.port]["data_bits"] // 8
        if len(t.beats) != t.length:
            self.miss("SOURCE_R_COUNT", port=t.port, tag=t.tag, got=len(t.beats), want=t.length)
            return
        lite = self.bench["bfms"][t.port]["lite"]
        for i, (data, resp, last) in enumerate(t.beats):
            if not lite and bool(last) != (i == t.length - 1):
                self.miss("SOURCE_R_LAST", port=t.port, tag=t.tag, beat=i)
        # Target words in visit order: (target, word) -> deque of (byte values, resp).
        supply = defaultdict(deque)
        for tt in t.targets:
            tbytes = self._target_kind(tt.port)["bus_bytes"]
            for i, (data, _) in enumerate(tt.beats):
                a = model.beat_addr(tt.addr, tt.length, tt.size, tt.burst, i)
                supply[(tt.port, a // tbytes * tbytes)].append(
                    (data, C.SLVERR if i < len(tt.resps) and tt.resps[i] == C.EXOKAY and not tt.lock
                     else tt.resps[i] if i < len(tt.resps) else None,
                     tt.users[0] if tt.users else 0))
        last = {}
        for i, (data, resp, _) in enumerate(t.beats):
            a = model.beat_addr(t.addr, t.length, t.size, t.burst, i)
            bytes_ = model.beat_bytes(a, t.size, sbytes)
            codes, values, users = [], {}, []
            if t.local_error is None:
                local = {b: self._local(t, b) for b in bytes_}
                for w in sorted({self._word(x) for x in local.values() if x is not None}):
                    # A merged narrow burst reads a target word once for
                    # several source beats: the last beat of a word serves on.
                    if supply[w]:
                        last[w] = supply[w].popleft()
                    elif w not in last:
                        continue
                    wdata, wresp, user = last[w]
                    users.append(user)
                    codes.append(wresp)
                    for b in bytes_:
                        if local[b] is not None and self._word(local[b]) == w:
                            tbytes = self._target_kind(w[0])["bus_bytes"]
                            values[b] = (wdata >> (8 * (local[b][1] % tbytes))) & 0xFF
            if i in t.beat_local:
                codes = codes + [t.beat_local[i]]
            # Only physical target words supplying bytes of this source beat contribute.
            # Explicit beat expectations have one exact response, never a run-wide alternative.
            want = ({exp["beat_resps"][i]} if "beat_resps" in exp
                    else self._expected_code(t, codes))
            if str(i) in exp.get("beat_resp", {}):
                # A beat the scenario expects with its own code.
                want = {exp["beat_resp"][str(i)]}
            user_bits = self.bench["bfms"][t.port].get("response_user_bits", 0)
            if user_bits:
                network_bits = self.case["model"].get("response_user_bits", user_bits)
                user_mask = (1 << min(user_bits, network_bits)) - 1
                want_user = (users[-1] if users and t.local_error is None else 0) & user_mask
                if t.rusers[i] != want_user:
                    self.miss("SOURCE_R_USER", port=t.port, tag=t.tag, beat=i,
                              got=t.rusers[i], want=want_user)
            if resp not in want:
                self.miss("SOURCE_R_RESP", port=t.port, tag=t.tag, beat=i, got=resp,
                          want=sorted(want))
                continue
            failed = resp in (C.SLVERR, C.DECERR)
            if failed and (t.local_error is not None or kind.get("error_data_zero", True)):
                if data != 0:
                    self.miss("SOURCE_R_DATA", port=t.port, tag=t.tag, beat=i,
                              detail="failed beat data not zero")
            elif not failed and t.local_error is None:
                for b, v in values.items():
                    if (data >> (8 * (b % sbytes))) & 0xFF != v:
                        self.miss("SOURCE_R_DATA", port=t.port, tag=t.tag, beat=i, byte=hex(b))
                        break

    # ------------------------------------------------------------------
    def _check_interpose(self, check, events, sources, targets):
        """An inner target access lands strictly between the first and last
        of an outer group (a higher level packet between packets of one
        lower level burst)."""
        port, lo, hi, inner = check["port"], check["outer"][0], check["outer"][1], check["inner"]
        seq = [e for e in events if e.get("p") == port and e.get("k") == "hs"
               and e.get("ch") == check.get("ch", "aw")]
        if "after_mark" in check:
            mark = self.tape["marks"].index(check["after_mark"])
            start = next((e["t"] for e in events if e.get("k") == "mk" and e.get("m") == mark
                          and e.get("v") == check.get("mark_value", e.get("v"))), None)
            seq = [e for e in seq if start is not None and e["t"] >= start]
        outer = [i for i, e in enumerate(seq) if lo <= int(e["a"], 16) < hi]
        mid = [i for i, e in enumerate(seq) if int(e["a"], 16) == inner]
        if not outer or not mid or not outer[0] < mid[0] < outer[-1]:
            self.miss("ORDER_INTERPOSE", port=port, outer=len(outer), inner=mid[:1],
                      first=outer[:1], last=outer[-1:])

    def _check_first(self, check, events, sources, targets):
        """After the mark, the first access on a target channel has the given
        address (a level decision between simultaneous candidates)."""
        mark = self.tape["marks"].index(check["after_mark"])
        start = next((e["t"] for e in events if e.get("k") == "mk" and e.get("m") == mark
                      and e.get("v") == check.get("mark_value", e.get("v"))), None)
        seq = [e for e in events if start is not None and e["t"] >= start
               and e.get("p") == check["port"] and e.get("k") == "hs" and e.get("ch") == check["ch"]]
        if not seq or int(seq[0]["a"], 16) != check["addr"]:
            self.miss("ORDER_INTERPOSE", port=check["port"], detail="first access",
                      got=seq[0]["a"] if seq else None, want=hex(check["addr"]))

    def _check_reorder(self, check, events, sources, targets):
        """After the mark, the first B at the target answers the second AW
        (a target may return B out of order across sources; the network must
        steer each to its own source)."""
        mark = self.tape["marks"].index(check["after_mark"])
        start = next((e["t"] for e in events if e.get("k") == "mk" and e.get("m") == mark
                      and e.get("v") == check.get("mark_value", e.get("v"))), None)
        seq = [e for e in events if start is not None and e["t"] >= start
               and e.get("p") == check["port"] and e.get("k") == "hs"]
        aws = [e for e in seq if e["ch"] == "aw"]
        bs = [e for e in seq if e["ch"] == "b"]
        if len(aws) < 2 or not bs or bs[0]["id"] != aws[1]["id"] or aws[0]["id"] == aws[1]["id"]:
            self.miss("ORDER_INTERPOSE", port=check["port"], detail="no cross-source B reorder")

    def _window(self, check, events):
        """Event time window of a check: from after_mark (with mark_value)
        until until_mark (with until_value), else the whole run."""
        def at(name, value):
            index = self.tape["marks"].index(name)
            return next((e["t"] for e in events if e.get("k") == "mk" and e.get("m") == index
                         and (value is None or e.get("v") == value)), None)
        lo = at(check["after_mark"], check.get("mark_value")) if "after_mark" in check else 0
        hi = at(check["until_mark"], check.get("until_value")) if "until_mark" in check else None
        return lo, hi

    def _check_rate(self, check, events, sources, targets):
        """Handshake rate at one port and channel, in beats per cycle of
        that port's clock: per burst (first to last beat of each W or R
        burst) or over the window (first to last handshake)."""
        lo, hi = self._window(check, events)
        if lo is None:
            self.miss("RATE_LOW", port=check["port"], detail="window mark missing")
            return
        seq = [e for e in events if e.get("p") == check["port"] and e.get("k") == "hs"
               and e.get("ch") == check["ch"] and e["t"] >= lo and (hi is None or e["t"] < hi)
               and (check.get("epoch") is None or e["_epoch"] == check["epoch"])]
        if check.get("per") == "burst":
            groups, current = [], []
            for e in seq:
                current.append(e["c"])
                if e.get("l", 1):
                    groups.append(current)
                    current = []
        else:
            groups = [[e["c"] for e in seq]] if seq else []
        # A one-beat burst passes in one cycle.
        measured = [(len(g), g[-1] - g[0] + 1) for g in groups]
        if not measured:
            self.miss("RATE_LOW", port=check["port"], ch=check["ch"], detail="no beats measured")
            return
        low = min(n / span for n, span in measured)
        self.rates.append(dict(port=check["port"], ch=check["ch"], per=check.get("per", "window"),
                               label=check.get("label"), lowest=round(low, 4),
                               groups=len(measured)))
        if low + 1e-9 < check["min"]:
            self.miss("RATE_LOW", port=check["port"], ch=check["ch"], label=check.get("label"),
                      got=round(low, 4), want=check["min"])

    def _check_rate_all(self, check, events, sources, targets):
        """Handshake rate over both data channels (W and R) of one port, in
        beats per cycle of that port's clock, over the window (first to last
        handshake of either channel). A target whose in-flight pool is shared
        by both directions serializes opposite-direction bursts, so only the
        aggregate shows whether it kept streaming."""
        lo, hi = self._window(check, events)
        if lo is None:
            self.miss("RATE_LOW", port=check["port"], detail="window mark missing")
            return
        seq = [e["c"] for e in events if e.get("p") == check["port"] and e.get("k") == "hs"
               and e["ch"] in ("w", "r") and e["t"] >= lo and (hi is None or e["t"] < hi)
               and (check.get("epoch") is None or e["_epoch"] == check["epoch"])]
        if not seq:
            self.miss("RATE_LOW", port=check["port"], detail="no beats measured")
            return
        rate = len(seq) / (seq[-1] - seq[0] + 1)
        self.rates.append(dict(port=check["port"], ch="w+r", per="window",
                               label=check.get("label"), lowest=round(rate, 4),
                               groups=1))
        if rate + 1e-9 < check["min"]:
            self.miss("RATE_LOW", port=check["port"], ch="w+r", label=check.get("label"),
                      got=round(rate, 4), want=check["min"])

    def _check_span(self, check, events, sources, targets):
        """Per source burst in the window: its target data beats over the
        cycles from the first to the last of them (all pieces or packets),
        the lowest over the window."""
        lo, hi = self._window(check, events)
        low, count = None, 0
        for t in sources:
            if (t.port != check["port"] or t.dir != check["dir"] or not t.targets
                    or t.issued_t is None or t.issued_t < (lo or 0)
                    or (hi is not None and t.issued_t >= hi)):
                continue
            cycles = sorted(c for tt in t.targets for c in tt.cycles if c is not None)
            if len(cycles) < 2:
                continue
            rate = len(cycles) / (cycles[-1] - cycles[0] + 1)
            low = rate if low is None else min(low, rate)
            count += 1
        if low is None:
            self.miss("RATE_LOW", port=check["port"], detail="no span measured")
            return
        self.rates.append(dict(port=check["port"], dir=check["dir"], per="span",
                               label=check.get("label"), lowest=round(low, 4), groups=count))
        if low + 1e-9 < check["min"]:
            self.miss("RATE_LOW", port=check["port"], dir=check["dir"], detail="span",
                      got=round(low, 4), want=check["min"])

    def _check_reach(self, check, events, sources, targets):
        """Coverage a scenario must reach: transactions of each named source
        and direction in flight at once (address handshake to completion,
        the lowest peak over the pairs), and completions that overtake an
        older open transaction of the same source and direction (summed)."""
        ports = check["port"] if isinstance(check["port"], list) else [check["port"]]
        dirs = check["dir"] if isinstance(check["dir"], list) else [check["dir"]]
        peak, overtakes = None, 0
        for port in ports:
            for d in dirs:
                mine = [t for t in sources if t.port == port and t.dir == d
                        and t.issued_t is not None]
                edges = sorted([(t.issued_t, 1) for t in mine] +
                               [(t.done_t, -1) for t in mine if t.done_t is not None])
                level = top = 0
                for _, step in edges:
                    level += step
                    top = max(top, level)
                peak = top if peak is None else min(peak, top)
                overtakes += sum(1 for t in mine if t.done_t is not None and any(
                    u.issued_t < t.issued_t and u.epoch == t.epoch
                    and (u.done_t is None or u.done_t > t.done_t) for u in mine))
        self.reached.append(dict(port=check["port"], dir=check["dir"], inflight=peak,
                                 overtakes=overtakes))
        if peak < check.get("inflight", 0) or overtakes < check.get("overtakes", 0):
            self.miss("REACH_MISS", port=check["port"], dir=check["dir"], inflight=peak,
                      overtakes=overtakes, want=dict(inflight=check.get("inflight", 0),
                                                     overtakes=check.get("overtakes", 0)))

    def _check_stream_beats(self, check, events, sources, targets):
        """Each transfer on an internal stream carries the aligned beat
        address and byte count of the next source beat (white-box)."""
        fields = next(s["fields"] for s in self.bench["streams"] if s["name"] == check["stream"])
        mine = [t for t in sources if t.port == check["port"] and t.dir == check["dir"]]
        clean = {t.epoch for t in mine} - {t.epoch for t in mine if t.cancelled}
        beats = []
        for t in mine:
            # A burst the ingress answers itself (4 KiB crossing) never reaches the stream.
            if t.epoch in clean and not model.crosses_4k(t.addr, t.length, t.size, t.burst):
                if check.get("per") == "burst":
                    beats.append((t.addr & ~((1 << t.size) - 1), 1 << t.size, t.tag))
                    continue
                for i in range(t.length):
                    a = model.beat_addr(t.addr, t.length, t.size, t.burst, i)
                    beats.append((a & ~((1 << t.size) - 1), 1 << t.size, t.tag))
        got = []
        for e in self.streams.get(check["stream"], []):
            if e["_epoch"] not in clean:
                continue
            value, shift, row = hexval(e, "x"), 0, {}
            for name, width in reversed(fields):
                row[name] = (value >> shift) & ((1 << width) - 1)
                shift += width
            got.append(row)
        for k, row in enumerate(got[:len(beats)]):
            addr, count, tag = beats[k]
            if row.get("address") != addr or row.get("count") != count:
                self.miss("STREAM_FIELD", stream=check["stream"], index=k, tag=tag,
                          got=(hex(row.get("address", 0)), row.get("count")), want=(hex(addr), count))
                break

    # ------------------------------------------------------------------
    # White-box probe replay (fixture probe groups, one event per clock).
    @staticmethod
    def _unpack(group, e):
        value = hexval(e, "x")
        if value is None:
            return None
        row, shift = defaultdict(list), 0
        for field, width in reversed(group["fields"]):
            row[field].insert(0, (value >> shift) & ((1 << width) - 1))
            shift += width
        return {k: v if len(v) > 1 or k in ("valid", "last", "flow", "level", "eligible")
                else v[0] for k, v in row.items()}

    def _probe_rows(self, group, events):
        """(epoch, cycle, row) of one probe group, or None on an X field."""
        rows = []
        for e in events:
            row = self._unpack(group, e)
            if row is None:
                self.miss("EVENT_X", port=group["port"], event=e["_n"])
                return None
            rows.append((e["_epoch"], e.get("c"), row))
        return rows

    @staticmethod
    def _replay(group, rows, policy, rank, levels):
        """Runs one arbiter's rows through the policy model; returns
        (first disagreement or None, statistics). With levels (fixed QoS) the
        candidates' own tails and the output ready feed the model and only
        the highest valid pressure is eligible; without, the granted packet's
        tail and the transfer stand for them."""
        n = group["count"]
        stats = dict(cycles=0, transfers=0, contended=0, filtered=0, held_higher=0)
        model, epoch = None, None
        for ep, cycle, row in rows:
            if ep != epoch:
                epoch = ep
                model = (LevelArbiter if levels is not None else Arbiter)(policy, n, rank=rank)
            valid = [bool(v) for v in row["valid"]]
            stats["cycles"] += 1
            if levels is not None:
                # A candidate's level is its pressure (a thermometer); a valid packet head
                # presses at least with its own flow's level.
                flow, level = row["flow"], row["level"]
                first = row.get("first", [0] * n)  # absent where the network reads no head
                for i in range(n):
                    if level[i] & (level[i] + 1) or (valid[i] and first[i] and (
                            flow[i] not in levels or bin(level[i]).count("1") < levels[flow[i]])):
                        return dict(c=cycle, detail="flow level", flow=flow, level=level), stats
                model.levels = [bin(x).count("1") for x in level]
                high = max((model.levels[i] for i in range(n) if valid[i]), default=0)
                eligible = [int(valid[i] and model.levels[i] == high) for i in range(n)]
                if row["eligible"] != eligible:
                    return dict(c=cycle, detail="eligible", got=row["eligible"],
                                want=eligible), stats
                want = model.step(valid, row["last"], row["ready"])
                stats["filtered"] += bool(not row["held"] and any(
                    valid[i] and model.levels[i] < high for i in range(n)))
                w = row["winner"]
                stats["held_higher"] += bool(row["held"] and w < n and any(
                    valid[i] and model.levels[i] > model.levels[w] for i in range(n)))
            else:
                want = model.step(valid, [bool(row["tail"])] * n, bool(row["transfer"]))
            stats["contended"] += bool(not row["held"] and sum(valid) > 1)
            got = dict(held=bool(row["held"]), transfer=bool(row["transfer"]),
                       release=bool(row["release"]))
            exp = {k: want[k] for k in got}
            if want["sink_valid"]:
                got["winner"], exp["winner"] = row["winner"], want["winner"]
            if got != exp:
                return dict(c=cycle, got=got, want=exp), stats
            stats["transfers"] += bool(row["transfer"])
        return None, stats

    def _probe_replay(self, probes):
        """Arbitrated outputs: every cycle the observed grant equals the
        policy model fed with the observed requests, under the policy the
        case states for that output (independent of the generator: the probe
        group's own policy and candidates must match it). Fixed levels: only
        the highest valid level is eligible, a packet grant is held head to
        tail. Controls: every other policy (fixed priority also the reversed
        order) disagrees with some output's log, and with every per-output
        override on that output itself. Link stages: a spare entry only
        behind a full one. Required reach: a held grant over a higher level,
        a level decision in each graph, a stage that filled, blocked and
        drained per graph, a number of contended decisions."""
        spec = self.case["probe_checks"]
        levels = ({int(k): v for k, v in spec["flow_levels"].items()}
                  if "flow_levels" in spec else None)
        stated = spec.get("policies", {})
        records = {}
        for group in self.bench["probes"]:
            port, kind = group["port"], group["kind"]
            rows = self._probe_rows(group, probes.get(port, []))
            if rows is None:
                return records
            if kind == "response_level":
                rec = records[group["name"]] = dict(kind=kind, graph="response",
                                                     cycles=len(rows), frozen_low=0)
                previous = None
                for ep, cycle, row in rows:
                    if previous is not None and previous["pending"] and row["pending"]:
                        if previous["level"] != row["level"]:
                            self.miss("PROBE_GRANT", port=port, c=cycle,
                                      detail="response level changed while pending",
                                      before=previous["level"], got=row["level"])
                            return records
                    rec["frozen_low"] += bool(row["pending"] and row["level"][0] == 0)
                    previous = row
                continue
            if kind == "pipeline":
                rec = records[group["name"]] = dict(
                    kind=kind, graph="request" if "/req/" in group["name"] else "response",
                    cycles=len(rows), full=0, blocked=0, pops=0, recovered=0)
                pending, epoch = False, None
                for ep, cycle, row in rows:
                    if ep != epoch:
                        pending, epoch = False, ep
                    if row["spare"] and not row["full"]:
                        self.miss("PROBE_GRANT", port=port, c=cycle, detail="spare without full")
                        return records
                    if row["pop"] and pending:
                        rec["recovered"] += 1
                        pending = False
                    if row["input.valid"] and not row["input.ready"]:
                        pending = True
                    rec["full"] += bool(row["full"] and row["spare"])
                    rec["blocked"] += bool(row["input.valid"] and not row["input.ready"])
                    rec["pops"] += row["pop"]
                continue
            n = group["count"]
            key = f"{group['graph']}:{group.get('node')}:{group['output']}"
            rule = spec.get("policy_rule")
            if rule:
                # The policy the input states: a per-output override, else the
                # written default, else round robin.
                policy, order = rule.get("overrides", {}).get(key, (rule["default"], None))
                if policy == "auto":
                    policy = "round_robin"
                stated = {key: (policy, order)}
            policy, order = stated.get(key, (group["policy"], None)) if stated else (
                group["policy"], None)
            rank = ([order.index(c) for c in group["candidates"]] if order
                    else group["rank"] or None)
            if stated and (key not in stated or policy != group["policy"] or (
                    order and rank != group["rank"])):
                self.miss("PROBE_GRANT", port=port, detail="output policy", output=key,
                          stated=stated.get(key), generated=(group["policy"], group["rank"]))
                return records
            mismatch, rec = self._replay(group, rows, policy, rank, levels)
            rec.update(kind=kind, graph=group["graph"], output=key, policy=policy, count=n)
            records[group["name"]] = rec
            if mismatch:
                self.miss("PROBE_GRANT", port=port, output=key, **mismatch)
                return records
            if spec.get("controls"):
                others = {p: (list(range(n)) if p == "fixed_priority" else None)
                          for p in Arbiter.POLICIES if p != policy}
                if policy == "fixed_priority":
                    others["fixed_priority/other_order"] = [n - 1 - r for r in rank]
                rec["distinguished"] = sorted(
                    name for name, other in others.items()
                    if self._replay(group, rows, name.split("/")[0], other, levels)[0])
        need = spec.get("reach", ())
        arbiters = [r for r in records.values() if r["kind"] == "arbiter"]
        stages = [r for r in records.values() if r["kind"] == "pipeline"]
        if "outputs" in spec and len(arbiters) != spec["outputs"]:
            self.miss("PROBE_GRANT", detail="arbitrated outputs", got=len(arbiters),
                      want=spec["outputs"])
        if "held_higher" in need and not sum(r["held_higher"] for r in arbiters):
            self.miss("REACH_MISS", detail="no grant held over a higher level")
        if "frozen_low" in need and not sum(r.get("frozen_low", 0) for r in records.values()):
            self.miss("REACH_MISS", detail="no low response level held while pending")
        for graph in ("request", "response"):
            if "contended" in need and not sum(r["filtered"] for r in arbiters
                                               if r["graph"] == graph):
                self.miss("REACH_MISS", detail="no level decision", graph=graph)
            if "backpressure" in need and not any(
                    r["full"] and r["blocked"] and r["recovered"] for r in stages
                    if r["graph"] == graph):
                self.miss("REACH_MISS", detail="no stage filled, blocked and drained",
                          graph=graph)
        if sum(r["contended"] for r in arbiters) < spec.get("min_contended", 0):
            self.miss("REACH_MISS", detail="too few contended decisions",
                      got=sum(r["contended"] for r in arbiters), want=spec["min_contended"])
        if spec.get("controls"):
            self._policy_controls(spec, arbiters)
        return records

    def _policy_controls(self, spec, arbiters):
        """A network that ignored a configured policy cannot pass: every
        policy other than those configured disagrees with some output's
        log (with two candidates FIFO and round robin grant alike once
        either was served, so they need not differ), and each per-output
        override is observable on its own output."""
        configured = {r["policy"] for r in arbiters}
        seen = {name.split("/")[0] for r in arbiters for name in r.get("distinguished", [])}
        separable = set()
        for r in arbiters:
            same = ({"fifo", "round_robin"} if r["count"] == 2 and
                    r["policy"] in ("fifo", "round_robin") else {r["policy"]})
            separable |= set(Arbiter.POLICIES) - same
        missing = sorted(p for p in separable - configured if p not in seen)
        if missing:
            self.miss("REACH_MISS", detail="policies not distinguished", policies=missing)
        for key in spec.get("overrides", ()):
            rows = [r for r in arbiters if r["output"] == key]
            if not rows:
                self.miss("REACH_MISS", detail="override output not observed", output=key)
                continue
            own = rows[0]["policy"]
            want = set(Arbiter.POLICIES) - {own}
            if own == "fixed_priority":
                want.add("fixed_priority/other_order")
            if rows[0]["count"] == 2 and own in ("fifo", "round_robin"):
                want -= {"fifo", "round_robin"}
            got = set(rows[0].get("distinguished", []))
            if want - got:
                self.miss("REACH_MISS", detail="override not observable", output=key,
                          policies=sorted(want - got))
