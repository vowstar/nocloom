"""Fragment-level round-trip, hold/release, error-merge and reset checks.

Parameterized per serialization hop: write_bench(work, wide_bytes, narrow_bytes)
generates a testbench for the 2:1 serializer / 1:2 deserializer pair of that
hop. The testbench drives the pair back to back (round-trip mode) and also
drives the deserializer directly. Lane placement comes from the low address
bits (each hop re-derives its used lanes from them) and the occupancy count is
in bytes. Expected values in phases A-E are hand-computed literals; phase F
checks a six-fragment stream against an independent no-wrap byte-range oracle.
All used-lane expectations are nonzero, so a zero expectation marks an unused
slot or lane that must emerge as zero fill.
"""

from pathlib import Path

LANE_BYTES = 4
COUNT_BITS = 6


def hop_shape(wide_bytes, narrow_bytes):
    """Geometry of one 2:1 hop; mirrors plan_width_adapter in the core."""
    lanes_wide = wide_bytes // LANE_BYTES
    lanes_narrow = narrow_bytes // LANE_BYTES
    assert lanes_wide == 2 * lanes_narrow
    return {
        'wide_bytes': wide_bytes,
        'narrow_bytes': narrow_bytes,
        'lanes_wide': lanes_wide,
        'lanes_narrow': lanes_narrow,
        # Phase A single-lane placements: both groups, both boundary lanes.
        'a_bases': sorted({0, lanes_narrow - 1, lanes_narrow, lanes_wide - 1}),
    }


def totals(shape):
    """Expected wide completions and narrow transfers across all phases."""
    a = len(shape['a_bases'])
    return a + 13, a + 21


