"""Install layer driver: the installed package against case tables.

The build is installed once into a prefix that is then moved (relocated), so
every consumer and the installed CLI find the package through find_package
or the prefix alone. A case file defines ENTRIES, a list of (name, function)
pairs; each function gets a Context (run commands, the installed CLI and its
version, consumers built from sources with find_package only, the verif
stack on the installed CLI's artifacts) and a private directory, raises
AssertionError when its contract fails and returns a JSON-able record. A case file sees its
--param KEY=VALUE values as PARAMS.

  python3 tests/verif/install.py --case FILE [--param KEY=VALUE]... --source DIR
         --build DIR --compiler CXX --generator G --make-program MAKE --work DIR
         [--sim ENGINE] [--sim-config JSON]

Exit code: 0 every entry passed, 1 one failed, 2 an installation, consumer
build or case file error. results.json lists every entry.
"""

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import traceback
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import sim_engine  # noqa: E402
from nlverif import config as nlconfig  # noqa: E402
from nlverif import run as nlrun  # noqa: E402


class Infra(RuntimeError):
    pass


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digests(directory):
    return {p.name: digest(p) for p in sorted(Path(directory).iterdir()) if p.is_file()}


def license_materials(prefix, source, datadir="share"):
    """The installed CLI package retains each applicable license and notice byte for byte."""
    root = Path(prefix) / datadir / "nocloom"
    files = ("LICENSE", "NOTICE", "docs/attribution.md",
             "LICENSES/Apache-2.0.txt", "LICENSES/LicenseRef-NocLoom-Output-1.0.txt",
             "third_party/licenses/nlohmann_json.txt", "third_party/licenses/yaml-cpp.txt",
             "third_party/licenses/CLI11.txt")
    for name in files:
        path = root / name
        if not path.is_file() or path.read_bytes() != (Path(source) / name).read_bytes():
            raise Infra("installed license material missing or changed: " + name)


