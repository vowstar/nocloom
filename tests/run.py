"""CTest integration driver. All generated inputs and logs stay in isolated run directories."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sim_process
import tempfile


parser = argparse.ArgumentParser()
for argument in ("case", "cli", "source", "build", "work"):
    parser.add_argument("--" + argument, required=True)
parser.add_argument("--compiler")
parser.add_argument("--c-compiler")
parser.add_argument("--definition-generator")
parser.add_argument("--generator", default="Ninja")
parser.add_argument("--make-program")
parser.add_argument("--toolchain")
parser.add_argument("--schema")
parser.add_argument("--xmllint")
args = parser.parse_args()
SOURCE, BUILD, CLI = Path(args.source), Path(args.build), Path(args.cli)
Path(args.work).mkdir(parents=True, exist_ok=True)
ROOT = Path(tempfile.mkdtemp(prefix="run-", dir=args.work))
print(f"Work directory: {ROOT}", flush=True)
command_count = 0


def run(command, cwd=ROOT, expected=0, timeout=90, **kwargs):
    global command_count
    command_count += 1
    command = list(map(str, command))
    result = sim_process.run(command, cwd=cwd, text=True, capture_output=True,
                            timeout=timeout, **kwargs)
    (ROOT / f"command-{command_count:03}.log").write_text(
        json.dumps(command) + f"\ncwd: {cwd}\nexit: {result.returncode}\n"
        + result.stdout + "\nstderr:\n" + result.stderr)
    if expected is not None and result.returncode != expected:
        raise AssertionError(f"Exit {result.returncode}, expected {expected}: {command}\n"
                             + result.stdout + result.stderr)
    return result


def definition(width, targets, decimal=False):
    text = f"""schema_version: 10
name: test_bus
address_width: {width}
reset_ports: [{{name: reset_n}}]
test_ports: [{{name: test}}]
clock_domains:
  - name: main
    clock_port: clk_main
    reset_port: reset_n
    test_port: test
initiators:
  - name: control
    clock_domain: main
    protocol: apb4
    data_width: 32
