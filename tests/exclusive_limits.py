"""An exclusive burst must fit its pair limits and selected mapping."""

import argparse
from pathlib import Path

from exclusive_regression import configuration, run, target


def scenario(width):
    targets, limits, body, guards = [], [], [], []
    for p, protocol in enumerate(('axi3', 'axi4', 'axi4_lite', 'ahb_lite', 'apb4')):
        for j, rule in enumerate(('max', 'boundary', 'window')):
            name = f't{p}{j}'
            base = 0x10000 + (3 * p + j) * 0x1000
            targets.append(target(name, protocol, base, 16 if rule == 'window' else 4096))
            if rule != 'window':
                for source in ('s0', 's1'):
                    limits.append({'source': source, 'target': name,
                                   'max_bytes' if rule == 'max' else 'boundary_bytes': 16})
            pins = {'axi3': ('awvalid', 'arvalid'), 'axi4': ('awvalid', 'arvalid'),
                    'axi4_lite': ('awvalid', 'arvalid'), 'ahb_lite': ('hsel',),
                    'apb4': ('psel',)}[protocol]
            for pin in pins:
                guards.append(f'if (reject_expected) check(!nl_t_{name}_{pin}, '
                              f'"{name}_{rule}_NO_TARGET_{pin}");')
            # Both full-word and packed narrow bursts exceed 16 bytes.
            for size, beats in ((2, 8), (1, 16)):
                body.append(f'''$display("CASE {protocol} {rule} size={size}");
reject_expected = 1;
read_s0(32'h{base:x}, {beats - 1}, {size}, 1, 1, 1, 3);
for (k = 0; k < {beats}; k = k + 1) begin
 check(s0_rresp[k] == 2, "{name}_{rule}_REJECT_RRESP");
 check(s0_rdata[k] == 0, "{name}_{rule}_REJECT_RDATA");
 s0_wq_data[k] = 32'h76543210;
 s0_wq_strb[k] = {width // 8}'h{(1 << (1 << size)) - 1:x} << ((k * {1 << size}) % {width // 8});
end
write_s0(32'h{base:x}, {beats - 1}, {size}, 1, 1, 1, 3);
check(s0_bresp == 2, "{name}_{rule}_REJECT_BRESP");
@(negedge clk_main); reject_expected = 0;
''')
            body.append(f'''// The exact limit succeeds, followed by ordinary traffic of the same ID.
read_s0(32'h{base:x}, 3, 2, 1, 1, 1, 3);
for (k = 0; k < 4; k = k + 1) begin
 check(s0_rresp[k] == 1, "{name}_FIT_RRESP");
 s0_wq_data[k] = {{{width // 32}{{32'h12345678 + k}}}};
 s0_wq_strb[k] = {width // 8}'d15 << (4 * (k % {width // 32}));
end
write_s0(32'h{base:x}, 3, 2, 1, 1, 1, 3);
check(s0_bresp == 1, "{name}_FIT_BRESP");
read_s0(32'h{base:x}, 3, 2, 1, 0, 1, 3);
for (k = 0; k < 4; k = k + 1) begin
 check(s0_rresp[k] == 0, "{name}_FOLLOW_RRESP");
 check(((s0_rdata[k] >> (32 * (k % {width // 32}))) & 32'hffffffff) == 32'h12345678 + k,
       "{name}_FOLLOW_RDATA");
end
s0_wq_data[0] = 0; s0_wq_strb[0] = 15;
write_s0(32'h{base:x}, 0, 2, 1, 0, 1, 3);
check(s0_bresp == 0, "{name}_FOLLOW_BRESP");
''')
    if width == 128:
        outer = target('outer', 'axi4', 0x100000, 256)
        outer['len_width'] = 8
        outer['mappings'].append({'base': '0x200000', 'size': '0x1000'})
        targets += [outer, target('inner', 'apb4', 0x100040, 16)]
        guards.append('if (reject_expected) check(!nl_t_outer_arvalid && '
                      '!nl_t_outer_awvalid, "NESTED_BOUNDARY_NO_TARGET");')
        for burst in (1, 2):
            body.append(f'''$display("CASE nested boundary burst={burst}");
reject_expected = 1;
read_s0(32'h100080, 15, 3, {burst}, 1, 1, 3);
for (k = 0; k < 16; k = k + 1) begin
 check(s0_rresp[k] == 2, "NESTED_BOUNDARY_RRESP");
 s0_wq_data[k] = 0; s0_wq_strb[k] = 16'hff << (8 * (k % 2));
end
write_s0(32'h100080, 15, 3, {burst}, 1, 1, 3);
check(s0_bresp == 2, "NESTED_BOUNDARY_BRESP");
@(negedge clk_main); reject_expected = 0;
''')
        body.append('''// Another mapping of the same target retains its larger boundary.
read_s0(32'h200000, 15, 3, 1, 1, 1, 3);
for (k = 0; k < 16; k = k + 1) begin
 check(s0_rresp[k] == 1, "OTHER_MAPPING_FIT_RRESP");
 s0_wq_data[k] = 0; s0_wq_strb[k] = 16'hff << (8 * (k % 2));
end
write_s0(32'h200000, 15, 3, 1, 1, 1, 3);
check(s0_bresp == 1, "OTHER_MAPPING_FIT_BRESP");
''')
    cfg = configuration(targets, width)
    cfg['transport']['packet_limits'] = limits
    return cfg, '\n'.join(body), ('always @(posedge clk_main) if (reset_n) begin\n'
                                  + '\n'.join(guards) + '\nend')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    for width in (32, 128):
        run(args.cli, args.work.resolve() / str(width), *scenario(width))


if __name__ == '__main__':
    main()
