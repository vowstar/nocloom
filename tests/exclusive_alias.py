"""Reservations and write interference use the target's local byte addresses."""

import argparse
from pathlib import Path

from exclusive_regression import configuration, run, target


def scenario():
    targets, body = [], []
    for i, protocol in enumerate(('axi4', 'apb4', 'axi4_lite', 'ahb_lite', 'wishbone')):
        name = f't{i}'
        a, b, c = (0x10000 + i * 0x10000 + k * 0x1000 for k in range(3))
        entry = target(name, protocol, a)
        # A and B alias. C has the same page offset but reaches different local bytes.
        entry['mappings'] += [{'base': hex(b), 'size': '0x1000'},
                              {'base': hex(c), 'size': '0x100', 'local_address': '0x100'}]
        targets.append(entry)
        body.append(f'''$display("CASE {protocol} aliases");
s0_gap = 3; s1_gap = 2;
s1_wq_data[0] = 32'h2468ace0; s1_wq_strb[0] = 15;
write_s1(32'h{a + 0x60:x}, 0, 2, 1, 0, 1, 0);
read_s0(32'h{b + 0x60:x}, 0, 2, 1, 1, 1, 0);
check(s0_rresp[0] == 1 && s0_rdata[0] == 32'h2468ace0, "{name}_ALIAS_READ");
s1_wq_data[0] = 32'habcdef01;
write_s1(32'h{a + 0x60:x}, 0, 2, 1, 0, 1, 0);
s0_wq_data[0] = 32'hdeadbeef; s0_wq_strb[0] = 15;
write_s0(32'h{b + 0x60:x}, 0, 2, 1, 1, 1, 0);
check(s0_bresp == 0, "{name}_ALIAS_WRITE_CLEARS");
read_s1(32'h{a + 0x60:x}, 0, 2, 1, 0, 1, 0);
check(s1_rdata[0] == 32'habcdef01, "{name}_INTERVENING_DATA");
// A matching write through the other alias succeeds.
read_s0(32'h{b + 0x60:x}, 0, 2, 1, 1, 1, 0);
write_s0(32'h{a + 0x60:x}, 0, 2, 1, 1, 1, 0);
check(s0_bresp == 1, "{name}_CROSS_ALIAS_MATCH");
// The same offset in a disjoint local page leaves the reservation intact.
read_s0(32'h{b + 0x60:x}, 0, 2, 1, 1, 1, 0);
write_s1(32'h{c + 0x60:x}, 0, 2, 1, 0, 1, 0);
write_s0(32'h{b + 0x60:x}, 0, 2, 1, 1, 1, 0);
check(s0_bresp == 1, "{name}_DISTINCT_LOCAL_BYTES");
// Clearing uses the entire write range, including a later beat and WRAP head.
read_s0(32'h{b + 0x70:x}, 0, 2, 1, 1, 1, 0);
for (k = 0; k < 4; k = k + 1) begin
 s1_wq_data[k] = 32'h11223344; s1_wq_strb[k] = 15;
end
write_s1(32'h{a + 0x6c:x}, 3, 2, 1, 0, 1, 0);
write_s0(32'h{b + 0x70:x}, 0, 2, 1, 1, 1, 0);
check(s0_bresp == 0, "{name}_INCR_OVERLAP");
read_s0(32'h{b + 0x60:x}, 0, 2, 1, 1, 1, 0);
write_s1(32'h{a + 0x6c:x}, 3, 2, 2, 0, 1, 0);
write_s0(32'h{b + 0x60:x}, 0, 2, 1, 1, 1, 0);
check(s0_bresp == 0, "{name}_WRAP_OVERLAP");
''')
        if protocol == 'axi4':
            for held in ('w', 'b'):
                setup = 't0_whold = 1;' if held == 'w' else 't0_blat = 80;'
                wait = ('wait (t0_awcount > mark_aw);' if held == 'w'
                        else 'wait (t0_wcount == mark_w + 1);')
                body.append(f'''// An alias read during a held write cannot create a reservation.
read_s0(32'h{b + 0x80:x}, 0, 2, 1, 1, 1, 0);
{setup}
mark_aw = t0_awcount; mark_w = t0_wcount;
fork
 begin write_s1(32'h{a + 0x60:x}, 0, 2, 1, 0, 1, 0); end
 begin
  {wait}
  read_s0(32'h{b + 0x60:x}, 0, 2, 1, 1, 1, 0);
  check(s0_rresp[0] == 1, "HELD_{held}_READ_EXOKAY");
  @(negedge clk_main); t0_whold = 0;
 end
join
t0_blat = 0;
write_s0(32'h{b + 0x60:x}, 0, 2, 1, 1, 1, 0);
check(s0_bresp == 0, "HELD_{held}_NO_NEW_RESERVATION");
write_s0(32'h{b + 0x80:x}, 0, 2, 1, 1, 1, 0);
check(s0_bresp == 1, "HELD_{held}_PRIOR_RESERVATION");
''')
    return configuration(targets), '\n'.join(body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    run(args.cli, args.work.resolve(), *scenario())


if __name__ == '__main__':
    main()