targets:
"""
    for name, begin, end in targets:
        base, size = (str(begin), str(end - begin)) if decimal else (hex(begin), hex(end - begin))
        text += f"  - name: {name}\n    clock_domain: main\n    protocol: apb4\n    attributes: amba\n"
        text += "    data_width: 32\n"
        text += f'    mappings: [{{base: "{base}", size: "{size}"}}]\n'
    return text + "connectivity: all\noutput:\n  backend: verilog\n  language: verilog_2001\n"


MAIN = [("alpha", 0x1000, 0x1100), ("beta", 0x2000, 0x2080),
        ("gamma", 0xFFFFF000, 1 << 32)]


def generate(name, width=32, targets=MAIN, text=None):
    folder = ROOT / name
    folder.mkdir()
    source = folder / "definition.yaml"
    source.write_text(definition(width, targets) if text is None else text)
    run([CLI, "generate", source, "--out-dir", folder / "bundle"])
    bundle = folder / "bundle"
    for artifact in bundle.iterdir():
        text = artifact.read_text()
        assert 'LicenseRef-NocLoom-Output-1.0' in text, artifact.name
        if artifact.suffix == '.json':
            assert json.loads(text)['_license'] == {
                'SPDX-License-Identifier': 'LicenseRef-NocLoom-Output-1.0',
                'scope': 'NocLoom supplied content only',
                'license_file': 'LICENSES/LicenseRef-NocLoom-Output-1.0.txt',
                'project_license_file': 'LICENSES/Apache-2.0.txt'}, artifact.name
    manifest = json.loads((bundle / "nocloom_test_bus.bundle.json").read_text())
    assert manifest["language"] == "verilog_2001"
    assert manifest["simulation_sources"] == manifest["synthesis_sources"] == [
        "nocloom_clock_gate.v", "nocloom_synchronizer.v", "nocloom_test_bus.v"]
    assert sorted(x["role"] for x in manifest["files"]) == ["cell_model", "cell_model", "dependency_graph",
                                                            "interface_map", "manifest", "rtl"]
    assert {x["path"] for x in manifest["files"]} == {x.name for x in bundle.iterdir()}
    for entry in manifest["files"]:
        assert Path(entry["path"]).name == entry["path"]
    # Exercise consumption after moving the complete generated bundle.
    moved = folder / "relocated"
    assert bundle.is_dir() and not bundle.is_symlink()
    assert not moved.exists() and not moved.is_symlink()
    parent = folder.resolve()
    assert parent != ROOT.resolve() and parent.is_relative_to(ROOT.resolve())
    assert bundle.resolve().parent == moved.resolve().parent == parent
    bundle.rename(moved)
    return moved


def version_only_in_bundle(folder, version):
    """The bundle alone records the generator version; the other files name the generator only."""
    dotted = re.compile(r"(?<![\d.])" + re.escape(version) + r"(?![\d.])")
    seen = set()
    for artifact in folder.iterdir():
        text, manifest = artifact.read_text(), artifact.name.endswith(".bundle.json")
        assert bool(dotted.search(text)) == manifest, artifact.name
        lines = text.splitlines()
        if artifact.suffix == ".v":
            assert lines[0] == "// Generated by NocLoom", artifact.name
        elif artifact.suffix == ".json":
            record = json.loads(text)
            assert record["generator"] == "NocLoom", artifact.name
            assert ("version" in record) == manifest, artifact.name
            assert not manifest or record["version"] == version
        elif artifact.suffix == ".xml":
            assert "<spirit:version>1.0</spirit:version>" in text
            assert "<spirit:description>Generated by NocLoom</spirit:description>" in text
        elif artifact.suffix != ".csv":
            assert lines[2] == "# Generated by NocLoom", artifact.name
        seen.add(artifact.name.split(".", 1)[1])
    return seen


def cli():
    for flag in ("--help", "--version"):
        assert run([CLI, flag]).stdout
    version = run([CLI, "--version"]).stdout.strip()
    run([CLI, "--unknown"], expected=2)
    run([CLI, "generate"], expected=2)
    run([CLI, "validate", ROOT / "missing.yaml"], expected=3)
    run([CLI, "validate", ROOT / "missing.json"], expected=1)
    first = generate("first")
    run([CLI, "validate", first.parent / "definition.yaml"])
    # An unknown option is a usage error (exit 2).
    unknown = ROOT / "unknown"
    run([CLI, "generate", first.parent / "definition.yaml", "--out-dir", unknown,
         "--no-such-option"], expected=2)
    assert not unknown.exists()
    before = {p.name: p.read_bytes() for p in first.iterdir()}
    run([CLI, "generate", first.parent / "definition.yaml", "--out-dir", first], expected=3)
    assert before == {p.name: p.read_bytes() for p in first.iterdir()}
    second = generate("second", targets=list(reversed(MAIN)), text=definition(32, list(reversed(MAIN)), True))
    assert before == {p.name: p.read_bytes() for p in second.iterdir()}
    # The address table is opt-in: it adds two files and two bundle entries, nothing else, and
    # equivalent declarations give the same bytes.
    tables = []
    for name in ("first", "second"):
        out = ROOT / ("table-" + name)
        run([CLI, "generate", ROOT / name / "definition.yaml", "--out-dir", out, "--address-table"])
        tables.append({p.name: p.read_bytes() for p in out.iterdir()})
    assert tables[0] == tables[1]
    files = tables[0]
    table = "nocloom_test_bus.address_table."
    assert set(files) - set(before) == {table + "json", table + "csv"}
    assert all(files[n] == b for n, b in before.items() if not n.endswith(".bundle.json"))
    manifest = json.loads(files["nocloom_test_bus.bundle.json"])
    assert manifest["files"][-2:] == [{"path": table + "json", "role": "address_table"},
                                      {"path": table + "csv", "role": "address_table"}]
    assert {x["path"] for x in manifest["files"]} == set(files)
    assert files[table + "csv"].decode().splitlines() == [
        "# SPDX-License-Identifier: LicenseRef-NocLoom-Output-1.0",
        "# NocLoom supplied content only. See NocLoom LICENSES/LicenseRef-NocLoom-Output-1.0.txt",
        "initiator,base,size,target,local_base,access,exclusive,default_target",
        "control,0x00001000,0x00000100,alpha,0x00000000,read_write,none,false",
        "control,0x00002000,0x00000080,beta,0x00000000,read_write,none,true",
        "control,0xfffff000,0x00001000,gamma,0x00000000,read_write,none,false"]
    assert json.loads(files[table + "json"])["initiators"][0]["default_target"] == "beta"
    bad = ROOT / "bad.yaml"
    bad.write_text(definition(32, MAIN).replace("protocol: apb4", "protocol: apb2", 1))
    absent = ROOT / "must-not-exist"
    result = run([CLI, "generate", bad, "--out-dir", absent], expected=1)
    assert not result.stdout and "NCL_UNSUPPORTED_PROTOCOL" in result.stderr and not absent.exists()
    run([CLI, "generate", bad, "--out-dir", absent, "--target", "systemc"], expected=1)
    assert not absent.exists()
    # CDC constraints are opt-in: without a crossing they are refused, with crossings they
    # add two files and their bundle entries and change nothing else.
    result = run([CLI, "generate", first.parent / "definition.yaml", "--out-dir", absent,
                  "--cdc-constraints"], expected=1)
    assert "NCL_UNSUPPORTED_BACKEND [output.cdc_constraints]" in result.stderr
    assert not absent.exists()
    clocked = SOURCE / "examples/axi4_graph_clocked.yaml"
    plain, extended = ROOT / "clocked-plain", ROOT / "clocked-cdc"
    run([CLI, "generate", clocked, "--out-dir", plain])
    run([CLI, "generate", clocked, "--out-dir", extended, "--cdc-constraints"])
    top = "nocloom_graph"
    added = {top + ".cdc.sdc", top + ".cdc_preserve.txt"}
    assert {p.name for p in extended.iterdir()} == {p.name for p in plain.iterdir()} | added
    for p in plain.iterdir():
        if not p.name.endswith(".bundle.json"):
            assert p.read_bytes() == (extended / p.name).read_bytes(), p.name
    manifest = json.loads((extended / (top + ".bundle.json")).read_text())
    assert {x["path"] for x in manifest["files"]} == {p.name for p in extended.iterdir()}
    assert manifest["files"][:-2] == json.loads(
        (plain / (top + ".bundle.json")).read_text())["files"]
    sdc = (extended / (top + ".cdc.sdc")).read_text()
    assert sdc.count("\nset_max_delay ") == sdc.count("\nset_min_delay 0 ") == 36
    complete = ROOT / "clocked-all"
    run([CLI, "generate", clocked, "--out-dir", complete, "--cdc-constraints", "--ipxact",
         "--address-table"])
    assert version_only_in_bundle(complete, version) == {
        "v", "map.json", "bundle.json", "dependency.json", "ipxact.xml", "address_table.json",
        "address_table.csv", "cdc.sdc", "cdc_preserve.txt"}
    link = ROOT / "link"
    link.symlink_to(first, target_is_directory=True)
    run([CLI, "generate", first.parent / "definition.yaml", "--out-dir", link / "new"], expected=3)
    assert not (first / "new").exists()
    # Reproducibly exercise a short write, including the names of incomplete outputs.
    if os.name == "posix":
        import resource
        import signal

        def limit_output():
            resource.setrlimit(resource.RLIMIT_FSIZE, (128, 128))
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)

        partial = ROOT / "partial"
        result = run([CLI, "generate", first.parent / "definition.yaml", "--out-dir", partial],
                     expected=3, preexec_fn=limit_output)
        assert "NCL_IO_WRITE" in result.stderr and "output created:" in result.stderr
        assert len(list(partial.iterdir())) == 1


def install():
    prefix = ROOT / "prefix"
    run(["cmake", "--install", BUILD, "--prefix", prefix])
    moved = ROOT / "relocated"
    assert prefix.is_dir() and not prefix.is_symlink()
    assert not moved.exists() and not moved.is_symlink()
    assert prefix.resolve().parent == moved.resolve().parent == ROOT.resolve()
    prefix.rename(moved)
    run([moved / "bin/nocloom", "--version"])
    for mode in ("core", "io", "subdirectory"):
        folder = ROOT / mode
        folder.mkdir()
        lines = ["cmake_minimum_required(VERSION 3.24)", "project(consumer LANGUAGES CXX)"]
        if mode == "subdirectory":
            lines += ['set(NOCLOOM_BUILD_CONFIG OFF CACHE BOOL "")',
                      f'add_subdirectory("{SOURCE.as_posix()}" nocloom)',
                      'if(TARGET nocloom_cli OR TARGET nocloom_unit OR TARGET nocloom_api_example)',
                      'message(FATAL_ERROR "Subdirectory builds must not enable applications by default")', 'endif()']
        else:
            lines += [f"find_package(nocloom CONFIG REQUIRED COMPONENTS {'Core' if mode == 'core' else 'IO'})"]
        lines += ["add_executable(consumer main.cpp)",
                  f"target_link_libraries(consumer PRIVATE nocloom::{'io' if mode == 'io' else 'core'})"]
        (folder / "CMakeLists.txt").write_text("\n".join(lines) + "\n")
        shutil.copyfile(SOURCE / "tests/basic_example_consumer.cpp", folder / "main.cpp")
        if mode == "io":
            with (folder / "CMakeLists.txt").open("a") as stream:
                stream.write("target_compile_definitions(consumer PRIVATE NOCLOOM_BASIC_IO=1)\n")
        command = ["cmake", "-S", folder, "-B", folder / "build", "-G", args.generator,
                   "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
                   f"-DCMAKE_PREFIX_PATH={moved}", "-DCMAKE_FIND_USE_PACKAGE_REGISTRY=OFF",
                   "-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF"]
        if args.toolchain:
            command += ["-DCMAKE_TOOLCHAIN_FILE=" + args.toolchain]
        if args.compiler:
            command += ["-DCMAKE_CXX_COMPILER=" + args.compiler]
        if args.make_program:
            command += ["-DCMAKE_MAKE_PROGRAM=" + args.make_program]
        if mode == "core":
            command += [f"-DCMAKE_DISABLE_FIND_PACKAGE_{dep}=ON" for dep in
                        ("yaml-cpp", "nlohmann_json", "CLI11", "Catch2")]
        run(command)
        run(["cmake", "--build", folder / "build", "-j4"], timeout=300)
        for example in ("apb_simple", "apb_shared", "axi_lite_apb", "apb_domains"):
            config = SOURCE / "examples" / (example + ".yaml")
            destination = folder / example
            run([folder / "build/consumer", example, destination, config])
            cli_output = ROOT / ("cli-" + example)
            if mode == "core":
                run([moved / "bin/nocloom", "generate", config, "--out-dir", cli_output])
            expected = {p.name: p.read_bytes() for p in cli_output.iterdir()}
            actual = {p.name: p.read_bytes() for p in destination.iterdir()}
            assert actual == expected, (mode, example, "installed artifact mismatch")
        if mode == "core":
            config = SOURCE / "examples/apb_domains.yaml"
            run([folder / "build/consumer", "apb_domains", folder / "ipxact", config, "ipxact"])
            run([moved / "bin/nocloom", "generate", config, "--out-dir", ROOT / "cli-ipxact",
                 "--ipxact"])
            expected = {p.name: p.read_bytes() for p in (ROOT / "cli-ipxact").iterdir()}
            actual = {p.name: p.read_bytes() for p in (folder / "ipxact").iterdir()}
            assert actual == expected and len(actual) == 7, "installed IP-XACT mismatch"
            run([folder / "build/consumer", "apb_simple", folder / "headers",
                 SOURCE / "examples/apb_simple.yaml", "c-header"])
            run([moved / "bin/nocloom", "generate", SOURCE / "examples/apb_simple.yaml",
                 "--c-header", "--out-dir", ROOT / "cli-headers"])
            expected = {p.name: p.read_bytes() for p in (ROOT / "cli-headers").iterdir()}
            actual = {p.name: p.read_bytes() for p in (folder / "headers").iterdir()}
            assert actual == expected and len(actual) == 7, "installed C header mismatch"


def c_header():
    source = ROOT / "wide.yaml"
    source.write_text(definition(48, [("wide", 0x100000, 0x101000)])
                      .replace('size: "0x1000"}',
                               'size: "0x1000", local_address: "0x1000"}, '
                               '{base: "0x123450000000", size: "0x1000", '
                               'local_address: "0x2000"}')
                      .replace("protocol: apb4", "protocol: axi4")
                      .replace("    data_width: 32\n", "    data_width: 32\n    len_width: 8\n"))
    run([CLI, "generate", source, "--c-header", "--address-table", "--ipxact",
         "--out-dir", ROOT / "wide"])
    folder = ROOT / "wide"
    header = folder / "nocloom_test_bus.control.addresses.h"
    log_source = ROOT / "logs.yaml"
    log_yaml = (definition(32, [("ram", 0, 0x1000)])
                          .replace("name: control", "name: logger_control") + """
