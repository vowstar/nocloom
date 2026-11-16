"""Check that a network monitor separates initiators and full-width AxIDs."""

import argparse
from pathlib import Path

from exclusive_regression import configuration, run, target


TRANSACTIONS = """
s0_wq_data[0] = 32'h11223344; s0_wq_strb[0] = 15;
s1_wq_data[0] = 32'h55667788; s1_wq_strb[0] = 15;
mark_w = regs_wcount;
read_s0(32'h10004, 0, 2, 1, 1, 1, 0);
check(s0_rresp[0] == 1, "SOURCE_RESERVATION");
write_s1(32'h10004, 0, 2, 1, 1, 1, 0);
check(s1_bresp == 0 && regs_wcount == mark_w, "SOURCE_IDENTITY");
write_s0(32'h10004, 0, 2, 1, 1, 1, 0);
check(s0_bresp == 1 && regs_wcount == mark_w + 1, "SOURCE_MATCH");
read_s1(32'h10004, 0, 2, 1, 1, 9, 0);
check(s1_rresp[0] == 1 && s1_rdata[0] == 32'h11223344, "HIGH_ID_RESERVATION");
write_s1(32'h10004, 0, 2, 1, 1, 1, 0);
check(s1_bresp == 0 && regs_wcount == mark_w + 1, "FULL_AXID");
write_s1(32'h10004, 0, 2, 1, 1, 9, 0);
check(s1_bresp == 1 && regs_wcount == mark_w + 2, "HIGH_ID_MATCH");
read_s0(32'h10004, 0, 2, 1, 0, 1, 0);
check(s0_rresp[0] == 0 && s0_rdata[0] == 32'h55667788, "FINAL_DATA");
"""


def config():
    regs = target('regs', 'apb4', 0x10000)
    regs.pop('exclusive_owner_flags')
    cfg = configuration([regs])
    cfg['initiators'][1]['id_width'] = 4
    return cfg


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', type=Path, required=True)
    args = parser.parse_args()
    run(args.cli, args.work, config(), TRANSACTIONS)