class Context:
    """What an entry may use: commands with logs, the relocated prefix, the
    installed CLI, consumers and the simulation engine."""

    DISABLED = ("yaml-cpp", "nlohmann_json", "CLI11", "Catch2")

    def __init__(self, args, root):
        self.args, self.root = args, root
        self.source = Path(args.source).resolve()
        self.prefix = root / "relocated"
        self.consumers = {}
        self.cli = None
        self.version = None

    def run(self, argv, label, cwd=None, expected=0, timeout=300):
        cwd = Path(cwd or self.root)
        try:
            r = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise AssertionError(f"{label}: no result within {timeout} s") from error
        (cwd / (label + ".log")).write_text(r.stdout + r.stderr)
        if expected is not None and r.returncode != expected:
            raise AssertionError(f"{label}: exit {r.returncode}, want {expected}: "
                                 f"{(r.stdout + r.stderr)[-1500:]}")
        return r

    def install(self):
        built = self.root / "prefix"
        r = subprocess.run(["cmake", "--install", self.args.build, "--prefix", str(built)],
                           capture_output=True, text=True, timeout=300)
        (self.root / "install.log").write_text(r.stdout + r.stderr)
        if r.returncode:
            raise Infra("install failed: " + r.stderr[-2000:])
        built.rename(self.prefix)
        if not (self.prefix / "include/nocloom/definition.hpp").is_file():
            raise Infra("relocated prefix lacks the public headers")
        cache = (Path(self.args.build) / "CMakeCache.txt").read_text()
        datadir = next((line.split("=", 1)[1] for line in cache.splitlines()
                        if line.startswith("CMAKE_INSTALL_DATADIR:PATH=")), "") or "share"
        license_materials(self.prefix, self.source, datadir)
        self.cli = self.prefix / "bin/nocloom"
        self.version = self.run([self.cli, "--version"], "version").stdout.strip()

    def consumer(self, name, files, component="Core"):
        """Builds one consumer from {destination: source path relative to the
        repository} with find_package of the relocated prefix only (the
        dependencies the package must not leak are disabled)."""
        if name in self.consumers:
            return self.consumers[name]
        directory = self.root / "consumer" / name
        directory.mkdir(parents=True)
        for destination, origin in files.items():
            (directory / destination).write_bytes((self.source / origin).read_bytes())
        target = "nocloom::" + component.lower()
        (directory / "CMakeLists.txt").write_text(f"""cmake_minimum_required(VERSION 3.24)
project({name}_consumer LANGUAGES CXX)
find_package(nocloom CONFIG REQUIRED COMPONENTS {component})
add_executable(consumer main.cpp)
target_link_libraries(consumer PRIVATE {target})
""")
        a = self.args
        try:
            self.run(["cmake", "-S", directory, "-B", directory / "build", "-G", a.generator,
                      "-DCMAKE_MAKE_PROGRAM=" + a.make_program,
                      "-DCMAKE_CXX_COMPILER=" + a.compiler, "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
                      "-DCMAKE_PREFIX_PATH=" + str(self.prefix),
                      "-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF",
                      "-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF",
                      *["-DCMAKE_DISABLE_FIND_PACKAGE_" + d + "=ON" for d in self.DISABLED]],
                     "configure", cwd=directory)
            self.run(["cmake", "--build", directory / "build", "-j2"], "build", cwd=directory)
        except AssertionError as error:
            raise Infra(f"consumer {name}: {error}") from error
        self.consumers[name] = directory / "build" / "consumer"
        return self.consumers[name]

    def generate(self, config, out, label="cli-generate"):
        """The installed CLI on a YAML file or a definition dict."""
        out = Path(out)
        if isinstance(config, dict):
            path = out.parent / (out.name + ".yaml")
            path.write_text(json.dumps(config, indent=1) + "\n")
            config = path
        self.run([self.cli, "generate", config, "--out-dir", out], label, cwd=out.parent)
        return digests(out)

    def verify(self, directory, case, scenario):
        """The installed CLI's artifacts in case["dut"] through the verif stack: its BFMs, the
        case's tape and the offline checker on the selected simulator engine; then the negative control,
        where every designated fault of the case must be caught. Returns the verdicts."""
        args = argparse.Namespace(cli=self.cli, fixture=None, only="", sim=self.args.sim,
                                  sim_config=self.args.sim_config)
        module = types.SimpleNamespace(scenario=scenario)
        results = dict(variants=[], timings={}, inputs={})
        directory = Path(directory)
        try:
            state = nlrun.run_baseline(args, module, case, directory / "main", "", [1], results)
            if not state["infra"] and case.get("faults"):
                nlrun.run_negative(args, module, case, state, directory, "", [1], results, False)
        except nlrun.Infra as error:
            results["error"] = str(error)
        (directory / "verif.json").write_text(json.dumps(results, indent=1, default=str) + "\n")
        verdicts = [dict(variant=v["variant"], status=v["status"], **(
            dict(cls=v["class"], caught_by=v.get("caught_by", [])) if "class" in v else {}))
            for v in results["variants"]]
        bad = [v for v in results["variants"] if v.get("class", "CAUGHT") != "CAUGHT"
               or ("class" not in v and v["status"] != "PASS")]
        build = results.get("fault_build", dict(status="OK"))
        assert "error" not in results and build["status"] == "OK" and not bad, \
            f"verif {directory.name}: {results.get('error', '')} {build['status']} " + \
            json.dumps([dict(variant=v["variant"], status=v["status"], cls=v.get("class"),
                             verdict=v.get("verdict"),
                             mismatches=v.get("check", {}).get("mismatches", [])[:4])
                        for v in bad])[:2500]
        return dict(engine=results.get("engine"), inputs=results["inputs"], variants=verdicts)