error_logging:
  registers: {name: csr, base: '0x100000', size: '0x100000', clock_domain: main}
  logs:
    - name: errors
      watch: [ram]
      loggers: [{offset: '0x200'}, {offset: '0x500'},
                {offset: '0x1000'}, {offset: '0xfffc0'}]
""")
    log_source.write_text(log_yaml)
    run([CLI, "generate", log_source, "--c-header", "--out-dir", ROOT / "logs"])
    for group, offsets in (("local", "[{offset: '0x1000'}]"),
                           ("cross", "[{offset: '0x1000'}, {offset: '0x1200'}, "
                            "{offset: '0x2000'}, {offset: '0xfffc0'}]")):
        local_yaml = re.sub(r"loggers: \[.*?\]", "loggers: " + offsets, log_yaml, flags=re.S)
        local_yaml = local_yaml.replace("size: '0x100000'", "size: '0x100000', "
                                       "local_address: '0x1000'")
        local_source = ROOT / (group + ".yaml")
        local_source.write_text(local_yaml)
        run([CLI, "generate", local_source, "--c-header", "--out-dir", ROOT / group])
    cut_source = ROOT / "cut.yaml"
    cut_source.write_text(definition(32, [("ram", 0, 0x1000), ("gap", 0x1007d0, 0x1007d8)])
                          .replace("name: control", "name: logger_control") + """
