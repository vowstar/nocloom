"""Check the CTest resource specification that resource_spec.py writes."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'resource_spec.py'


def spec(work, largest, memory=None):
    output = Path(tempfile.mkdtemp(prefix='spec-', dir=work)) / 'spec.json'
    environment = {k: v for k, v in os.environ.items() if k != 'NOCLOOM_TEST_MEMORY_MIB'}
    if memory is not None:
        environment['NOCLOOM_TEST_MEMORY_MIB'] = memory
    result = subprocess.run([sys.executable, str(SCRIPT), '--output', str(output),
                             '--largest', str(largest)], env=environment,
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        return None, result.stdout + result.stderr
    data = json.loads(output.read_text())
    assert data['version'] == {'major': 1, 'minor': 0}, data
    assert len(data['local']) == 1 and list(data['local'][0]) == ['memory_mib'], data
    [resource] = data['local'][0]['memory_mib']
    assert resource['id'] == '0', resource
    return resource['slots'], result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work', required=True)
    args = parser.parse_args()
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='run-', dir=work))
    slots, text = spec(work, 1024, '4096')
    assert slots == 4096, (slots, text)
    # A capacity below the largest reservation still lets that test run alone.
    slots, text = spec(work, 1024, '512')
    assert slots == 1024 and 'runs alone' in text, (slots, text)
    for wrong in ('0', '-1', '2G', ''):
        slots, text = spec(work, 1024, wrong)
        assert slots is None and 'NOCLOOM_TEST_MEMORY_MIB' in text, (wrong, slots, text)
    # Measured from this machine: never below the largest reservation.
    slots, text = spec(work, 1, None)
    assert isinstance(slots, int) and slots >= 1 and 'reserve' in text, (slots, text)
    slots, text = spec(work, 1 << 30, None)
    assert slots == 1 << 30 and 'runs alone' in text, (slots, text)
    print('PASS resource specification')


if __name__ == '__main__':
    main()
