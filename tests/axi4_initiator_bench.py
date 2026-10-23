"""Transaction-level bench for the full AXI4 ingress endpoint (initiator adapter).

write_bench(work, data_bits, len_bits, id_bits) emits tb.sv driving the AXI
slave face as a master and playing the network on the Weft faces. Expected
packet fragments and AXI completions are precomputed here (independent oracle)
and checked by FIFO monitors; all bursts use hand-scripted beats, so a wrong
LEN/4KiB/WRAP/wlast behavior is caught by the designated checker named in each
check call. The bench is written for the 4-engine portability rules: 2-state
constructs, no generate-scope disable, guard-bounded waits.
"""

from pathlib import Path

# Weft burst packet contract field sets (mirror of message.hpp; drift is caught
# by the unit test's exact field-list comparison and by the TB compile).
REQUEST_FIELDS = ["address", "write", "protection", "length", "size", "burst", "cache",
                  "id", "source", "count", "error", "first", "last", "data", "be"]
RESPONSE_FIELDS = ["address", "result", "id", "source", "count", "error", "first", "last",
                   "data"]


def field_widths(data_bits, len_bits, id_bits, source_bits=0):
    req = {"address": 32, "write": 1, "protection": 3, "length": len_bits, "size": 3,
           "burst": 2, "cache": 4, "count": 6, "error": 1, "first": 1, "last": 1,
           "data": data_bits, "be": data_bits // 8}
    if id_bits:
        req["id"] = id_bits
    if source_bits:
        req["source"] = source_bits
    rsp = {"address": 32, "result": 2, "count": 6, "error": 1, "first": 1, "last": 1,
           "data": data_bits}
    if id_bits:
        rsp["id"] = id_bits
    if source_bits:
        rsp["source"] = source_bits
    req = [(f, req[f]) for f in REQUEST_FIELDS if f in req]
    rsp = [(f, rsp[f]) for f in RESPONSE_FIELDS if f in rsp]
    return req, rsp


def hexn(value, bits):
    return f"{bits}'h{value:0{(bits + 3) // 4}X}"


class Bench:
    def __init__(self, data_bits, len_bits, id_bits):
        self.db = data_bits
        self.lb = len_bits
        self.idb = id_bits
        self.sz_full = (data_bits // 8).bit_length() - 1
        self.word = data_bits // 8
        self.lines = []
        self.wq_total = 0   # write request fragments transferred
        self.rq_total = 0   # read request fragments transferred
        self.b_total = 0
        self.r_total = 0
        self.max_len = (1 << len_bits) - 1
        self.w_extra = 0    # accepted W beats that never become a fragment
        self.w_tight_pairs = 0  # in-burst W beat pairs under the throughput check

    def emit(self, text=""):
        self.lines.append(text)

    # ---- expectation pushers ---------------------------------------------
    def xpush_w(self, addr, length, size, burst, cache, prot, ident, count, first, last, data, be):
        self.wq_total += 1
        self.emit(f"  xpush_w(32'h{addr:08X}, {self.lb}'d{length}, 3'd{size}, 2'd{burst}, "
                  f"4'h{cache:X}, 3'b{prot:03b}, {max(self.idb, 1)}'d{ident}, 6'd{count}, "
                  f"{first}, {last}, {self.db}'h{data:0{self.db // 4}X}, {self.word}'h{be:X});")

    def xpush_rq(self, addr, length, size, burst, cache, prot, ident, count):
        self.rq_total += 1
        self.emit(f"  xpush_rq(32'h{addr:08X}, {self.lb}'d{length}, 3'd{size}, 2'd{burst}, "
                  f"4'h{cache:X}, 3'b{prot:03b}, {max(self.idb, 1)}'d{ident}, 6'd{count});")

    def xpush_b(self, resp, ident):
        self.b_total += 1
        self.emit(f"  xpush_b(2'd{resp}, {max(self.idb, 1)}'d{ident});")

    def xpush_r(self, data, resp, last, ident):
        self.r_total += 1
        self.emit(f"  xpush_r({self.db}'h{data:0{self.db // 4}X}, 2'd{resp}, {last}, "
                  f"{max(self.idb, 1)}'d{ident});")

    # ---- drivers ----------------------------------------------------------
    def aw(self, addr, length, size, burst, lock, cache, prot, ident):
        self.emit(f"  aw_send(32'h{addr:08X}, {self.lb}'d{length}, 3'd{size}, 2'd{burst}, "
                  f"{lock}, 4'h{cache:X}, 3'b{prot:03b}, {max(self.idb, 1)}'d{ident});")

    def w(self, data, be, last):
        self.emit(f"  w_send({self.db}'h{data:0{self.db // 4}X}, {self.word}'h{be:X}, {last});")

    def wrsp(self, result, error=0, first=1, last=1):
        self.emit(f"  wrsp_send(2'd{result}, {error}, {first}, {last});")

    def ar(self, addr, length, size, burst, lock, cache, prot, ident):
        self.emit(f"  ar_send(32'h{addr:08X}, {self.lb}'d{length}, 3'd{size}, 2'd{burst}, "
                  f"{lock}, 4'h{cache:X}, 3'b{prot:03b}, {max(self.idb, 1)}'d{ident});")

    def rrsp(self, data, result, error, last):
        self.emit(f"  rrsp_send({self.db}'h{data:0{self.db // 4}X}, 2'd{result}, {error}, {last});")

    def wait_b(self, note):
        self.emit(f"  wait_b({note});")

    def wait_r(self, note):
        self.emit(f"  wait_r({note});")

    def data_pattern(self, burst_tag, beat):
        value = (0xA5000000 + (burst_tag << 16) + beat) & 0xFFFFFFFF
        if self.db >= 64:
            value |= ((0x5A000000 + (burst_tag << 12) + beat) & 0xFFFFFFFF) << 32
        for word in range(2, self.db // 32):
            value |= ((0xD3000000 + (word << 20) + (burst_tag << 12) + beat) & 0xFFFFFFFF) << (32 * word)
        return value

    def write_burst(self, tag, addr, length, size, burst, cache, prot, ident, result,
                    wrsp_error=0, wrsp_first=1, wrsp_last=1, be_mode="full", ready_mode=0,
                    expect_resp=None, lock=0):
        """Emits one complete admitted write burst: expectations, AW, beats, response."""
        count = 1 << size
        beats = length + 1
        # Fragment expectations (oracle): INCR/WRAP/FIXED per-beat container bases.
        addrs = []
        base = addr & ~(count - 1)
        region = beats * count if burst == 2 else 0
        for k in range(beats):
            if burst == 0:
                addrs.append(base)
            elif burst == 2:
                # WRAP: advance inside the region, folding at the top.
                prev = addrs[-1] if addrs else base
                if not addrs:
                    addrs.append(base)
                    continue
                nxt = prev + count
                wrap_base = addr & ~(region - 1)
                addrs.append(wrap_base if nxt % region == 0 and nxt > wrap_base else nxt)
            else:
                addrs.append(base + k * count)
        for k in range(beats):
            be = ((1 << self.word) - 1 if be_mode == "full" else
                  (1 << (self.word - 1) if k % 2 else 0) if be_mode == "top" else
                  ((0x5 + k) % ((1 << self.word) - 1)) + 1)
            self.xpush_w(addrs[k], length, size, burst, cache, prot, ident, count,
                         1 if k == 0 else 0, 1 if k == beats - 1 else 0,
                         self.data_pattern(tag, k), be)
        self.xpush_b(expect_resp if expect_resp is not None else {0: 0, 1: 3}.get(result, 2), ident)
        self.emit(f"  wrq_mode = {ready_mode};")
        self.aw(addr, length, size, burst, lock, cache, prot, ident)
        for k in range(beats):
            be = ((1 << self.word) - 1 if be_mode == "full" else
                  (1 << (self.word - 1) if k % 2 else 0) if be_mode == "top" else
                  ((0x5 + k) % ((1 << self.word) - 1)) + 1)
            self.w(self.data_pattern(tag, k), be, 1 if k == beats - 1 else 0)
        self.wrsp(result, wrsp_error, wrsp_first, wrsp_last)
        self.wait_b(1)

    def build(self):
        self.phases()
        body = "\n".join(self.lines)
        return TEMPLATE \
            .replace("@DB@", str(self.db)) \
            .replace("@DW@", str(self.db - 1)) \
            .replace("@WB@", str(self.word)) \
            .replace("@SW@", str(self.word - 1)) \
            .replace("@LB@", str(self.lb)) \
            .replace("@LW@", str(self.lb - 1)) \
            .replace("@IDB@", str(max(self.idb, 1))) \
            .replace("@HAS_ID@", "1" if self.idb else "0") \
            .replace("@WQ_TOTAL@", str(self.wq_total)) \
            .replace("@RQ_TOTAL@", str(self.rq_total)) \
            .replace("@B_TOTAL@", str(self.b_total)) \
            .replace("@R_TOTAL@", str(self.r_total)) \
            .replace("@W_EXTRA@", str(self.w_extra)) \
            .replace("@W_TIGHT_PAIRS@", str(self.w_tight_pairs)) \
            .replace("@PHASES@", body)

    # ------------------------------------------------------------------
    def phases(self):
        full = self.sz_full
        word = self.word
        emit = self.emit
        emit("  // ---- Phase W1: aligned full-width INCR, toggling network ready ----")
        self.write_burst(1, 0x1000, 3, full, 1, 0x3, 0b010, 5 % (1 << max(self.idb, 4)), 0,
                         ready_mode=1)
        emit("  // ---- Phase W2: narrow unaligned INCR, target error -> SLVERR ----")
        self.write_burst(2, 0x803, 5, 1, 1, 0x0, 0b111, 9 % (1 << max(self.idb, 4)), 2,
                         be_mode="sparse")
        if self.db == 64:
            emit("  // ---- Phase W2b: the mid narrow size on a 64-bit bus ----")
            self.write_burst(9, 0x0902, 2, 2, 1, 0x6, 0b100, 10, 0, be_mode="sparse")
        emit("  // ---- Phase R2b: WRAP and FIXED reads carry the descriptor verbatim ----")
        self.read_burst(14, 0x0904, 3, 2, 2, 0x1, 0b011, 11, [0, 0, 0, 0], [0, 0, 0, 0])
        self.read_burst(15, 0x098C, 2, 2, 0, 0x2, 0b101, 0, [0, 0, 0], [0, 0, 0])
        emit("  // ---- Phase W3: WRAP around the region, network miss -> DECERR ----")
        if self.db == 32:
            self.write_burst(3, 0x408, 3, 2, 2, 0xF, 0b000, 3, 1)
        else:
            # Four beats so the wrap fold has non-trivial middle beats (a
            # two-beat wrap cannot distinguish a stuck wrap turn).
            self.write_burst(3, 0x410, 3, 3, 2, 0xF, 0b000, 3, 1)
        emit("  // ---- Phase W4: FIXED burst, junk response sideband fields ----")
        self.write_burst(4, 0x331, 2, 0, 0, 0x5, 0b101, 4, 0, be_mode="sparse")
        emit("  // ---- Phase W5: longest burst, exact 4 KiB fit, stalled network ----")
        if self.lb == 8:
            self.write_burst(5, 0x0F00, 255, 0, 1, 0x0, 0b000, 1, 0, be_mode="sparse",
                             ready_mode=2)
        else:
            self.write_burst(5, 0x0FF0, 15, 0, 1, 0x0, 0b000, 1, 0, be_mode="sparse",
                             ready_mode=2)
        if self.db == 128:
            emit("  // Full-width boundary, high lanes, and highest valid WRAP size.")
            self.write_burst(16, 0x1000, 255, 4, 1, 0, 0, 1, 0, ready_mode=2)
            self.write_burst(17, 0x2C30, 3, 4, 2, 0, 0, 2, 0, be_mode="top")
            self.write_burst(18, 0x2D08, 3, 3, 1, 0, 0, 3, 0, be_mode="sparse")
            self.read_burst(19, 0x2000, 255, 4, 1, 0, 0, 4, [0] * 256, [0] * 256,
                            rready_mode=2, rrsp_gaps=1)
            for addr, length, size, kind in [(0xFF0, 1, 4, 1), (0x410, 1, 5, 2),
                                             (0x418, 1, 4, 2)]:
                self.violation_write("wide-boundary", addr, length, size, kind, 0)
                self.violation_read("wide-boundary", addr, length, size, kind, 0)
        emit("  // ---- Phase W6: WLAST early (beat 1 of 4) then correct; B = SLVERR ----")
        addrs = [0x2000 + k * word for k in range(4)]
        for k in range(4):
            self.xpush_w(addrs[k], 3, full, 1, 0x0, 0b000, 6 % (1 << max(self.idb, 4)), word,
                         1 if k == 0 else 0, 1 if k == 3 else 0, self.data_pattern(6, k),
                         (1 << word) - 1)
        self.xpush_b(2, 6 % (1 << max(self.idb, 4)))
        self.aw(0x2000, 3, full, 1, 0, 0x0, 0b000, 6 % (1 << max(self.idb, 4)))
        self.w(self.data_pattern(6, 0), (1 << word) - 1, 0)
        self.w(self.data_pattern(6, 1), (1 << word) - 1, 1)  # violation: early WLAST
        self.w(self.data_pattern(6, 2), (1 << word) - 1, 0)
        self.w(self.data_pattern(6, 3), (1 << word) - 1, 1)
        self.wrsp(0)
        self.wait_b(1)
        emit("  // ---- Phase W7: WLAST never asserted; B = SLVERR ----")
        for k in range(2):
            self.xpush_w(0x2400 + k * word, 1, full, 1, 0x0, 0b000, 7 % (1 << max(self.idb, 4)),
                         word, 1 if k == 0 else 0, 1 if k == 1 else 0, self.data_pattern(7, k),
                         (1 << word) - 1)
        self.xpush_b(2, 7 % (1 << max(self.idb, 4)))
        self.aw(0x2400, 1, full, 1, 0, 0x0, 0b000, 7 % (1 << max(self.idb, 4)))
        self.w(self.data_pattern(7, 0), (1 << word) - 1, 0)
        self.w(self.data_pattern(7, 1), (1 << word) - 1, 0)
        self.wrsp(0)
        self.wait_b(1)
        emit("  // ---- Phase W8: FIXED with the maximum legal length (16 beats) ----")
        if self.lb == 8:
            self.write_burst(8, 0x0700, 15, 0, 0, 0x0, 0b000, 2, 0, be_mode="sparse")
        emit("  // ---- Phase WS: W stream runs ahead of AW (W leads its AW) ----")
        # Rejected bursts sit between admitted ones: a 4 KiB-crossing INCR
        # (multi-beat) and an illegal single-beat WRAP. Their beats are
        # absorbed at the face (SLVERR, no fragment) and the next burst's beat
        # 0 must still wait for its own AW.
        cross = 0x7000 - word
        stream = [(0x20, 0x6100, 3, 1), (0x26, cross, 2, 1), (0x21, 0x6200, 0, 1),
                  (0x22, 0x6300, 2, 1), (0x27, 0x6700, 0, 2), (0x23, 0x6400, 0, 1),
                  (0x24, 0x6500, 1, 1), (0x25, 0x6600, 0, 1)]
        self.write_stream(stream, ready_mode=0)
        # 0x1000 keeps the INCR at the next 4 KiB boundary.
        self.write_stream([(tag + 8, addr + 0x1000, length, burst)
                           for tag, addr, length, burst in stream], ready_mode=2)
        emit("  // ---- Phase X: AxLOCK without an exclusive window is a normal write ----")
        self.write_burst(14, 0x3000, 2, full, 1, 0x0, 0b000, 0xE % (1 << max(self.idb, 4)), 0,
                         lock=1)
        emit("  // ---- Phase V: admission violations, SLVERR without the network ----")
        cross_addr, cross_size, cross_len = (0x0FF4, 2, 3) if self.db == 32 else (0x0FF8, 3, 1)
        self.violation_write("4k", cross_addr, cross_len, cross_size, 1, 0)
        self.violation_write("wraplen", 0x0400, 2, 1, 2, 0)
        self.violation_write("wrapalign", 0x0402, 1, 2, 2, 0)
        self.violation_write("size", 0x0500, 0, full + 1, 1, 0)
        self.violation_write("burst", 0x0600, 1, 1, 3, 0)
        if self.lb == 8:
            self.violation_write("fixed16", 0x0700, 16, 0, 0, 0)
        emit("  // ---- Phase R1: full-width INCR read, mid-burst SLVERR beat ----")
        self.read_burst(11, 0x2000, 3, full, 1, 0x5, 0b001, 7 % (1 << max(self.idb, 4)),
                        [0, 0, 2, 0], [0, 0, 0, 0], rready_mode=1)
        emit("  // ---- Phase R2: narrow unaligned read, DECERR and error fold ----")
        self.read_burst(12, 0x0403, 2, 1, 1, 0x0, 0b010, 8 % (1 << max(self.idb, 4)),
                        [1, 0, 0], [0, 1, 0])
        emit("  // ---- Phase R3: read under response and consumer jitter ----")
        self.read_burst(13, 0x3000, 7, full, 1, 0x2, 0b000, 1, [0] * 8, [0] * 8,
                        rready_mode=2, rrsp_gaps=1)
        emit("  // ---- Phase RX: AxLOCK without an exclusive window is a normal read ----")
        self.read_burst(15, 0x3000, 2, full, 1, 0x0, 0b000, 0xD % (1 << max(self.idb, 4)),
                        [0, 0, 0], [0, 0, 0], lock=1)
        emit("  // ---- Phase RV: read admission violations ----")
        self.violation_read("4k", cross_addr, cross_len, cross_size, 1, 0)
        emit("  // ---- Phase OV: read and write bursts overlap ----")
        self.overlap()
        emit("  // ---- Phase RST: mid-burst resets cancel in-flight work ----")
        self.reset_phase()

    def write_stream(self, bursts, ready_mode):
        """W beats of every burst are queued as one stream that runs ahead of AW.

        The stream presents beat 0 of burst k+1 while burst k is still draining
        and holds it across the B handshake and an idle gap; AW k+1 comes later.
        Each entry is (tag, addr, length, burst), full width. Admitted bursts
        complete OKAY through the network; a rejected burst (4 KiB-crossing
        INCR or illegal WRAP) has its length + 1 beats absorbed at the face,
        completes SLVERR and emits no fragment. With ready_mode 0 (network
        always ready) the beats inside one burst must be taken back to back.
        """
        word = self.word
        full = self.sz_full
        ident = 0xB % (1 << max(self.idb, 4))

        def admitted(addr, length, burst):
            if burst == 2:
                return length in (1, 3, 7, 15) and addr % word == 0
            return burst == 1 and addr % 4096 + (length + 1) * word <= 4096

        for tag, addr, length, burst in bursts:
            if not admitted(addr, length, burst):
                self.xpush_b(2, ident)
                self.w_extra += length + 1  # absorbed locally, never on the network
                continue
            assert burst == 1, "the stream oracle models admitted INCR bursts only"
            for k in range(length + 1):
                self.xpush_w(addr + k * word, length, full, 1, 0x0, 0b000, ident, word,
                             1 if k == 0 else 0, 1 if k == length else 0,
                             self.data_pattern(tag, k), (1 << word) - 1)
            self.xpush_b(0, ident)
        if ready_mode == 0:
            self.w_tight_pairs += sum(length for _, _, length, _ in bursts)
        self.emit("  ws_n = 0; ws_r = 0;")
        for tag, addr, length, burst in bursts:
            for k in range(length + 1):
                self.emit(f"  ws_push({self.db}'h{self.data_pattern(tag, k):0{self.db // 4}X}, "
                          f"{word}'h{(1 << word) - 1:X}, {1 if k == length else 0});")
        self.emit(f"  wrq_mode = {ready_mode}; ws_on = 1; ws_tight = {1 if ready_mode == 0 else 0};")
        self.emit("  step(); step(); step();")  # W leads the first AW: held, not taken
        for tag, addr, length, burst in bursts:
            self.emit("  mark_wq = wq_seen;")
            self.aw(addr, length, full, burst, 0, 0x0, 0b000, ident)
            if admitted(addr, length, burst):
                self.wrsp(0)
                self.wait_b(1)
            else:
                self.wait_b(1)
                self.emit("  if (wq_seen != mark_wq) fail(\"AXI4I.NO_TOUCH\");")
            self.emit("  step(); step(); step();")  # next burst's beat 0 is held, not taken
        self.emit("  if (ws_r != ws_n) fail(\"AXI4I.W_AHEAD_OF_AW\");")
        self.emit("  ws_on = 0; ws_tight = 0; wvalid = 0; wlast = 0; wrq_mode = 0;")

    def violation_write(self, tag, addr, length, size, burst, lock):
        ident = 0xE % (1 << max(self.idb, 4))
        self.emit(f"  // violation {tag}: SLVERR, beats absorbed, zero fragments")
        self.xpush_b(2, ident)
        self.emit("  mark_wq = wq_seen; mark_rq = rq_seen;")
        self.aw(addr, length, size, burst, lock, 0x0, 0b000, ident)
        self.w_extra += length + 1  # absorbed locally, never on the network
        for k in range(length + 1):
            self.w(self.data_pattern(0xE, k), (1 << self.word) - 1, 1 if k == length else 0)
        self.wait_b(1)
        self.emit("  if (wq_seen != mark_wq || rq_seen != mark_rq) fail(\"AXI4I.NO_TOUCH\");")

    def violation_read(self, tag, addr, length, size, burst, lock):
        ident = 0xD % (1 << max(self.idb, 4))
        self.emit(f"  // read violation {tag}: N SLVERR beats, zero fragments")
        for k in range(length + 1):
            self.xpush_r(0, 2, 1 if k == length else 0, ident)
        self.emit("  mark_wq = wq_seen; mark_rq = rq_seen;")
        self.ar(addr, length, size, burst, lock, 0x0, 0b000, ident)
        for _ in range(length + 1):
            self.wait_r(1)
        self.emit("  if (wq_seen != mark_wq || rq_seen != mark_rq) fail(\"AXI4I.NO_TOUCH\");")

    def read_burst(self, tag, addr, length, size, burst, cache, prot, ident, results, errors,
                   rready_mode=0, rrsp_gaps=0, lock=0):
        count = 1 << size
        base = addr & ~(count - 1)
        self.xpush_rq(base, length, size, burst, cache, prot, ident, count)
        for k in range(length + 1):
            resp = 3 if results[k] == 1 else (2 if (results[k] != 0 or errors[k]) else 0)
            self.xpush_r(self.data_pattern(tag, k), resp, 1 if k == length else 0, ident)
        self.ar(addr, length, size, burst, lock, cache, prot, ident)
        self.emit(f"  rready_mode = {rready_mode};")
        for k in range(length + 1):
            if rrsp_gaps and k % 3 == 1:
                self.emit("  step(); step();")
            self.rrsp(self.data_pattern(tag, k), results[k], errors[k], 1 if k == length else 0)
        for _ in range(length + 1):
            self.wait_r(1)
        self.emit("  rready_mode = 0;")

    def overlap(self):
        word = self.word
        full = self.sz_full
        wid = 3 % (1 << max(self.idb, 4))
        rid = 12 % (1 << max(self.idb, 4))
        for k in range(3):
            self.xpush_w(0x4000 + k * word, 2, full, 1, 0x0, 0b000, wid, word,
                         1 if k == 0 else 0, 1 if k == 2 else 0, self.data_pattern(0xA, k),
                         (1 << word) - 1)
        self.xpush_b(0, wid)
        self.xpush_rq(0x4402, 1, 1, 1, 0x0, 0b000, rid, 2)
        for k in range(2):
            self.xpush_r(self.data_pattern(0xB, k), 0, 1 if k == 1 else 0, rid)
        # Follow-up single-beat write (blocked while the first is in flight).
        self.xpush_w(0x4800, 0, full, 1, 0x0, 0b000, (4 % (1 << max(self.idb, 4))), word, 1, 1,
                     self.data_pattern(0xC, 0), (1 << word) - 1)
        self.xpush_b(0, 4 % (1 << max(self.idb, 4)))
        self.xpush_rq(0x4C00, 0, 1, 1, 0x0, 0b000, (13 % (1 << max(self.idb, 4))), 2)
        self.xpush_r(self.data_pattern(0xD, 0), 0, 1, 13 % (1 << max(self.idb, 4)))
        emit = self.emit
        emit(f"  aw_send(32'h0000_4000, {self.lb}'d2, 3'd{full}, 2'd1, 0, 4'h0, 3'b000, "
             f"{max(self.idb, 1)}'d{wid});")
        # Beat 0 of the write while the read AR is presented: both directions
        # are captured in the same window.
        emit(f"  wvalid = 1; wdata = {self.db}'h{self.data_pattern(0xA, 0):0{self.db // 4}X}; "
             f"wstrb = {word}'h{(1 << word) - 1:X}; wlast = 0;")
        emit(f"  ar_done = 0; arvalid = 1; araddr = 32'h0000_4402; arlen = {self.lb}'d1; "
             f"arsize = 3'd1; arburst = 2'd1; arlock = 0; arcache = 4'h0; arprot = 3'b000; "
             f"arid = {max(self.idb, 1)}'d{rid};")
        emit("  w_done = 0; guard = 0;")
        emit("  while (!(w_done && ar_done)) begin step(); guard = guard + 1;")
        emit("    if (guard > 500) fail(\"AXI4I.HOLD_RELEASE\"); end")
        emit("  wvalid = 0; arvalid = 0;")
        # The follow-up write's AW is presented now and must stay blocked for
        # the rest of the in-flight burst.
        emit(f"  aw_done = 0; awvalid = 1; awaddr = 32'h0000_4800; awlen = {self.lb}'d0; awsize = 3'd{full}; "
             f"awburst = 2'd1; awlock = 0; awcache = 4'h0; awprot = 3'b000; "
             f"awid = {max(self.idb, 1)}'d{4 % (1 << max(self.idb, 4))};")
        emit("  expect_aw_block = 1;")
        self.w(self.data_pattern(0xA, 1), (1 << word) - 1, 0)
        self.w(self.data_pattern(0xA, 2), (1 << word) - 1, 1)
        self.wrsp(0)
        self.wait_b(1)  # first write B; window covered the whole burst
        emit("  expect_aw_block = 0;")
        emit("  guard = 0;")
        emit("  while (!aw_done) begin step(); guard = guard + 1;")
        emit("    if (guard > 500) fail(\"AXI4I.HOLD_RELEASE\"); end")
        emit("  awvalid = 0;")
        # The follow-up write fragment pends against a stalled network while
        # the read burst streams: both directions live in the same window. Its
        # W beat passes through, so it waits on the W face until the network takes it.
        emit(f"  ar_done = 0; arvalid = 1; araddr = 32'h0000_4C00; arlen = {self.lb}'d0; "
             f"arsize = 3'd1; arburst = 2'd1; arlock = 0; arcache = 4'h0; arprot = 3'b000; "
             f"arid = {max(self.idb, 1)}'d{13 % (1 << max(self.idb, 4))};")
        emit("  expect_ar_block = 1;")
        emit("  wrq_mode = 3;")
        emit(f"  w_done = 0; wvalid = 1; wdata = {self.db}'h{self.data_pattern(0xC, 0):0{self.db // 4}X}; "
             f"wstrb = {word}'h{(1 << word) - 1:X}; wlast = 1;")
        self.rrsp(self.data_pattern(0xB, 0), 0, 0, 0)
        self.rrsp(self.data_pattern(0xB, 1), 0, 0, 1)
        self.wait_r(1)
        self.wait_r(1)
        emit("  expect_ar_block = 0;")
        emit("  guard = 0;")
        emit("  while (!ar_done) begin step(); guard = guard + 1;")
        emit("    if (guard > 500) fail(\"AXI4I.HOLD_RELEASE\"); end")
        emit("  arvalid = 0;")
        emit("  if (!overlap_seen) fail(\"AXI4I.OVERLAP\");")
        emit("  if (w_done) fail(\"AXI4I.HOLD_RELEASE\");")
        emit("  wrq_mode = 0;")
        emit("  guard = 0;")
        emit("  while (!w_done) begin step(); guard = guard + 1;")
        emit("    if (guard > 500) fail(\"AXI4I.HOLD_RELEASE\"); end")
        emit("  wvalid = 0; wlast = 0;")
        self.wrsp(0)
        self.wait_b(1)
        self.rrsp(self.data_pattern(0xD, 0), 0, 0, 1)
        self.wait_r(1)

    def reset_phase(self):
        word = self.word
        full = self.sz_full
        emit = self.emit
        wid = 2 % (1 << max(self.idb, 4))
        # Write burst cancelled after its first fragment is on the wire.
        for k in range(4):
            self.xpush_w(0x5000 + k * word, 3, full, 1, 0x0, 0b000, wid, word,
                         1 if k == 0 else 0, 1 if k == 3 else 0, self.data_pattern(0x5, k),
                         (1 << word) - 1)
        self.xpush_b(0, wid)
        # Only the first fragment transfers before the reset; the B never comes.
        # Beat 1 waits on the W face against the stalled network and is never taken.
        self.wq_total -= 3
        self.b_total -= 1
        emit("  mark_wq = wq_seen;")
        self.aw(0x5000, 3, full, 1, 0, 0x0, 0b000, wid)
        emit("  wrq_mode = 0;")
        self.w(self.data_pattern(0x5, 0), (1 << word) - 1, 0)
        emit("  wait_wq(mark_wq + 1);")  # the first fragment is out; the rest never comes
        emit(f"  wvalid = 1; wdata = {self.db}'h{self.data_pattern(0x5, 1):0{self.db // 4}X}; "
             f"wstrb = {word}'h{(1 << word) - 1:X}; wlast = 0;")
        emit("  wrq_mode = 3;")  # network stalls: the burst pends mid-packet
        emit("  step(); step();")
        emit("  reset_n = 0; step(); step();")
        emit("  if (nl_wreq_valid || bvalid || rvalid || awready || wready || arready || "
             "nl_wrsp_ready || nl_rrsp_ready || nl_rreq_valid) fail(\"AXI4I.RESET\");")
        emit("  reset_n = 1; step(); step();")
        emit("  if (wq_seen != mark_wq + 1) fail(\"AXI4I.RESET\");")  # no stale fragment
        emit("  wvalid = 0; wrq_mode = 0; fifo_drain();")
        # Clean burst after the reset.
        self.xpush_w(0x5800, 0, full, 1, 0x0, 0b000, wid, word, 1, 1, self.data_pattern(0x6, 0),
                     (1 << word) - 1)
        self.xpush_b(0, wid)
        self.aw(0x5800, 0, full, 1, 0, 0x0, 0b000, wid)
        self.w(self.data_pattern(0x6, 0), (1 << word) - 1, 1)
        self.wrsp(0)
        self.wait_b(1)
        # Read burst cancelled mid response stream.
        rid2 = 14 % (1 << max(self.idb, 4))
        self.xpush_rq(0x6000, 2, full, 1, 0x0, 0b000, rid2, word)
        for k in range(3):
            self.xpush_r(self.data_pattern(0x7, k), 0, 1 if k == 2 else 0, rid2)
        emit("  mark_rq = r_seen;")
        self.ar(0x6000, 2, full, 1, 0, 0x0, 0b000, rid2)
        self.rrsp(self.data_pattern(0x7, 0), 0, 0, 0)
        # Only the first response beat is consumed before the reset.
        self.r_total -= 2
        self.wait_r(1)
        emit("  reset_n = 0; step(); step();")
        emit("  if (nl_wreq_valid || bvalid || rvalid || awready || wready || arready || "
             "nl_rrsp_ready || nl_rreq_valid) fail(\"AXI4I.RESET\");")
        emit("  reset_n = 1; step();")
        emit("  if (r_seen != mark_rq + 1) fail(\"AXI4I.RESET\");")  # no stale beat
        emit("  fifo_drain();")
        # Clean read after the reset.
        self.xpush_rq(0x6800, 0, full, 1, 0x0, 0b000, rid2, word)
        self.xpush_r(self.data_pattern(0x8, 0), 0, 1, rid2)
        self.ar(0x6800, 0, full, 1, 0, 0x0, 0b000, rid2)
        self.rrsp(self.data_pattern(0x8, 0), 0, 0, 1)
        self.wait_r(1)


TEMPLATE = r'''`timescale 1ns/1ps
module tb;
reg clk = 0, reset_n = 1;
always #5 clk = ~clk;
integer cycle = 0, guard = 0;
integer mark_wq = 0, mark_rq = 0;

// ---------------- AXI slave face ----------------
reg [@IDB@-1:0] awid = 0;
reg [31:0] awaddr = 0;
reg [@LB@-1:0] awlen = 0;
reg [2:0] awsize = 0;
reg [1:0] awburst = 0;
reg awlock = 0;
reg [3:0] awcache = 0;
reg [2:0] awprot = 0;
reg awvalid = 0;
wire awready;
reg [@DB@-1:0] wdata = 0;
reg [@WB@-1:0] wstrb = 0;
reg wlast = 0;
reg wvalid = 0;
wire wready;
wire [@IDB@-1:0] bid;
wire [1:0] bresp;
wire bvalid;
reg bready = 0;
reg [@IDB@-1:0] arid = 0;
reg [31:0] araddr = 0;
reg [@LB@-1:0] arlen = 0;
reg [2:0] arsize = 0;
reg [1:0] arburst = 0;
reg arlock = 0;
reg [3:0] arcache = 0;
reg [2:0] arprot = 0;
reg arvalid = 0;
wire arready;
wire [@IDB@-1:0] rid;
wire [@DB@-1:0] rdata;
wire [1:0] rresp;
wire rlast;
wire rvalid;
reg rready = 0;

// ---------------- Weft faces ----------------
wire nl_wreq_valid;
reg nl_wreq_ready = 0;
@WREQ_WIRES@
reg nl_wrsp_valid = 0;
wire nl_wrsp_ready;
@WRSP_REGS@
wire nl_rreq_valid;
reg nl_rreq_ready = 1;
@RREQ_WIRES@
reg nl_rrsp_valid = 0;
wire nl_rrsp_ready;
@RRSP_REGS@

nocloom_axi4_initiator_probe dut(
  .nl_clk(clk), .nl_reset_n(reset_n),
  .nl_axi_awid(awid), .nl_axi_awaddr(awaddr), .nl_axi_awlen(awlen),
  .nl_axi_awsize(awsize), .nl_axi_awburst(awburst), .nl_axi_awlock(awlock),
  .nl_axi_awcache(awcache), .nl_axi_awprot(awprot), .nl_axi_awvalid(awvalid),
  .nl_axi_awready(awready),
  .nl_axi_wdata(wdata), .nl_axi_wstrb(wstrb), .nl_axi_wlast(wlast),
  .nl_axi_wvalid(wvalid), .nl_axi_wready(wready),
  .nl_axi_bid(bid), .nl_axi_bresp(bresp), .nl_axi_bvalid(bvalid), .nl_axi_bready(bready),
  .nl_axi_arid(arid), .nl_axi_araddr(araddr), .nl_axi_arlen(arlen),
  .nl_axi_arsize(arsize), .nl_axi_arburst(arburst), .nl_axi_arlock(arlock),
  .nl_axi_arcache(arcache), .nl_axi_arprot(arprot), .nl_axi_arvalid(arvalid),
  .nl_axi_arready(arready),
  .nl_axi_rid(rid), .nl_axi_rdata(rdata), .nl_axi_rresp(rresp), .nl_axi_rlast(rlast),
  .nl_axi_rvalid(rvalid), .nl_axi_rready(rready),
  .nl_wreq_valid(nl_wreq_valid), .nl_wreq_ready(nl_wreq_ready),
@WREQ_CONNS@
  .nl_wrsp_valid(nl_wrsp_valid), .nl_wrsp_ready(nl_wrsp_ready),
@WRSP_CONNS@
  .nl_rreq_valid(nl_rreq_valid), .nl_rreq_ready(nl_rreq_ready),
@RREQ_CONNS@
  .nl_rrsp_valid(nl_rrsp_valid), .nl_rrsp_ready(nl_rrsp_ready),
@RRSP_CONNS@
);

// ---------------- expectation FIFOs ----------------
reg [31:0] xq_addr [0:8191]; reg [@LW@:0] xq_len [0:8191]; reg [2:0] xq_size [0:8191];
reg [1:0] xq_burst [0:8191]; reg [3:0] xq_cache [0:8191]; reg [2:0] xq_prot [0:8191];
reg [@IDB@-1:0] xq_id [0:8191]; reg [5:0] xq_count [0:8191];
reg xq_first [0:8191]; reg xq_last [0:8191];
reg [@DW@:0] xq_data [0:8191]; reg [@SW@:0] xq_be [0:8191];
integer xq_n = 0, xq_r = 0;
reg [31:0] rq_addr [0:1023]; reg [@LW@:0] rq_len [0:1023]; reg [2:0] rq_size [0:1023];
reg [1:0] rq_burst [0:1023]; reg [3:0] rq_cache [0:1023]; reg [2:0] rq_prot [0:1023];
reg [@IDB@-1:0] rq_id [0:1023]; reg [5:0] rq_count [0:1023];
integer rq_n = 0, rq_r = 0;
reg [1:0] bq_resp [0:1023]; reg [@IDB@-1:0] bq_id [0:1023];
integer bq_n = 0, bq_r = 0;
reg [@DW@:0] rq2_data [0:1023]; reg [1:0] rq2_resp [0:1023]; reg rq2_last [0:1023];
reg [@IDB@-1:0] rq2_id [0:1023];
integer rq2_n = 0, rq2_r = 0;

integer wq_seen = 0, rq_seen = 0, b_seen = 0, r_seen = 0;
reg w_done = 0, aw_done = 0, ar_done = 0, expect_aw_block = 0, expect_ar_block = 0;
integer b_total_mark = 0, r_total_mark = 0;
reg wrsp_done = 0, rrsp_done = 0;
reg [1:0] wrq_mode = 0, rready_mode = 0;
reg overlap_seen = 0;
// W stream driver: beats run ahead of their AW (W may lead AW in AXI).
reg [@DW@:0] ws_data [0:63]; reg [@SW@:0] ws_be [0:63]; reg ws_last [0:63];
integer ws_n = 0, ws_r = 0;
reg ws_on = 0;
// Throughput: with ws_tight set (stream on, network always ready) beat k+1
// of a burst is taken in the cycle right after beat k.
reg ws_tight = 0;
integer w_beats = 0, w_prev_cycle = 0, w_tight_pairs = 0;
// W/AW ordering monitor: the beats a burst may still accept, and the total
// number of accepted W beats (each must become exactly one fragment, except
// the beats the bench names as absorbed or cancelled).
integer w_budget = 0, w_accepted = 0;

// Stall-stability snapshots.
reg stall_active = 0;
reg [31:0] snap_addr = 0; reg [@LW@:0] snap_len = 0; reg [2:0] snap_size = 0;
reg [1:0] snap_burst = 0; reg [3:0] snap_cache = 0; reg [2:0] snap_prot = 0;
reg [@IDB@-1:0] snap_id = 0; reg [5:0] snap_count = 0;
reg snap_first = 0, snap_last = 0; reg [@DW@:0] snap_data = 0; reg [@SW@:0] snap_be = 0;
reg r_stall = 0; reg [@DW@:0] r_snap_data = 0; reg [1:0] r_snap_resp = 0; reg r_snap_last = 0;
reg [@IDB@-1:0] r_snap_id = 0;
reg b_stall = 0; reg [1:0] b_snap_resp = 0; reg [@IDB@-1:0] b_snap_id = 0;

task fail(input [8*64-1:0] name);
  begin $display("ASSERT_FAIL:%0s", name); $fatal(1); end
endtask

task tick;
  begin @(posedge clk); #1; @(negedge clk); #1; cycle = cycle + 1; end
endtask

// Bit mask of the strobed byte lanes: the source clears unstrobed bytes.
function [@DW@:0] strobe_lanes(input [@SW@:0] be);
  integer q;
  begin
    for (q = 0; q < @WB@; q = q + 1) strobe_lanes[8*q +: 8] = {8{be[q]}};
  end
endfunction
task xpush_w(input [31:0] addr, input [@LW@:0] length, input [2:0] size,
             input [1:0] burst, input [3:0] cache, input [2:0] prot,
             input [@IDB@-1:0] ident, input [5:0] count, input first, input last,
             input [@DW@:0] data, input [@SW@:0] be);
  begin
    xq_addr[xq_n] = addr; xq_len[xq_n] = length; xq_size[xq_n] = size;
    xq_burst[xq_n] = burst; xq_cache[xq_n] = cache; xq_prot[xq_n] = prot;
    xq_id[xq_n] = ident; xq_count[xq_n] = count; xq_first[xq_n] = first;
    xq_last[xq_n] = last; xq_data[xq_n] = data & strobe_lanes(be); xq_be[xq_n] = be; xq_n = xq_n + 1;
  end
endtask
task xpush_rq(input [31:0] addr, input [@LW@:0] length, input [2:0] size,
              input [1:0] burst, input [3:0] cache, input [2:0] prot,
              input [@IDB@-1:0] ident, input [5:0] count);
  begin
    rq_addr[rq_n] = addr; rq_len[rq_n] = length; rq_size[rq_n] = size;
    rq_burst[rq_n] = burst; rq_cache[rq_n] = cache; rq_prot[rq_n] = prot;
    rq_id[rq_n] = ident; rq_count[rq_n] = count; rq_n = rq_n + 1;
  end
endtask
task xpush_b(input [1:0] resp, input [@IDB@-1:0] ident);
  begin bq_resp[bq_n] = resp; bq_id[bq_n] = ident; bq_n = bq_n + 1; end
endtask
task xpush_r(input [@DW@:0] data, input [1:0] resp, input last, input [@IDB@-1:0] ident);
  begin
    rq2_data[rq2_n] = data; rq2_resp[rq2_n] = resp; rq2_last[rq2_n] = last;
    rq2_id[rq2_n] = ident; rq2_n = rq2_n + 1;
  end
endtask
task ws_push(input [@DW@:0] data, input [@SW@:0] be, input last);
  begin ws_data[ws_n] = data; ws_be[ws_n] = be; ws_last[ws_n] = last; ws_n = ws_n + 1; end
endtask
task fifo_drain;
  begin xq_r = xq_n; rq_r = rq_n; bq_r = bq_n; rq2_r = rq2_n; end
endtask

task aw_send(input [31:0] addr, input [@LW@:0] length, input [2:0] size,
             input [1:0] burst, input lock, input [3:0] cache, input [2:0] prot,
             input [@IDB@-1:0] ident);
  begin
    awvalid = 1; awaddr = addr; awlen = length; awsize = size; awburst = burst;
    awlock = lock; awcache = cache; awprot = prot; awid = ident;
    aw_done = 0; guard = 0;
    while (!aw_done) begin
      step(); guard = guard + 1;
      if (guard > 500) fail("AXI4I.HOLD_RELEASE");
    end
    awvalid = 0;
  end
endtask
task w_send(input [@DW@:0] data, input [@SW@:0] be, input last);
  begin
    wvalid = 1; wdata = data; wstrb = be; wlast = last;
    w_done = 0; guard = 0;
    while (!w_done) begin
      step(); guard = guard + 1;
      if (guard > 500) fail("AXI4I.HOLD_RELEASE");
    end
    wvalid = 0; wlast = 0;
  end
endtask
task wrsp_send(input [1:0] result, input error, input first, input last);
  begin
    nl_wrsp_valid = 1; nl_wrsp_result = result; nl_wrsp_error = error;
    nl_wrsp_first = first; nl_wrsp_last = last;
    nl_wrsp_address = 32'hDEAD_0000 + cycle; nl_wrsp_count = 6'h3F;
    nl_wrsp_data = {@DB@{1'b1}}; @WRSP_JUNK_ID@
    wrsp_done = 0; guard = 0;
    while (!wrsp_done) begin
      step(); guard = guard + 1;
      if (guard > 500) fail("AXI4I.HOLD_RELEASE");
    end
    nl_wrsp_valid = 0;
  end
endtask
task ar_send(input [31:0] addr, input [@LW@:0] length, input [2:0] size,
             input [1:0] burst, input lock, input [3:0] cache, input [2:0] prot,
             input [@IDB@-1:0] ident);
  begin
    arvalid = 1; araddr = addr; arlen = length; arsize = size; arburst = burst;
    arlock = lock; arcache = cache; arprot = prot; arid = ident;
    ar_done = 0; guard = 0;
    while (!ar_done) begin
      step(); guard = guard + 1;
      if (guard > 500) fail("AXI4I.HOLD_RELEASE");
    end
    arvalid = 0;
  end
endtask
task rrsp_send(input [@DW@:0] data, input [1:0] result, input error, input last);
  begin
    nl_rrsp_valid = 1; nl_rrsp_data = data; nl_rrsp_result = result;
    nl_rrsp_error = error; nl_rrsp_last = last;
    nl_rrsp_address = 32'hBEEF_0000 + cycle; nl_rrsp_count = 6'h2A;
    nl_rrsp_first = 1'b0; @RRSP_JUNK_ID@
    rrsp_done = 0; guard = 0;
    while (!rrsp_done) begin
      step(); guard = guard + 1;
      if (guard > 500) fail("AXI4I.HOLD_RELEASE");
    end
    nl_rrsp_valid = 0;
  end
endtask
task wait_b(input dummy);
  begin
    guard = 0;
    while (b_seen == b_total_mark) begin
      step(); guard = guard + 1;
      if (guard > 2000) fail("AXI4I.WRITE_COMPLETION");
    end
    b_total_mark = b_total_mark + 1;
  end
endtask
task wait_r(input dummy);
  begin
    guard = 0;
    while (r_seen == r_total_mark) begin
      step(); guard = guard + 1;
      if (guard > 2000) fail("AXI4I.READ_DATA");
    end
    r_total_mark = r_total_mark + 1;
  end
endtask
task wait_wq(input integer target);
  begin
    guard = 0;
    while (wq_seen != target) begin
      step(); guard = guard + 1;
      if (guard > 2000) fail("AXI4I.PACKET_BOUNDARY");
    end
  end
endtask

task step;
  begin
    // Network ready modes (the TB plays the network). Driven first, then a
    // settle delay, so the sampled DUT outputs reflect this cycle's ready.
    nl_wreq_ready = (wrq_mode == 0) ? 1'b1 : (wrq_mode == 1) ? (cycle % 2 == 0)
                    : (wrq_mode == 2) ? (cycle % 3 != 1) : 1'b0;
    rready = (rready_mode == 0) ? 1'b1 : (rready_mode == 1) ? (cycle % 2 == 0)
             : (cycle % 3 != 2);
    bready = 1;
    if (ws_on) begin
      if (ws_r < ws_n) begin
        wvalid = 1; wdata = ws_data[ws_r]; wstrb = ws_be[ws_r]; wlast = ws_last[ws_r];
      end else begin
        wvalid = 0; wlast = 0;
      end
    end
    #1;
    // Handshake trackers for the driver tasks.
    if (reset_n && awvalid && awready) aw_done = 1;
    if (reset_n && wvalid && wready) w_done = 1;
    // W/AW ordering: a W beat is taken only inside the burst of a captured AW,
    // and a new AW is taken only after the previous burst took all its beats.
    if (!reset_n) w_budget = 0;
    if (reset_n && awvalid && awready) begin
      if (w_budget != 0) fail("AXI4I.W_BURST_COUNT");
      w_budget = awlen + 1; w_beats = awlen + 1;
    end
    if (reset_n && wvalid && wready) begin
      if (w_budget == 0) fail("AXI4I.W_AHEAD_OF_AW");
      if (ws_tight && w_budget != w_beats) begin
        if (cycle != w_prev_cycle + 1) fail("AXI4I.W_THROUGHPUT");
        w_tight_pairs = w_tight_pairs + 1;
      end
      w_prev_cycle = cycle;
      w_budget = w_budget - 1; w_accepted = w_accepted + 1;
      if (ws_on) ws_r = ws_r + 1;
    end
    if (reset_n && arvalid && arready) ar_done = 1;
    if (reset_n && nl_wrsp_valid && nl_wrsp_ready) wrsp_done = 1;
    if (reset_n && nl_rrsp_valid && nl_rrsp_ready) rrsp_done = 1;
    // Scoped single-in-flight monitors.
    if (expect_aw_block && awready) fail("AXI4I.SINGLE_INFLIGHT");
    if (expect_ar_block && arready) fail("AXI4I.SINGLE_INFLIGHT");
    // Black-box overlap witness: a write fragment live while a read beat streams.
    if (reset_n && nl_wreq_valid && rvalid) overlap_seen = 1;
    // Write request fragment monitor: pop and check every field.
    if (reset_n && nl_wreq_valid && nl_wreq_ready) begin
      if (xq_r == xq_n) fail("AXI4I.NO_TOUCH");
      if (nl_wreq_first != xq_first[xq_r] || nl_wreq_last != xq_last[xq_r])
        fail("AXI4I.PACKET_BOUNDARY");
      if (nl_wreq_address != xq_addr[xq_r]) fail("AXI4I.BEAT_ADDRESS");
      if (nl_wreq_length != xq_len[xq_r] || nl_wreq_size != xq_size[xq_r] ||
          nl_wreq_burst != xq_burst[xq_r] || nl_wreq_cache != xq_cache[xq_r] ||
          nl_wreq_protection != xq_prot[xq_r] || nl_wreq_write != 1 || nl_wreq_count != xq_count[xq_r])
        fail("AXI4I.CONTROL_FIELD");
      @WREQ_ID_CHECK@
      if (nl_wreq_error != 0) fail("AXI4I.CONTROL_FIELD");
      if (nl_wreq_data != xq_data[xq_r]) fail("AXI4I.BYTE_CONSERVATION");
      if (nl_wreq_be != xq_be[xq_r]) fail("AXI4I.WSTRB_FIDELITY");
      xq_r = xq_r + 1; wq_seen = wq_seen + 1;
    end
    // Read request fragment monitor (single-fragment packets).
    if (reset_n && nl_rreq_valid && nl_rreq_ready) begin
      if (rq_r == rq_n) fail("AXI4I.NO_TOUCH");
      if (nl_rreq_first != 1 || nl_rreq_last != 1) fail("AXI4I.PACKET_BOUNDARY");
      if (nl_rreq_address != rq_addr[rq_r]) fail("AXI4I.BEAT_ADDRESS");
      if (nl_rreq_length != rq_len[rq_r] || nl_rreq_size != rq_size[rq_r] ||
          nl_rreq_burst != rq_burst[rq_r] || nl_rreq_cache != rq_cache[rq_r] ||
          nl_rreq_protection != rq_prot[rq_r] || nl_rreq_write != 0 ||
          nl_rreq_count != rq_count[rq_r]) fail("AXI4I.CONTROL_FIELD");
      @RREQ_ID_CHECK@
      if (nl_rreq_error != 0 || nl_rreq_data != 0 || nl_rreq_be != 0)
        fail("AXI4I.CONTROL_FIELD");
      rq_r = rq_r + 1; rq_seen = rq_seen + 1;
    end
    // B monitor.
    if (reset_n && bvalid && bready) begin
      if (bq_r == bq_n) fail("AXI4I.WRITE_COMPLETION");
      if (bresp != bq_resp[bq_r]) fail("AXI4I.WRITE_COMPLETION");
      @B_ID_CHECK@
      bq_r = bq_r + 1; b_seen = b_seen + 1;
    end
    // R monitor.
    if (reset_n && rvalid && rready) begin
      if (rq2_r == rq2_n) fail("AXI4I.READ_DATA");
      if (rdata != rq2_data[rq2_r]) fail("AXI4I.READ_DATA");
      if (rresp != rq2_resp[rq2_r]) fail("AXI4I.READ_RESP");
      if (rlast != rq2_last[rq2_r]) fail("AXI4I.RLAST_POSITION");
      @R_ID_CHECK@
      rq2_r = rq2_r + 1; r_seen = r_seen + 1;
    end
    // Stall stability: a stalled fragment must hold its payload.
    if (reset_n && nl_wreq_valid && !nl_wreq_ready) begin
      if (!stall_active) begin
        stall_active = 1;
        snap_addr = nl_wreq_address; snap_len = nl_wreq_length; snap_size = nl_wreq_size;
        snap_burst = nl_wreq_burst; snap_cache = nl_wreq_cache; snap_prot = nl_wreq_protection;
        snap_id = nl_wreq_id; snap_count = nl_wreq_count; snap_first = nl_wreq_first;
        snap_last = nl_wreq_last; snap_data = nl_wreq_data; snap_be = nl_wreq_be;
      end else if (nl_wreq_address != snap_addr || nl_wreq_length != snap_len ||
                   nl_wreq_size != snap_size || nl_wreq_burst != snap_burst ||
                   nl_wreq_cache != snap_cache || nl_wreq_protection != snap_prot ||
                   nl_wreq_id != snap_id || nl_wreq_count != snap_count ||
                   nl_wreq_first != snap_first || nl_wreq_last != snap_last ||
                   nl_wreq_data != snap_data || nl_wreq_be != snap_be)
        fail("AXI4I.HOLD_RELEASE");
    end else stall_active = 0;
    if (reset_n && rvalid && !rready) begin
      if (!r_stall) begin
        r_stall = 1; r_snap_data = rdata; r_snap_resp = rresp; r_snap_last = rlast;
        r_snap_id = rid;
      end else if (rdata != r_snap_data || rresp != r_snap_resp || rlast != r_snap_last ||
                   rid != r_snap_id)
        fail("AXI4I.HOLD_RELEASE");
    end else r_stall = 0;
    if (reset_n && bvalid && !bready) begin
      if (!b_stall) begin b_stall = 1; b_snap_resp = bresp; b_snap_id = bid; end
      else if (bresp != b_snap_resp || bid != b_snap_id) fail("AXI4I.HOLD_RELEASE");
    end else b_stall = 0;
    tick();
  end
endtask

initial begin
  reset_n = 0;
  step(); step();
  reset_n = 1;
  step();
@PHASES@
  if (xq_r != xq_n || rq_r != rq_n || bq_r != bq_n || rq2_r != rq2_n)
    fail("AXI4I.PACKET_BOUNDARY");
  // Every accepted W beat became exactly one fragment (minus named exceptions).
  if (w_accepted != wq_seen + @W_EXTRA@) fail("AXI4I.W_FRAGMENT");
  // The throughput check ran on every in-burst beat pair of the tight stream.
  if (w_tight_pairs != @W_TIGHT_PAIRS@) fail("AXI4I.W_THROUGHPUT");
  if (wq_seen != @WQ_TOTAL@ || rq_seen != @RQ_TOTAL@ || b_seen != @B_TOTAL@ ||
      r_seen != @R_TOTAL@)
    fail("AXI4I.FRAGMENT_COUNT");
  $display("AXI4_INITIATOR_PASS wfrag=%0d rfrag=%0d b=%0d r=%0d", wq_seen, rq_seen,
           b_seen, r_seen);
  $finish;
end
endmodule
'''


def _wires_and_conns(bench):
    """Weft face wire declarations and port connections per the contract."""
    req, rsp = field_widths(bench.db, bench.lb, bench.idb)
    wreq_wires = "\n".join(f"wire [{w - 1}:0] nl_wreq_{f};" if w > 1 else f"wire nl_wreq_{f};"
                           for f, w in req)
    rreq_wires = "\n".join(f"wire [{w - 1}:0] nl_rreq_{f};" if w > 1 else f"wire nl_rreq_{f};"
                           for f, w in req)
    wrsp_regs = "\n".join(f"reg [{w - 1}:0] nl_wrsp_{f} = 0;" if w > 1 else f"reg nl_wrsp_{f} = 0;"
                          for f, w in rsp)
    rrsp_regs = "\n".join(f"reg [{w - 1}:0] nl_rrsp_{f} = 0;" if w > 1 else f"reg nl_rrsp_{f} = 0;"
                          for f, w in rsp)
    wreq_conns = ",\n".join(f"  .nl_wreq_{f}(nl_wreq_{f})" for f, _ in req) + ","
    rreq_conns = ",\n".join(f"  .nl_rreq_{f}(nl_rreq_{f})" for f, _ in req) + ","
    wrsp_conns = ",\n".join(f"  .nl_wrsp_{f}(nl_wrsp_{f})" for f, _ in rsp) + ","
    rrsp_conns = ",\n".join(f"  .nl_rrsp_{f}(nl_rrsp_{f})" for f, _ in rsp)
    return {"@WREQ_WIRES@": wreq_wires, "@RREQ_WIRES@": rreq_wires,
            "@WRSP_REGS@": wrsp_regs, "@RRSP_REGS@": rrsp_regs,
            "@WREQ_CONNS@": wreq_conns, "@RREQ_CONNS@": rreq_conns,
            "@WRSP_CONNS@": wrsp_conns, "@RRSP_CONNS@": rrsp_conns}


def write_bench(work, data_bits=32, len_bits=8, id_bits=4):
    work = Path(work)
    bench = Bench(data_bits, len_bits, id_bits)
    text = bench.build()
    for key, value in _wires_and_conns(bench).items():
        text = text.replace(key, value)
    if id_bits:
        text = text.replace("@WREQ_ID_CHECK@",
                            "if (nl_wreq_id != xq_id[xq_r]) fail(\"AXI4I.CONTROL_FIELD\");")
        text = text.replace("@RREQ_ID_CHECK@",
                            "if (nl_rreq_id != rq_id[rq_r]) fail(\"AXI4I.CONTROL_FIELD\");")
        text = text.replace("@B_ID_CHECK@",
                            "if (bid != bq_id[bq_r]) fail(\"AXI4I.RESPONSE_ID\");")
        text = text.replace("@R_ID_CHECK@",
                            "if (rid != rq2_id[rq2_r]) fail(\"AXI4I.RESPONSE_ID\");")
        text = text.replace("@WRSP_JUNK_ID@", "nl_wrsp_id = {(@IDB@/2){2'b10}};")
        text = text.replace("@RRSP_JUNK_ID@", "nl_rrsp_id = {(@IDB@/2){2'b01}};")
    else:
        for tag in ("@WREQ_ID_CHECK@", "@RREQ_ID_CHECK@", "@B_ID_CHECK@", "@R_ID_CHECK@",
                        "@WRSP_JUNK_ID@", "@RRSP_JUNK_ID@"):
            text = text.replace(tag, "")
        # The ID ports do not exist on a zero-ID profile.
        for conn in (".nl_axi_awid(awid), ", ".nl_axi_bid(bid), ",
                     ".nl_axi_arid(arid), ", ".nl_axi_rid(rid), "):
            text = text.replace(conn, "")
    text = text.replace("@IDB@", str(max(id_bits, 1)))
    # The stall snapshot uses the id wire only when present.
    if not id_bits:
        text = text.replace("snap_id = nl_wreq_id;", "snap_id = 0;")
        text = text.replace("nl_wreq_id != snap_id ||", "")
        text = text.replace("r_snap_id = rid;", "r_snap_id = 0;")
        text = text.replace("rid != r_snap_id ||", "")
        text = text.replace("b_snap_id = bid;", "b_snap_id = 0;")
        text = text.replace(" || bid != b_snap_id", "")
    (work / "tb.sv").write_text(text)
    return bench
