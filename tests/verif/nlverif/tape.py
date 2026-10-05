"""Instruction tape for the SV interpreter (closed-loop stimulus).

A scenario builds a Tape with zero-time pushes and blocking waits on BFM
counters; the interpreter in tb.sv executes it on the falling edge of the
main clock, so every push is seen by the BFMs at the next rising edge.
Records are 16 x 64-bit words: op, bfm, then up to 14 arguments.
"""

OPS = dict(END=0, WRITE=1, READ=2, WAIT=3, DELAY=4, RESET=5, SET=6, EXPECT=7, MARK=8,
           DRAIN=9, SNAP=10, SETTLE=11, PORT_RESET=12, MODE=13)
CMP = {"==": 0, "!=": 1, ">=": 2, "<=": 3, ">": 4, "<": 5}
BURST = {"fixed": 0, "incr": 1, "wrap": 2}
STROBE = {"full": 0, "sparse": 1, "none": 2, "first": 3, "mix": 4, "keep": 5,
          "table": 6}
READY = {"always": 0, "never": 1, "delay": 2, "modulo": 3, "random": 4, "await_b": 5,
         "last_hold": 6, "beats": 7}

MASTER_COUNTERS = dict(aw=0, w=1, b=2, ar=3, r=4, rlast=5, wpush=6, rpush=7, bvalid=8,
                       rvalid=9, w_out=10, r_out=11, bresp=12, rresp=13, wlast=14,
                       awvalid=15, wvalid=16, arvalid=17, cycle=18, cancel=19)
SLAVE_COUNTERS = dict(aw=0, w=1, b=2, ar=3, r=4, rlast=5, w_pend=6, r_pend=7, awvalid=8,
                      wvalid=9, arvalid=10, bvalid=11, rvalid=12, peak_w=13, peak_r=14,
                      sideband=15, wlast=16, applied=17, bready=18, rready=19, cycle=20)
APB_COUNTERS = dict(access=0, write=1, read=2, psel=8, b2b=9, sideband=15, cycle=20)
STREAM_COUNTERS = dict(n=0, valid=8)

MASTER_CTL = dict(b_mode=0, b_a1=1, b_a2=2, r_mode=3, r_a1=4, r_a2=5, gate_aw=6, gate_w=7,
                  gate_ar=8, w_after_aw=9, aw_gap=10, w_gap=11, ar_gap=12, quiet_b=13,
                  quiet_r=14, tput_ch=15, tput_num=16, tput_den=17, seed=18, w_rg_beat=19,
                  w_rg_n=20, vrate_aw=21, vrate_w=22, vrate_ar=23, keep0=24, keep1=25,
                  keep2=26, keep3=27, stab_n=28, stab_w=29)
for _k in range(64):
    MASTER_CTL[f"stab{_k}"] = 32 + _k
SLAVE_CTL = dict(aw_mode=0, aw_a1=1, aw_a2=2, w_mode=3, w_a1=4, w_a2=5, ar_mode=6, ar_a1=7,
                 ar_a2=8, gate_b=9, gate_r=10, b_lat=11, r_lat=12, cap_w=13, cap_r=14,
                 b_default=15, r_default=16, b_order=33, r_order=34, quiet_aw=35,
                 quiet_w=36, quiet_ar=37, gap_dir=38, gap_lo=39, gap_hi=40, base=41,
                 out_cap=42, reg0_lo=43, reg0_hi=44, reg0_w=45, reg0_r=46, reg1_lo=47,
                 reg1_hi=48, reg1_w=49, reg1_r=50, tput_ch=51, tput_num=52, tput_den=53,
                 seed=54, errm0_mask=55, errm0_match=56, errm0_code=57, errm1_mask=58,
                 errm1_match=59, errm1_code=60, w_rg_n=61, b_rate=62, r_rate=63, lock_ok=64, id_xor=65,
                 rlast_flip=66, err_addr=67, err_on=68, fifo_en=69, fifo_addr=70, w_stop=71,
                 r_stop=72, r_ilv=73, r_user_base=74, r_user_step=75, b_user_base=76)