error_logging:
  registers: {name: csr, base: '0x100000', size: '0x100000', local_address: '0x1000',
              clock_domain: main}
  revision: 305419896
  logs:
    - name: errors
      watch: [ram]
      loggers: [{offset: '0x1000'}, {offset: '0x17c0'}, {offset: '0x2000'}]
""")
    run([CLI, "generate", cut_source, "--c-header", "--out-dir", ROOT / "cut"])
    window = "NOCLOOM_TEST_BUS_LOGGER_CONTROL_CSR_WINDOW_0"
    layout = "NOCLOOM_TEST_BUS_ERROR_LOG"
    held = layout + "_RECORD_HELD_OFFSET"
    prelude = """#ifdef __cplusplus
#define CHECK static_assert
#else
#define CHECK _Static_assert
#endif
#define PART(word, field) (((word) & field##_MASK) >> field##_SHIFT)
"""
    # A register is at the window's base plus the bank's offset plus the field's offset.
    assertions = f"""#include "nocloom_test_bus.control.addresses.h"
#include "nocloom_test_bus.control.addresses.h"
#include "nocloom_test_bus.logger_control.addresses.h"
#include "nocloom_test_bus.error_logs.h"
#include "nocloom_test_bus.error_logs.h"
{prelude}CHECK(NOCLOOM_TEST_BUS_CONTROL_WIDE_WINDOW_0_BASE == 0x100000U, "global base");
CHECK(NOCLOOM_TEST_BUS_CONTROL_WIDE_WINDOW_0_BASE + 0x10U == 0x100010U, "global word");
CHECK(NOCLOOM_TEST_BUS_CONTROL_WIDE_WINDOW_0_SIZE == 0x1000U, "size");
CHECK(NOCLOOM_TEST_BUS_CONTROL_WIDE_WINDOW_1_BASE == 0x123450000000ULL, "wide alias");
CHECK({layout}_LAYOUT == 0x2U, "layout");
CHECK({layout}_BANK_BYTES == 0x40U, "bank bytes");
CHECK({layout}_CLASS_DECODE == 0x1U, "decode class");
CHECK({layout}_CLASS_TARGET == 0x2U, "target class");
CHECK({held} == 0x4U, "state word");
CHECK({window}_CSR_OFFSET + {held} == 0x204U, "first bank");
CHECK({window}_ERRORS_1_OFFSET + {held} == 0x504U, "second bank");
CHECK({window}_ERRORS_2_OFFSET + {held} == 0x1004U, "bank above 4 KiB");
CHECK({window}_ERRORS_3_OFFSET + {held} == 0xfffc4U, "last bank");
CHECK({window}_BASE + {window}_ERRORS_2_OFFSET + {held} == 0x101004U, "global register");
CHECK({window}_BASE + {window}_ERRORS_3_OFFSET + {held} == 0x1fffc4U, "last register");
CHECK({window}_BASE + {window}_ERRORS_3_OFFSET + {layout}_REVISION_OFFSET == 0x1fffdcU,
      "last revision");