def _phases(shape):
    lw = shape['lanes_wide']
    ln = shape['lanes_narrow']
    p = []

    def narrow_call(idx, first, last, err, addr, count, wr, prot, slots):
        args = [f"{idx}, {first},{last},{err}, {addr},{count}, {wr},3'b{prot:03b}"]
        args += [f"32'h{d:08X}" for d, _ in slots]
        args += [f"4'h{b:X}" for _, b in slots]
        return '  check_narrow(' + ','.join(args) + ');'

    def wide_call(idx, first, last, err, addr, count, wr, prot, lanes):
        args = [f"{idx}, {first},{last},{err}, {addr},{count}, {wr},3'b{prot:03b}"]
        args += [f"32'h{d:08X}" for d, _ in lanes]
        args += [f"4'h{b:X}" for _, b in lanes]
        return '  check_wide(' + ','.join(args) + ');'

    zero_slot = (0, 0)
    # Phase A: admitted single-lane traffic is 1:1 in both directions. The low
    # address bits place the lane and travel unmodified with the byte enables
    # and the control bundle; unused lanes emerge as zero.
    p.append('  // Phase A: admitted single-lane traffic is 1:1; the low address bits')
    p.append('  // place the lane and travel unmodified, unused lanes emerge as zero.')
    n_idx = 0
    for k, b in enumerate(shape['a_bases']):
        data = 0xAA000000 + (k << 8) + b
        be = (k * 5 + 7) % 15 + 1
        addr = 0x00001000 + k * 0x1000 + b * LANE_BYTES
        wr = k % 2
        prot = (k * 3 + 1) % 8
        slots = [(data, be) if s == b % ln else zero_slot for s in range(ln)]
        lanes = [(data, be) if lane == b else zero_slot for lane in range(lw)]
        p.append(f"  junk_lanes(32'h{k + 1:04X}_0000); set_lane({b}, 32'h{data:08X}, 4'h{be:X});")
        p.append(f"  drive_wide(32'h{addr:08X}, 4, 1, 1, 0, {wr}, 3'b{prot:03b}); send_wide(1, 0);")
        p.append(narrow_call(n_idx, 1, 1, 0, f"32'h{addr:08X}", 4, wr, prot, slots))
        p.append(wide_call(n_idx, 1, 1, 0, f"32'h{addr:08X}", 4, wr, prot, lanes))
        n_idx += 1
    # Phase B: a full word splits into two sub-fragments. The wide fragment is
    # held until the final sub-fragment is accepted; a downstream stall then
    # holds the final sub-fragment with a stable payload, and the parent error
    # fans out to both sub-fragments.
    p.append('  // Phase B: full word splits into two sub-fragments; the wide fragment')
    p.append('  // is held until the final sub-fragment is accepted; stall stability and')
    p.append('  // error fan-out are monitored.')
    p.append(f"  for(lane=0; lane<{lw}; lane=lane+1) set_lane(lane, 32'hB100_0000 + lane, lane+1);")
    p.append(f"  drive_wide(32'h0000_4000, {lw * 4}, 1, 1, 1, 1, 3'b100); send_wide(2, 3);")
    for g in range(2):
        slots = [(0xB1000000 + g * ln + j, g * ln + j + 1) for j in range(ln)]
        p.append(narrow_call(n_idx + g, 1 if g == 0 else 0, 1 if g == 1 else 0, 1,
                             "32'h0000_4000", lw * 4, 1, 0b100, slots))
    p.append(wide_call(n_idx, 1, 1, 1, "32'h0000_4000", lw * 4, 1, 0b100,
                       [(0xB1000000 + lane, lane + 1) for lane in range(lw)]))
    n_idx += 2
    w_idx = len(shape['a_bases'])
    # Phase C: partial byte ranges cross the group boundary or sit in the high
    # group only; sub-fragment slots outside the used range are zero filled.
    p.append('  // Phase C: partial byte ranges cross the group boundary or sit in the')
    p.append('  // high group only; slots outside the used range are zero filled.')
    c1_base = ln - 1
    c1_lanes = {c1_base: (0xC0C10000 + c1_base, (c1_base * 3 + 5) % 15 + 1),
                ln: (0xC0C10000 + ln, (ln * 3 + 5) % 15 + 1)}
    c1_addr = 0x00005000 + c1_base * LANE_BYTES
    p.append("  junk_lanes(32'hC0C1_0000);")
    for lane in sorted(c1_lanes):
        p.append(f"  set_lane({lane}, 32'h{c1_lanes[lane][0]:08X}, 4'h{c1_lanes[lane][1]:X});")
    p.append(f"  drive_wide(32'h{c1_addr:08X}, 8, 1, 1, 0, 1, 3'b001); send_wide(2, 0);")
    frag0 = [c1_lanes.get(ln - 1, zero_slot) if j == ln - 1 else zero_slot for j in range(ln)]
    frag1 = [c1_lanes.get(ln, zero_slot) if j == 0 else zero_slot for j in range(ln)]
    p.append(narrow_call(n_idx, 1, 0, 0, f"32'h{c1_addr:08X}", 8, 1, 0b001, frag0))
    p.append(narrow_call(n_idx + 1, 0, 1, 0, f"32'h{c1_addr:08X}", 8, 1, 0b001, frag1))
    p.append(wide_call(w_idx + 1, 1, 1, 0, f"32'h{c1_addr:08X}", 8, 1, 0b001,
                       [c1_lanes.get(lane, zero_slot) for lane in range(lw)]))
    n_idx += 2
    c2_base = ln
    c2_count = min(2, ln)
    c2_lanes = {ln + j: (0xC0C20000 + ln + j, ((ln + j) * 7 + 3) % 15 + 1) for j in range(c2_count)}
    c2_addr = 0x00006000 + c2_base * LANE_BYTES
    p.append("  junk_lanes(32'hC0C2_0000);")
    for lane in sorted(c2_lanes):
        p.append(f"  set_lane({lane}, 32'h{c2_lanes[lane][0]:08X}, 4'h{c2_lanes[lane][1]:X});")
    p.append(f"  drive_wide(32'h{c2_addr:08X}, {c2_count * 4}, 1, 1, 0, 1, 3'b110); send_wide(1, 0);")
    frag = [c2_lanes.get(ln + j, zero_slot) for j in range(ln)]
    p.append(narrow_call(n_idx, 1, 1, 0, f"32'h{c2_addr:08X}", c2_count * 4, 1, 0b110, frag))
    p.append(wide_call(w_idx + 2, 1, 1, 0, f"32'h{c2_addr:08X}", c2_count * 4, 1, 0b110,
                       [c2_lanes.get(lane, zero_slot) for lane in range(lw)]))
    n_idx += 1
    # Phase D: direct deserializer drive. A non-completing fragment is accepted
    # even while the wide side is stalled; the completing fragment is held with
    # a stable wide payload until acceptance; control comes from the first
    # fragment and errors OR-accumulate across the gather.
    p.append('  // Phase D: direct deserializer drive; non-completing fragments are')
    p.append('  // accepted under a stalled wide side, the completing fragment is held')
    p.append('  // stable, control is captured from the first fragment, errors OR-merge.')
    d1_slots0 = [(0xD0000000 + j, (1 << j) & 0xF) for j in range(ln)]
    d1_slots1 = [(0xE0000000 + ln + j, (8 >> j) & 0xF) for j in range(ln)]
    p.append('  direct=1; set_d_ready(0);')
    p.append(f"  t_first=1; t_last=0; t_error=0; t_count={lw * 4};")
    p.append("  t_address=32'hD1D1_0000; t_write=1; t_protection=3'b101;")
    for j in range(ln):
        p.append(f"  t_data{j}=32'h{d1_slots0[j][0]:08X}; t_be{j}=4'h{d1_slots0[j][1]:X};")
    p.append('  t_valid=1; #1;')
    p.append('  if(!d_n_ready || d_w_valid) fail("ADAPTER.HOLD_RELEASE");')
    p.append('  capture_narrow(); tick(); t_valid=0; tick();')
    p.append(f"  t_first=0; t_last=1; t_error=1; t_count={lw * 4};")
    p.append("  t_address=32'hE2E2_0000; t_write=0; t_protection=3'b010;")
    for j in range(ln):
        p.append(f"  t_data{j}=32'h{d1_slots1[j][0]:08X}; t_be{j}=4'h{d1_slots1[j][1]:X};")
    p.append('  t_valid=1; #1;')
    p.append('  if(d_n_ready || !d_w_valid) fail("ADAPTER.HOLD_RELEASE");')
    p.append('  if(d_w_first!==1 || d_w_last!==1) fail("ADAPTER.PACKET_BOUNDARY");')
    p.append('  if(d_w_error!==1) fail("ADAPTER.ERR_MERGE");')
    p.append("  if(d_w_address!==32'hD1D1_0000 || d_w_write!==1 || d_w_protection!==3'b101 ||")
    p.append(f"     d_w_count!=={lw * 4}) fail(\"ADAPTER.CONTROL_FIELD\");")
    p.append('  if(' + ' || '.join(f"d_w_data{lane}!==32'h{d1_slots0[lane][0]:08X}" for lane in range(ln)) +
             ') fail("ADAPTER.BYTE_CONSERVATION");')
    p.append('  if(' + ' || '.join(f"d_w_data{lane}!==32'h{d1_slots1[lane - ln][0]:08X}" for lane in range(ln, lw)) +
             ') fail("ADAPTER.BYTE_CONSERVATION");')
    p.append('  if(' + ' || '.join(
        [f"d_w_be0!==4'h{d1_slots0[0][1]:X}", f"d_w_be{ln - 1}!==4'h{d1_slots0[ln - 1][1]:X}",
         f"d_w_be{ln}!==4'h{d1_slots1[0][1]:X}", f"d_w_be{lw - 1}!==4'h{d1_slots1[ln - 1][1]:X}"]) +
             ') fail("ADAPTER.BYTE_CONSERVATION");')
    p.append('  tick(); #1;')
    p.append(f"  if(d_n_ready || d_w_data0!==32'h{d1_slots0[0][0]:08X} || "
             f"d_w_data{lw - 1}!==32'h{d1_slots1[ln - 1][0]:08X})")
    p.append('    fail("ADAPTER.HOLD_RELEASE");')
    p.append('  set_d_ready(1); guard=0;')
    p.append('  while(!(d_w_valid && d_w_ready)) begin')
    p.append('    tick(); guard=guard+1; if(guard>100) fail("ADAPTER.HOLD_RELEASE");')
    p.append('  end')
    p.append('  capture_narrow(); capture_wide(); tick(); t_valid=0; #1;')
    p.append('  if(d_w_valid) fail("ADAPTER.HOLD_RELEASE");')
    p.append(wide_call(w_idx + 3, 1, 1, 1, "32'hD1D1_0000", lw * 4, 1, 0b101,
                       d1_slots0 + d1_slots1))
    n_idx += 2
    # A single high-group fragment completes combinationally 1:1.
    p.append('  // A single high-group fragment completes combinationally 1:1.')
    d2_base = ln
    d2_count = min(2, ln)
    d2_addr = 0xD2D20000 + d2_base * LANE_BYTES
    d2_slots = [(0xD2000000 + ln + j, 0xF) if j < d2_count
                else (0x5A5A1000 + j * 0x1111, 0xA) for j in range(ln)]
    p.append(f"  t_first=1; t_last=1; t_error=0; t_count={d2_count * 4};")
    p.append(f"  t_address=32'h{d2_addr:08X}; t_write=0; t_protection=3'b000;")
    for j in range(ln):
        p.append(f"  t_data{j}=32'h{d2_slots[j][0]:08X}; t_be{j}=4'h{d2_slots[j][1]:X};")
    p.append('  set_d_ready(0); t_valid=1; #1;')
    p.append('  if(d_n_ready || !d_w_valid) fail("ADAPTER.HOLD_RELEASE");')
    p.append('  set_d_ready(1);')
    p.append('  if(!d_n_ready) fail("ADAPTER.HOLD_RELEASE");')
    p.append('  guard=0;')
    p.append('  while(!(d_w_valid && d_w_ready)) begin')
    p.append('    tick(); guard=guard+1; if(guard>100) fail("ADAPTER.HOLD_RELEASE");')
    p.append('  end')
    p.append('  capture_narrow(); capture_wide(); tick(); t_valid=0;')
    p.append(wide_call(w_idx + 4, 1, 1, 0, f"32'h{d2_addr:08X}", d2_count * 4, 0, 0b000,
                       [d2_slots[lane - ln] if ln <= lane < ln + d2_count else zero_slot
                        for lane in range(lw)]))
    p.append('  direct=0;')
    n_idx += 1
    # Phase E: a common reset mid-packet cancels both occupations. No stale
    # sub-fragment flows after release and no aborted gather leaks into the
    # next wide fragment.
    p.append('  // Phase E: a common reset mid-packet cancels both occupations; no')
    p.append('  // stale sub-fragment flows after release and no aborted gather leaks.')
    p.append(f"  for(lane=0; lane<{lw}; lane=lane+1) set_lane(lane, 32'hE100_0000 + lane, 4'hF);")
    p.append(f"  drive_wide(32'hE1E1_0000, {lw * 4}, 1, 1, 0, 1, 3'b111);")
    p.append('  exp_groups=2; frag_index=0; guard=0; w_valid=1; stall_watch=0;')
    p.append('  while(frag_index==0) begin')
    p.append('    step(); guard=guard+1; if(guard>100) fail("ADAPTER.HOLD_RELEASE");')
    p.append('  end')
    p.append('  w_valid=0; reset_n=0; tick(); #1;')
    p.append('  if(s_n_valid || d_w_valid) fail("ADAPTER.RESET");')
    p.append('  tick(); reset_n=1; #1;')
    e1_base = ln - 1
    e1_data = 0xE100F000 + e1_base
    e1_addr = 0xE1E11000 + e1_base * LANE_BYTES
    p.append(f"  junk_lanes(32'hE1F0_0000); set_lane({e1_base}, 32'h{e1_data:08X}, 4'h3);")
    p.append(f"  drive_wide(32'h{e1_addr:08X}, 4, 1, 1, 0, 0, 3'b001); send_wide(1, 0);")
    slots = [(e1_data, 3) if j == e1_base % ln else zero_slot for j in range(ln)]
    p.append(narrow_call(n_idx + 1, 1, 1, 0, f"32'h{e1_addr:08X}", 4, 0, 0b001, slots))
    p.append(wide_call(w_idx + 5, 1, 1, 0, f"32'h{e1_addr:08X}", 4, 0, 0b001,
                       [(e1_data, 3) if lane == e1_base else zero_slot for lane in range(lw)]))
    n_idx += 2
    p.append('  direct=1;')
    p.append(f"  t_first=1; t_last=0; t_error=0; t_count={lw * 4};")
    p.append("  t_address=32'hE2E2_0000; t_write=1; t_protection=3'b100;")
    for j in range(ln):
        p.append(f"  t_data{j}=32'hE200_{j:04X}; t_be{j}=4'hF;")
    p.append('  set_d_ready(1); t_valid=1; #1;')
    p.append('  if(!d_n_ready || d_w_valid) fail("ADAPTER.HOLD_RELEASE");')
    p.append('  capture_narrow(); tick(); t_valid=0;')
    p.append('  reset_n=0; tick(); #1;')
    p.append('  if(d_w_valid || d_n_ready) fail("ADAPTER.RESET");')
    p.append('  tick(); reset_n=1; #1;')
    p.append('  t_first=1; t_last=1; t_error=0; t_count=4;')
    p.append("  t_address=32'hE2E2_2000; t_write=1; t_protection=3'b010;")
    p.append("  t_data0=32'hE200_00AA; t_be0=4'h1;")
    for j in range(1, ln):
        p.append(f"  t_data{j}=32'h5A5A_{j + 2:04X}; t_be{j}=4'h{(j * 5) % 15 + 1:X};")
    p.append('  t_valid=1; guard=0;')
    p.append('  while(!(d_w_valid && d_w_ready)) begin')
    p.append('    tick(); guard=guard+1; if(guard>100) fail("ADAPTER.HOLD_RELEASE");')
    p.append('  end')
    p.append('  capture_narrow(); capture_wide(); tick(); t_valid=0;')
    p.append(wide_call(w_idx + 6, 1, 1, 0, "32'hE2E2_2000", 4, 1, 0b010,
                       [(0xE20000AA, 1)] + [zero_slot] * (lw - 1)))
    p.append('  direct=0;')
    n_idx += 2
    # Phase F: a six-fragment stream under a toggling downstream ready, checked
    # against an independent no-wrap byte-range oracle.
    p.append('  // Phase F: a six-fragment stream under a toggling downstream ready,')
    p.append('  // checked against an independent no-wrap byte-range oracle.')
    p.append('  cycle=0;')
    p.append('  for(f=0; f<6; f=f+1) begin')
    p.append(f"    for(lane=0; lane<{lw}; lane=lane+1)")
    p.append("      set_lane(lane, 32'hF000_0000 + (f*256) + lane, ((f+lane)%15)+1);")
    p.append(f"    base_idx=(f%2==0) ? (f*3)%{lw} : 0;")
    p.append(f"    drive_wide(32'hF000_0000 + f*4096 + base_idx*4, (f%2==0)?4:{lw * 4},"
             " 1, 1, 0, f%2, f%8);")
    p.append(f"    groups=((base_idx<{ln})?1:0) + ((((f%2==0)?(base_idx+1):(base_idx+{lw}))>{ln})?1:0);")
    p.append('    exp_groups=groups; frag_index=0; wide_target=w_got+1; guard=0; stall_watch=0;')
    p.append('    w_valid=1;')
    p.append('    while(!(frag_index==exp_groups && w_got==wide_target)) begin')
    p.append('      set_d_ready(cycle%2); cycle=cycle+1;')
    p.append('      step();')
    p.append('      guard=guard+1; if(guard>500) fail("ADAPTER.HOLD_RELEASE");')
    p.append('    end')
    p.append('    w_valid=0;')
    p.append('    k=n_got-groups;')
    p.append('    for(gidx=0; gidx<groups; gidx=gidx+1) begin')
    p.append(f"      lane=((gidx==0 && base_idx>={ln})?1:gidx)*{ln};")
    p.append("      check_stream_narrow(k+gidx, (gidx==0), (gidx==groups-1),")
    p.append(f"                          32'hF000_0000 + f*4096 + base_idx*4, (f%2==0)?4:{lw * 4},")
    p.append("                          f%2, f%8, f, lane);")
    p.append('    end')
    p.append(f"    check_stream_wide(w_got-1, 32'hF000_0000 + f*4096 + base_idx*4,")
    p.append(f"                        (f%2==0)?4:{lw * 4}, f%2, f%8, f);")
    p.append('  end')
    p.append('  set_d_ready(1);')
    return '\n'.join(p), n_idx, w_idx + 13


