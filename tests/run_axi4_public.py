"""Exercise installed AXI4 CLI and independent Core outputs through real target ports."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from axi4_master_exchange_bench import write_bench
from cross_hardware_sim import simulate
from sim_engine import generated_rtl


def windows(width, address, heterogeneous=False):
    if heterogeneous:
        return 0, 1 << 32, 32768
    if address > 32:
        return (1 << 32, (1 << (address - 1)) if width == 32 else (1 << address) - 65536,
                32768 if width == 32 else 65536)
    return 0, 65536, 32768


def configuration(width, address, heterogeneous=False, mixed=False, source_width=None):
    """Schema 10 two-by-two crossbar: targets at `width`, sources at `source_width`
    (default `width`); mixed makes s1 an AXI4-Lite source."""
    base0, base1, size = windows(width, address, heterogeneous)
    bits = width if source_width is None else source_width
    sw = [", address_width: 40", ", address_width: 32"] if heterogeneous else ["", ""]
    protocols = ["axi4", "axi4_lite" if mixed else "axi4"]
    # t1 lies above s1's address space in the heterogeneous network.
    connectivity = "{s0: [t0, t1], s1: [t0]}" if heterogeneous else "all"
    tw = [", address_width: 16", ", address_width: 32"] if heterogeneous else ["", ""]
    # The ports of the 32-bit profile carry 4-bit IDs and 8-bit lengths, the
    # 64-bit profile no ID (targets too: their ID width is given as 0) and
    # 4-bit lengths.
    ids, lens = (4, 8) if width == 32 else (0, 4)
    sw = [s + (f", id_width: {ids}, len_width: {lens}" if p == "axi4" else "")
          for s, p in zip(sw, protocols)]
    tw = [t + f", attributes: amba, id_width: {ids}, len_width: {lens}" for t in tw]
    return f"""schema_version: 10
name: axi4_public
address_width: {address}
arbitration: {{policy: round_robin}}
reset_ports: [{{name: reset_n}}]
test_ports: [{{name: test}}]
clock_domains: [{{name: main, clock_port: clk_main, reset_port: reset_n, test_port: test}}]
initiators:
 - {{name: s0, clock_domain: main, protocol: {protocols[0]}, data_width: {bits}{sw[0]}}}
 - {{name: s1, clock_domain: main, protocol: {protocols[1]}, data_width: {bits}{sw[1]}}}
targets:
 - {{name: t0, clock_domain: main, protocol: axi4, data_width: {width}{tw[0]}, mappings: [{{base: '{hex(base0)}', size: '{hex(size)}'}}]}}
 - {{name: t1, clock_domain: main, protocol: axi4, data_width: {width}{tw[1]}, mappings: [{{base: '{hex(base1)}', size: '{hex(size)}'}}]}}
