"""Check local width adapter hold disciplines.

This suite proves local safety under legal ready/valid timing. Reachability covers
and single-fault controls are separate tasks; it does not prove whole-network
liveness, arbitrary CDC convergence, or bounded draining.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True

OP_STATE = 9


def wire(index):
    return f"nl_internal_v{index}"


def load(path):
    return json.loads(Path(path).read_text())


class Module:
    """One generated module with its binding metadata."""

    def __init__(self, rtl, meta):
        self.rtl = rtl
        self.meta = meta
        self.bindings = meta["bindings"]
        self.values = meta["values"]
        self.ports = meta["ports"]
        self.clk = next(p["name"] for p in self.ports if p["signal"] == "clock")
        self.rst = next(p["name"] for p in self.ports if p["name"].endswith("reset_n"))

    def has(self, key):
        return key in self.bindings

    def at(self, key):
        return wire(self.bindings[key])

    def is_state(self, key):
        return self.values[self.bindings[key]]["op"] == OP_STATE


def split_modules(rtl):
    """Split a multi-module emission into {module_name: text}."""
    out = {}
    for chunk in rtl.split("endmodule"):
        if "module" not in chunk:
            continue
        chunk = chunk + "endmodule\n"
        head = chunk[chunk.index("module") :].split(None, 2)[1]
        out[head.split("(")[0]] = chunk
    return out


def harness_block(
    clk, rst, assumptions, assertions, covers=None, outer_assertions=None, decls=""
):
    """Formal block: reset asserted at cycle zero;
    assumptions constrain legal timing only; `$past`-using assertions are
    gated on two settled reset cycles; covers are gated out of reset."""
    lines = []
    if decls:
        lines.append(decls)
    lines.append("reg nl_past=0;")
    lines.append(
        f"always @(posedge {clk}) begin nl_past<=1; if(!nl_past)assume(!{rst});"
    )
    lines.append("if(nl_past)begin")
    lines += assumptions
    if outer_assertions:
        lines += outer_assertions
    if assertions:
        lines.append(f"if({rst} && $past({rst}))begin")
        lines += assertions
        lines.append("end")
    if covers:
        lines.append(f"if({rst})begin")
        lines += covers
        lines.append("end")
    lines.append("end end")
    return "\n".join(lines)


def instrument_module(rtl, block):
    return rtl.replace("endmodule", block + "\nendmodule")


def stable_of(indices):
    return " && ".join(f"{wire(i)}==$past({wire(i)})" for i in indices) or "1"


# ---------------------------------------------------------------------------
# Property encodings.


def hold_axioms(mod, name, held, prog, a1_prog, atom_state_indices):
    """Occupancy, stability and release plus reset clearing, for one hold point.  `prog` is the
    OR of all declared events (used by the stability and release rules); `a1_prog` is the
    episode-internal subset for the occupancy rule (acquisition events of a new
    episode legitimately fire while unheld and are excluded).  Returns
    (inner, outer) assertion lists; the outer list needs no $past gate."""
    stable = stable_of(atom_state_indices)
    inner = []
    if a1_prog is not None:
        inner.append(f"nl2_held_on_prog_{name}: assert((!({a1_prog})) || ({held}));")
    inner += [
        f"nl2_hold_stable_{name}: assert((!$past(({held}) && !({prog}))) || (({held}) && ({stable})));",
        f"nl2_release_is_progress_{name}: assert((!($past({held}) && !({held}))) || $past({prog}));",
    ]
    outer = [
        # Asynchronous active-low reset: no hold point is occupied in reset.
        f"nl2_reset_clears_{name}: assert(({mod.rst}) || !({held}));",
    ]
    return inner, outer


def input_legality(mod, extra=None):
    """Legal initiator timing: a valid that is not accepted holds its value
    and payload (held-state discipline of the environment side)."""
    assumptions = []
    ports = {p["name"]: p for p in mod.ports}
    for name, port in ports.items():
        if not port["input"] or not name.endswith("valid"):
            continue
        prefix = name[:-5]
        if prefix + "ready" not in ports:
            continue
        ready = prefix + "ready"
        fields = [
            p["name"]
            for n, p in ports.items()
            if p["input"] and n.startswith(prefix) and n != name
        ]
        stable = " && ".join(f"{p}==$past({p})" for p in fields) or "1"
        assumptions.append(
            f"if($past({mod.rst} && {name} && !{ready})) assume({name} && ({stable}));"
        )
    if extra:
        assumptions += extra
    return assumptions


def write_sby(
    task_dir,
    top,
    rtl_text,
    mode,
    depth,
    engine="smtbmc z3",
    timeout=60,
    script_extra="",
):
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "formal.v").write_text(rtl_text)
    (task_dir / "task.sby").write_text(
        f"[options]\nmode {mode}\ndepth {depth}\ntimeout {timeout}\n"
        f"[engines]\n{engine}\n[script]\nread -formal formal.v\n{script_extra}prep -top {top}\n[files]\nformal.v\n"
    )
    return hashlib.sha256(rtl_text.encode()).hexdigest()


def execute_sby(task_dir, shim, timeout):
    env = dict(os.environ)
    env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
    run = subprocess.run(
        ["sby", "-f", "task.sby"],
        cwd=task_dir,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout + 90,
    )
    (task_dir / "run.log").write_text(run.stdout + run.stderr)
    status_file = task_dir / "task" / "status"
    status = status_file.read_text().split()[0] if status_file.exists() else "ERROR"
    if (status == "PASS" and run.returncode != 0) or (
        status == "FAIL" and run.returncode != 2
    ):
        return "ERROR"
    return status


def python_shim(work):
    shim = work / "python-shim"
    shim.mkdir(parents=True, exist_ok=True)
    link = shim / "python3"
    if not link.is_symlink() or os.readlink(link) != sys.executable:
        # Unlink the directory entry, never the interpreter the shim points to.
        entry = link.parent.resolve() / link.name
        assert entry != work.resolve() and entry.is_relative_to(work.resolve())
        link.unlink(missing_ok=True)
        link.symlink_to(sys.executable)
    return shim


# ---------------------------------------------------------------------------
# Fixture preparation.


def prepare(fixture, out):
    out.mkdir(parents=True, exist_ok=True)
    specs = [
        ("adapter-8x4", ["adapter", "8", "4"]),
        ("fault-adapter-ser", ["adapter", "8", "4", "drop_hold_term"]),
        ("fault-adapter-des", ["adapter", "8", "4", "des_drop_hold"]),
        ("fault-adapter-last", ["adapter", "8", "4", "premature_last"]),
    ]
    for name, argv in specs:
        # Insert the output directory after the mode selector and the widths.
        argv = [argv[0], argv[1], argv[2], str(out / name), *argv[3:]]
        log = out / f"prepare-{name}.log"
        with log.open("w") as fh:
            subprocess.run(
                [fixture, *argv],
                stdout=fh,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=180,
            )
    (out / "prepare.ok").write_text("ok\n")


# ---------------------------------------------------------------------------
# Case builders.


def adapter_payload_legality(mod, side):
    """Fragment-format legality of the producer (the ladder's static contract):
    count is a multiple of four between four and the wide word, and the used
    lane range never wraps a word boundary (width_adapter.hpp)."""
    if side == "serializer":
        return [
            f"if({mod.rst}) assume(nl_wide_count[1:0] == 2'b00 && nl_wide_count >= 6'd4 && nl_wide_count <= 6'd8);",
            f"if({mod.rst}) assume((nl_wide_address & 32'd7) + {{26'b0, nl_wide_count}} <= 8);",
        ]
    return [
        f"if({mod.rst}) assume(nl_narrow_count[1:0] == 2'b00 && nl_narrow_count >= 6'd4 && nl_narrow_count <= 6'd8);",
        f"if({mod.rst}) assume((nl_narrow_address & 32'd7) + {{26'b0, nl_narrow_count}} <= 8);",
    ]


def adapter_property_sets(mod, side):
    assumptions = input_legality(mod) + adapter_payload_legality(mod, side)
    if side == "serializer":
        started = mod.at("ser.started")
        accept_any = mod.at("ser.accept")
        accept = mod.at("ser.wide_accept")
        is_last = mod.at("ser.is_last")
        group = mod.at("ser.group")
        high_used = mod.at("ser.high_used")
        # Episode: started rises on a non-last sub-fragment acceptance and
        # clears on the last one (wide_accept = accept && is_last).  A
        # single-group message completes without a held episode, so the
        # occupancy rule is pinned at the episode boundary instead.
        inner, outer = hold_axioms(
            mod, "ser", started, accept_any, None, [mod.bindings["ser.started"]]
        )
        inner += [
            f"nl2_release_episode_ser: assert((!({accept})) || (({started}) || (({group}) == ({high_used}))));",
            f"nl2_release_at_last_ser: assert((!({accept})) || ({is_last}));",
        ]
        return assumptions, inner, outer
    have_low = mod.at("des.have_low")
    accept = mod.at("des.wide_accept")
    completes = mod.at("des.completes")
    low_accept = mod.at("des.low_accept")
    atom = [
        i
        for k, i in mod.bindings.items()
        if (k.startswith("des.control.") or k == "des.err_accum") and mod.is_state(k)
    ]
    inner, outer = hold_axioms(
        mod, "des", have_low, f"({low_accept}) || ({accept})", None, atom
    )
    inner += [
        # A completing narrow fragment is not accepted unless the wide side
        # takes the gathered fragment.
        f"nl2_complete_waits_wide_des: assert(!(nl_narrow_valid && ({completes}) && nl_narrow_ready) || nl_wide_ready);",
    ]
    # Legal fragment timing: a first fragment starts a gather, a continuation
    # requires one in progress.
    assumptions.append(
        f"if({mod.rst}) assume(!nl_narrow_valid || (nl_narrow_first == !({have_low})));"
    )
    return assumptions, inner, outer


def case_adapter_axioms(prepared):
    d = prepared / "adapter-8x4"
    meta = load(d / "bindings.json")
    modules = split_modules((d / "adapter.v").read_text())
    tasks = []
    for side in ("serializer", "deserializer"):
        mm = meta[side]
        mod = Module(modules[mm["module"]], mm)
        assumptions, inner, outer = adapter_property_sets(mod, side)
        block = harness_block(
            mod.clk, mod.rst, assumptions, inner, outer_assertions=outer
        )
        tasks.append(
            {
                "name": f"adapter_axioms.{side}",
                "top": mm["module"],
                "rtl": instrument_module(mod.rtl, block),
                "mode": "prove",
                "depth": 12,
                "expect": "PASS",
            }
        )
    for side, held in (("serializer", "ser.started"), ("deserializer", "des.have_low")):
        mm = meta[side]
        mod = Module(modules[mm["module"]], mm)
        assumptions, _, _ = adapter_property_sets(mod, side)
        block = harness_block(
            mod.clk, mod.rst, assumptions, [], covers=[f"cover({mod.at(held)});"]
        )
        tasks.append(
            {
                "name": f"adapter_cover.{side}",
                "top": mm["module"],
                "rtl": instrument_module(mod.rtl, block),
                "mode": "cover",
                "depth": 24,
                "expect": "PASS",
            }
        )
    for fault_dir, side, broken in (
        ("fault-adapter-ser", "serializer", "nl2_hold_stable_ser"),
        ("fault-adapter-des", "deserializer", "nl2_hold_stable_des"),
        ("fault-adapter-last", "serializer", "nl2_release_episode_ser"),
    ):
        fm = load(prepared / fault_dir / "bindings.json")
        fmodules = split_modules((prepared / fault_dir / "adapter.v").read_text())
        mm = fm[side]
        fmod = Module(fmodules[mm["module"]], mm)
        assumptions, inner, outer = adapter_property_sets(fmod, side)
        block = harness_block(
            fmod.clk, fmod.rst, assumptions, inner, outer_assertions=outer
        )
        tasks.append(
            {
                "name": f"adapter_axioms.{fault_dir}",
                "top": mm["module"],
                "rtl": instrument_module(fmod.rtl, block),
                "mode": "bmc",
                "depth": 24,
                "expect": "FAIL",
                "broken": broken,
            }
        )
    return tasks


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--fixture")
    p.add_argument("--prepared", type=Path)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument(
        "--case",
        choices=("prepare", "adapter_axioms"),
        required=True,
    )
    args = p.parse_args()
    args.work = args.work.resolve()
    args.work.mkdir(parents=True, exist_ok=True)
    if args.case == "prepare":
        prepare(args.fixture, args.work)
        return
    builder = case_adapter_axioms
    shim = python_shim(args.work)
    rows = []
    tasks = builder(args.prepared)
    tasks += [
        dict(task, name=task["name"] + ".restored")
        for task in tasks
        if task["mode"] == "prove" and task["expect"] == "PASS"
    ]
    for task in tasks:
        tdir = args.work / task["name"]
        sha = write_sby(tdir, task["top"], task["rtl"], task["mode"], task["depth"])
        status = execute_sby(tdir, shim, 60)
        # An arbitrary failing assertion is not the designated fault witness.
        text = (tdir / "run.log").read_text()
        named_failure = task.get("broken")
        traces = sorted(tdir.glob("task/engine_*/trace*.vcd"))
        witness = (
            status == "FAIL" and bool(traces) and named_failure in text
            if named_failure
            else True
        )
        row = {
            "name": task["name"],
            "mode": task["mode"],
            "depth": task["depth"],
            "formal_sha256": sha,
            "expected": task["expect"],
            "status": status,
            "designated_failure": named_failure,
            "witness": witness,
            "traces": [
                {
                    "path": str(t.relative_to(args.work)),
                    "sha256": hashlib.sha256(t.read_bytes()).hexdigest(),
                }
                for t in traces
            ],
            "ok": status == task["expect"] and witness,
        }
        rows.append(row)
        (args.work / "results.json").write_text(json.dumps(rows, indent=2) + "\n")
        print(json.dumps(row), flush=True)
    if not all(row["ok"] for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
