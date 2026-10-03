"""Configuration checks of the width-conversion networks that the
transaction cases (width_stress.py) do not judge: the map records a target
that keeps several bursts and merges them."""

import importlib.util
from pathlib import Path


def stress_config(mode):
    path = Path(__file__).resolve().parent / "width_stress.py"
    spec = importlib.util.spec_from_file_location("nlv_config_stress_" + mode, path)
    module = importlib.util.module_from_spec(spec)
    module.PARAMS = dict(mode=mode)
    spec.loader.exec_module(module)
    return module.configuration(False)


def merge_contexts(mapping, out, peers):
    definition = mapping["definition"]
    assert definition["outstanding"]["targets"]["t0"] == 4, definition.get("outstanding")
    assert "merge_contexts" in definition["contract"]["target_outstanding"], \
        definition["contract"].get("target_outstanding")


CASES = [
    dict(name="target_merge_id16", config=stress_config("target_merge_id16"), expect="accept",
         check=merge_contexts),
]