for _k in range(4):
    SLAVE_CTL[f"b_ovr_i{_k}"] = 17 + _k
    SLAVE_CTL[f"b_ovr_c{_k}"] = 21 + _k
    SLAVE_CTL[f"r_ovr_i{_k}"] = 25 + _k
    SLAVE_CTL[f"r_ovr_c{_k}"] = 29 + _k
APB_CTL = dict(wait=0, ready_mode=1, ready_a1=2, ready_a2=3, err_default=15, quiet=35,
               base=41, reg0_lo=43, reg0_hi=44, reg0_c=45, reg1_lo=47, reg1_hi=48,
               reg1_c=49, seed=54, errm_mask=55, errm_match=56, slow_mask=57, slow_match=58,
               slow_wait=59, fifo_en=60, fifo_addr=61, gap_hi=40)
for _k in range(4):
    APB_CTL[f"ovr_i{_k}"] = 17 + _k
    APB_CTL[f"ovr_c{_k}"] = 21 + _k
CHANNEL = dict(aw=1, w=2, ar=3, b=4, r=5)

APB_MASTER_CTL = dict(seed=18, stab_n=24, stab0=32)
COUNTERS = dict(axi_master=MASTER_COUNTERS, apb_master=MASTER_COUNTERS, axi_slave=SLAVE_COUNTERS, apb_slave=APB_COUNTERS,
                ahb_slave=dict(addr=0, data=1, active=8, trans=9, err_next=10, retry_next=11,
                               sideband=15, cycle=20),
                stream=STREAM_COUNTERS)
CONTROLS = dict(axi_master=MASTER_CTL, apb_master=APB_MASTER_CTL, axi_slave=SLAVE_CTL, apb_slave=APB_CTL,
                ahb_slave=dict(wait=0, wait_mode=1, idle_mode=2, err_default=15, quiet=35, base=41,
                               response_one_cycle=55,
                               seed=54, reg0_lo=43, ovr_i0=17, ovr_i1=18, ovr_c0=21, ovr_c1=22,
                               reg0_hi=44, reg0_c=45, reg1_lo=47, reg1_hi=48, reg1_c=49,
                               fifo_en=60, fifo_addr=61, gap_hi=40,
                               slow_mask=57, slow_match=58, slow_wait=59, busy_hold=62,
                               busy_from=63), stream={})


# Records a tape may hold (the bench's tape memory has one more for the header).
RECORDS = 32767


class TapeError(ValueError):
    pass