CHECK({window}_CSR_ID_VALUE == 0x02000008U, "first bank id");
CHECK({window}_ERRORS_3_ID_VALUE == 0x02000300U, "last bank id");
CHECK(PART({window}_ERRORS_3_ID_VALUE, {layout}_LAYOUT) == {layout}_LAYOUT, "layout of a bank");
CHECK(PART({window}_ERRORS_3_ID_VALUE, {layout}_LOGGER_INDEX) == 3U, "logger of a bank");
CHECK(PART({window}_ERRORS_3_ID_VALUE, {layout}_LOG_INDEX) == 0U, "log of a bank");
CHECK(PART({window}_CSR_ID_VALUE, {layout}_CAPABILITY_NEXT_LOGGER) == 1U, "a logger follows");
CHECK(PART({window}_ERRORS_3_ID_VALUE, {layout}_CAPABILITY_NEXT_LOGGER) == 0U, "last logger");
CHECK(PART(0x12345678U, {layout}_RECORD_CLASS) == 0x7U, "class of a state word");
CHECK(PART(0x12345678U, {layout}_RECORD_BYTES) == 0x1234U, "bytes of a word");
CHECK(PART(0xffffffffU, {layout}_RECORD_TARGET_PATH) == 0x1fffU, "target path of a word");
CHECK(PART(0x87654321U, {layout}_RECORD_USER_FLAGS7) == 0x87654321U, "user flags of a word");
#ifdef {window}_CSR_REVISION_VALUE
#error "a bank states a revision value only when the definition gives one"
#endif
#ifdef {window}_CSR_RECORD_HELD_OFFSET
#error "a whole bank has no macro per field"
#endif
int main(void) {{ return 0; }}
"""
    run([args.definition_generator, ROOT / "direct"])
    from log_mapping_bench import simulate
    for output in (ROOT, ROOT / "direct"):
        simulate(output / "local", [0x1000], header=True)
    for group in ("wide", "logs", "local", "cross", "cut"):
        for generated in (ROOT / group).glob("*.h"):
            assert generated.read_bytes() == (ROOT / "direct" / group / generated.name).read_bytes()

    def compiles(folder, checks, spoiled, include=None):
        """Both compilers take the checks, and refuse them with one expectation spoiled."""
        for compiler, standard, suffix in ((args.c_compiler, "c11", "c"),
                                           (args.compiler, "c++20", "cpp")):
            for k, (right, wrong) in enumerate([(None, None)] + spoiled):
                assert right is None or checks.count(right) == 1, right
                unit = folder / f"check-{k}.{suffix}"
                unit.write_text(checks if right is None else checks.replace(right, wrong))
                result = run([compiler, "-std=" + standard, "-pedantic-errors", "-Wall", "-Wextra",
                              "-Werror", *(["-I", include] if include else []), "-c", unit,
                              "-o", unit.with_suffix(".o")], expected=None)
                assert (result.returncode != 0) == (right is not None), (
                    folder, standard, right, result.stderr)

    # A wrong expected address, a wrong field and a macro that must be absent each fail.
    for output in (ROOT, ROOT / "direct"):
        compiles(output / "wide", assertions,
                 [("== 0x100000U", "== 0x100001U"), ("== 0x1fffc4U", "== 0x1fffc8U"),
                  ("== 0x1234U", "== 0x5678U"),
                  (f"#ifdef {window}_CSR_REVISION_VALUE", f"#ifdef {window}_CSR_ID_VALUE")],
                 include=output / "logs")
    local_assertions = f"""#include "nocloom_test_bus.logger_control.addresses.h"
