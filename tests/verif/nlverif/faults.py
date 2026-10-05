"""Negative-control support: the fault wrapper and result classification.

A fault build compiles once: the fixture emits each faulted component as a
core module with an added nl_nlv_fault_sel input (inline faults) and whole
variant copies (variant faults); the wrapper here keeps the original module
name and port list, reads +NLV_FAULT=k once at time zero and selects the
outputs. With k = 0 the build must reproduce the baseline event hash.

A fault with op "scenario" changes no RTL: it breaks an endpoint premise
through the stimulus. It runs the baseline build with the tape that the case
scenario builds from the fault's params, and it is CAUGHT only when every
expectation is hit (the hang and the premise named for it).
"""

import re

CLASSES = ("CAUGHT", "CAUGHT_OTHER", "SURVIVED", "TIMEOUT", "TOOL_ERROR", "BUILD_FAIL",
           "ANCHOR_LOST")


class AnchorLost(ValueError):
    pass


def text_faults(component, rtl, faults):
    """Variant copies of one generated module for faults with op "text":
    each "subs" entry [pattern, replacement, hits] must match exactly hits
    times (an anchor that drifts is ANCHOR_LOST, never a silent no-op). The
    unmodified module becomes the core; returns (faults_json, verilog).
    "subs" may be a function of (rtl, map) giving that list, for an anchor
    named by the interface map (an internal net of a CLI netlist)."""
    module = component["module"]
    head = re.compile(r"^module " + re.escape(module) + r" \(", re.M)
    if len(head.findall(rtl)) != 1:
        raise AnchorLost(f"module {module} not found once")
    out = [head.sub(f"module {module}__nlv (", rtl)]
    variants, resolved = [], []
    for k, f in enumerate(faults, 1):
        text = rtl
        subs = f["subs"](rtl, component.get("map")) if callable(f["subs"]) else f["subs"]
        for pattern, replacement, hits in subs:
            text, n = re.subn(pattern, replacement, text, flags=re.M)
            if n != hits:
                raise AnchorLost(f"{f['id']}: {pattern!r} matched {n} times, want {hits}")
        name = f"{module}__nlvf{k}"
        out.append(head.sub(f"module {name} (", text))
        variants.append(dict(index=k, module=name))
        resolved.append(dict(id=f["id"], index=k, component=component["label"], kind="text",
                             op="text", hits=sum(h for _, _, h in subs)))
    fjson = dict(recipe="text", faults=resolved, modules=[dict(
        label=component["label"], module=module, core=module + "__nlv", variants=variants)])
    return fjson, "\n".join(out)


def wrapper(components, faults_json):
    by_label = {c["label"]: c for c in components}
    out = ["// Test-only fault selection wrapper (tests/verif); not a product artifact."]
    for m in faults_json["modules"]:
        comp = by_label[m["label"]]
        ports = comp["ports"]
        decl = []
        for p in ports:
            w = "" if p["width"] == 1 else f"[{p['width']-1}:0] "
            decl.append(f"  {'input' if p['input'] else 'output'} wire {w}{p['name']}")
        body = [f"module {m['module']} (\n" + ",\n".join(decl) + "\n);",
                "  reg [7:0] nlv_sel;",
                "  integer nlv_value;",
                "  initial begin",
                "    nlv_sel = 8'd0;",
                "    if ($value$plusargs(\"NLV_FAULT=%d\", nlv_value)) nlv_sel = nlv_value;",
                "  end"]
        outputs = [p for p in ports if not p["input"]]
        conns = [f".{p['name']}({p['name'] if p['input'] else 'core_' + p['name']})" for p in ports]
        if "sel_port" in m:
            conns.append(f".{m['sel_port']}(nlv_sel)")
        for p in outputs:
            w = "" if p["width"] == 1 else f"[{p['width']-1}:0] "
            body.append(f"  wire {w}core_{p['name']};")
        body.append(f"  {m['core']} core (" + ", ".join(conns) + ");")
        for v in m["variants"]:
            prefix = "v%d_" % v["index"]
            vc = [".%s(%s)" % (p["name"], p["name"] if p["input"] else prefix + p["name"])
                  for p in ports]
            for p in outputs:
                w = "" if p["width"] == 1 else f"[{p['width']-1}:0] "
                body.append(f"  wire {w}v{v['index']}_{p['name']};")
            body.append(f"  {v['module']} variant{v['index']} (" + ", ".join(vc) + ");")
        for p in outputs:
            expr = f"core_{p['name']}"
            for v in reversed(m["variants"]):
                expr = f"(nlv_sel == 8'd{v['index']}) ? v{v['index']}_{p['name']} : {expr}"
            body.append(f"  assign {p['name']} = {expr};")
        body.append("endmodule")
        out.append("\n".join(body))
    return "\n".join(out) + "\n"


def expect_errors(expect):
    """A TAPE.* rule matches any hang, so it is accepted only with the label
    of the wait that must time out (TAPE.WAIT_TIMEOUT:PROGRESS)."""
    return [e for e in expect if e.startswith("TAPE.") and ":" not in e]


def hits(result):
    """Names a run failed with: the first online rule, the rule with its tape
    label, every offline mismatch kind, and the kind with its direction
    ("TARGET_FOOTPRINT:w") when the mismatch records one."""
    out = set()
    verdict = result.get("verdict", {})
    for rule_key, label_key in (("first_rule", "first_label"), ("hang_rule", "hang_label")):
        rule = verdict.get(rule_key)
        if rule:
            out.add(rule)
            label = verdict.get(label_key)
            if label:
                out.add(rule + ":" + label)
    for m in result.get("check", {}).get("mismatches", []):
        out.add(m["kind"])
        if m.get("dir") in ("w", "r"):
            out.add(m["kind"] + ":" + m["dir"])
    return out


def classify(fault, result):
    """CAUGHT only when the first online rule or an offline mismatch kind is
    one the fault declared; TIMEOUT and tool errors never count."""
    bad = expect_errors(fault.get("expect", []))
    if bad:
        raise ValueError(f"fault {fault.get('id')}: unlabeled expectation {bad}")
    status = result.get("status")
    if status in ("TOOL_ERROR", "BUILD_FAIL"):
        return status
    if result.get("verdict", {}).get("status") == "TIMEOUT":
        return "TIMEOUT"
    if status == "PASS":
        return "SURVIVED"
    found = hits(result)
    result["caught_by"] = sorted(found)
    want = set(fault.get("expect", []))
    if fault.get("op") == "scenario":
        return "CAUGHT" if want and want <= found else "CAUGHT_OTHER"
    return "CAUGHT" if found & want else "CAUGHT_OTHER"
