"""Independent AHB transfer-shape model and bus rules for the AHB outlet
cases.

A source burst reaches the outlet as one packet. Its target view: a source
beat wider than the target word is clamped into target words (INCR and WRAP
keep their start and container); a WRAP that the network cuts reaches the
target as INCR. The model gives, for every target word of the packet, the
HBURST of its transfers and whether a word starts with NONSEQ, from the
packet's HBURST (INCRn/WRAPn for 4, 8 or 16 words of at least 32 bits,
SINGLE for one word, a two-word WRAP, bytes and halfwords, INCR otherwise;
INCR across 1 KiB or the window end; SINGLE for a WRAP container past the
window end or above 1 KiB), the write shaping (INCRn becomes INCR
and WRAP SINGLE; without undefined-length INCR an INCR packet goes out as
INCR16, INCR8, INCR4 and SINGLE blocks bounded by the packet end, 1 KiB and
the window end) and the packet write rule (from the first sparse word on,
NONSEQ SINGLE). A write word makes one transfer when its strobes are its
whole container, the start block of a one-beat or FIXED beat, or exactly the
lower or upper half of the target word, else one transfer per strobed byte;
a one-beat or FIXED read partitions its address-to-container-end suffix into
aligned blocks, choosing the largest block at each address.

Bus rules over every address phase and every bus state edge: HSIZE within
the bus, HADDR aligned and inside the window, no HMASTLOCK, a SEQ continues
its burst (next address, same HBURST, HSIZE, HWRITE, HPROT), no INCR SEQ at
a 1 KiB boundary, no INCR without undefined-length bursts, fixed INCRn
inside 1 KiB and the window, WRAP containers inside 1 KiB and the window,
writes without undefined-length INCR as SINGLE, a fixed-length
burst with exactly its beats and no IDLE inside it, and a packet that keeps the
bus lets no transfer of the other direction in until its last one."""

def bits(mask):
    return bin(mask).count("1")