#include "nocloom_test_bus.error_logs.h"
{prelude}CHECK({window}_BASE == 0x100000U, "window");
CHECK({window}_CSR_OFFSET + {held} == 0x4U, "window offset");
CHECK({window}_BASE + {window}_CSR_OFFSET + {held} == 0x100004U,
      "global register with nonzero local base");
"""
    cross_assertions = local_assertions + f"""
CHECK({window}_ERRORS_1_OFFSET + {held} == 0x204U, "second bank");
CHECK({window}_ERRORS_2_OFFSET + {held} == 0x1004U, "bank across 4 KiB");
CHECK({window}_BASE + {window}_ERRORS_2_OFFSET + {held} == 0x101004U,
      "global register across 4 KiB");
CHECK({window}_BASE + {window}_ERRORS_3_OFFSET + {held} == 0x1fefc4U,
      "last global register");
"""
    # Eight bytes of the target gap lie in the second bank, over its words at 0x10 and 0x14: the
    # first window reaches the four words before them, the second window the words after them.
    # The bank's target path is in the word at 0x10, so no window reaches it.
    after = "NOCLOOM_TEST_BUS_LOGGER_CONTROL_CSR_WINDOW_1"
    cut_assertions = f"""#include "nocloom_test_bus.logger_control.addresses.h"
