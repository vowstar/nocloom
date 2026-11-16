"""AXI3 exclusive output cacheability is cleared after attribute mapping."""

import argparse
from pathlib import Path

from exclusive_regression import configuration, run, target


def scenario():
    targets, body, checks = [], [], []
    for i, (protocol, mode, mapped) in enumerate((
            ('axi3', 'target', False), ('axi3', 'target', True),
            ('axi4', 'target', False), ('axi3', 'network', True))):
        name, base = f't{i}', 0x10000 + 0x1000 * i
        entry = target(name, protocol, base, mode=mode)
        if mapped:
            entry['attributes'] = {'read_cache': [1, 1, 1, 1],
                                   'write_cache': [1, 1, 1, 1]}
        targets.append(entry)
        for channel in ('ar', 'aw'):
            checks.append(f'''if (nl_t_{name}_{channel}valid && nl_t_{name}_{channel}ready) begin
 check(nl_t_{name}_{channel}cache == expected_cache, "{name}_{channel.upper()}CACHE");
 check(nl_t_{name}_{channel}lock == expected_lock, "{name}_{channel.upper()}LOCK");
end''')
        for lock in (1, 0):
            for cache in (range(16) if lock else (0, 1, 2, 3, 6, 7, 10, 11, 14, 15)):
                low = cache & 3
                lookup = int(bool(cache & 12))
                read = 15 if mapped else low | (cache & 4) | (lookup << 3)
                write = 15 if mapped else low | (lookup << 2) | (cache & 8)
                # AXI3 reserves RA or WA without C, so only bufferable remains.
                if lock and protocol == 'axi3' and mode == 'target':
                    read &= 1
                    write &= 1
                response = 1 if lock else 0
                target_lock = lock if mode == 'target' else 0
                body.append(f'''$display("CASE {name} lock={lock} cache={cache}");
expected_lock = {target_lock}; expected_cache = {read};
read_s0(32'h{base:x}, 0, 2, 1, {lock}, 1, {cache});
check(s0_rresp[0] == {response}, "{name}_RRESP");
s0_wq_data[0] = 32'h2468ace0; s0_wq_strb[0] = 15;
expected_cache = {write};
write_s0(32'h{base:x}, 0, 2, 1, {lock}, 1, {cache});
check(s0_bresp == {response}, "{name}_BRESP");
''')
    return configuration(targets), '\n'.join(body), (
        'integer expected_cache = 0, expected_lock = 0;\n'
        'always @(posedge clk_main) if (reset_n) begin\n' + '\n'.join(checks) + '\nend')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    run(args.cli, args.work.resolve(), *scenario())


if __name__ == '__main__':
    main()