transport: {{max_outstanding: 1, transport_word_bytes: {32 if width == 32 else 4}}}
connectivity: {connectivity}
output: {{backend: verilog, language: verilog_2001}}
"""


def inspect(directory, width, address, heterogeneous=False, mixed=False):
    top = "nocloom_axi4_public"
    expected = {top+suffix for suffix in (".v", ".map.json", ".dependency.json", ".bundle.json")} | {"nocloom_clock_gate.v", "nocloom_synchronizer.v"}
    files = {p.name for p in directory.iterdir() if p.is_file()}
    assert files == expected, "artifact set"
    m = json.loads((directory / (top+".map.json")).read_text())
    b = json.loads((directory / (top+".bundle.json")).read_text())
    assert m["module"] == b["top"] == top, "module identity"
    assert m["definition"] == b["definition"], "definition identity"
    d = m["definition"]
    assert "input_schema_version" not in d and d["address_width"] == address, "address definition"
    assert not {"protocol", "data_width"} & set(d), "data definition"
    assert d["transport"] == {"data_width": width, "id_bits": 4 if width == 32 else 0,
                              "len_bits": 8 if width == 32 else 4, "max_outstanding": 1,
                              "transport_word_bytes": 32 if width == 32 else 4,
                              "response_codes": "distinct"}, "burst definition"
    assert m["format_version"] == b["format_version"] == 1 and "schema_version" not in m, "format version"
    base0, base1, size = windows(width, address, heterogeneous)
    assert d["targets"] == [{"name": "t0", "begin": str(base0), "end": str(base0+size)},
                            {"name": "t1", "begin": str(base1), "end": str(base1+size)}], "windows"
    assert d["contract"]["write_data_unstrobed_bytes"] == \
        "zero_at_every_target_cleared_at_source_capture", "unstrobed write bytes"
    assert b["synthesis_sources"] == b["simulation_sources"] == ["nocloom_clock_gate.v", "nocloom_synchronizer.v",
                                                                       top+".v"], "sources"
    assert {x["path"] for x in b["files"]} == expected and len(b["files"]) == 6, "manifest references"
    if heterogeneous:
        assert d["endpoint_address_widths"] == {"initiators": {"s0": 40, "s1": 32},
            "targets": {"t0": 16, "t1": 32}, "transport": 40, "target_address": "local_byte_offset"}, "endpoint widths"
    else:
        assert "endpoint_address_widths" not in d, "canonical homogeneous metadata"
    if mixed:
        assert d["endpoint_protocols"] == {"initiators": {"s0": "axi4", "s1": "axi4_lite"}, "targets": "axi4"}, "endpoint protocols"
        assert d["contract"]["lite_ingress"]["unaligned"] == "address_cleared_to_bus_width_before_checks_WSTRB_as_given_read_whole_word"
    else:
        assert "endpoint_protocols" not in d, "canonical full protocol metadata"
    ports = {p["name"]: p for p in m["ports"]}
    assert len(ports) == len(m["ports"]), "port uniqueness"
    for side in ("i", "t"):
        for number in (0, 1):
            name = ("s" if side == "i" else "t") + str(number)
            prefix = f"nl_{side}_{name}_"
            for signal in ("awaddr", "araddr"):
                p = ports[prefix+signal]
                expected_width = ([40, 32] if side == "i" else [16, 32])[number] if heterogeneous else address
                assert p["width"] == expected_width and p["direction"] == ("input" if side == "i" else "output"), "address port"
            for signal in ("wdata", "rdata"):
                assert ports[prefix+signal]["width"] == width, "data port"
            for signal in ("awid", "bid", "arid", "rid"):
                assert (prefix+signal in ports) == (width == 32 and not (mixed and side == "i" and number == 1)), "ID port"
            for signal in ("awlen", "arlen", "awsize", "arsize", "awburst", "arburst", "awcache", "arcache", "awlock", "arlock", "wlast", "rlast"):
                assert (prefix+signal in ports) == (not (mixed and side == "i" and number == 1)), "protocol port"
    return m


def check_controls(source, work, width, address, heterogeneous=False, mixed=False):
    rows = []
    inspect(source, width, address, heterogeneous, mixed)
    faults = [("missing_rtl", "artifact set"),
              ("bundle_top", "module identity"),
              ("port_width", "address port"),
              ("window", "windows"),
              ("manifest_path", "manifest references")]
    if mixed:
        faults += [("protocol", "endpoint protocols"), ("lite_port", "protocol port")]
    for fault, expected in faults:
        directory = work / f"metadata-{width}-{fault}"
        shutil.copytree(source, directory)
        mapping = directory / "nocloom_axi4_public.map.json"
        bundle = directory / "nocloom_axi4_public.bundle.json"
        if fault == "missing_rtl":
            rtl = directory / "nocloom_axi4_public.v"
            assert not rtl.is_symlink()
            assert rtl.resolve() != work.resolve() and rtl.resolve().is_relative_to(work.resolve())
            rtl.unlink()
        elif fault == "port_width":
            data = json.loads(mapping.read_text())
            next(p for p in data["ports"] if p["name"] == "nl_i_s0_awaddr")["width"] += 1
            mapping.write_text(json.dumps(data))
        elif fault == "protocol":
            for path in (mapping, bundle):
                data = json.loads(path.read_text())
                data["definition"]["endpoint_protocols"]["initiators"]["s1"] = "axi4"
                path.write_text(json.dumps(data))
        elif fault == "lite_port":
            data = json.loads(mapping.read_text())
            data["ports"].append({"name": "nl_i_s1_awlen", "width": 8, "direction": "input"})
            mapping.write_text(json.dumps(data))
        elif fault == "window":
            for path in (mapping, bundle):
                data = json.loads(path.read_text())
                data["definition"]["targets"][1]["begin"] = "65540"
                path.write_text(json.dumps(data))
        else:
            data = json.loads(bundle.read_text())
            if fault == "bundle_top":
                data["top"] = "other"
            else:
                data["files"][0]["path"] = "absent.v"
            bundle.write_text(json.dumps(data))
        try:
            inspect(directory, width, address, heterogeneous, mixed)
        except AssertionError as e:
            assert str(e) == expected, (fault, str(e))
        else:
            raise AssertionError("Metadata mutation escaped: " + fault)
        inspect(source, width, address, heterogeneous, mixed)
        rows.append({"fault": fault, "expected_fail": expected, "status": "CAUGHT",
                         "layer": "ARTIFACT_CHECKER", "baseline": "PASS", "restored": "PASS"})
    (work / f"metadata-controls-{width}.json").write_text(json.dumps(rows, indent=2)+"\n")


def address_faults(baseline, work, width, address, heterogeneous=False):
    rtl = (baseline / "network.v").read_text()
    # The faulted source port is the global width (heterogeneous: s0 at 40 bits).
    high = (40 if heterogeneous else address) - 32
    rows = []
    variants = (("write_high", "nl_i_s1_awaddr", "B_RESULT"),
                ("read_high", "nl_i_s1_araddr", "R_RESULT"),
                ("translation", "nl_t_t1_awaddr", "RAW_WRITE_ADDRESS"))
    if heterogeneous:
        variants = (("write_high", "nl_i_s0_awaddr", "RAW_WRITE_ADDRESS"),
                    ("read_high", "nl_i_s0_araddr", "RAW_READ_ADDRESS"),
                    ("translation", "nl_t_t0_awaddr", "RAW_WRITE_ADDRESS"))
    for name, port, expected in variants:
        case = work / f"address-{width}-{name}"
        case.mkdir()
        shutil.copyfile(baseline / "tb.sv", case / "tb.sv")
        if name == "translation":
            changed, count = re.subn(r"(assign " + port + r" = )[^;]+;",
                                     r"\g<1>" + str(40 if heterogeneous else address) + "'h10000;", rtl)
        else:
            changed, count = re.subn(r"(assign nl_internal_v[0-9]+ = )" + port + r";",
                                    r"\g<1>{" + str(high) + "'b0, " + port + "[31:0]};", rtl)
        assert count == 1, (name, count)
        (case / "network.v").write_text(changed)
        rc = simulate(case, work / "compiled")
        log = (case / "simulation.log").read_text()
        assert rc != 0 and "ASSERT_FAIL:AXI4_MASTER."+expected in log and "AXI4_TARGET_PASS" not in log, log[-3000:]
        rows.append({"name": name, "status": "CAUGHT", "expected_fail": expected,
                     "rtl_sha256": hashlib.sha256(changed.encode()).hexdigest(),
                     "tb_sha256": hashlib.sha256((case/"tb.sv").read_bytes()).hexdigest()})
    restored = work / f"address-{width}-restored"
    restored.mkdir()
    for name in ("network.v", "tb.sv"):
        shutil.copyfile(baseline / name, restored / name)
    rc = simulate(restored, work / "compiled")
    text = (restored / "simulation.log").read_text()
    assert rc == 0 and "AXI4_TARGET_PASS" in text and "ASSERT_FAIL" not in text
    (work / f"address-controls-{width}.json").write_text(json.dumps({"baseline": str(baseline), "rows": rows,
        "restored": "PASS", "restored_rtl_sha256": hashlib.sha256((restored/"network.v").read_bytes()).hexdigest()}, indent=2)+"\n")


def mixed_faults(baseline, work, width):
    rtl = (baseline / "network.v").read_text()
    faults = (("pre_aw", "nl_i_s1_wready", "({expr}) & nl_i_s1_awvalid", "LITE_W_BEFORE_AW"),
              ("length", "nl_t_t1_awlen", "1", "WRITE_DESCRIPTOR"),
              ("strobe", "nl_t_t1_wstrb", "0", "W_STROBES"),
              ("return_data", "nl_i_s1_rdata", "({expr}) ^ 1", "R_DATA"))
    rows = []
    for name, port, expression, expected in faults:
        case = work / f"mixed-{width}-{name}"
        case.mkdir()
        shutil.copyfile(baseline / "tb.sv", case / "tb.sv")
        changed, count = re.subn(r"(assign " + port + r" = )([^;]+);",
            lambda m, expression=expression: m[1]+expression.format(expr=m[2])+";", rtl)
        assert count == 1, (name, count)
        (case / "network.v").write_text(changed)
        rc = simulate(case, work / "compiled")
        log = (case / "simulation.log").read_text()
        assert rc != 0 and "ASSERT_FAIL:AXI4_MASTER."+expected in log and "AXI4_TARGET_PASS" not in log, log[-3000:]
        rows.append({"name": name, "status": "CAUGHT", "expected_fail": expected,
                     "rtl_sha256": hashlib.sha256(changed.encode()).hexdigest(),
                     "tb_sha256": hashlib.sha256((case/"tb.sv").read_bytes()).hexdigest()})
    restored = work / f"mixed-{width}-restored"
    restored.mkdir()
    for name in ("network.v", "tb.sv"):
        shutil.copyfile(baseline / name, restored / name)
    rc = simulate(restored, work / "compiled")
    text = (restored / "simulation.log").read_text()
    assert rc == 0 and "AXI4_TARGET_PASS" in text and "ASSERT_FAIL" not in text
    (work / f"mixed-controls-{width}.json").write_text(json.dumps({"baseline": str(baseline), "rows": rows,
        "restored": "PASS", "restored_rtl_sha256": hashlib.sha256((restored/"network.v").read_bytes()).hexdigest()}, indent=2)+"\n")


def main():
    p = argparse.ArgumentParser()
    for name in ("build", "source", "work", "compiler"):
        p.add_argument("--"+name, required=True)
    p.add_argument("--address48", action="store_true")
    p.add_argument("--heterogeneous", action="store_true")
    p.add_argument("--mixed", action="store_true")
    a = p.parse_args()
    source, build, work = (Path(getattr(a, k)).resolve() for k in ("source", "build", "work"))
    work.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=work))
    records = []

    def run(argv, name, expected=0):
        argv = list(map(str, argv))
        started = time.monotonic()
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False)
        (work / (name+".log")).write_text(result.stdout+result.stderr)
        records.append({"name": name, "argv": argv, "rc": result.returncode,
                            "elapsed_seconds": time.monotonic()-started, "expected_rc": expected})
        (work / "commands.json").write_text(json.dumps(records, indent=2)+"\n")
        assert result.returncode == expected, result.stderr
        return result

    prefix = work / "installed"
    run(["cmake", "--install", build, "--prefix", prefix], "install")
    consumer = work / "consumer"
    consumer.mkdir(exist_ok=True)
    shutil.copyfile(source / "tests/axi4_public_consumer.cpp", consumer / "main.cpp")
    shutil.copyfile(source / "tests/endpoint_ports.hpp", consumer / "endpoint_ports.hpp")
    (consumer / "CMakeLists.txt").write_text('''cmake_minimum_required(VERSION 3.24)
project(axi4_consumer LANGUAGES CXX)
find_package(nocloom CONFIG REQUIRED COMPONENTS Core)
add_executable(consumer main.cpp)
target_link_libraries(consumer PRIVATE nocloom::core)
''')
    run(["cmake", "-S", consumer, "-B", consumer/"build", "-G", "Unix Makefiles",
         "-DCMAKE_CXX_COMPILER="+a.compiler, "-DCMAKE_PREFIX_PATH="+str(prefix),
         *["-DCMAKE_DISABLE_FIND_PACKAGE_"+dep+"=ON" for dep in ("yaml-cpp", "nlohmann_json", "CLI11", "Catch2")]], "consumer-configure")
    run(["cmake", "--build", consumer/"build", "-j2"], "consumer-build")
    results = []
    wide = 48 if a.address48 else 40
    for width, address in (((32, wide), (64, wide)) if a.address48 or a.heterogeneous
                           else ((32, 32), (64, 17))):
        config = work / f"axi4-{width}.yaml"
        config.write_text(configuration(width, address, a.heterogeneous, a.mixed))
        dirs = []
        for origin in ("cli", "api", "repeat"):
            directory = work / f"{origin}-{width}"
            if origin == "api":
                run([consumer/"build/consumer", directory, width, address, *(["heterogeneous"] if a.heterogeneous else ["mixed"] if a.mixed else [])], f"api-{width}")
            else:
                run([prefix/"bin/nocloom", "generate", config, "--out-dir", directory], f"{origin}-{width}")
            inspect(directory, width, address, a.heterogeneous, a.mixed)
            dirs.append(directory)
        hashes = [{p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in d.iterdir()} for d in dirs]
        assert hashes[0] == hashes[1] == hashes[2], "CLI/API/repeat byte difference"
        check_controls(dirs[2], work, width, address, a.heterogeneous, a.mixed)
        for directory in dirs[:2]:
            mapping = inspect(directory, width, address, a.heterogeneous, a.mixed)
            meta = {"module": mapping["module"], "data_bits": width, "address_bits": address,
                        "id_bits": 4 if width == 32 else 0, "len_bits": 8 if width == 32 else 4,
                        "sources": ["s0", "s1"], "heterogeneous": a.heterogeneous, "mixed": a.mixed,
                        "target_ids": mapping["definition"].get("target_ids"),
                        "targets": [{"name": t["name"], "base": int(t["begin"]), "size": int(t["end"])-int(t["begin"])}
                                 for t in mapping["definition"]["targets"]],
                        "ports": [dict(p, input=p["direction"] == "input") for p in mapping["ports"]]}
            (directory / "bindings.json").write_text(json.dumps(meta, indent=2)+"\n")
            (directory / "network.v").write_text(generated_rtl(directory / (mapping["module"]+".v")))
            write_bench(directory, decerr=3, run_word=32 if width == 32 else 4)  # distinct response classes
            rc = simulate(directory, work / "compiled")
            log = (directory / "simulation.log").read_text()
            assert rc == 0 and "AXI4_TARGET_PASS" in log and "ASSERT_FAIL" not in log, log[-3000:]
            results.append({"case": directory.name, "status": "PASS", "artifacts": hashes[0],
                                "tb_sha256": hashlib.sha256((directory/"tb.sv").read_bytes()).hexdigest()})
            (work/"results.json").write_text(json.dumps(results, indent=2)+"\n")
        if a.mixed:
            mixed_faults(dirs[0], work, width)
        if a.address48 or a.heterogeneous:
            address_faults(dirs[0], work, width, address, a.heterogeneous)
        rejected = work / f"reject-{width}.yaml"
        rejected.write_text(configuration(width, address, a.heterogeneous, a.mixed).replace("max_outstanding: 1", "max_outstanding: 65"))
        dest = work / f"rejected-{width}"
        r = run([prefix/"bin/nocloom", "generate", rejected, "--out-dir", dest], f"reject-{width}", expected=1)
        assert "NCL_AXI4_CAPACITY" in r.stderr and not dest.exists()
        if a.mixed:
            rejected.write_text(configuration(width, address, a.heterogeneous, True).replace("protocol: axi4_lite", "protocol: apb2"))
            r = run([prefix/"bin/nocloom", "generate", rejected, "--out-dir", dest], f"reject-protocol-{width}", expected=1)
            assert "NCL_UNSUPPORTED_PROTOCOL" in r.stderr and not dest.exists()
    print("AXI4_TARGET_PASS installed CLI+Core generation and target transactions")


if __name__ == "__main__":
    main()