def endpoints(cfg, mem_bytes=65536):
    """BFMs and offline model of an independent schema 10 endpoint table (names, protocols,
    widths, domains, windows; not read back from the artifacts): an APB or AXI4-Lite
    initiator BFM per source and an APB or AXI4-Lite target BFM per target, each on the clock
    of its domain. The input selects response classes, with zero data on failure."""
    bfms = []
    for s in cfg["initiators"]:
        spec = dict(name=s["name"], endpoint="initiator:" + s["name"], clock=s["clock_domain"])
        if s["protocol"].startswith("apb"):
            spec.update(kind="apb_master", dialect=int(s["protocol"][-1]))
        else:
            spec.update(kind="axi_master", lite=True)
        bfms.append(spec)
    for k, t in enumerate(cfg["targets"]):
        spec = dict(name=t["name"], endpoint="target:" + t["name"], clock=t["clock_domain"],
                    salt=k + 1, mem_bytes=mem_bytes)
        spec.update(dict(kind="apb_slave") if t["protocol"] == "apb4"
                    else dict(kind="axi_slave", lite=True))
        bfms.append(spec)
    model = dict(routes=nlconfig.routes(cfg), error_codes=nlconfig.error_codes(cfg),
                 targets=nlconfig.targets(cfg, error_data_zero=True))
    return dict(bfms=bfms, model=model)


def error_word(t, target, kind, word=0x38):
    """The target answers SLVERR for the local word at `word` (PSLVERR, BRESP and RRESP)."""
    if kind == "apb_slave":
        t.set(target, "errm_mask", 0xFFFFFFFC)
        t.set(target, "errm_match", word)
    else:
        t.set(target, "errm0_mask", 0xFFFFFFFC)
        t.set(target, "errm0_match", word)
        t.set(target, "errm0_code", 2)


def cancel_and_resume(t, source, target, kind, address):
    """A read of `source` held at `target` (APB ACCESS waiting for PREADY, or AXI4-Lite
    ARVALID never accepted) is cut by the common reset; afterwards one fresh read completes
    and is the target's only access since the cut."""
    t.mark("phase", 9)
    counter = "access" if kind == "apb_slave" else "ar"
    since = t.snap(target, counter)
    if kind == "apb_slave":
        t.set(target, "wait", 100000)
        t.read(source, address)
        t.wait(target, "psel", "==", 1, label="TARGET_WAITING")
    else:
        t.ready(target, "ar", "never")
        t.read(source, address)
        t.wait(target, "arvalid", "==", 1, label="TARGET_WAITING")
    t.delay(6)
    t.reset(10)
    if kind == "apb_slave":
        t.set(target, "wait", 0)
    else:
        t.ready(target, "ar", "always")
    t.delay(16)
    t.read(source, address, targets=1)
    t.drain(limit=4000, label="AFTER_RESET")
    t.expect_now(target, counter, "==", 1, "FRESH_READ_ONLY", since=since)


def concurrent(minimum=2):
    """A case oracle: at least `minimum` sources have a transaction in flight at once."""
    def oracle(checker, events, sources, targets):
        edges = sorted([(s.issued_t, 1, s.port) for s in sources if s.issued_t is not None
                        and s.done_t is not None] +
                       [(s.done_t, 0, s.port) for s in sources if s.issued_t is not None
                        and s.done_t is not None])
        open_, peak = {}, 0
        for _, up, port in edges:
            open_[port] = open_.get(port, 0) + (1 if up else -1)
            peak = max(peak, sum(1 for v in open_.values() if v > 0))
        return [] if peak >= minimum else [dict(detail="sources never overlapped", peak=peak,
                                                want=minimum)]
    return oracle


