"""TileLink UH size encodings and capacity diagnostic fields."""

from copy import deepcopy
import runpy
from pathlib import Path

configuration = runpy.run_path(str(Path(__file__).with_name("tilelink_uh.py")))[
    "configuration"
]

CASES = []
for maximum in (4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096):
    width = 2 if maximum <= 8 else 3 if maximum <= 128 else 4
    cfg = configuration(maximum, 4, 4)
    cfg["initiators"][0]["size_width"] = width
    CASES.append(dict(name=f"size_{maximum}", config=cfg, expect="accept"))
    short = deepcopy(cfg)
    short["initiators"][0]["size_width"] = width - 1
    CASES.append(
        dict(
            name=f"short_size_{maximum}",
            config=short,
            expect="reject",
            code="NCL_TILELINK_PROFILE",
            field="initiators[0].size_width",
        )
    )
for capacity in (0, 65):
    cfg = configuration(1024, 4, capacity)
    CASES.append(
        dict(
            name=f"capacity_{capacity}",
            config=cfg,
            expect="reject",
            code="NCL_AXI4_CAPACITY",
            field="initiators[0].max_outstanding",
        )
    )