#include "nocloom_test_bus.error_logs.h"
{prelude}CHECK({window}_BASE == 0x100000U, "first window");
CHECK({window}_SIZE == 0x7d0U, "first window ends in the bank");
CHECK({after}_BASE == 0x1007d8U, "second window");
CHECK({window}_BASE + {window}_CSR_OFFSET + {held} == 0x100004U, "whole bank before the cut one");
CHECK({window}_CSR_ID_VALUE == 0x02000018U, "id with a given revision");
CHECK({window}_CSR_REVISION_VALUE == 0x12345678U, "given revision");
CHECK({window}_BASE + {window}_ERRORS_1_LAYOUT_OFFSET == 0x1007c0U, "cut bank, first word");
CHECK({window}_BASE + {window}_ERRORS_1_RECORD_HELD_OFFSET == 0x1007c4U, "cut bank, state word");
CHECK({window}_BASE + {window}_ERRORS_1_RECORD_ADDRESS_LOW_OFFSET == 0x1007c8U,
      "cut bank, address word");
CHECK({window}_BASE + {window}_ERRORS_1_RECORD_BYTES_OFFSET == 0x1007ccU,
      "cut bank, last word of the first window");
CHECK({after}_BASE + {after}_ERRORS_1_REVISION_OFFSET == 0x1007dcU,
      "cut bank, revision in the second window");
CHECK({after}_BASE + {after}_ERRORS_2_OFFSET + {held} == 0x101004U, "whole bank after the cut one");
CHECK({after}_ERRORS_2_ID_VALUE == 0x02000210U, "id of the last bank");
#if defined({window}_ERRORS_1_OFFSET) || defined({after}_ERRORS_1_OFFSET) || \\
    defined({window}_ERRORS_1_ID_VALUE) || defined({after}_ERRORS_1_ID_VALUE) || \\
    defined({window}_ERRORS_1_REVISION_VALUE) || defined({after}_ERRORS_1_REVISION_VALUE)
#error "a cut bank states no offset of its own and no value of a word"
#endif
#if defined({window}_ERRORS_1_RECORD_TARGET_PATH_OFFSET) || \\
    defined({after}_ERRORS_1_RECORD_TARGET_PATH_OFFSET) || \\
    defined({window}_ERRORS_1_REVISION_OFFSET) || defined({after}_ERRORS_1_RECORD_HELD_OFFSET)
