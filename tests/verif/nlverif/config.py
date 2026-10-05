"""Independent reading of a schema 10 input for the transaction model.

Only the fields the transaction model needs: endpoints, protocols, widths,
target windows and connectivity. Unknown connectivity forms raise instead
of being guessed. Inputs are JSON or JSON-compatible YAML; block YAML needs
PyYAML and is refused without it.
"""

import json


class ConfigError(ValueError):
    pass


def load(path):
    text = open(path).read()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # noqa: PLC0415
        except ImportError as error:
            raise ConfigError("block YAML needs PyYAML; give JSON-compatible YAML") from error
        return yaml.safe_load(text)


def number(value):
    return int(value, 0) if isinstance(value, str) else int(value)


def error_codes(cfg):
    """Distinct is the schema default. Uniform is an explicit selection."""
    policy = cfg.get("transport", {}).get("response_codes", "distinct")
    if policy not in ("distinct", "uniform"):
        raise ConfigError("unsupported response_codes")
    return policy == "distinct"


def routes(cfg, modes=None):
    """source -> [(target, base, size[, local])] for every mapping of the reachable targets;
    local, the target address at base, only when it is not zero. With modes (address input ->
    value), only the mappings whose condition holds (docs/address-map.md#address-selectors)."""
    if cfg.get("schema_version") != 10:
        raise ConfigError("schema_version 10 expected")
    flags = {f["name"]: all((modes or {}).get(p, 0) == v for p, v in f["equals"].items())
             for f in cfg.get("address_selectors", [])}
    targets = {t["name"]: [(m.get("flow", 0), (interface(t, m.get("flow", 0)), number(m["base"]),
                                                number(m["size"]))
                            + ((number(m["local_address"]),) if number(m.get("local_address", 0))
                               else ())) for m in t["mappings"]
                           if all(flags[f] == v for f, v in m.get("when", {}).items())]
               for t in cfg["targets"]}
    conn = cfg.get("connectivity", "all")
    out = {}
    for s in cfg["initiators"]:
        # target -> its flows the source reaches by some flow pair (None: every flow).
        reach = {}
        if conn == "all":
            reach = dict.fromkeys(targets)
        elif isinstance(conn, list):
            reach = dict.fromkeys(c["target"] for c in conn if c["source"] == s["name"])
        elif isinstance(conn, dict):
            for item in conn.get(s["name"], []):
                if not isinstance(item, dict):
                    reach[item] = None
                elif "target_path" not in item:
                    reach[item["target"]] = None
                elif reach.get(item["target"], set()) is not None:
                    reach[item["target"]] = reach.get(item["target"], set()) | {
                        item["target_path"]}
        else:
            raise ConfigError("unsupported connectivity form")
        out[s["name"]] = [m for t, flows in reach.items() for f, m in targets[t]
                          if flows is None or f in flows]
    return out


def interface(t, flow):
    """The target model name of a target flow: an APB, AHB or AXI4-Lite target of several flows
    has a port set per flow, modeled as target <name>_<flow>; any other target is one model."""
    sets = t.get("protocol", "axi4") not in ("axi4", "axi3")
    return f"{t['name']}_{flow}" if sets and t.get("flows", 1) > 1 else t["name"]


def mode_routes(cfg, values):
    """routes() for each tuple of address input values (in cfg address_inputs order), keyed as the
    checker keys a burst's modes: ((index, value), ...)."""
    names = [p["name"] for p in cfg["address_inputs"]]
    return {tuple(enumerate(v)): routes(cfg, dict(zip(names, v))) for v in values}


DEDICATED = {"axi4_lite": "lite", "apb4": "apb", "apb3": "apb", "apb2": "apb", "ahb_v3": "ahb"}


def response_runs(mapping):
    """The response run rule of every route of a generated map, as "source|target": the widest word
    the route's responses gather to and whether the target can end a run early."""
    network = mapping["network"]
    widens = [a for a in network["adapters"]
              if a["direction"] == "response" and a["kind"] == "deserializer"]
    out = {}
    for route in network["response_routes"]:
        chain = [route["entry"], *route["hardware_nodes"], route["exit"]]
        links = {f"rsp/{a}>{b}" for a, b in zip(chain, chain[1:])}
        word = max((a["wide_bytes"] for a in widens if a["segment"].split("/link-")[0] in links),
                   default=0)
        out[f'{route["exit"]}|{route["entry"]}'] = dict(
            word=word, interleave=route["entry"] in network["interleaved_targets"])
    return out


def targets(cfg, **extra):
    """Target models: protocol kind, bus bytes; dedicated outlets (APB, AHB,
    AXI4-Lite) also their engine word (32 bits for APB, the bus otherwise),
    FIXED burst policy, APB dialect and attribute map; one-direction targets."""
    out = {}
    for t in cfg["targets"]:
        protocol = t["protocol"]
        kind = "axi" if protocol in ("axi4", "axi3") else DEDICATED.get(protocol)
        if kind is None:
            raise ConfigError("target protocol not modeled: " + protocol)
        entry = dict(kind=kind, bus_bytes=t["data_width"] // 8,
                     read_only=t.get("access") == "read_only",
                     write_only=t.get("access") == "write_only")
        if kind != "axi":
            entry.update(engine_bytes=4 if kind == "apb" else t["data_width"] // 8,
                         fixed=t.get("fixed_bursts", "reject"), prot=False)
            if protocol in ("apb3", "apb2"):
                entry.update(dialect=int(protocol[-1]))
            else:
                entry.update(attributes=t.get("attributes"))
        for flow in range(t.get("flows", 1)):
            out[interface(t, flow)] = dict(entry, **extra)
    return out


def ungated(cfg):
    """A copy of cfg without clock gates (clock_gating none) and without test ports."""
    out = json.loads(json.dumps(cfg))
    out.pop("test_ports", None)
    out["clock_gating"] = "none"
    for domain in out["clock_domains"]:
        domain.pop("test_port", None)
    return out
