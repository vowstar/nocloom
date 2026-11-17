"""Seeded AXI4 tapes of several IDs at a TL-UH target, with decode misses."""
import argparse
import json
import re
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sim_engine

READ, WRITE, CONTROL, HEADER, FAILED = 1, 2, 3, 4, 5
TARGET, OTHER, MISS = 0, 1, 2
INCR, WRAP = 1, 2
PAGE = 4096
OTHER_BASE = 0x20000
MISS_PAGES = (0x10000, 0x1F000, 0x21000, 0x30000, 0x40000000, 0xFFFFF000)
# Coverage requirement bits of the tape header; the bench reads the same order.
REQUIRE = ("cross", "resume", "later", "full", "wrap", "incr", "unaligned", "narrow", "wide",
           "r_stall", "d_gap", "a_stall", "aw_overlap", "miss_two", "miss_w_between",
           "miss_same", "miss_pair", "miss_full", "d_reorder", "miss_gets", "writes", "other",
           "r_error")


class Random:
    """xorshift64*: the same stream on every Python version."""

    def __init__(self, seed):
        self.state = (seed * 0x9E3779B97F4A7C15 + 0x1234567) & (2**64 - 1) or 1

    def below(self, limit):
        x = self.state
        x ^= x >> 12
        x ^= (x << 25) & (2**64 - 1)
        x ^= x >> 27
        self.state = x
        return (((x * 0x2545F4914F6CDD1D) & (2**64 - 1)) >> 33) % limit

    def pick(self, items):
        return items[self.below(len(items))]

    def chance(self, percent):
        return self.below(100) < percent

    def shuffled(self, items):
        items = list(items)
        for i in range(len(items) - 1, 0, -1):
            j = self.below(i + 1)
            items[i], items[j] = items[j], items[i]
        return items


def blocks(lo, hi, maximum):
    """Messages of the aligned power-of-two cover of [lo, hi)."""
    count = 0
    while lo < hi:
        size = min(lo & -lo, maximum) if lo else maximum
        while size > hi - lo:
            size >>= 1
        lo += size
        count += 1
    return count


def runs(address, beats, size, burst):
    """Byte runs in transfer order: a WRAP gives its tail and then its head."""
    step = 1 << size
    if burst == WRAP:
        span = beats * step
        base = address // span * span
        return [(address, base + span)] + ([(base, address)] if address != base else [])
    return [(address, (address & ~(step - 1)) + beats * step)]


def middle(shape):
    """The address of the middle beat of a burst."""
    address, beats, size, _ = shape
    return address + beats // 2 * (1 << size)