class Packet:
    """One source burst at an AHB target of `lanes` bytes from a source of
    `slanes` bytes. start: the global start address; win: (base, size) of
    the outlet window; strobes: per source beat (writes); cut: the network
    sends this WRAP as INCR."""

    def __init__(self, write, start, length, size, burst, strobes, lanes, slanes, win,
                 complete=False, no_incr=False, cut=False):
        self.write, self.start, self.n, self.sz, self.burst = write, start, length, size, burst
        self.strobes = strobes or [0] * length
        self.lanes, self.slanes = lanes, slanes
        self.big = lanes.bit_length() - 1
        self.win, self.limit = win[0], win[1]
        self.complete, self.no_incr = complete, no_incr
        self.tburst = 1 if cut else burst
        self.tsz = min(size, self.big)
        self.tn = length << (size - self.big) if size > self.big else length

    # ---- addresses ----
    def address(self, i):
        step, span = 1 << self.sz, self.n << self.sz
        if i == 0 or self.burst == 0:
            return self.start
        if self.burst == 2:
            return self.start // span * span + (self.start // step * step + i * step) % span
        return self.start // step * step + i * step

    def taddress(self, k):
        step, span = 1 << self.tsz, self.tn << self.tsz
        if k == 0 or self.tburst == 0:
            return self.start
        if self.tburst == 2:
            return self.start // span * span + (self.start // step * step + k * step) % span
        return self.start // step * step + k * step

    def tcontainer(self, k):
        """Local address of target word k's container."""
        return (self.taddress(k) >> self.tsz << self.tsz) - self.win

    def tbeat_strobes(self, k):
        j = k // (self.tn // self.n)
        m = self.strobes[j]
        lo = self.taddress(k) >> self.tsz << self.tsz
        out = 0
        for q in range(self.slanes):
            x = self.address(j) // self.slanes * self.slanes + q
            if m >> q & 1 and lo <= x < lo + (1 << self.tsz):
                out |= 1 << (x % self.lanes)
        return out

    def tcontainer_mask(self, k):
        lo = (self.taddress(k) >> self.tsz << self.tsz) % self.lanes
        return ((1 << (1 << self.tsz)) - 1) << lo

    # ---- shapes ----
    def sparse_from(self):
        if not self.write:
            return self.tn
        for j in range(self.tn):
            if (self.tbeat_strobes(j) != self.tcontainer_mask(j)
                    or self.tcontainer(j) >= self.limit):
                return j
        return self.tn

    def packet_code(self):
        base = self.start - self.win
        span = self.tn << self.tsz
        if self.tsz < 2 or self.tburst == 0:
            return 0
        if self.tburst == 2:
            code = {4: 2, 8: 4, 16: 6}.get(self.tn, 0)
            if base // span * span + span > self.limit or span > 1024:
                code = 0
            return code
        code = {1: 0, 4: 3, 8: 5, 16: 7}.get(self.tn, 1)
        first = base >> self.tsz << self.tsz
        if self.tn > 1 and (first % 1024 + span > 1024 or first + span > self.limit):
            code = 1
        return code

    def expect_word(self, j, sparse):
        """HBURST of word j's transfers, plus 8 when the word starts with
        NONSEQ."""
        c = self.packet_code()
        if self.write:
            c = 1 if c % 2 == 1 else 0
        blocked = self.no_incr and c == 1
        if blocked and self.write:
            c, blocked = 0, False
        if j >= sparse:
            return 8
        if not blocked:
            a = self.tcontainer(j)
            return c + (8 if j == 0 or c == 0 or (c == 1 and a % 1024 == 0) else 0)
        s = 0
        while True:
            a = self.tcontainer(s)
            length = next((m for m in (16, 8, 4) if self.tn - s >= m
                           and a % 1024 + (m << self.tsz) <= 1024
                           and a + (m << self.tsz) <= self.limit), 1)
            if j < s + length:
                return {16: 7, 8: 5, 4: 3, 1: 0}[length] + (8 if j == s else 0)
            s += length

    def word_of(self, local):
        """The packet word a transfer at local belongs to (the last of
        repeated words)."""
        found = 0
        for k in range(self.tn):
            if self.tcontainer(k) == local >> self.tsz << self.tsz:
                found = k
        return found

    def keeps_bus(self, j, sparse):
        if self.packet_code() == 0 or (self.write and j >= sparse):
            return False
        return any(self.tcontainer(k) < self.limit
                   and (not self.write or self.tbeat_strobes(k) != 0)
                   for k in range(j + 1, self.tn))

    def read_shapes(self):
        shapes = []
        for j in range(self.tn):
            base = self.tcontainer(j)
            if base >= self.limit:
                continue
            at = self.taddress(j) - self.win
            end = base + (1 << self.tsz)
            if self.tn != 1 and self.tburst != 0:
                shapes.append((base, self.tsz))
                continue
            while at < end:
                size = next(k for k in range(self.tsz, -1, -1)
                            if at % (1 << k) == 0 and at + (1 << k) <= end)
                shapes.append((at, size))
                at += 1 << size
        return shapes

    def write_transfers(self):
        total = 0
        half = (1 << (self.lanes // 2)) - 1
        whole = (1 << self.lanes) - 1
        for j in range(self.tn):
            if self.tcontainer(j) >= self.limit:
                continue
            scan, chunk = self.tbeat_strobes(j), self.tcontainer_mask(j)
            lane = self.taddress(j) % self.lanes
            x = (1 << self.tsz) - lane % (1 << self.tsz)
            tail = sum(1 << k for k in range(self.lanes) if chunk >> k & 1 and k >= lane)
            start_block = (self.tn == 1 or self.tburst == 0) and x & (x - 1) == 0 and scan == tail
            count = bits(scan)
            if scan == chunk or start_block or scan == half or scan == whole & ~half:
                count = int(count != 0)
            total += count
        return total

    def reads_in_window(self):
        return len(self.read_shapes())


def next_addr(a, code, size):
    span = (4 << ((code - 2) // 2)) << size if code >= 2 else 0
    if code >= 2 and code % 2 == 0:
        return a // span * span + (a + (1 << size)) % span
    return a + (1 << size)


def beats_of(code):
    return 0 if code < 2 else 4 << ((code - 2) // 2)


def judge_packet(p, transfers, out, coverage, abort_after=None):
    """One packet's transfers (target transactions with hburst, htrans, addr,
    size in order) against the model."""
    sparse = p.sparse_from()
    read_shapes = p.read_shapes() if not p.write else []
    for index, tt in enumerate(transfers):
        local = tt.addr
        j = p.word_of(local)
        xc = p.expect_word(j, sparse)
        want_trans = 2 if xc >= 8 else 3
        if tt.hburst != xc % 8 or tt.htrans != want_trans:
            out.append(dict(rule="BURST", a=hex(local), j=j, got=(tt.hburst, tt.htrans),
                            want=(xc % 8, want_trans)))
            return
        if p.write:
            if tt.size > p.tsz or (xc % 8 != 0 and tt.size != p.tsz):
                out.append(dict(rule="HSIZE_BEAT", a=hex(local), got=tt.size, want=p.tsz))
                return
        else:
            if index >= len(read_shapes):
                out.append(dict(rule="READ_ONCE", got=len(transfers), want=len(read_shapes)))
                return
            address, size = read_shapes[index]
            if tt.size != size:
                out.append(dict(rule="HSIZE_BEAT", a=hex(local), got=tt.size,
                                want=size))
                return
            if local != address:
                out.append(dict(rule="ADDRESS", a=hex(local), want=hex(address)))
                return
        if xc % 8 != 0:
            coverage["full" if tt.size == p.big else "narrow"] += 1
        if p.write and tt.size < p.tsz:
            coverage["split"] += 1
            coverage["split_size_%d" % tt.size] += 1
    expected = abort_after if abort_after is not None else (
        p.write_transfers() if p.write else p.reads_in_window())
    if p.write and len(transfers) != expected:
        out.append(dict(rule="WRITE_ONCE", got=len(transfers), want=expected,
                        start=hex(p.start)))
    if not p.write and len(transfers) != expected:
        out.append(dict(rule="READ_ONCE", got=len(transfers), want=expected,
                        start=hex(p.start)))


def bus_rules(events, port, limit, big, complete, no_incr):
    """Bus rules over the address phases and bus state edges of one AHB port
    (module docstring). Reset and error responses permit cancellation."""
    out = []
    phases = [e for e in events if e.get("p") == port and e.get("k") == "ahb_addr"]
    edges = [e for e in events if e.get("p") == port and e.get("k") == "tr"]
    resets = [e["t"] for e in events if e.get("p") == port and e.get("k") == "rst"
              and e.get("v") == 0]
    resets += [e["t"] for e in events if e.get("p") == port and e.get("k") == "ahb_data"
               and e.get("r", 0) >= 2]
    open_ = None  # [code, addr, size, write, prot, left, first_t]
    for e in phases:
        a, sz, code, tr, w = int(e["a"], 16), e["sz"], e["bu"], e["tr"], e["w"]
        if open_ is not None and any(open_[6] < r <= e["t"] for r in resets):
            open_ = None
        if sz > big or a % (1 << sz) or a >= limit or e.get("lk"):
            out.append(dict(rule="ADDRESS_OR_SIZE", a=hex(a), sz=sz, lk=e.get("lk")))
        if tr == 3:
            if open_ is None or (code, sz, w, e["pr"]) != (open_[0], open_[2], open_[3], open_[4]):
                out.append(dict(rule="SEQ_CONTROL", a=hex(a), c=e.get("c")))
            elif a != next_addr(open_[1], code, sz):
                out.append(dict(rule="SEQ_ADDRESS", a=hex(a), want=hex(next_addr(open_[1], code,
                                                                                sz))))
            if code % 2 == 1 and a % 1024 == 0:
                out.append(dict(rule="KIB_CROSS", a=hex(a)))
            if open_ is not None:
                open_[1] = a
                if code >= 2:
                    open_[5] -= 1
                    if open_[5] == 0:
                        open_ = None
            continue
        # NONSEQ: a fixed-length burst still open ended early.
        if open_ is not None and open_[5] > 0:
            out.append(dict(rule="FIXED_EARLY", a=hex(a), left=open_[5]))
        open_ = [code, a, sz, w, e["pr"], beats_of(code) - 1, e["t"]]
        if beats_of(code) == 0 and code != 1:
            open_ = None
        if no_incr and code == 1:
            out.append(dict(rule="INCR_USED", a=hex(a)))
        if code > 1 and code % 2 == 1 and (a % 1024 + (beats_of(code) << sz) > 1024
                                           or a + (beats_of(code) << sz) > limit):
            out.append(dict(rule="BLOCK_BOUNDS", a=hex(a), code=code))
        if code > 1 and code % 2 == 0:
            span = beats_of(code) << sz
            if span > 1024 or (a // span + 1) * span > limit:
                out.append(dict(rule="WRAP_BOUNDS", a=hex(a), code=code))
        if no_incr and w and code != 0:
            out.append(dict(rule="WRITE_SINGLE", a=hex(a), code=code))
    if open_ is not None and open_[5] > 0 and not any(r > open_[6] for r in resets):
        out.append(dict(rule="FIXED_END", left=open_[5]))
    # No IDLE with HREADY high inside an open fixed-length burst: between a
    # burst's NONSEQ and its last SEQ the bus shows SEQ or BUSY only.
    bursts, current = [], None
    for e in phases:
        if e["tr"] == 2:
            current = [e["t"], e["t"], e["bu"], 1]
            bursts.append(current)
        elif current is not None:
            current[1] = e["t"]
            current[3] += 1
    for start, end, code, count in bursts:
        if code < 2:
            continue
        for e in edges:
            if start < e["t"] < end and e["rd"] and (not e["s"] or e["tr"] == 0):
                out.append(dict(rule="IDLE_IN_BURST", t=e["t"], code=code))
                break
    return out[:6]


def bus_state(events, port):
    """The (time, cycle, hsel, htrans, hready) edges of one AHB port."""
    return [(e["t"], e["c"], e["s"], e["tr"], e["rd"]) for e in events
            if e.get("p") == port and e.get("k") == "tr"]


def runs(edges, lo, hi, predicate):
    """Cycle intervals [c0, c1) inside the time window where the bus state
    satisfies predicate(hsel, htrans, hready)."""
    out = []
    for (t, c, s, tr, rd), nxt in zip(edges, edges[1:] + [None]):
        if t >= hi or (nxt is not None and nxt[0] <= lo):
            continue
        if predicate(s, tr, rd):
            out.append((c, nxt[1] if nxt is not None else c + 1, t))
    return out


# ---------------------------------------------------------------------------
# The scenario tables (ahb_outlet_bench.cases and byte_mask).

def cases(lanes, slanes, window, fixed_single):
    """The case list (ahb_outlet_bench.cases) for a target word of lanes
    bytes, without the beat wider than the source bus."""
    if lanes == 4:
        body = [(4, 2, 1, 0, 0), (8, 2, 1, 1, 0), (4, 2, 1, 2, 0), (4, 2, 1, 5, 0),
                (4, 2, 1, 6, 0), (4, 2, 1, 7, 0), (4, 2, 1, 8, 0), (16, 2, 1, 4, 0),
                (8, 0, 1, 0, 1), (8, 1, 1, 0, 1), (8, 2, 1, 0, 1),
                (8, 2, 0, 3, 0), (1, 2, 0, 3, 0), (8, 2, 2, 0, 4), (4, 2, 2, 0, 8),
                (4, 2, 1, 15, 0), (16, 2, 1, 15, 0), (16, 2, 2, 15, 0), (4, 2, 1, 16, 0),
                (16, 2, 1, 16, 0), (16, 2, 2, 16, 0),
                (5, 2, 1, 17, 0), (32, 2, 1, 0, 0), (256, 2, 1, 0, 0), (4, 2, 1, 0, 732),
                (1, 2, 1, 0, 1), (1, 2, 1, 0, 2), (1, 2, 1, 0, 3), (1, 1, 1, 0, 1)]
        shapes = [(4, 2, 1, 17, 0), (8, 2, 2, 17, 0), (4, 2, 1, 18, 0), (8, 2, 1, 18, 0),
                  (8, 2, 1, 20, 0), (8, 2, 2, 19, 4), (16, 2, 2, 19, 0), (16, 2, 1, 0, 720),
                  (16, 2, 1, 21, 1008), (2, 2, 1, 0, 0), (3, 2, 1, 0, 0), (5, 2, 1, 0, 0),
                  (7, 2, 1, 0, 0), (17, 2, 1, 0, 0), (20, 2, 1, 0, 0), (20, 2, 1, 21, 0),
                  (36, 2, 1, 21, 4), (20, 2, 1, 13, 0), (20, 2, 1, 17, 0), (4, 2, 1, 22, 0),
                  (8, 2, 2, 22, 0), (20, 2, 1, 22, 0), (4, 2, 1, 23, 0), (20, 2, 1, 23, 0)]
        if window is not None:
            body = []
            shapes = [(20, 2, 1, 0, 0xc4), (20, 2, 1, 0, 0x1c4), (20, 2, 1, 0, 0xb0),
                      (8, 2, 1, 0, 0xf0), (8, 2, 1, 0, 0x1f0), (6, 2, 1, 21, 0xe8),
                      (16, 2, 1, 17, 0xc4), (4, 2, 2, 0, 0xf0), (8, 2, 2, 0, 0xf0),
                      (20, 2, 1, 23, 0x1c4)]
        if fixed_single and body:
            at = body.index((8, 2, 0, 3, 0))
            body[at:at + 2] = [(8, 2, 0, 0, 0), (1, 2, 0, 0, 0), (8, 2, 0, 1, 0),
                               (6, 0, 0, 0, 1), (6, 1, 0, 0, 2), (5, 2, 0, 13, 0),
                               (8, 2, 0, 14, 0), (6, 0, 0, 14, 1), (6, 2, 0, 0, 1)]
        if window is None:
            shapes += [(16, 2, 1, 25, 0), (16, 2, 2, 25, 0), (64, 2, 1, 25, 0),
                       (256, 2, 1, 25, 0)]
        return body + shapes
    full = min(lanes, slanes).bit_length() - 1
    longest = min(256, 0x700 // lanes)
    sixteen = min(16, longest)
    narrow = max(2, full - 1)
    wrap_long = 16 if lanes < 128 else 8
    if window is not None:
        beats = 2 if lanes == 128 else 4
        offset = 0x100 - (beats // 2) * lanes
        body = [(beats, full, 1, 0, offset), (beats, full, 1, 24, offset), (4, 2, 1, 0, 0xf0),
                (4, 2, 2, 0, 0xf0), (2, full, 2, 0, 0)]
        if fixed_single:
            body += [(beats, full, 0, 0, offset), (4, 0, 0, 0, 0xf1)]
        return body + [(beats, full, 1, 21, offset), (beats, full, 1, 23, offset),
                       (6, 2, 1, 21, 0xe8), (20, 2, 1, 21, 0xc4)]
    body = [(4, full, 1, 0, 0), (8, full, 1, 1, 0), (4, full, 1, 2, 0), (4, full, 1, 5, 0),
            (4, full, 1, 6, 0), (4, full, 1, 7, 0), (4, full, 1, 8, 0),
            (sixteen, full, 1, 4, 0),
            (8, 0, 1, 0, 1), (8, 1, 1, 0, 2), (8, 2, 1, 0, 4), (6, narrow, 1, 0, 0),
            (4, 2, 2, 0, 8), (8, narrow, 2, 0, 0), (20, 2, 1, 0, 0),
            (8, full, 1, 24, 0), (8, narrow, 1, 24, 0), (8, 2, 1, 24, 0),
            (8, full, 0, 3, 0), (1, full, 0, 3, 0),
            (8, full, 2, 0, 4 * lanes), (4, full, 2, 0, 2 * lanes), (16, full, 2, 0, 0),
            (4, full, 1, 15, 0), (sixteen, full, 1, 15, 0), (wrap_long, full, 2, 15, 0),
            (4, full, 1, 16, 0), (sixteen, full, 1, 16, 0), (wrap_long, full, 2, 16, 0),
            (5, full, 1, 17, 0), (longest, full, 1, 0, 0),
            (4, full, 1, 0, 0x300 - 2 * lanes), (5, full, 1, 13, 0),
            (1, full, 1, 0, 1), (1, full, 1, 0, 1 << (full - 1)), (1, 2, 1, 0, 5),
            (1, 2, 1, 18, 0)] + ([(1, 3, 1, 0, 6)] if slanes >= 8 else [])
    shapes = [(4, full, 1, 17, 0), (8, full, 2, 17, 0), (4, full, 1, 18, 0), (8, full, 1, 18, 0),
              (8, full, 1, 20, 0), (8, full, 2, 19, lanes), (2, full, 1, 0, 0), (3, full, 1, 0, 0),
              (5, full, 1, 0, 0), (7, full, 1, 0, 0), (min(17, longest), full, 1, 0, 0),
              (sixteen, full, 1, 21, 0), (4, full, 1, 21, 0x300 - 2 * lanes),
              (sixteen, full, 1, 13, 0), (sixteen, full, 1, 17, 0), (4, full, 1, 22, 0),
              (8, full, 2, 22, 0), (4, full, 1, 23, 0), (sixteen, full, 1, 23, 0),
              (20, 2, 1, 21, 0)]
    if fixed_single:
        at = body.index((8, full, 0, 3, 0))
        body[at:at + 2] = [(8, full, 0, 0, 0), (1, full, 0, 0, 0), (8, full, 0, 1, 0),
                           (6, 0, 0, 0, 1), (6, 1, 0, 0, 2), (5, full, 0, 13, 0),
                           (8, full, 0, 14, 0), (6, 0, 0, 14, 1), (4, narrow, 0, 24, 0)]
    shapes += [(sixteen, full, 1, 25, 0), (16 if lanes < 128 else 8, full, 2, 25, 0),
               (longest, full, 1, 25, 0)]
    return body + shapes


def byte_masks(start, beats, size, burst, mode, slanes):
    """Source lane masks per beat (ahb_outlet_bench.byte_mask)."""
    p = Packet(True, start, beats, size, burst, None, slanes, slanes, (0, 1 << 32))
    out = []
    for index in range(beats):
        first = p.address(index)
        last = (first >> size << size) + (1 << size)
        word = first // slanes * slanes
        mask = sum(1 << b for b in range(slanes) if first <= word + b < last)
        base_lane = (first >> size << size) % slanes
        c = 1 << size
        if mode == 1:
            mask &= [5 << base_lane, 10 << base_lane, 0,
                     ~(((1 << (c // 2)) - 1) << base_lane)][index % 4]
        if mode == 7:
            mask = 0
        if mode in (17, 22, 23) and index == beats - 1:
            mask = 0
        if mode == 18 and index == beats - 1:
            mask &= ((1 << (c // 2)) - 1) << base_lane
        if mode == 19 and index == beats // 2:
            mask &= 6 << base_lane
        if mode == 20 and index == 0:
            mask &= 14 << base_lane
        if mode == 24:
            if index % 4 in (0, 3):
                mask &= ~(1 << base_lane)
            if index % 4 in (1, 3):
                mask &= ~(1 << (base_lane + c - 1))
            if index % 4 == 2:
                for b in range(base_lane + 1, base_lane + c, 2):
                    mask &= ~(1 << b)
        out.append(mask & ((1 << slanes) - 1))
    return out


STROBE_MODES = (1, 7, 17, 18, 19, 20, 22, 23, 24)
