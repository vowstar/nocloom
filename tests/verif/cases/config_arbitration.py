"""Arbitration configuration: the map contract of every arbitration case
states each arbitrated output with the policy and order the input sets, and the
fixed-level QoS input: wrong types and unknown fields are refused, absent
levels and zero levels give the same bytes, a network without a topology
takes levels."""

import copy
import importlib.util
from pathlib import Path


def networks():
    path = Path(__file__).resolve().parent / "arbitration.py"
    spec = importlib.util.spec_from_file_location("nlv_config_arbitration", path)
    module = importlib.util.module_from_spec(spec)
    module.PARAMS = {}
    spec.loader.exec_module(module)
    return module


ARB = networks()


def stated_outputs(net):
    def check(mapping, out, peers):
        outputs = mapping["definition"]["contract"]["arbitration"]["outputs"]
        assert len(outputs) == ARB.OUTPUTS[net.shape], f"{len(outputs)} arbitrated outputs"
        for o in outputs:
            key = f"{o['graph']}:{o['node']}:{o['output']}"
            policy, order = net.overrides().get(key, (None, None))
            if policy is None:
                policy = (net.policy if net.policy not in ("auto", "mixed")
                          else "round_robin")
            assert (o["policy"], o.get("priority")) == (policy, order), (key, o, policy, order)
    return check


CASES = []
for name in ARB.NETWORKS[:-1]:
    net = ARB.Network(name)
    CASES.append(dict(name=f"contract_{name}", config=net.config, expect="accept",
                      commands=("generate",), check=stated_outputs(net)))

QOS = ARB.Network("qos").config
for k, value in enumerate((True, "high", 1.5, None)):
    bad = copy.deepcopy(QOS)
    bad["initiators"][0]["qos"]["levels"]["write"] = value
    CASES.append(dict(name=f"qos_type_{k}", config=bad, expect="reject", code="NCL_SCHEMA_TYPE"))
bad = copy.deepcopy(QOS)
bad["initiators"][0]["qos"]["unknown"] = 1
CASES.append(dict(name="qos_unknown_field", config=bad, expect="reject",
                  code="NCL_SCHEMA_UNKNOWN_FIELD"))
absent = copy.deepcopy(QOS)
for source in absent["initiators"]:
    source.pop("qos")
zero = copy.deepcopy(absent)
for source in zero["initiators"]:
    source["qos"] = dict(levels=dict(write=0, read=0))
flat = copy.deepcopy(QOS)
flat.pop("topology")
CASES += [dict(name="qos_absent", config=absent, expect="accept", commands=("generate",)),
          dict(name="qos_zero", config=zero, expect="accept", commands=("generate",),
               same_as="qos_absent"),
          dict(name="qos_active", config=QOS, expect="accept", commands=("generate",)),
          dict(name="qos_default_graph", config=flat, expect="accept", commands=("generate",))]
