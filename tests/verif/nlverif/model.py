"""Independent oracle model: AXI burst arithmetic and target access rules.

Nothing here reads generator output. Burst addresses follow AMBA AXI A3.4.1;
the width-conversion cut rules restate the docs/protocols.md contract; target memory
starts from initial_byte(), mirrored in sv/nlv_common.svh.
"""

from . import contracts as C


def initial_byte(addr, salt):
    """Initial target memory byte (nlv_target_byte in nlv_common.svh)."""
    return (((addr * 13) + (addr >> 8) * 7 + salt * 57) ^ 0x5A) & 0xFF


def beat_addr(start, length, size, burst, i):
    """Byte address of beat i (0-based) of a burst of `length` beats."""
    nbytes = 1 << size
    aligned = start & ~(nbytes - 1)
    if burst == 0 or i == 0:
        return start
    if burst == 2:
        total = nbytes * length
        base = start & ~(total - 1)
        return base + ((aligned - base + nbytes * i) % total)
    return aligned + nbytes * i


def lanes(addr, size, bus_bytes):
    """Active byte lanes [lo, hi] of a beat at addr on a bus of bus_bytes."""
    nbytes = 1 << size
    lo = addr % bus_bytes
    hi = ((addr & ~(nbytes - 1)) % bus_bytes) + nbytes - 1
    return lo, hi


def beat_bytes(addr, size, bus_bytes):
    """Byte addresses carried by the active lanes of one beat."""
    lo, hi = lanes(addr, size, bus_bytes)
    word = addr - addr % bus_bytes
    return [word + j for j in range(lo, hi + 1)]


def amba_cache(cache, write):
    """Target AxCACHE under the amba attribute preset (docs/user-signals.md#target-attributes): the low bits follow the source; ARCACHE[2] and AWCACHE[3]
    are allocate (AxCACHE[2] on reads, AxCACHE[3] on writes), ARCACHE[3]
    and AWCACHE[2] are lookup (AxCACHE[2] or AxCACHE[3])."""
    allocate = (cache >> (3 if write else 2)) & 1
    lookup = int(bool(cache & 0b1100))
    high = (allocate << 3 | lookup << 2) if write else (lookup << 3 | allocate << 2)
    return (cache & 0b11) | high


AMBA_BITS = {'read_cache': ['bufferable', 'modifiable', 'allocate', 'lookup'],
             'write_cache': ['bufferable', 'modifiable', 'lookup', 'allocate'],
             'prot': ['privileged', 'nonsecure', 'instruction'],
             'hprot': ['instruction', 'privileged', 'bufferable', 'modifiable']}


def target_attributes(spec, kind, write, cache, prot):
    """(AxCACHE, AxPROT or HPROT) a target sees under its attribute map
    (docs/user-signals.md#target-attributes): a preset ("amba", "zero") or an object with
    an optional preset and bit lists, least significant bit first, of
    constants or source flags with an optional "~". AHB HPROT is
    {m[3], m[2], m[1], ~m[0]} of the four mapped bits m. None for a target
    without attributes; None in a field the protocol does not carry."""
    if spec is None:
        # Without a map an AXI4 target takes the source attributes as they
        # are; the dedicated targets apply the AMBA preset (PPROT and
        # AxPROT unchanged, HPROT from AxCACHE and AxPROT).
        if kind not in ("apb", "ahb", "lite"):
            return None
        spec = "amba"
    if isinstance(spec, str):
        spec = {"preset": spec}
    preset = spec.get("preset", "zero")
    flags = {'0': 0, '1': 1, 'bufferable': cache & 1, 'modifiable': cache >> 1 & 1,
             'allocate': cache >> (3 if write else 2) & 1,
             'lookup': (cache >> 2 | cache >> 3) & 1, 'privileged': prot & 1,
             'nonsecure': prot >> 1 & 1, 'instruction': prot >> 2 & 1}

    def bits(field):
        names = spec.get(field)
        if names is None:
            names = AMBA_BITS[field] if preset == "amba" else ['0'] * len(AMBA_BITS[field])
        return sum((flags[n.lstrip('~')] ^ n.startswith('~')) << i for i, n in enumerate(names))
    if kind == "ahb":
        m = bits('hprot')
        return None, (m & 0b1110) | (~m & 1)
    mapped_prot = bits('prot')
    if kind in ("apb", "lite"):
        return None, mapped_prot
    return bits('write_cache' if write else 'read_cache'), mapped_prot


