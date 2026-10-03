"""Configuration-only consumers of source conversion and bounded capacities.

Reuse the installed and transaction tests' validation matrices without repeating
installation or protocol simulations when only acceptance expectations change.
"""
import argparse
import json
import tempfile
from pathlib import Path
from run_multi_outstanding import rejections as multi_rejections
from run_lite_outstanding import rejections as lite_rejections


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cli', required=True)
    parser.add_argument('--work', required=True)
    args = parser.parse_args()
    base = Path(args.work).resolve()
    base.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=base))
    (base / 'latest.txt').write_text(str(work)+'\n')
    rows = []
    for family, check, modes in (
            ('multi', multi_rejections, ('graph', 'funnel')),
            ('lite', lite_rejections, ('mixed', 'exclusive', 'clocked_bounded', 'multi_target', 'perwidth'))):
        for mode in modes:
            case = work / (family+'-'+mode)
            case.mkdir()
            rows.append({'family': family, 'mode': mode, 'cases': check(args, case, mode)})
    # tests/verif/cases/config_endpoint_widths.py covers downsizing acceptance at every width.
    (work/'results.json').write_text(json.dumps(rows,indent=1)+'\n')
    print('NOCLOOM_PASS conversion configurations groups='+str(len(rows)))

if __name__ == '__main__':
    main()