def port_faults(source, source_kind, target, target_kind, target_bits=32):
    """Text faults on the generated module's port assignments (one hit each): a flipped
    target write data bit, a flipped source read data bit, a source error response lost, a
    flipped target address bit (the lowest above the target's bus word). The offline checker
    must catch each."""
    word = max(2, (target_bits // 8).bit_length() - 1)
    apb_s, apb_t = source_kind == "apb_master", target_kind == "apb_slave"

    def flip(port, mask):
        return [(r"^(assign " + port + r" = )([^;]+);", r"\g<1>\g<2> ^ " + mask + ";", 1)]
    lost = ([(r"^assign nl_i_" + source + r"_pslverr = [^;]+;",
              "assign nl_i_" + source + "_pslverr = 1'b0;", 1)] if apb_s else
            [(r"^assign nl_i_" + source + r"_rresp = [^;]+;",
              "assign nl_i_" + source + "_rresp = 2'b00;", 1)])
    return [
        dict(id="target_wdata", op="text", expect=["TARGET_WRITE_BYTES"],
             subs=flip(f"nl_t_{target}_" + ("pwdata" if apb_t else "wdata"), "1'b1")),
        dict(id="source_rdata", op="text", expect=["SOURCE_R_DATA"],
             subs=flip(f"nl_i_{source}_" + ("prdata" if apb_s else "rdata"), "1'b1")),
        dict(id="source_error_lost", op="text", expect=["SOURCE_R_RESP", "SOURCE_B_RESP"],
             subs=lost),
        dict(id="target_address", op="text",
             expect=["TARGET_UNATTRIBUTED", "TARGET_FOOTPRINT", "TARGET_DESCRIPTOR",
                     "TARGET_WRITE_BYTES", "SOURCE_R_DATA"],
             subs=flip(f"nl_t_{target}_" + ("paddr" if apb_t else "araddr"),
                       f"{word + 1}'b1" + "0" * word)),
    ]


def controls(check, base, mutations):
    """Negative controls of a metadata check: each mutation of the record
    must fail the check with the named cause. Returns the witnesses."""
    out = []
    for name, mutate, cause in mutations:
        bad = json.loads(json.dumps(base))
        mutate(bad)
        try:
            check(bad)
        except AssertionError as failure:
            assert cause is None or str(failure) == cause, \
                f"control {name}: failed with {failure!r}, want {cause!r}"
            out.append(dict(control=name, status="CAUGHT", cause=str(failure)))
        else:
            raise AssertionError("SURVIVED " + name)
    return out


def load(path, params):
    spec = importlib.util.spec_from_file_location("nlv_install_" + Path(path).stem, path)
    module = importlib.util.module_from_spec(spec)
    module.PARAMS = dict(params)
    spec.loader.exec_module(module)
    return module


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--case", required=True)
    p.add_argument("--param", action="append", default=[])
    for key in ("source", "build", "compiler", "generator", "make-program", "work"):
        p.add_argument("--" + key, required=True)
    sim_engine.add_arguments(p)
    a = p.parse_args()
    top = Path(a.work).resolve()
    top.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="run-", dir=top))
    (top / "latest.txt").write_text(str(root) + "\n")
    results = dict(schema=1, layer="install", case=Path(a.case).name, entries=[], work=str(root))

    def save(code):
        results["exit"] = code
        text = json.dumps(results, indent=1, default=str) + "\n"
        (root / "results.json").write_text(text)
        (top / "results.json").write_text(text)
        passed = sum(row["status"] == "PASS" for row in results["entries"])
        print("INSTALL", "PASS" if code == 0 else "FAIL",
              f"{passed}/{planned or len(results['entries'])} entries passed")
        return code

    failed = False
    planned = 0
    current = None
    try:
        module = load(a.case, dict(kv.split("=", 1) for kv in a.param))
        ctx = Context(a, root)
        started = time.monotonic()
        ctx.install()
        results["install_seconds"] = round(time.monotonic() - started, 3)
        results["version"] = ctx.version
        planned = len(module.ENTRIES)
        for name, entry in module.ENTRIES:
            current = name
            directory = root / name
            directory.mkdir()
            started = time.monotonic()
            row = dict(name=name)
            try:
                row["record"] = entry(ctx, directory)
                row["status"] = "PASS"
            except AssertionError as error:
                row["status"] = "FAIL"
                row["detail"] = str(error)[-3000:]
                print(f"INSTALL FAIL {name}: {str(error)[-600:]}")
                failed = True
            row["seconds"] = round(time.monotonic() - started, 3)
            results["entries"].append(row)
    except Infra as error:
        results["error"] = str(error)
        print(f"INSTALL ERROR {current or 'setup'}: {str(error)[-600:]}")
        return save(2)
    except Exception:
        results["error"] = traceback.format_exc()
        print(f"INSTALL ERROR {current or 'setup'}: {results['error'][-600:]}")
        return save(2)
    return save(1 if failed else 0)


if __name__ == "__main__":
    sys.exit(main())