def crosses_4k(start, length, size, burst):
    nbytes = 1 << size
    return burst == 1 and ((start % 4096) // nbytes + length) * nbytes > 4096


def merge(codes):
    """Most severe response of several (DECERR over SLVERR over OKAY)."""
    if not codes:
        return C.OKAY
    failed = [code for code in codes if code in (C.SLVERR, C.DECERR)]
    if failed:
        return max(failed, key=lambda code: C.SEVERITY[code])
    return C.EXOKAY if all(code == C.EXOKAY for code in codes) else C.OKAY


def downsize_pieces(length, size, burst, start):
    """Target bursts (addr, len-1, size, burst) of one wide source burst on a
    32-bit target, plus the source beat of every target word in order.

    Cut rules (docs/width-conversion.md#width-conversion): a beat of at most four bytes passes
    unchanged; a wider WRAP of 2, 4, 8 or 16 target words stays one WRAP; a
    longer WRAP becomes INCR pieces from the start to the container top and
    then from the container base; an INCR covers the words from the start
    word to the burst end; each INCR piece holds at most 256 words; a FIXED
    wide beat repeats per beat."""
    n, sz = length, size
    step = 1 << sz
    if sz <= 2:
        return [(start, n - 1, sz, burst)], list(range(n))
    aligned = start // step * step
    first = start // 4 * 4
    if burst == 2:
        span = n * step
        base = start // span * span
        words = span // 4
        order = [base + (start - base + 4 * i) % span for i in range(words)]
        beats = [((w - aligned) % span) // step for w in order]
        if words in (2, 4, 8, 16):
            return [(start, words - 1, 2, 2)], beats
        runs = [list(range(start, base + span, 4)), list(range(base, start, 4))]
    elif burst == 0:
        runs = [list(range(first, aligned + step, 4)) for _ in range(n)]
    else:
        runs = [list(range(first, aligned + n * step, 4))]
    out, beats = [], []
    for index, run in enumerate(runs):
        for k in range(0, len(run), 256):
            words = run[k:k + 256]
            out.append((start if words[0] == first else words[0], len(words) - 1, 2, 1))
            if burst == 0:
                beats += [index] * len(words)
            elif burst == 1:
                beats += [(w - aligned) // step for w in words]
    if burst == 2:
        beats = [((w - aligned) % span) // step for run in runs for w in run]
    return out, beats


def folds(length, size, burst, sbytes):
    """A narrow WRAP whose container fits the source word (network sources
    send it as one INCR beat at the container base)."""
    return burst == 2 and 1 << size < sbytes and length << size <= sbytes


def network_downsize_pieces(length, size, burst, start, sbytes, write=False):
    """Target bursts (addr, len-1, size, burst) of one burst of an sbytes-wide
    network source reaching a 32-bit AXI4 target. The source's packet
    splitter acts first: a folding WRAP becomes one INCR beat at the
    container base; an INCR is cut into packets of 1 KiB from its alignment
    word (the 32-bit target's packet limit), and a packet narrower than the
    32-bit word, or of one beat of it, goes out packed into words before the
    cut rules of downsize_pieces apply."""
    step = 1 << size
    if folds(length, size, burst, sbytes if write else min(sbytes, 4)):
        span = length * step
        base = start // span * span
        if span <= 4:
            return [(base, 0, span.bit_length() - 1, 1)]
        return [(base, span // 4 - 1, 2, 1)]
    if burst != 1:
        return downsize_pieces(length, size, burst, start)[0]
    align, lw, word = max(sbytes, 4), 2, 4
    end, out, at = start // step * step + length * step, [], start
    while at < end:
        stop = min(at // align * align + 1024, end)
        if size < lw or length == 1 and size == lw:
            words = (stop + word - 1) // word - at // word
            cover = lw if words > 1 else next(
                q for q in range(lw + 1) if at >> q == (stop - 1) >> q)
            out += downsize_pieces(words, cover, 1, at)[0]
        else:
            count = (stop - at // step * step + step - 1) // step
            out += downsize_pieces(count, size, 1, at)[0]
        at = stop
    return out


class Window:
    def __init__(self, target, base, size):
        self.target, self.base, self.size = target, base, size

    def contains(self, addr):
        return self.base <= addr < self.base + self.size


class Route:
    """Address decode of one source: ordered windows, independent of map.json."""

    def __init__(self, windows):
        self.windows = list(windows)

    def decode(self, addr):
        for w in self.windows:
            if w.contains(addr):
                return w
        return None


def target_pool(port, capacity, at_least=2):
    """A case oracle for an AXI4 target of `capacity` bursts: from its
    handshakes, AW and AR not yet answered by B or the last R never exceed
    the capacity and reach at_least at some point."""
    def oracle(checker, events, sources, targets):
        out, writes, reads, peak = [], [], [], 0
        for e in events:
            if e.get("p") != port:
                continue
            if e.get("k") == "rst" and e.get("v") == 0:
                writes, reads = [], []
                continue
            ch = e.get("ch")
            if e.get("k") != "hs":
                continue
            if ch == "aw":
                writes.append(e["id"])
            elif ch == "ar":
                reads.append(e["id"])
            elif ch == "b" and e["id"] in writes:
                writes.remove(e["id"])
            elif ch == "r" and e.get("l") and e["id"] in reads:
                reads.remove(e["id"])
            level = len(writes) + len(reads)
            if level > capacity:
                out.append(dict(detail="pool exceeded", port=port, level=level, c=e.get("c")))
            peak = max(peak, level)
        if peak < at_least:
            out.append(dict(detail="too few bursts in flight at the target", port=port,
                            peak=peak, want=at_least))
        return out[:8]
    return oracle


def exact_blocks(start, end, cap):
    """The byte ranges [lo, hi) of exact packets over [start, end): from the
    start, each is the largest naturally aligned power-of-two block of the
    bytes left, at most cap (a power of two)."""
    out = []
    while start < end:
        size = min(start & -start or cap, 1 << ((end - start).bit_length() - 1), cap)
        out.append((start, start + size))
        start += size
    return out


def exact_access(port, cap):
    """A case oracle for a target with read side effects at local base 0: an
    INCR source burst with AxCACHE[1] and AxLOCK clear that reaches it without
    a local error makes one target access per exact block, in block order,
    each covering exactly its block."""
    def oracle(checker, events, sources, targets):
        out, split = [], 0
        for s in sources:
            if (s.cancelled or not s.done or s.burst != 1 or s.cache & 2 or s.lock
                    or s.local_error is not None or not s.targets
                    or any(t.port != port for t in s.targets)):
                continue
            end = (s.addr >> s.size << s.size) + (s.length << s.size)
            got = [(t.addr, (t.addr >> t.size << t.size) + (t.length << t.size))
                   for t in sorted(s.targets, key=lambda t: t.t)]
            want = exact_blocks(s.addr, end, cap)
            split += len(want) > 1 and want[0][1] - want[0][0] < 1 << s.size
            if got != want:
                out.append(dict(detail="target accesses are not the exact blocks", port=s.port,
                                dir=s.dir, addr=hex(s.addr), size=s.size, beats=s.length,
                                want=[f"{a:#x}+{b - a}" for a, b in want],
                                got=[f"{a:#x}+{b - a}" for a, b in got]))
        if not split:
            out.append(dict(detail="no burst split inside its first beat", port=port))
        return out[:8]
    return oracle
