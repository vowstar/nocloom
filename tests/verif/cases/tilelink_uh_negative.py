"""TileLink UH runtime fault contracts and message configuration."""

import runpy
from pathlib import Path

configuration = runpy.run_path(str(Path(__file__).with_name("tilelink_uh.py")))[
    "configuration"
]
FAULTS = {
    1: ("changing_d_source", ("TL.4.2.D_OWNER", "UH.D.IDENTITY")),
    2: ("first_denied_only", ("TL.4.2.D_OWNER", "UH.D.IDENTITY")),
    3: ("early_atomic_response", ("UH.LOCAL.EARLY_D",)),
    4: (
        "extra_ar_beat",
        ("UH.D.", "UH.AXI.", "UH.READ.FOOTPRINT", "UH.BOUNDED_PROGRESS"),
    ),
    5: ("hint_ar_leak", ("AXI.AR.HOLD", "UH.LOCAL.FOOTPRINT", "UH.READ.FOOTPRINT")),
    6: ("b_waits_for_d", ("UH.OWNER.B",)),
    7: ("ignored_continuation_mask", ("UH.D.DATA",)),
    8: ("rejected_continuation_write", ("UH.D.DATA",)),
}

CASE = dict(
    contracts=["TL.ACCESS"],
    faults=[dict(id=name, contract="TL.ACCESS") for name, _ in FAULTS.values()],
)
