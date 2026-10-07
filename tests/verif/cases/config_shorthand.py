"""Configuration checks of the YAML shorthand and of `nocloom diff`.

Every short file under tests/shorthand, and the shorthand example
scale_axi_12_short.yaml, generates the same artifacts byte for byte as its
written-out partner (the unit case [config-shorthand] also compares the
parsed definitions field by field), and `diff` finds no difference between
them, from the YAML or from the partner's generated map. A changed copy
gives exactly the expected difference lines with exit code 4; a definition
that does not parse makes `diff` exit 1 with its diagnostic. Misused
shorthand is rejected with its code."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PAIRS = ROOT / "tests" / "shorthand"
SAME = "No semantic differences\n"


def read(path):
    return path.read_text()


def edit(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"{old!r} occurs {text.count(old)} times")
    return text.replace(old, new)


def same(full):
    return [dict(against=full, exit=0, stdout=SAME), dict(against=full, map=True, exit=0, stdout=SAME)]


CASES = []
for pair in ("templates", "groups", "mirror", "node_pipes"):
    CASES.append(dict(name=f"{pair}-full", text=read(PAIRS / f"{pair}_full.yaml"), expect="accept"))
    CASES.append(dict(name=f"{pair}-short", text=read(PAIRS / f"{pair}_short.yaml"), expect="accept",
                      same_as=f"{pair}-full", diffs=same(f"{pair}-full")))
CASES.append(dict(name="example-full", text=read(ROOT / "examples" / "scale_axi_12.yaml"),
                  expect="accept"))
CASES.append(dict(name="example-short", text=read(ROOT / "examples" / "scale_axi_12_short.yaml"),
                  expect="accept", same_as="example-full", diffs=same("example-full")))

TEMPLATES = read(PAIRS / "templates_short.yaml")
BACK_TO_BACK = (
    '+ definition.contract.apb_back_to_back: {"access":"SETUP_then_ACCESS_without_idle_when_ready",'
    '"packet_capacity_per_direction":1,"read_response_capacity":2,'
    '"reservation":"reserve_response_space_before_read_SETUP"}\n'
    '+ definition.apb_back_to_back_targets: ["timer"]\n')
CASES.append(dict(name="templates-back-to-back", expect="accept",
                  text=edit(TEMPLATES, "name: timer, use: regs", "name: timer, use: regs, apb_back_to_back: true"),
                  diffs=[dict(against="templates-full", exit=4, stdout=BACK_TO_BACK),
                         dict(against="templates-full", map=True, exit=4, stdout=BACK_TO_BACK)]))
MIRROR = read(PAIRS / "mirror_short.yaml")
CASES.append(dict(name="mirror-no-override", expect="accept",
                  text=edit(MIRROR, "      link_overrides:\n        - {from: periph_rsp, to: core_rsp, "
                            "sender_pipeline: {forward: true}}\n", ""),
                  diffs=[dict(against="mirror-full", exit=4, stdout=(
                      "~ definition.topology.response.links[periph_rsp->core_rsp]"
                      ".sender_pipeline.backward: false -> true\n"))]))

GROUPS = read(PAIRS / "groups_short.yaml")
CASES.append(dict(name="groups-unknown-member", expect="reject", code="NCL_GROUP_REFERENCE",
                  text=edit(GROUPS, "[cpu0, cpu1]", "[cpu0, cpu9]"),
                  diffs=[dict(against="groups-full", exit=1, code="NCL_GROUP_REFERENCE")]))
CASES.append(dict(name="groups-unused", expect="reject", code="NCL_GROUP_UNUSED",
                  text=edit(GROUPS, "    memory: [ram, rom]\n", "    memory: [ram, rom]\n    spare: [ram]\n")))
CASES.append(dict(name="groups-except-unknown", expect="reject", code="NCL_CONNECTIVITY_REFERENCE",
                  text=edit(GROUPS, "[peripherals, boot]", "[peripherals, boat]")))
CASES.append(dict(name="templates-unknown", expect="reject", code="NCL_TEMPLATE_REFERENCE",
                  text=edit(TEMPLATES, "use: cpu, max", "use: cpux, max")))
CASES.append(dict(name="templates-conflict", expect="reject", code="NCL_TEMPLATE_CONFLICT",
                  text=edit(TEMPLATES, "use: cpu, max_outstanding: 2", "use: cpu, data_width: 32")))
CASES.append(dict(name="mirror-override-unknown", expect="reject", code="NCL_MIRROR_OVERRIDE",
                  text=edit(MIRROR, "{from: periph_rsp, to: core_rsp", "{from: periph_rsp, to: cpu_rsp")))
NODE_PIPES = read(PAIRS / "node_pipes_short.yaml")
CASES.append(dict(name="node-pipes-conflict", expect="reject", code="NCL_NODE_PIPE_CONFLICT",
                  text=edit(NODE_PIPES, "{sender_pipeline: {forward: true}}\n      - {name: leaf0}",
                            "{sender_pipeline: {forward: true}}\n"
                            "      - {name: leaf0, input_links: {pipeline: {forward: true}}}")))