class Tape:
    """Assembles a scenario. `bfms` maps a BFM name to its bench entry."""

    def __init__(self, bfms, max_cycles=200000):
        self.bfms = bfms
        self.records = []
        self.labels = ["<none>"]
        self.marks = ["<none>"]
        self.snaps = 0
        self.tag = 0
        self.expect = {}      # tag -> expectation overrides for the checker
        self.checks = []      # scenario-level offline checks
        self.max_cycles = max_cycles
        self.ended = False

    # -- encoding helpers --
    def _bfm(self, name):
        if name not in self.bfms:
            raise TapeError("unknown BFM " + str(name))
        return self.bfms[name]

    def _label(self, text):
        if text not in self.labels:
            self.labels.append(text)
        return self.labels.index(text)

    def _emit(self, op, bfm=0, *args):
        if self.ended:
            raise TapeError("record after END")
        if len(args) > 14:
            raise TapeError("too many arguments")
        values = [OPS[op], bfm, *args] + [0] * (14 - len(args))
        for v in values:
            if not 0 <= int(v) < (1 << 64):
                raise TapeError(f"value out of range in {op}: {v}")
        self.records.append([int(v) for v in values])

    def counter(self, name, counter):
        b = self._bfm(name)
        table = COUNTERS[b["kind"]]
        if counter not in table:
            raise TapeError(f"{name}: unknown counter {counter}")
        return b["index"], table[counter]

    # -- stimulus --
    def write(self, port, addr, beats=1, size=None, burst="incr", id=0, lock=0, cache=0,
              prot=0, qos=0, strobe="full", salt=None, keep=None, **expect):
        """keep: a lane mask (bit j strobes lane j of every beat's active
        lanes), for strobe "keep"."""
        b = self._bfm(port)
        if keep is not None:
            if strobe != "keep" or not 0 <= keep < 1 << 128:
                raise TapeError("keep needs strobe keep and at most 128 lanes")
            for k in range(4):
                self.set(port, f"keep{k}", keep >> (32 * k) & 0xFFFFFFFF)
        if b["kind"] not in ("axi_master", "apb_master"):
            raise TapeError(port + " is not an initiator BFM")
        size = b["size"] if size is None else size
        if size > b["size"] or not 1 <= beats <= 1 << b.get("len_bits", 8):
            raise TapeError("illegal write shape")
        if b["kind"] == "apb_master" and (beats != 1 or size != b["size"]):
            raise TapeError("an APB transfer is one bus-wide beat")
        self.tag += 1
        tag = self.tag
        self._emit("WRITE", b["index"], addr, beats - 1, size, BURST[burst], id, lock, cache,
                   prot, qos, STROBE[strobe], tag if salt is None else salt, tag)
        if expect:
            self.expect[tag] = expect
        return tag

    def read(self, port, addr, beats=1, size=None, burst="incr", id=0, lock=0, cache=0,
             prot=0, qos=0, **expect):
        b = self._bfm(port)
        if b["kind"] not in ("axi_master", "apb_master"):
            raise TapeError(port + " is not an initiator BFM")
        size = b["size"] if size is None else size
        if size > b["size"] or not 1 <= beats <= 1 << b.get("len_bits", 8):
            raise TapeError("illegal read shape")
        if b["kind"] == "apb_master" and (beats != 1 or size != b["size"]):
            raise TapeError("an APB transfer is one bus-wide beat")
        self.tag += 1
        tag = self.tag
        self._emit("READ", b["index"], addr, beats - 1, size, BURST[burst], id, lock, cache,
                   prot, qos, tag)
        if expect:
            self.expect[tag] = expect
        return tag

    # -- control --
    def set(self, port, register, value):
        b = self._bfm(port)
        table = CONTROLS[b["kind"]]
        if register not in table:
            raise TapeError(f"{port}: unknown control {register}")
        self._emit("SET", b["index"], table[register], int(value))

    def ready(self, port, channel, mode, a1=0, a2=0):
        """Ready pattern of one channel. "await_b" (initiator R only) keeps
        RREADY low until a1 B handshakes (default 1) happened after this
        record: R acceptance that depends on B (premise E2). "beats"
        (initiator R only) takes a1 R beats after this record, then holds
        RREADY low."""
        prefix = {"axi_master": {"b": "b", "r": "r"},
                  "axi_slave": {"aw": "aw", "w": "w", "ar": "ar"}}[self._bfm(port)["kind"]]
        p = prefix[channel]
        if mode == "await_b":
            if p != "r":
                raise TapeError("await_b applies to an initiator R channel only")
            a1 = a1 or 1
        if mode == "beats" and p != "r":
            raise TapeError("beats applies to an initiator R channel only")
        if mode == "last_hold" and p != "w":
            raise TapeError("last_hold applies to a target W channel only")
        # Zero-time records: the await_b base (B count when the mode is set)
        # is the count at this record.
        self.set(port, p + "_mode", READY[mode])
        self.set(port, p + "_a1", a1)
        self.set(port, p + "_a2", a2)

    def release_w(self, port, after_r=0, from_beat=0):
        """W release gate counted from this record. On an initiator, every W
        beat at index from_beat or later of a burst waits until after_r R
        handshakes happened (premise E1 broken when the write has started);
        on a target, WREADY stays low until after_r last R beats were sent
        (premise E3). after_r=0 opens the gate."""
        kind = self._bfm(port)["kind"]
        if kind == "axi_master":
            self.set(port, "w_rg_beat", from_beat)
        elif kind != "axi_slave" or from_beat:
            raise TapeError(f"{port}: no W release gate with from_beat={from_beat}")
        self.set(port, "w_rg_n", after_r)

    def strobes(self, port, masks, lanes=32):
        """Lane masks of strobe mode "table": beat k of a write takes
        masks[k % len(masks)] (bit j for source lane j; with lanes up to 32 a
        mask repeats every 32 lanes, above that each mask spans its lanes in
        32-bit table words). The BFM reads the table when it loads a beat, so
        a scenario sets it while no other "table" write of that port is in
        flight."""
        words = max(1, -(-lanes // 32))
        if not 1 <= len(masks) * words <= 64:
            raise TapeError("1 to 64 strobe table words")
        for k, m in enumerate(masks):
            for w in range(words):
                self.set(port, f"stab{k * words + w}", m >> (32 * w) & 0xFFFFFFFF)
        if "stab_w" in CONTROLS[self._bfm(port)["kind"]]:
            self.set(port, "stab_w", words)
        elif words > 1:
            raise TapeError(f"{port}: a strobe table of one 32-bit word per beat")
        self.set(port, "stab_n", len(masks) * words)

    def gate(self, port, channel, on):
        self.set(port, "gate_" + channel, 1 if on else 0)

    def quiet(self, port, channel, on):
        self.set(port, "quiet_" + channel, 1 if on else 0)

    def throughput(self, port, channel, num=None, den=1):
        """Open (num given) or close and judge a handshake-rate window."""
        if num is not None:
            self.set(port, "tput_num", num)
            self.set(port, "tput_den", den)
            self.set(port, "tput_ch", CHANNEL[channel])
        else:
            self.set(port, "tput_ch", 0)

    def snap(self, port, counter):
        b, c = self.counter(port, counter)
        # 63 slots reused round robin: a snapshot lives for the next 62 snaps.
        self.snaps += 1
        slot = (self.snaps - 1) % 63 + 1
        self._emit("SNAP", b, c, slot)
        return slot

    def wait(self, port, counter, cmp, value, limit=5000, label=None, since=None):
        b, c = self.counter(port, counter)
        text = label or f"wait {port}.{counter} {cmp} {value}"
        self._emit("WAIT", b, c, CMP[cmp], value, limit, self._label(text), since or 0)

    def expect_now(self, port, counter, cmp, value, label, since=None):
        b, c = self.counter(port, counter)
        self._emit("EXPECT", b, c, CMP[cmp], value, self._label(label), since or 0)

    def delay(self, cycles):
        self._emit("DELAY", 0, cycles)

    def reset(self, cycles=8):
        self._emit("RESET", 0, cycles)

    def port_reset(self, port, cycles):
        """Assert one reset port of the case "resets" (index in name order) alone for cycles
        main clock cycles; it then releases as after the common reset."""
        self._emit("PORT_RESET", 0, port, cycles)

    def mode(self, port, value):
        """Address input `port` (its index in case "modes") takes `value`; the "mode<port>" mark
        tells the checker, which decodes each burst in the modes of its first request."""
        self._emit("MODE", 0, port, value)
        self.mark(f"mode{port}", value)

    def mark(self, name, value=0):
        if name not in self.marks:
            self.marks.append(name)
        self._emit("MARK", 0, self.marks.index(name), value)

    def drain(self, limit=20000, label="drain"):
        self._emit("DRAIN", 0, limit, self._label(label))

    def settle(self, limit, reset=4):
        """Continue once every initiator is idle; when that takes more than
        limit cycles, apply the common reset for reset cycles instead."""
        self._emit("SETTLE", 0, limit, reset)

    def end(self):
        self._emit("END")
        self.ended = True

    # -- offline expectations --
    def check(self, **spec):
        self.checks.append(spec)

    def assemble(self):
        if not self.ended:
            raise TapeError("tape has no END")
        if len(self.records) > RECORDS:
            raise TapeError(f"{len(self.records)} records, the bench holds {RECORDS}")
        header = [0x4E4C5654, len(self.records), self.max_cycles] + [0] * 13
        lines = ["%016x" % v for v in header]
        for r in self.records:
            lines += ["%016x" % v for v in r]
        return "\n".join(lines) + "\n"

    def metadata(self):
        return dict(labels=self.labels, marks=self.marks, expect=self.expect,
                    checks=self.checks, records=len(self.records), tags=self.tag)
