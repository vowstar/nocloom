"""Exercise full-width strobe patterns and address offsets on an AHB target."""

from collections import Counter
import random
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ahb_plan  # noqa: E402
from ahb_outlet import CASE as OUTLET, config, every_use, fault  # noqa: E402

WIDTH = int(globals().get("PARAMS", {}).get("data_bits", "32"))
LANES = WIDTH // 8
BASE = 0x1000
assert LANES in (4, 8, 16)


def packet(t, base, size, burst, masks, index):
    t.strobes("s0", masks, lanes=LANES)
    t.write("s0", base, size=size, burst=burst, beats=len(masks), strobe="table", salt=index)
    t.drain(limit=2048, label="WRITE")
    # Read back the physical bytes from the target's memory model.
    span = (1 if burst == "fixed" else len(masks)) << size
    start = base & ~((span if burst == "wrap" else 1 << size) - 1)
    first, last = start // LANES, (start + span - 1) // LANES
    t.read("s0", first * LANES, size=LANES.bit_length() - 1, beats=last - first + 1)
    t.drain(limit=2048, label="READ")


def scenario(t, seed, **_):
    rng = random.Random(seed)
    t.reset(4)
    t.delay(2)
    size = LANES.bit_length() - 1
    # Run every 4-byte mask in one incrementing burst.
    # The wider targets run 2048 full-width random strobe patterns each.
    if LANES == 4:
        masks = [3, 0, 1, 2, *range(4, 16)]
        packet(t, BASE + 0x100, size, "incr", masks, 0)
    else:
        for index in range(128):
            packet(t, BASE + 0x100, size, "incr",
                   [rng.getrandbits(LANES) for _ in range(16)], index)
    # A natural 4-beat WRAP starts on a beat boundary. FIXED and INCR
    # also exercise every unaligned suffix of a size container.
    for burst in ("fixed", "incr", "wrap"):
        for subsize in range(size + 1):
            step = 1 << subsize
            for offset in range(step if burst != "wrap" else 1):
                address = BASE + 0x200 + step + offset
                if burst == "wrap":
                    addresses = [BASE + 0x200 + (step + i * step) % (4 * step) for i in range(4)]
                elif burst == "fixed":
                    addresses = [address] * 4
                else:
                    addresses = [address] + [(address & ~(step - 1)) + i * step for i in range(1, 4)]
                masks = []
                for a in addresses:
                    valid = ((1 << step) - 1) << ((a & ~(step - 1)) % LANES)
                    valid &= ~((1 << (a % LANES)) - 1)
                    masks.append(valid & rng.getrandbits(LANES))
                packet(t, address, subsize, burst, masks, offset)
    t.end()


def oracle(checker, events, sources, targets):
    segment = ahb_plan.Segment("s0", "t0", BASE, 0x1000, LANES, LANES, [])
    return ahb_plan.judge(checker, events, sources, targets, [segment], Counter())[:8]


profile = config("fixed_single")
profile["dut"]["params"]["data_bits"] = str(WIDTH)
profile["model"]["targets"]["t0"].update(bus_bytes=LANES, engine_bytes=LANES)
profile["faults"] = [
    fault("unstrobed_byte", ["TARGET_FOOTPRINT", "TARGET_WRITE_BYTES", "ORACLE"],
          "APB.TARGET.ACCESS", **every_use("bus.write_size", 1),
          note="two-byte HSIZE sends an additional byte on a one-byte write"),
    fault("split_block", ["ORACLE"], "APB.TARGET.ACCESS",
          **every_use("bus.write_fits.1", 0)),
    fault("misalign_block", ["ORACLE", "TARGET_FOOTPRINT"], "APB.TARGET.ACCESS",
          op="set_driver", port="nl_ahb_haddr",
          **{"with": {"op": "BitOr", "args": [{"self": True}, {"const": 1}]}},
          note="use an odd address for a two-byte transfer"),
] if WIDTH == 32 else []
CASE = dict(OUTLET, name=f"ahb_write_masks_{WIDTH}", configs=[profile], oracle=oracle,
            contracts=["APB.TARGET.ACCESS"], max_cycles=250000, run_timeout=35)