#error "a field has a macro only in the window that reaches its word"
#endif
"""
    cut_spoiled = [("== 0x1007c4U", "== 0x1007c8U"), ("== 0x1007dcU", "== 0x1007d8U"),
                   (f"defined({window}_ERRORS_1_OFFSET)", f"defined({window}_CSR_OFFSET)"),
                   (f"defined({after}_ERRORS_1_RECORD_HELD_OFFSET)",
                    f"defined({after}_ERRORS_1_REVISION_OFFSET)")]
    for output in (ROOT, ROOT / "direct"):
        for group, checks, spoiled in (
                ("local", local_assertions, [("== 0x100004U", "== 0x101004U")]),
                ("cross", cross_assertions, [("== 0x100004U", "== 0x101004U"),
                                             ("== 0x1fefc4U", "== 0x1fffc4U")]),
                ("cut", cut_assertions, cut_spoiled)):
            compiles(output / group, checks + "int main(void) { return 0; }\n", spoiled)
    # Each field of the cut bank has one macro, in the window that reaches its word.
    cut_macros = {name: int(number, 16) for name, number in re.findall(
        r"#define (\w+_ERRORS_1_\w+) \((0x[0-9a-f]+)U\)",
        (ROOT / "cut" / "nocloom_test_bus.error_logs.h").read_text())}
    cut_bank = json.loads((ROOT / "cut" / "nocloom_test_bus.map.json").read_text())[
        "definition"]["log_registers"]["csr"]["banks"][1]
    assert cut_bank["bank"] == "errors_1" and cut_bank["base"] == 0x17c0
    offsets = {f["name"].upper(): f["offset"] for f in cut_bank["registers"]}
    assert offsets["RECORD_TARGET_PATH"] == 0x10 and offsets["REVISION"] == 0x1c
    assert cut_macros == {
        **{f"{window}_ERRORS_1_{name}_OFFSET": 0x7c0 + offset
           for name, offset in offsets.items() if offset < 0x10},
        **{f"{after}_ERRORS_1_{name}_OFFSET": offset - 0x18
           for name, offset in offsets.items() if offset >= 0x18}}
    assert "ULL)" in header.read_text()
    manifest = json.loads((folder / "nocloom_test_bus.bundle.json").read_text())
    assert [f for f in manifest["files"] if f["role"] == "c_header"] == [
        dict(path=header.name, role="c_header")]
    conflict = ROOT / "conflict.yaml"
    conflict.write_text(definition(32, [("ram", 0, 0x1000), ("RAM", 0x1000, 0x2000)]))
    rejected = ROOT / "rejected"
    result = run([CLI, "generate", conflict, "--c-header", "--out-dir", rejected], expected=1)
    assert "NCL_C_HEADER_NAME" in result.stderr and not rejected.exists()
    for name in ("double__name", "trailing_"):
        reserved = ROOT / (name + ".yaml")
        reserved.write_text(definition(32, [(name, 0, 0x1000)]))
        run([CLI, "generate", reserved, "--out-dir", ROOT / (name + "-plain")])
        rejected = ROOT / (name + "-rejected")
        result = run([CLI, "generate", reserved, "--c-header", "--out-dir", rejected], expected=1)
        assert "NCL_C_HEADER_RESERVED" in result.stderr and not rejected.exists()


def log_mapping():
    from log_mapping_bench import simulate
    shadow = ROOT / "zero-shadow"
    run([CLI, "generate", SOURCE / "tests/log_zero_shadow.yaml", "--out-dir", shadow,
         "--ipxact", "--address-table"])
    expected = json.loads((SOURCE / "tests/log_zero_shadow.json").read_text())
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in shadow.iterdir()} == expected
    run([args.definition_generator, ROOT / "direct"])
    failures = []
    for group, banks in (("local", [0x1000]),
                         ("cross", [0x1000, 0x1200, 0x4000, 0xfffc0]),
                         ("shadow", [0x2000, 0x2200, 0x4000, 0xfffc0])):
        source = ROOT / (group + ".yaml")
        text = definition(32, [("ram", 0x100000, 0x101000)] if group == "shadow"
                          else [("ram", 0, 0x1000)])
        if group == "shadow":
            text = text.replace('size: "0x1000"}', 'size: "0x1000"}, '
                                '{base: "0x108000", size: "0x1000"}')
        source.write_text(text
                          .replace("name: control", "name: logger_control") + """
error_logging:
  registers: {name: csr, base: '0x100000', size: '0x100000',
              local_address: '0x1000', clock_domain: main}
  logs:
    - name: errors
      watch: [ram]
      loggers: [""" + ", ".join("{offset: '" + hex(b) + "'}" for b in banks) + "]\n")
        run([CLI, "generate", source, "--out-dir", ROOT / group])
        for path in (ROOT / group).iterdir():
            assert path.read_bytes() == (ROOT / "direct" / group / path.name).read_bytes()
        for output in (ROOT, ROOT / "direct"):
            try:
                simulate(output / group, banks)
            except AssertionError as error:
                failures.append(f"{output / group}: {error}")
    assert not failures, "\n".join(failures)


def ipxact():
    """Every example: IEEE 1685-2009 schema-valid component, other files unchanged, stable bytes."""
    assert args.schema and args.xmllint, "--schema and --xmllint are required"
    schema = Path(args.schema) / "index.xsd"
    for config in sorted((SOURCE / "examples").glob("*.yaml")):
        plain, first, second = (ROOT / f"{config.stem}-{tag}" for tag in ("plain", "a", "b"))
        run([CLI, "generate", config, "--out-dir", plain])
        for folder in (first, second):
            run([CLI, "generate", config, "--out-dir", folder, "--ipxact"])
        before = {p.name: p.read_bytes() for p in plain.iterdir()}
        after = {p.name: p.read_bytes() for p in first.iterdir()}
        assert after == {p.name: p.read_bytes() for p in second.iterdir()}, config.name
        added = set(after) - set(before)
        assert len(added) == 1 and set(before) <= set(after), (config.name, added)
        component = added.pop()
        assert component.endswith(".ipxact.xml"), component
        for name, text in before.items():
            if name.endswith(".bundle.json"):
                manifest = json.loads(text)
                manifest["files"].append({"path": component, "role": "ipxact_component"})
                assert json.loads(after[name]) == manifest, config.name
            else:
                assert after[name] == text, (config.name, name)
        run([args.xmllint, "--noout", "--schema", schema, first / component])



run([CLI, "--version"])
globals()[args.case]()
print(f"PASS {args.case}; {command_count} commands", flush=True)
