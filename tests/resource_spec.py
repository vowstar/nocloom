"""Write the CTest resource specification for one test run.

Tests that need much memory under some simulation engine reserve it with the
RESOURCE_GROUPS property "memory_mib:N". CTest runs this script first, as the
setup of the fixture those tests require, and keeps the sum of N over the
running tests within the capacity written here.

  python3 tests/resource_spec.py --output FILE --largest MIB

The capacity in MiB is NOCLOOM_TEST_MEMORY_MIB when it is set. Otherwise it is
the memory available now, less a reserve of 10% of physical memory and at
least 2 GiB. On Linux the available memory is MemAvailable, or the room left
under a cgroup v2 memory limit when that is smaller. A capacity below the
largest reservation is raised to it, so that test can still run, alone.
"""

import argparse
import json
import os
from pathlib import Path

MIB = 1 << 20
RESERVE_MIN_MIB = 2048


def physical_mib():
    return os.sysconf('SC_PHYS_PAGES') * os.sysconf('SC_PAGE_SIZE') // MIB


def available_mib():
    """MemAvailable, or physical memory where /proc/meminfo does not exist."""
    try:
        text = Path('/proc/meminfo').read_text()
    except OSError:
        return physical_mib()
    for line in text.splitlines():
        name, _, value = line.partition(':')
        if name == 'MemAvailable':
            return int(value.split()[0]) // 1024
    return physical_mib()


def cgroup_room_mib():
    """The smallest room under a memory limit of this process's cgroup v2 or its parents."""
    try:
        lines = Path('/proc/self/cgroup').read_text().splitlines()
    except OSError:
        return None
    relative = next((line[3:] for line in lines if line.startswith('0::')), None)
    if relative is None:
        return None
    room = None
    group = Path('/sys/fs/cgroup') / relative.lstrip('/')
    for directory in (group, *group.parents):
        try:
            limit = (directory / 'memory.max').read_text().strip()
            used = int((directory / 'memory.current').read_text())
        except (OSError, ValueError):
            if directory == Path('/sys/fs/cgroup'):
                break
            continue
        if limit != 'max':
            left = max(int(limit) - used, 0) // MIB
            room = left if room is None else min(room, left)
        if directory == Path('/sys/fs/cgroup'):
            break
    return room


def capacity_mib(largest, environment):
    explicit = environment.get('NOCLOOM_TEST_MEMORY_MIB')
    if explicit is not None:
        if not explicit.isdigit() or int(explicit) < 1:
            raise SystemExit('NOCLOOM_TEST_MEMORY_MIB must be a positive integer')
        found, source = int(explicit), 'NOCLOOM_TEST_MEMORY_MIB'
    else:
        available = available_mib()
        room = cgroup_room_mib()
        if room is not None and room < available:
            available, source = room, 'cgroup memory limit'
        else:
            source = 'available memory'
        reserve = max(RESERVE_MIN_MIB, physical_mib() // 10)
        found = max(available - reserve, 0)
        source += f' less a {reserve} MiB reserve'
    if found < largest:
        return largest, f'{source} gives {found} MiB; the largest reservation runs alone'
    return found, source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--largest', type=int, required=True, help='largest reservation in MiB')
    args = parser.parse_args()
    if args.largest < 1:
        parser.error('--largest must be positive')
    capacity, reason = capacity_mib(args.largest, os.environ)
    spec = {'version': {'major': 1, 'minor': 0},
            'local': [{'memory_mib': [{'id': '0', 'slots': capacity}]}]}
    args.output.write_text(json.dumps(spec, indent=1) + '\n')
    print(f'TEST_MEMORY {capacity} MiB ({reason})')


if __name__ == '__main__':
    main()
