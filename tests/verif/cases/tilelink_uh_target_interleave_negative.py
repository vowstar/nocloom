"""Response path and decode miss faults that the interleave scoreboard must find."""

import runpy
from pathlib import Path

configuration = runpy.run_path(str(Path(__file__).with_name("tilelink_uh_target_interleave.py")))[
    "configuration"
]
# selector: (name, rules that can report it)
FAULTS = {
    "interleave": {
        1: ("swapped_response_sources", ("UH.R.DATA",)),
        2: ("corrupt_response_word", ("UH.R.DATA",)),
        3: ("lost_final_response_beat", ("UH.WATCHDOG", "UH.R.DATA", "UH.R.LAST")),
        4: ("changed_read_id", ("UH.R.ID", "UH.R.DATA", "UH.R.LAST")),
        5: ("corrupt_write_word", ("UH.W.DATA", "UH.W.UNSELECTED")),
        6: ("early_read_last", ("UH.R.LAST",)),
    },
    "miss": {
        1: ("swapped_response_sources", ("UH.R.DATA",)),
        2: ("corrupt_response_word", ("UH.R.DATA",)),
        7: ("miss_read_okay", ("UH.R.RESP",)),
        8: ("miss_read_data", ("UH.R.MISS_DATA",)),
        9: ("miss_write_id", ("UH.B.ID", "UH.B.RESP", "UH.B.EARLY", "UH.B.ORDER")),
        10: ("miss_target_access", ("UH.TL.UNEXPECTED",)),
    },
}

CASE = dict(
    contracts=["TL.ACCESS"],
    faults=[
        dict(id=f"{profile}_{name}", contract="TL.ACCESS")
        for profile, faults in FAULTS.items()
        for name, _ in faults.values()
    ],
)
