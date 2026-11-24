"""Prove the emitted cycle assertions on generated Verilog with SymbiYosys."""
import itertools
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

PROPERTY = re.compile(
    r"^(\w+): (assert|assume|cover) property \(@\(posedge (\w+)\)(.*?)\);\n", re.S | re.M)
WINDOW = re.compile(r"##\[(\d+):(\d+)\] (.+)")
DELAY = re.compile(r" ##(\d+|\[\d+:\d+\]) ")
REFUTE_DEPTH = 12
COVER_DEPTH = 16


def window(label, kind, clock, before, low, high, after):
    """State for `before |-> ##[low:high] after`. Bit i of the open vector is a
    request made i cycles ago that `after` has not answered at an age from low on."""
    if not 0 <= low <= high or high == 0:
        raise ValueError(f"Unsupported delay range in {label}")
    answered = sum(1 << age for age in range(low, high))
    return (f"reg [{high - 1}:0] {label}_wait = 0;\n"
            f"wire [{high}:0] {label}_open = {{{label}_wait, ({before}) ? 1'b1 : 1'b0}};\n"
            f"always @(posedge {clock}) {label}_wait <= {label}_open[{high - 1}:0] & "
            f"~(({after}) ? {high}'d{answered} : {high}'d0);\n"
            f"always @(posedge {clock}) {label}: {kind} (!{label}_open[{high}] || ({after}));\n")


def sequence(body):
    """A cover sequence as one expression at its last cycle. Each term is read
    the right number of cycles back, and that many cycles must have passed."""
    terms = DELAY.split(body)
    spans = []
    for delay in terms[1::2]:
        low, _, high = delay.strip("[]").partition(":")
        spans.append(range(int(low), int(high or low) + 1))
    choices = []
    for delays in itertools.product(*spans):
        back = [sum(delays[i:]) for i in range(len(delays) + 1)]
        parts = [f"$past({term}, {n})" if n else f"({term})" for term, n in zip(terms[0::2], back)]
        if back[0]:
            parts.insert(0, f"formal_age >= {back[0]}")
        choices.append("(" + " && ".join(parts) + ")")
    return " || ".join(choices)


def clocked(text, clock, reset):
    """Rewrite each concurrent property as a clocked immediate statement that
    Yosys reads. Reset is low before the first clock edge."""
    def rewrite(match):
        label, kind, edge, body = match[1], match[2], match[3], " ".join(match[4].split())
        if edge != clock:
            raise ValueError(f"Unexpected clock in {label}")
        if kind == "cover":
            if "|->" in body or "|=>" in body:
                raise ValueError(f"Unsupported cover {label}")
            return f"always @(posedge {clock}) {label}: cover ({sequence(body)});\n"
        before, operator, after = re.split(r" (\|->|\|=>) ", body)
        delayed = WINDOW.fullmatch(after)
        if "##" in before or "|->" in after or "|=>" in after:
            raise ValueError(f"Unsupported property {label}")
        if operator == "|=>":
            if kind != "assume" or "##" in after:
                raise ValueError(f"Unsupported property {label}")
            check = f"formal_age == 0 || !$past({before}) || ({after})"
        elif delayed:
            return window(label, kind, clock, before, int(delayed[1]), int(delayed[2]), delayed[3])
        elif "##" in after:
            raise ValueError(f"Unsupported property {label}")
        else:
            check = f"!({before}) || ({after})"
        return f"always @(posedge {clock}) {label}: {kind} ({check});\n"

    first = PROPERTY.search(text)
    if not first or text.count("\nendmodule\n") != 1:
        raise ValueError("Unexpected assertion text")
    start = (f"reg [3:0] formal_age = 0;\n"
             f"always @(posedge {clock}) if (formal_age != 15) formal_age <= formal_age + 1;\n"
             f"always @(*) if (formal_age == 0) assume (!{reset});\n")
    result = text[:first.start()] + start + PROPERTY.sub(rewrite, text[first.start():])
    if " property " in result:
        raise ValueError("Unexpected assertion text")
    return result


MODES = {
    "prove": "mode prove\naigsmt z3\n\n[engines]\nabc pdr",
    "refute": f"mode bmc\ndepth {REFUTE_DEPTH}\naigsmt z3\n\n[engines]\nabc bmc3",
    "reach": f"mode bmc\ndepth {COVER_DEPTH}\naigsmt z3\n\n[engines]\nabc bmc3",
}
COVER = re.compile(r"^(always @\(posedge \w+\) )(\w+): cover \((.*)\);$", re.M)
WORKERS = 3


def solve(work, name, text, proof, mode, limit):
    """Run SymbiYosys once on clocked text in a new folder. The folder must not exist."""
    folder = work / "solver" / name
    folder.mkdir(parents=True)
    (folder / "design.sv").write_text(text)
    (folder / "task.sby").write_text(
        f"[options]\n{MODES[mode]}\n\n[script]\nread_verilog -formal -sv design.sv\n"
        f"prep -top {proof['module']}\n\n[files]\ndesign.sv\n")
    try:
        result = subprocess.run(["sby", "task.sby"], cwd=folder, text=True, timeout=limit,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise RuntimeError("SymbiYosys (sby) is required for this test") from None
    (folder / "sby.log").write_text(result.stdout)
    return result


def failures(result):
    return set(re.findall(r"failed assertion \w+\.(\w+) at ", result.stdout))


def prove(work, name, design, proof, limit=900):
    """Prove every assertion of one design for all reachable states."""
    text = clocked(design, proof["clock"], proof["reset"])
    result = solve(work, name, text, proof, "prove", limit)
    if result.returncode != 0 or "DONE (PASS, rc=0)" not in result.stdout:
        raise RuntimeError(f"No proof for {name}:\n{result.stdout[-2000:]}")


def refute(work, name, design, proof, expected, limit=600):
    """Find a counterexample within REFUTE_DEPTH cycles, failing only at
    assertions with the expected names."""
    text = clocked(design, proof["clock"], proof["reset"])
    result = solve(work, name, text, proof, "refute", limit)
    failed = failures(result)
    if result.returncode != 2 or not failed or not failed <= set(expected):
        raise RuntimeError(f"Expected {name} to fail at {sorted(expected)}, got {sorted(failed)}:\n"
                           f"{result.stdout[-2000:]}")


def reach(work, name, text, proof, label, limit=600):
    """A counterexample to the negated cover is a trace that reaches it."""
    result = solve(work, name, text, proof, "reach", limit)
    if result.returncode != 2 or failures(result) != {label}:
        raise RuntimeError(f"Cover {label} not reached within {COVER_DEPTH} cycles:\n"
                           f"{result.stdout[-2000:]}")


def covers(work, name, design, proof):
    """One job per cover statement: reach it within COVER_DEPTH cycles."""
    text = clocked(design, proof["clock"], proof["reset"])
    labels = [match[2] for match in COVER.finditer(text)]
    if not labels:
        raise RuntimeError(f"{name} has no cover statement")

    def negated(label):
        return COVER.sub(lambda m: f"{m[1]}{m[2]}: assert (!({m[3]}));" if m[2] == label
                         else f"{m[1]};", text)
    return [(reach, work, f"{name}_{label}", negated(label), proof, label) for label in labels]


def run_all(jobs):
    """Run (function, arguments...) jobs, WORKERS at a time. Raise the first failure."""
    with ThreadPoolExecutor(WORKERS) as pool:
        for future in [pool.submit(*job) for job in jobs]:
            future.result()