def write_bench(work, wide_bytes=32, narrow_bytes=16):
    work = Path(work)
    shape = hop_shape(wide_bytes, narrow_bytes)
    lw = shape['lanes_wide']
    ln = shape['lanes_narrow']
    wide_total, narrow_total = totals(shape)
    tag = f'{wide_bytes}x{narrow_bytes}'
    declarations = []
    ser_ports = ['.nl_clk(clk)', '.nl_reset_n(reset_n)']
    des_ports = ['.nl_clk(clk)', '.nl_reset_n(reset_n)']
    # Serializer wide side (driven inputs) and narrow side (observed outputs).
    declarations += [
        'reg w_valid=0, w_first=0, w_last=0, w_error=0;',
        'reg [31:0] w_address=0; reg w_write=0; reg [2:0] w_protection=0;',
        f'reg [{COUNT_BITS - 1}:0] w_count=0;',
        'wire w_ready;',
    ]
    ser_ports += ['.nl_wide_valid(w_valid)', '.nl_wide_ready(w_ready)',
                  '.nl_wide_first(w_first)', '.nl_wide_last(w_last)',
                  '.nl_wide_error(w_error)', '.nl_wide_address(w_address)',
                  '.nl_wide_write(w_write)', '.nl_wide_protection(w_protection)',
                  '.nl_wide_count(w_count)']
    for i in range(lw):
        declarations += [f'reg [31:0] w_data{i}=0;', f'reg [3:0] w_be{i}=0;']
        ser_ports += [f'.nl_wide_data{i}(w_data{i})', f'.nl_wide_be{i}(w_be{i})']
    declarations += [
        'wire s_n_valid, s_n_first, s_n_last, s_n_error;',
        'wire [31:0] s_n_address; wire s_n_write; wire [2:0] s_n_protection;',
        f'wire [{COUNT_BITS - 1}:0] s_n_count;',
        'wire d_n_ready;',
    ]
    ser_ports += ['.nl_narrow_valid(s_n_valid)', '.nl_narrow_ready(d_n_ready)',
                  '.nl_narrow_first(s_n_first)', '.nl_narrow_last(s_n_last)',
                  '.nl_narrow_error(s_n_error)', '.nl_narrow_address(s_n_address)',
                  '.nl_narrow_write(s_n_write)', '.nl_narrow_protection(s_n_protection)',
                  '.nl_narrow_count(s_n_count)']
    for i in range(ln):
        declarations += [f'wire [31:0] s_n_data{i};', f'wire [3:0] s_n_be{i};']
        ser_ports += [f'.nl_narrow_data{i}(s_n_data{i})', f'.nl_narrow_be{i}(s_n_be{i})']
    # Direct-drive fragment and the mux into the deserializer narrow side.
    declarations += [
        'reg direct=0;',
        'reg t_valid=0, t_first=0, t_last=0, t_error=0;',
        'reg [31:0] t_address=0; reg t_write=0; reg [2:0] t_protection=0;',
        f'reg [{COUNT_BITS - 1}:0] t_count=0;',
    ]
    for i in range(ln):
        declarations += [f'reg [31:0] t_data{i}=0;', f'reg [3:0] t_be{i}=0;']
    narrow_mux = [('valid', ''), ('first', ''), ('last', ''), ('error', ''),
                  ('address', '[31:0] '), ('write', ''), ('protection', '[2:0] '),
                  ('count', f'[{COUNT_BITS - 1}:0] ')]
    for signal, width in narrow_mux:
        declarations.append(f'wire {width}n_{signal} = direct ? t_{signal} : s_n_{signal};')
    for i in range(ln):
        declarations.append(f'wire [31:0] n_data{i} = direct ? t_data{i} : s_n_data{i};')
        declarations.append(f'wire [3:0] n_be{i} = direct ? t_be{i} : s_n_be{i};')
    # Deserializer narrow inputs and wide outputs.
    des_ports += ['.nl_narrow_valid(n_valid)', '.nl_narrow_ready(d_n_ready)',
                  '.nl_narrow_first(n_first)', '.nl_narrow_last(n_last)',
                  '.nl_narrow_error(n_error)', '.nl_narrow_address(n_address)',
                  '.nl_narrow_write(n_write)', '.nl_narrow_protection(n_protection)',
                  '.nl_narrow_count(n_count)']
    for i in range(ln):
        des_ports += [f'.nl_narrow_data{i}(n_data{i})', f'.nl_narrow_be{i}(n_be{i})']
    declarations += [
        'wire d_w_valid, d_w_first, d_w_last, d_w_error;',
        'wire [31:0] d_w_address; wire d_w_write; wire [2:0] d_w_protection;',
        f'wire [{COUNT_BITS - 1}:0] d_w_count;',
        'reg d_w_ready=0;',
    ]
    des_ports += ['.nl_wide_valid(d_w_valid)', '.nl_wide_ready(d_w_ready)',
                  '.nl_wide_first(d_w_first)', '.nl_wide_last(d_w_last)',
                  '.nl_wide_error(d_w_error)', '.nl_wide_address(d_w_address)',
                  '.nl_wide_write(d_w_write)', '.nl_wide_protection(d_w_protection)',
                  '.nl_wide_count(d_w_count)']
    for i in range(lw):
        declarations += [f'wire [31:0] d_w_data{i};', f'wire [3:0] d_w_be{i};']
        des_ports += [f'.nl_wide_data{i}(d_w_data{i})', f'.nl_wide_be{i}(d_w_be{i})']
    # Capture stores for narrow transfers and wide completions.
    for i in range(ln):
        declarations += [f'reg [31:0] cap_d{i} [0:63];', f'reg [3:0] cap_b{i} [0:63];']
    declarations += [
        'reg cap_first [0:63]; reg cap_last [0:63]; reg cap_err [0:63];',
        f'reg [{COUNT_BITS - 1}:0] cap_count [0:63];',
        'reg [31:0] cap_addr [0:63]; reg cap_write [0:63]; reg [2:0] cap_prot [0:63];',
    ]
    for i in range(lw):
        declarations += [f'reg [31:0] ow_d{i} [0:31];', f'reg [3:0] ow_b{i} [0:31];']
    declarations += [
        'reg ow_first [0:31]; reg ow_last [0:31]; reg ow_err [0:31];',
        f'reg [{COUNT_BITS - 1}:0] ow_count [0:31];',
        'reg [31:0] ow_addr [0:31]; reg ow_write [0:31]; reg [2:0] ow_prot [0:31];',
    ]
    text = '''`timescale 1ns/1ps
module tb;
reg clk=0, reset_n=0;
always #5 clk=~clk;
DECLARATIONS
nocloom_request_serializer_TAG ser(SER_PORTS);
nocloom_request_deserializer_TAG des(DES_PORTS);
integer n_got=0, w_got=0, frag_index=0, exp_groups=1, wide_target=0;
integer guard=0, stall_watch=0, stall_snap=0, stall_seen=0, stall_limit=0;
reg snap_first=0, snap_last=0, snap_err=0;
reg [31:0] snap_addr=0; reg snap_write=0; reg [2:0] snap_prot=0;
integer cycle=0;
integer f=0, k=0, lane=0, groups=0, base_idx=0, gidx=0;
task fail(input string name);
  $display("ASSERT_FAIL:%s",name); $fatal(1);
endtask
task tick;
  @(posedge clk); #1;
  @(negedge clk); #1;
endtask
task set_d_ready(input integer value);
  d_w_ready=value; #1;
endtask
task reset_dut;
  reset_n=0; w_valid=0; t_valid=0; direct=0; d_w_ready=0; stall_watch=0;
  tick(); tick(); reset_n=1; #1;
endtask
task set_lane(input integer idx, input [31:0] data, input [3:0] be);
SET_LANE
endtask
task junk_lanes(input [31:0] seed);
JUNK_LANES
endtask
task drive_wide(input [31:0] addr, input integer count,
                input integer first, input integer last, input integer err,
                input integer wr, input [2:0] prot);
  w_address=addr; w_count=count; w_first=first; w_last=last; w_error=err;
  w_write=wr; w_protection=prot;
endtask
task capture_narrow;
  cap_first[n_got]=n_first; cap_last[n_got]=n_last; cap_err[n_got]=n_error;
  cap_count[n_got]=n_count;
  cap_addr[n_got]=n_address; cap_write[n_got]=n_write; cap_prot[n_got]=n_protection;
CAPTURE_NARROW
  n_got=n_got+1;
endtask
task capture_wide;
  ow_first[w_got]=d_w_first; ow_last[w_got]=d_w_last; ow_err[w_got]=d_w_error;
  ow_count[w_got]=d_w_count;
  ow_addr[w_got]=d_w_address; ow_write[w_got]=d_w_write; ow_prot[w_got]=d_w_protection;
CAPTURE_WIDE
  w_got=w_got+1;
endtask
task check_narrow(input integer idx,
                  input integer first, input integer last, input integer err,
                  input [31:0] addr, input integer count,
                  input integer wr, input [2:0] prot,
                  NARROW_ARGS);
  if(cap_first[idx]!==first[0] || cap_last[idx]!==last[0]) fail("ADAPTER.PACKET_BOUNDARY");
  if(cap_err[idx]!==err[0]) fail("ADAPTER.ERR_MERGE");
  if(cap_addr[idx]!==addr || cap_count[idx]!==count) fail("ADAPTER.CONTROL_FIELD");
  if(cap_write[idx]!==wr[0] || cap_prot[idx]!==prot) fail("ADAPTER.CONTROL_FIELD");
CHECK_NARROW
endtask
task check_wide(input integer idx,
                input integer first, input integer last, input integer err,
                input [31:0] addr, input integer count,
                input integer wr, input [2:0] prot,
                WIDE_ARGS);
  if(ow_first[idx]!==first[0] || ow_last[idx]!==last[0]) fail("ADAPTER.PACKET_BOUNDARY");
  if(ow_err[idx]!==err[0]) fail("ADAPTER.ERR_MERGE");
  if(ow_addr[idx]!==addr || ow_count[idx]!==count) fail("ADAPTER.CONTROL_FIELD");
  if(ow_write[idx]!==wr[0] || ow_prot[idx]!==prot) fail("ADAPTER.CONTROL_FIELD");
CHECK_WIDE
endtask
task check_stream_narrow(input integer idx, input integer first, input integer last,
                         input [31:0] addr, input integer count,
                         input integer wr, input [2:0] prot,
                         input integer frag, input integer group_lane);
  integer j, ln; reg [31:0] ed; reg [3:0] eb;
  if(cap_first[idx]!==first[0] || cap_last[idx]!==last[0]) fail("ADAPTER.PACKET_BOUNDARY");
  if(cap_err[idx]!==0) fail("ADAPTER.ERR_MERGE");
  if(cap_addr[idx]!==addr || cap_count[idx]!==count) fail("ADAPTER.CONTROL_FIELD");
  if(cap_write[idx]!==wr[0] || cap_prot[idx]!==prot) fail("ADAPTER.CONTROL_FIELD");
  for(j=0;j<LN;j=j+1) begin
    ln=group_lane+j;
    if(ln*4>=addr%MODW && ln*4<addr%MODW+count) begin
      ed=32'hF000_0000 + frag*256 + ln; eb=((frag+ln)%15)+1;
    end else begin
      ed=0; eb=0;
    end
STREAM_NARROW
  end
endtask
task check_stream_wide(input integer idx,
                       input [31:0] addr, input integer count,
                       input integer wr, input [2:0] prot,
                       input integer frag);
  integer j; reg [31:0] ed; reg [3:0] eb;
  if(ow_first[idx]!==1 || ow_last[idx]!==1) fail("ADAPTER.PACKET_BOUNDARY");
  if(ow_err[idx]!==0) fail("ADAPTER.ERR_MERGE");
  if(ow_addr[idx]!==addr || ow_count[idx]!==count) fail("ADAPTER.CONTROL_FIELD");
  if(ow_write[idx]!==wr[0] || ow_prot[idx]!==prot) fail("ADAPTER.CONTROL_FIELD");
  for(j=0;j<LW;j=j+1) begin
    if(j*4>=addr%MODW && j*4<addr%MODW+count) begin
      ed=32'hF000_0000 + frag*256 + j; eb=((frag+j)%15)+1;
    end else begin
      ed=0; eb=0;
    end
STREAM_WIDE
  end
endtask
// One cycle of the round-trip wait loop with the stall-stability, packet
// boundary and hold/release monitors. The entry delay lets driven values
// settle before the combinational sampling below.
task step;
  #1;
  if(stall_watch && n_valid && !d_n_ready) begin
    if(!stall_snap) begin
      stall_snap=1;
      snap_first=n_first; snap_last=n_last; snap_err=n_error;
      snap_addr=n_address; snap_write=n_write; snap_prot=n_protection;
      snap_count=n_count;
SNAP_LANES
    end else begin
      if(n_first!==snap_first || n_last!==snap_last || n_error!==snap_err ||
         n_address!==snap_addr || n_write!==snap_write || n_protection!==snap_prot ||
SNAP_COMPARE)
        fail("ADAPTER.HOLD_RELEASE");
    end
    stall_seen=stall_seen+1;
    if(stall_seen==stall_limit) set_d_ready(1);
  end
  if(n_valid && d_n_ready) begin
    if(n_first!==(frag_index==0)) fail("ADAPTER.PACKET_BOUNDARY");
    if(n_last!==(frag_index==exp_groups-1)) fail("ADAPTER.PACKET_BOUNDARY");
    capture_narrow();
    frag_index=frag_index+1;
  end
  if(d_w_valid && d_w_ready) capture_wide();
  if(w_valid && w_ready && frag_index!=exp_groups) fail("ADAPTER.HOLD_RELEASE");
  tick();
endtask
// Sends the driven wide fragment through the serializer/deserializer pair and
// waits for its single acceptance and the matching wide completion.
task send_wide(input integer groups_expected, input integer stall_cycles);
  exp_groups=groups_expected; frag_index=0; stall_watch=0; stall_snap=0;
  stall_seen=0; stall_limit=stall_cycles; guard=0; wide_target=w_got+1;
  if(stall_cycles>0) begin set_d_ready(0); stall_watch=1; end
  else set_d_ready(1);
  w_valid=1;
  while(!(frag_index==exp_groups && w_got==wide_target)) begin
    step();
    guard=guard+1;
    if(guard>500) fail("ADAPTER.HOLD_RELEASE");
  end
  w_valid=0; stall_watch=0; set_d_ready(1);
endtask
initial begin
  reset_dut();
PHASES
  if(n_got!==NARROW_TOTAL) fail("ADAPTER.FRAGMENT_COUNT");
  if(w_got!==WIDE_TOTAL) fail("ADAPTER.FRAGMENT_COUNT");
  $display("WIDTH_ADAPTER_PASS deliveries=%0d narrow=%0d", w_got, n_got);
  $finish;
end
endmodule
'''
    set_lane = []
    for i in range(lw):
        set_lane.append(f'  if(idx=={i}) begin w_data{i}=data; w_be{i}=be; end')
    junk = []
    for i in range(lw):
        junk.append(f"  w_data{i}=32'h5A5A_0000 ^ seed ^ {i}; w_be{i}=4'h5 ^ {i};")
    capture_narrow = []
    for i in range(ln):
        capture_narrow.append(f'  cap_d{i}[n_got]=n_data{i}; cap_b{i}[n_got]=n_be{i};')
    capture_wide = []
    for i in range(lw):
        capture_wide.append(f'  ow_d{i}[w_got]=d_w_data{i}; ow_b{i}[w_got]=d_w_be{i};')
    # Used-lane expectations are always nonzero, so a zero expectation marks an
    # unused slot or lane: a mismatch there is a zero-fill (leak) violation,
    # everywhere else a byte-conservation violation.
    check_narrow = []
    for i in range(ln):
        check_narrow.append(f'  if(cap_d{i}[idx]!==d{i}) begin\n'
                            f'    if(d{i}==0) fail("ADAPTER.ZERO_FILL");\n'
                            f'    fail("ADAPTER.BYTE_CONSERVATION"); end')
        check_narrow.append(f'  if(cap_b{i}[idx]!==b{i}) begin\n'
                            f'    if(b{i}==0) fail("ADAPTER.ZERO_FILL");\n'
                            f'    fail("ADAPTER.BYTE_CONSERVATION"); end')
    check_wide = []
    for i in range(lw):
        check_wide.append(f'  if(ow_d{i}[idx]!==d{i}) begin\n'
                          f'    if(d{i}==0) fail("ADAPTER.ZERO_FILL");\n'
                          f'    fail("ADAPTER.BYTE_CONSERVATION"); end')
        check_wide.append(f'  if(ow_b{i}[idx]!==b{i}) begin\n'
                          f'    if(b{i}==0) fail("ADAPTER.ZERO_FILL");\n'
                          f'    fail("ADAPTER.BYTE_CONSERVATION"); end')
    stream_narrow = []
    for i in range(ln):
        stream_narrow.append(f'    if(j=={i} && (cap_d{i}[idx]!==ed || cap_b{i}[idx]!==eb))\n'
                             f'      fail("ADAPTER.BYTE_CONSERVATION");')
    stream_wide = []
    for i in range(lw):
        stream_wide.append(f'    if(j=={i} && (ow_d{i}[idx]!==ed || ow_b{i}[idx]!==eb))\n'
                           f'      fail("ADAPTER.BYTE_CONSERVATION");')
    snap_lanes = []
    for i in range(ln):
        snap_lanes.append(f'      snap_d{i}=n_data{i}; snap_b{i}=n_be{i};')
    snap_compare = [f'         n_count!==snap_count ||']
    snap_compare.append('         ' + ' ||\n         '.join(
        [f'n_data{i}!==snap_d{i}' for i in range(ln)] + [f'n_be{i}!==snap_b{i}' for i in range(ln)]))
    narrow_args = []
    for i in range(ln):
        narrow_args.append(f'input [31:0] d{i}')
    for i in range(ln):
        narrow_args.append(f'input [3:0] b{i}')
    wide_args = []
    for i in range(lw):
        wide_args.append(f'input [31:0] d{i}')
    for i in range(lw):
        wide_args.append(f'input [3:0] b{i}')
    snap_decls = []
    for i in range(ln):
        snap_decls.append(f'reg [31:0] snap_d{i}=0; reg [3:0] snap_b{i}=0;')
    snap_decls.append(f'reg [{COUNT_BITS - 1}:0] snap_count=0;')
    phases, _, _ = _phases(shape)
    text = text.replace('DECLARATIONS', '\n'.join(declarations) + '\n' + '\n'.join(snap_decls))
    text = text.replace('TAG', tag)
    text = text.replace('SER_PORTS', ','.join(ser_ports))
    text = text.replace('DES_PORTS', ','.join(des_ports))
    text = text.replace('SET_LANE', '\n'.join(set_lane))
    text = text.replace('JUNK_LANES', '\n'.join(junk))
    text = text.replace('CAPTURE_NARROW', '\n'.join(capture_narrow))
    text = text.replace('CAPTURE_WIDE', '\n'.join(capture_wide))
    text = text.replace('CHECK_NARROW', '\n'.join(check_narrow))
    text = text.replace('CHECK_WIDE', '\n'.join(check_wide))
    text = text.replace('STREAM_NARROW', '\n'.join(stream_narrow))
    text = text.replace('STREAM_WIDE', '\n'.join(stream_wide))
    text = text.replace('SNAP_LANES', '\n'.join(snap_lanes))
    text = text.replace('SNAP_COMPARE', '\n'.join(snap_compare))
    text = text.replace('NARROW_ARGS', ','.join(narrow_args))
    text = text.replace('WIDE_ARGS', ','.join(wide_args))
    text = text.replace('MODW', str(shape['wide_bytes']))
    text = text.replace('LN', str(ln))
    text = text.replace('LW', str(lw))
    text = text.replace('PHASES', phases)
    text = text.replace('NARROW_TOTAL', str(narrow_total))
    text = text.replace('WIDE_TOTAL', str(wide_total))
    (work / 'tb.sv').write_text(text)