class Tape:
    def __init__(self, seed, width, capacity, maximum, require):
        self.rng = Random(seed)
        self.lanes = width // 8
        self.full = self.lanes.bit_length() - 1
        self.capacity, self.maximum = capacity, maximum
        bits = sum(1 << REQUIRE.index(name) for name in require)
        self.rows = [HEADER | (seed & 0xFFFFFFFF) << 4 | bits << 36]
        self.written = set()
        self.salt = seed * 7919 + 1
        self.transactions = 0

    def control(self, a=255, d=255, latency=0, order=0, r=255, b=255, w=255, hold=0, early=0,
                extra=0, q=255, failed=()):
        """Ready and valid densities of 256, response latency and order, and the response hold.

        `failed` lists target words that answer Gets with corrupt set in this batch.
        """
        fields = ((4, 8, a), (12, 8, d), (20, 8, latency), (28, 2, order), (30, 8, r), (38, 8, b),
                  (46, 8, w), (54, 2, hold), (56, 1, early), (57, 8, extra), (65, 8, q))
        self.rows.append(self.pack(CONTROL, fields))
        self.rows += [self.pack(FAILED, ((12, 32, address & ~3),)) for address in failed]

    @staticmethod
    def pack(op, fields):
        value = op
        for lsb, width, field in fields:
            assert 0 <= field < 1 << width, (lsb, width, field)
            value |= field << lsb
        return value

    def request(self, op, kind, ident, address, beats, size, burst=INCR, delay=0, wait=0, strobe=0):
        step = 1 << size
        assert 1 <= beats <= 256 and step <= self.lanes
        if burst == WRAP:
            assert beats in (2, 4, 8, 16) and address % step == 0
        else:
            assert (address & ~(step - 1)) + beats * step <= (address & ~(PAGE - 1)) + PAGE
        low = high = 0
        if kind == TARGET:
            pieces = runs(address, beats, size, burst)
            exact = sum(blocks(lo, hi, self.maximum) for lo, hi in pieces)
            if op == READ or strobe == 0:
                low = high = exact
            else:
                high = sum(hi - lo for lo, hi in pieces)
            if op == WRITE:
                self.written.add(address // PAGE)
        self.salt = (self.salt * 1103515245 + 12345) & 0xFFFFFFFF
        fields = ((4, 8, ident), (12, 32, address), (44, 8, beats - 1), (52, 3, size), (55, 2, burst),
                  (57, 2, kind), (63, 8, delay), (71, 2, strobe), (73, 4, wait), (77, 16, low),
                  (94, 32, self.salt), (126, 16, high))
        self.rows.append(self.pack(op, fields))
        self.transactions += 1

    def shape(self, base, long=False):
        """A legal burst inside one 4 KiB page: address, beats, size, burst."""
        rng = self.rng
        size = self.full if long or rng.chance(55) else rng.below(self.full + 1)
        step = 1 << size
        slots = PAGE // step
        if not long and rng.chance(25):
            return base + rng.below(slots) * step, rng.pick((2, 4, 8, 16)), size, WRAP
        choice = 0 if long else rng.below(10)
        if choice < 3:
            first = rng.below(4)
            beats = min(256, slots - first)
        elif choice < 7:
            beats = 2 + rng.below(31)
            first = rng.below(slots - beats + 1)
        else:
            beats = 1 + rng.below(4)
            first = rng.below(slots - beats + 1)
        address = base + first * step
        if step > 1 and rng.chance(35):
            address += 1 + rng.below(step - 1)
        return address, beats, size, INCR

    def several(self, base):
        """A full-width read that needs at least two target messages."""
        step = self.lanes
        slots = PAGE // step
        if min(256, slots) * step > self.maximum and self.rng.chance(50):
            return base, min(256, slots), self.full, INCR
        first = 1 + self.rng.below(3)
        return base + first * step, min(256, slots - first), self.full, INCR

    def single(self, base):
        """A full-width read that one target message covers."""
        return base, max(1, min(256, self.maximum // self.lanes)), self.full, INCR

    def corners(self):
        """Every size as WRAP and as unaligned INCR, and overlapping writes of two IDs."""
        rng = self.rng
        self.control(a=200, d=200, r=200, w=220, b=200, latency=2, early=1)
        pages = rng.shuffled(range(16))
        read_base, write_base = pages[0] * PAGE, pages[1] * PAGE
        ids = rng.shuffled(range(32))
        for size in range(self.full + 1):
            step = 1 << size
            self.request(READ, TARGET, ids[size], read_base + rng.below(64) * step,
                         rng.pick((2, 4, 8, 16)), size, WRAP)
            self.request(READ, TARGET, ids[size + 5], read_base + 1024 + rng.below(8) * step + step // 2,
                         3 + rng.below(6), size)
        step = self.lanes
        self.request(WRITE, TARGET, ids[10], write_base, 8, self.full)
        self.request(WRITE, TARGET, ids[11], write_base + 4 * step, 8, self.full, strobe=1)
        self.request(WRITE, TARGET, ids[10], write_base + 2 * step, 4, self.full, WRAP, strobe=2)
        self.request(WRITE, TARGET, ids[12], write_base + 6 * step + step // 2, 5, self.full, strobe=3)
        self.request(WRITE, TARGET, ids[11], write_base + 3, 9, 0)

    def read_back(self):
        """Read every written page in full, with several IDs in flight."""
        self.control(d=220, r=220, order=0, latency=3)
        step = self.lanes
        beats = min(256, PAGE // step)
        for page in sorted(self.written):
            for address in range(page * PAGE, (page + 1) * PAGE, beats * step):
                self.request(READ, TARGET, self.rng.below(32), address, beats, self.full)

    def random_batch(self, count, kinds, long_share=0):
        rng = self.rng
        pages = rng.shuffled(range(16))
        split = 1 + rng.below(4)
        write_pages, read_pages = pages[:split], pages[split:]
        failed = [rng.pick(read_pages) * PAGE + rng.below(PAGE // 4) * 4 for _ in range(rng.below(4))]
        self.control(a=rng.pick((255, 255, 200, 120)), d=rng.pick((255, 220, 150, 90)),
                     latency=rng.pick((0, 0, 4, 20, 60)), order=rng.below(3),
                     r=rng.pick((255, 230, 140, 70)), b=rng.pick((255, 160, 60)),
                     w=rng.pick((255, 200, 100)), early=rng.below(2), q=rng.pick((255, 160)),
                     failed=failed)
        other_writes = rng.chance(50)
        ids = [rng.below(32) for _ in range(rng.pick((2, 4, 8)))]
        for _ in range(count):
            kind = rng.pick(kinds)
            write = other_writes if kind == OTHER else rng.chance(35)
            if kind == TARGET:
                base = rng.pick(write_pages if write else read_pages) * PAGE
            else:
                base = OTHER_BASE if kind == OTHER else rng.pick(MISS_PAGES)
            address, beats, size, burst = self.shape(base, long=rng.chance(long_share))
            self.request(WRITE if write else READ, kind, rng.pick(ids), address, beats, size, burst,
                         delay=rng.pick((0, 0, 0, 1, 3, 12, 40)), strobe=rng.pick((0, 0, 1, 2, 3)))

    def text(self):
        return "".join(f"{row:040x}\n" for row in self.rows + [0])


def interleave_tape(seed, width, capacity, maximum, batches, require):
    tape = Tape(seed, width, capacity, maximum, require)
    rng = tape.rng
    # One message per context fills the pool; the newest message answers first.
    # Long reads of other IDs arrive while the pool is full.
    tape.control(hold=1, extra=30, order=1, d=200, a=220, r=230)
    for index in range(capacity):
        tape.request(READ, TARGET, index, *tape.single((index % 16) * PAGE))
    for index in range(2):
        tape.request(READ, TARGET, capacity + index, *tape.several((capacity + index) % 16 * PAGE), wait=1)
    # Long reads of different IDs under response gaps and read backpressure.
    # Two of them use one corrupt target word each.
    shapes = [tape.several(page * PAGE) for page in rng.shuffled(range(16))[:4]]
    failed = [middle(shape) for shape in shapes[:2]]
    tape.control(order=0, d=150, a=180, r=150, latency=2, failed=failed)
    first = rng.below(32)
    for index, ident in enumerate((first, (first + 1) % 32, (first + 2) % 32, first)):
        tape.request(READ, TARGET, ident, *shapes[index], delay=index)
    tape.corners()
    for _ in range(batches):
        tape.random_batch(6 + rng.below(8), (TARGET,), long_share=20)
    tape.read_back()
    return tape


def miss_tape(seed, width, capacity, maximum, other, batches, require):
    tape = Tape(seed, width, capacity, maximum, require)
    rng = tape.rng
    full = tape.full
    ids = rng.shuffled(range(32))
    miss = lambda: rng.pick(MISS_PAGES) + rng.below(8) * tape.lanes
    # Misses while two target reads are in flight, and misses back to back.
    shapes = [tape.several(0 * PAGE), tape.several(1 * PAGE)]
    tape.control(latency=40, d=200, order=0, failed=[middle(shapes[1])])
    tape.request(READ, TARGET, ids[0], *shapes[0])
    tape.request(READ, TARGET, ids[1], *shapes[1])
    tape.request(READ, MISS, ids[2], miss(), 8, full, wait=3)
    tape.request(WRITE, MISS, ids[3], miss(), 8, full, wait=3)
    tape.request(READ, MISS, ids[4], miss(), 1, full)
    tape.request(READ, MISS, ids[4], miss(), 16, full)
    tape.request(WRITE, MISS, ids[3], miss(), 1, full)
    tape.request(READ, TARGET, ids[5], *tape.several(2 * PAGE))
    if other:
        tape.request(READ, OTHER, ids[2], OTHER_BASE + 64, 8, full)
    # A miss behind an in-flight read of its ID, and a target read behind that miss.
    tape.control(latency=30, d=180, r=160)
    tape.request(READ, TARGET, ids[6], *tape.several(3 * PAGE))
    tape.request(READ, MISS, ids[6], miss(), 4, full, wait=2)
    tape.request(READ, TARGET, ids[6], *tape.several(4 * PAGE))
    tape.request(WRITE, TARGET, ids[6], 5 * PAGE, 16, full)
    tape.request(WRITE, MISS, ids[6], miss(), 4, full)
    tape.request(WRITE, TARGET, ids[6], 5 * PAGE + 64, 16, full)
    if other:
        tape.request(WRITE, OTHER, ids[6], OTHER_BASE + 1024, 8, full, strobe=1)
    # Miss writes between the beats of target reads.
    tape.control(r=50, w=120, d=220)
    tape.request(READ, TARGET, ids[7], *tape.several(6 * PAGE))
    tape.request(READ, TARGET, ids[8], *tape.several(7 * PAGE))
    tape.request(WRITE, MISS, ids[9], miss(), 8, full, wait=2)
    tape.request(WRITE, MISS, ids[10], miss(), 3, max(0, full - 1), wait=2, strobe=1)
    tape.request(READ, MISS, ids[11], miss(), 2, full, wait=2)
    # Misses while every context has a message in flight.
    tape.control(hold=1, extra=40, order=1, d=220)
    for index in range(capacity):
        tape.request(READ, TARGET, ids[12 + index], *tape.single((8 + index % 8) * PAGE))
    tape.request(READ, MISS, ids[30], miss(), 4, full, wait=1)
    tape.request(WRITE, MISS, ids[31], miss(), 4, full, wait=1)
    tape.request(READ, MISS, ids[12], miss(), 2, full, wait=1)
    tape.request(READ, TARGET, ids[30], *tape.several(0), wait=1)
    tape.corners()
    kinds = (TARGET,) * 5 + (MISS,) * 3 + ((OTHER,) * 2 if other else ())
    for _ in range(batches):
        tape.random_batch(8 + rng.below(8), kinds, long_share=25)
    tape.read_back()
    return tape


def requirements(profile, capacity, other):
    names = ["full", "wrap", "incr", "unaligned", "narrow", "wide", "r_stall", "d_gap", "a_stall",
             "aw_overlap", "writes", "r_error"]
    if capacity > 1:
        names += ["cross", "resume", "later", "d_reorder"]
    if profile == "miss":
        names += ["miss_two", "miss_w_between", "miss_same", "miss_pair", "miss_full"]
        names += ["miss_gets"] if capacity > 1 else []
        names += ["other"] if other else []
    return names


OTHER_PORTS = (("awid", 4, 0), ("awaddr", 32, 0), ("awlen", 8, 0), ("awsize", 3, 0), ("awburst", 2, 0),
               ("awvalid", 1, 0), ("awready", 1, 1), ("wdata", 32, 0), ("wstrb", 4, 0), ("wlast", 1, 0),
               ("wvalid", 1, 0), ("wready", 1, 1), ("bid", 4, 1), ("bresp", 2, 1), ("bvalid", 1, 1),
               ("bready", 1, 0), ("arid", 4, 0), ("araddr", 32, 0), ("arlen", 8, 0), ("arsize", 3, 0),
               ("arburst", 2, 0), ("arvalid", 1, 0), ("arready", 1, 1), ("rid", 4, 1), ("rdata", 32, 1),
               ("rresp", 2, 1), ("rlast", 1, 1), ("rvalid", 1, 1), ("rready", 1, 0))


def bench(mapping, cfg, body):
    declarations, connections = [], []
    for port in mapping["ports"]:
        name, width = port["name"], port["width"]
        alias = name.replace("nl_i_s_", "s_").replace("nl_t_t_", "t_").replace("nl_t_q_", "q_")
        incoming = port["direction"] == "input"
        declarations.append(f"{'reg' if incoming else 'wire'} [{width-1}:0] {alias}"
                            + ("=0;" if incoming else ";"))
        connections.append(f".{name}({alias})")
    names = {port["name"] for port in mapping["ports"]}
    target = mapping["definition"]["tilelink"]["targets"]["t"]
    if target["source_width"] == 0:
        declarations += ["wire t_a_source=0;", "reg t_d_source=0;"]
    if "nl_t_q_arvalid" not in names:
        declarations += [f"{'reg' if driven else 'wire'} [{width-1}:0] q_{name}=0;"
                         for name, width, driven in OTHER_PORTS]
    initiator = cfg["initiators"][0]
    declarations.append(
        f"localparam DATA_WIDTH={initiator['data_width']}, ID_WIDTH={initiator['id_width']}, "
        f"CAPACITY={target['max_outstanding']}, MAXIMUM={target['max_transfer_bytes']};")
    fields = ("awaddr araddr awid arid bid rid awlen arlen awsize arsize awprot arprot awburst arburst "
              "bresp rresp awcache arcache awlock arlock awvalid awready arvalid arready wdata rdata "
              "wstrb wlast wvalid wready bvalid bready rlast rvalid rready").split()
    axi = (f'nlv_tl_uh_axi_check #(.NAME("source"),.DATA_W({initiator["data_width"]}),'
           f'.ID_W({initiator["id_width"]})) checker_s(.clk(clk),.rst_n(reset_n),'
           + ",".join(f".{f}(s_{f})" for f in fields) + ");")
    tl_fields = ("a_opcode a_param a_size a_source a_address a_mask a_data a_corrupt a_valid a_ready "
                 "d_opcode d_param d_size d_source d_data d_corrupt d_denied d_valid d_ready").split()
    tl = (f'nlv_tl_checker #(.NAME("target"),.SOURCE_W({max(1, target["source_width"])}),'
          f'.SIZE_W({target["size_width"]}),.UH(1),.SOURCE_LIMIT(CAPACITY)) checker_t(.clk(clk),'
          ".rst_n(reset_n),.allow_reject(1'b0)," + ",".join(f".{f}(t_{f})" for f in tl_fields) + ");")
    if "nl_t_q_arvalid" in names:
        axi += ('\nnlv_tl_uh_axi_check #(.NAME("other")) checker_q(.clk(clk),.rst_n(reset_n),'
                + ",".join(f".{f}(q_{f})" for f in fields) + ");")
    return ("`timescale 1ns/1ps\nmodule tb;\n" + "\n".join(declarations)
            + f"\n{mapping['module']} dut(" + ",".join(connections) + ");\n"
            + body + "\n" + axi + "\n" + tl + "\nendmodule\n")


def build(args, work):
    cfg = runpy.run_path(str(args.case))["configuration"](
        args.width, args.capacity, args.maximum, args.other, None if args.default == "auto" else args.default)
    (work / "input.yaml").write_text(json.dumps(cfg))
    generated = work / "generated"
    result = subprocess.run([str(args.cli.resolve()), "generate", str(work / "input.yaml"),
                             "--out-dir", str(generated)], capture_output=True, text=True, timeout=120)
    (work / "generation.log").write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(result.stderr)
    mapping = json.loads(next(generated.glob("*.map.json")).read_text())
    selected = mapping["definition"].get("default_targets", {})
    if (args.default == "auto") != (selected == {}) or (args.default != "auto" and selected != {"s": args.default}):
        raise RuntimeError(f"default target is {selected}")
    sv = Path(__file__).parent / "sv"
    rtl = sim_engine.generated_rtl(generated / (mapping["module"] + ".v"))
    checks = (sv / "nlv_tl_checker.sv").read_text() + (sv / "nlv_tl_uh_axi_check.sv").read_text()
    return cfg, mapping, rtl, checks


def tape_for(args, seed, covered=True):
    require = requirements(args.profile, args.capacity, args.other) if covered else []
    if args.profile == "miss":
        return miss_tape(seed, args.width, args.capacity, args.maximum, args.other, args.batches, require)
    return interleave_tape(seed, args.width, args.capacity, args.maximum, args.batches, require)


FAULT_STATE = """
integer nlv_fault=0;
initial if(!$value$plusargs("FAULT=%d",nlv_fault)) nlv_fault=0;
reg nlv_get0=0, nlv_get1=0, nlv_other=0, nlv_hit=0, nlv_leak=0;
reg [3:0] nlv_size0=0, nlv_size1=0;
integer nlv_d_left=0, nlv_gets_done=0, nlv_phase=0, nlv_r_beats=0, nlv_put_beats=0;
integer nlv_miss_r=0, nlv_miss_b=0, nlv_inflight=0, nlv_put_left=0;
wire [3:0] nlv_d_source, nlv_a_source;
wire nlv_d_first, nlv_d_final, nlv_swap_begin, nlv_swap_end, nlv_swap, nlv_corrupt, nlv_drop;
wire nlv_miss_request, nlv_leak_now;
"""
FAULT_LOGIC = """
assign nlv_d_first=nl_t_t_d_valid && nlv_d_left==0;
assign nlv_d_final=nl_t_t_d_valid && (nlv_d_left==1 ||
  (nlv_d_left==0 && !(nl_t_t_d_opcode==1 && nl_t_t_d_size>2)));
assign nlv_swap_begin=nlv_fault==1 && nlv_phase==0 && nlv_d_first && nl_t_t_d_opcode==1 &&
  nlv_d_source<2 && nlv_get0 && nlv_get1 && nlv_size0==nlv_size1;
assign nlv_swap_end=nlv_fault==1 && nlv_phase==2 && nlv_d_first && nlv_d_source=={3'b0,nlv_other};
assign nlv_swap=nlv_swap_begin || nlv_phase==1 || nlv_swap_end || nlv_phase==3;
assign nlv_corrupt=nlv_fault==2 && !nlv_hit && nl_t_t_d_opcode==1 && nlv_gets_done>=1 && nlv_d_left>1;
assign nlv_drop=nlv_fault==3 && nl_t_t_d_opcode==1 && nlv_gets_done>=2 && nlv_d_final && !nlv_hit;
assign nlv_miss_request=nl_i_s_arvalid && nl_i_s_arready && nl_i_s_araddr>=32'h10000 &&
  nl_i_s_araddr[31:12]!=20'h20;
assign nlv_leak_now=nlv_fault==10 && nlv_leak && !NLV_A_VALID && nlv_inflight==0 && nlv_put_left==0;
always @(posedge clk) begin
  if(!reset_n) begin
    nlv_get0=0; nlv_get1=0; nlv_hit=0; nlv_leak=0; nlv_d_left=0; nlv_gets_done=0; nlv_phase=0;
    nlv_r_beats=0; nlv_put_beats=0; nlv_miss_r=0; nlv_miss_b=0; nlv_inflight=0; nlv_put_left=0;
  end else begin
    if(nlv_miss_request && nlv_miss_r==0 && !nlv_hit) nlv_leak=1;
    if(nl_t_t_d_valid && nl_t_t_d_ready) begin
      if(nlv_corrupt || nlv_drop) nlv_hit=1;
      if(nlv_swap_begin) begin nlv_phase=1; nlv_other=!nlv_d_source[0]; end
      if(nlv_swap_end) nlv_phase=3;
      if(nlv_d_final) begin
        if(nl_t_t_d_opcode==1) begin
          nlv_gets_done=nlv_gets_done+1;
          if(nlv_d_source==0) nlv_get0=0;
          if(nlv_d_source==1) nlv_get1=0;
        end
        if(nlv_phase==1) nlv_phase=2;
        if(nlv_phase==3) nlv_phase=4;
        nlv_d_left=0; nlv_inflight=nlv_inflight-1;
      end else if(nlv_d_left==0) nlv_d_left=(1<<(nl_t_t_d_size-2))-1;
      else nlv_d_left=nlv_d_left-1;
    end
    if(nl_t_t_a_valid && nl_t_t_a_ready) begin
      if(nlv_leak_now) begin nlv_leak=0; nlv_hit=1; end
      if(nl_t_t_a_opcode==4) begin
        nlv_inflight=nlv_inflight+1;
        if(nlv_a_source==0) begin nlv_get0=1; nlv_size0=nl_t_t_a_size; end
        if(nlv_a_source==1) begin nlv_get1=1; nlv_size1=nl_t_t_a_size; end
      end else begin
        nlv_put_beats=nlv_put_beats+1;
        if(nlv_put_left==0) begin
          nlv_inflight=nlv_inflight+1;
          nlv_put_left=nl_t_t_a_size>2 ? (1<<(nl_t_t_a_size-2)) : 1;
        end
        nlv_put_left=nlv_put_left-1;
      end
    end
    if(nl_i_s_rvalid && nl_i_s_rready) begin
      nlv_r_beats=nlv_r_beats+1;
      if(NLV_R_RESP==3) nlv_miss_r=nlv_miss_r+1;
    end
    if(nl_i_s_bvalid && nl_i_s_bready && nl_i_s_bresp==3) nlv_miss_b=nlv_miss_b+1;
  end
end
"""


def faulted(rtl, module, source_width):
    """The network with selectable faults; +FAULT=0 leaves every signal as generated."""

    def substitute(pattern, replacement):
        nonlocal rtl
        rtl, count = re.subn(pattern, replacement, rtl, flags=re.M)
        if count != 1:
            raise RuntimeError(f"fault anchor matched {count} times: {pattern}")

    def incoming(port, value):
        substitute(rf"^(assign nl_internal_v\d+ = )({port});$", lambda m: f"{m[1]}{value(m[2])};")

    def outgoing(port, value):
        substitute(rf"^(assign {port} = )(nl_internal_v\d+);$", lambda m: f"{m[1]}{value(m[2])};")

    logic = FAULT_LOGIC
    if source_width:
        incoming("nl_t_t_d_source", lambda v: f"(nlv_swap ? {v} ^ {source_width}'d1 : {v})")
        logic += "assign nlv_d_source=nl_t_t_d_source;\nassign nlv_a_source=nl_t_t_a_source;\n"
    else:
        logic += "assign nlv_d_source=0;\nassign nlv_a_source=0;\n"
    incoming("nl_t_t_d_data", lambda v: f"({v} ^ (nlv_corrupt ? 32'h00010000 : 32'h0))")
    incoming("nl_t_t_d_valid", lambda v: f"({v} && !nlv_drop)")
    incoming("nl_t_t_d_corrupt", lambda v: f"({v} && nlv_fault!=11)")
    outgoing("nl_i_s_rid", lambda v: f"({v} ^ (nlv_fault==4 && nlv_r_beats==40 ? 1'b1 : 1'b0))")
    outgoing("nl_t_t_a_data", lambda v: f"({v} ^ (nlv_fault==5 && nl_t_t_a_opcode!=4 && nlv_put_beats==3 ? 32'h100 : 32'h0))")
    outgoing("nl_i_s_rlast", lambda v: f"({v} || (nlv_fault==6 && nlv_r_beats>=60 && nlv_r_beats<64))")
    response = re.search(r"^assign nl_i_s_rresp = (nl_internal_v\d+);$", rtl, flags=re.M)[1]
    logic = logic.replace("NLV_R_RESP", response)
    outgoing("nl_i_s_rresp", lambda v: f"(nlv_fault==7 && {v}==2'd3 && nlv_miss_r==1 ? 2'd0 : {v})")
    outgoing("nl_i_s_rdata", lambda v: f"({v} | (nlv_fault==8 && {response}==2'd3 && nlv_miss_r==2 ? 512 : 0))")
    outgoing("nl_i_s_bid", lambda v: f"({v} ^ (nlv_fault==9 && nl_i_s_bresp==2'd3 && nlv_miss_b==0 ? 1'b1 : 1'b0))")
    valid = re.search(r"^assign nl_t_t_a_valid = (nl_internal_v\d+);$", rtl, flags=re.M)[1]
    logic = logic.replace("NLV_A_VALID", valid)
    outgoing("nl_t_t_a_valid", lambda v: f"({v} || nlv_leak_now)")
    outgoing("nl_t_t_a_opcode", lambda v: f"(nlv_leak_now ? 3'd4 : {v})")
    outgoing("nl_t_t_a_size", lambda v: f"(nlv_leak_now ? 4'd2 : {v})")
    outgoing("nl_t_t_a_address", lambda v: f"(nlv_leak_now ? 32'hf000 : {v})")
    outgoing("nl_t_t_a_mask", lambda v: f"(nlv_leak_now ? 4'hf : {v})")
    if source_width:
        outgoing("nl_t_t_a_source", lambda v: f"(nlv_leak_now ? {source_width}'d0 : {v})")
    start = rtl.index("module " + module)
    head = rtl.index("\n", rtl.index(");", start))
    tail = rtl.index("endmodule", start)
    return rtl[:head] + FAULT_STATE + rtl[head:tail] + logic + rtl[tail:]


def summary(output):
    return [line for line in output.splitlines() if line.startswith(("UH_COVER", "UH_PASS"))]


def negative(args):
    """Each fault must stop the bench with one of its rules. The fault copy with
    no fault selected must behave as the generated network."""
    args.work.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    faults = runpy.run_path(str(args.case))["FAULTS"][args.profile]
    cfg, mapping, rtl, checks = build(args, root)
    target = mapping["definition"]["tilelink"]["targets"]["t"]
    tape = tape_for(args, args.seeds[0], covered=False)
    options = ["+TAPE=tape.hex", f"+ROWS={len(tape.rows) + 1}"]
    sims = {}
    for name, text in (("normal", rtl), ("faults", faulted(rtl, mapping["module"], target["source_width"]))):
        work = root / name
        work.mkdir()
        (work / "tb.sv").write_text(bench(mapping, cfg, args.bench.read_text()))
        (work / "network.v").write_text(text + checks)
        (work / "tape.hex").write_text(tape.text())
        sims[name] = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                           compile_timeout=600, run_timeout=args.run_timeout,
                                           success=("UH_PASS",))
    base = sims["normal"].run(options)
    if base.returncode:
        raise RuntimeError(f"generated network failed: {base.stdout[-2000:]}")
    disabled = sims["faults"].run(options + ["+FAULT=0"])
    if disabled.returncode or summary(disabled.stdout) != summary(base.stdout):
        raise RuntimeError("the fault copy with no fault differs from the generated network")
    rows = [dict(selector=0, status="PASS", summary=summary(base.stdout)[-1])]
    for selector, (name, rules) in faults.items():
        if selector == 1 and not target["source_width"]:
            continue
        result = sims["faults"].run(options + [f"+FAULT={selector}"])
        (root / f"fault-{selector}.log").write_text(result.stdout)
        assertion = next((line for line in result.stdout.splitlines() if "ASSERT_FAIL:" in line), "")
        caught = result.returncode != 0 and any(f"ASSERT_FAIL:{rule} " in assertion for rule in rules)
        stuck = next((line for line in result.stdout.splitlines() if line.startswith("STUCK")), "")
        rows.append(dict(selector=selector, fault=name, status="CAUGHT" if caught else "SURVIVED",
                         assertion=assertion, stuck=stuck))
        print(json.dumps(rows[-1]), flush=True)
    (root / "results.json").write_text(json.dumps(rows, indent=2))
    print(root)
    return 0 if all(row["status"] in ("PASS", "CAUGHT") for row in rows) else 1


def run(args):
    args.work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work)).resolve()
    cfg, mapping, rtl, checks = build(args, work)
    (work / "tb.sv").write_text(bench(mapping, cfg, args.bench.read_text()))
    (work / "network.v").write_text(rtl + checks)
    sim = sim_engine.Simulation(work, "network.v", engine=args.sim, config=args.sim_config,
                                compile_timeout=600, run_timeout=args.run_timeout, success=("UH_PASS",))
    status = 0
    for seed in args.seeds:
        tape = tape_for(args, seed)
        name = f"tape-{seed}.hex"
        (work / name).write_text(tape.text())
        result = sim.run([f"+TAPE={name}", f"+ROWS={len(tape.rows) + 1}"] + (["+TRACE"] if args.trace else []))
        (work / f"simulation-{seed}.log").write_text(result.stdout)
        lines = [line for line in result.stdout.splitlines()
                 if line.startswith(("UH_COVER", "UH_PASS", "ASSERT_FAIL", "STUCK", "COVERAGE"))]
        print(f"seed={seed} transactions={tape.transactions} exit={result.returncode}")
        print("\n".join(lines), flush=True)
        if result.returncode:
            print(result.stdout[-3000:])
            status = 1
            break
    print(work)
    return status


def seed_list(text):
    seeds = []
    for part in text.split(","):
        first, _, last = part.partition("-")
        seeds += range(int(first), int(last or first) + 1)
    return seeds


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("cli", "case", "bench", "work"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--profile", choices=("interleave", "miss"), default="interleave")
    parser.add_argument("--width", type=int, default=128)
    parser.add_argument("--capacity", type=int, default=3)
    parser.add_argument("--maximum", type=int, default=1024)
    parser.add_argument("--other", action="store_true")
    parser.add_argument("--default", choices=("t", "q", "auto"), default="t")
    parser.add_argument("--batches", type=int, default=4, help="Random batches after the directed ones")
    parser.add_argument("--seeds", type=seed_list, default=[1], help="Such as 1,2,3 or 6-10")
    parser.add_argument("--run-timeout", type=int, default=240)
    parser.add_argument("--trace", action="store_true", help="Log requests and target messages")
    parser.add_argument("--negative", action="store_true", help="Run the fault table of the case")
    sim_engine.add_arguments(parser)
    args = parser.parse_args()
    return negative(args) if args.negative else run(args)


if __name__ == "__main__":
    sys.exit(main())
