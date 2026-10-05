"""Generates tb.sv: clocks, DUT instances, BFMs, stream monitors, the tape
interpreter and the event/verdict plumbing. The output is one self-contained
SystemVerilog file (BFM sources inlined) so every simulation engine compiles it
with the DUT file alone."""

from pathlib import Path

SV = Path(__file__).resolve().parent.parent / "sv"
from .tape import RECORDS

TAPE_WORDS = 16 * (RECORDS + 1)

# BFM port: (name, direction seen from the BFM, width expression or fixed width)
AXI_MASTER = [
    ("awid", "o", "ID_W"), ("awaddr", "o", "ADDR_W"), ("awlen", "o", "LEN_W"), ("awsize", "o", 3),
    ("awburst", "o", 2), ("awlock", "o", "LOCK_W"), ("awcache", "o", 4), ("awprot", "o", 3),
    ("awqos", "o", "QOS_W"), ("awregion", "o", 4), ("awvalid", "o", 1), ("awready", "i", 1),
    ("wid", "o", "ID_W"), ("wdata", "o", "DATA_W"), ("wstrb", "o", "STRB_W"), ("wlast", "o", 1), ("wvalid", "o", 1),
    ("wready", "i", 1), ("bid", "i", "ID_W"), ("bresp", "i", 2), ("buser", "i", "USER_W"), ("bvalid", "i", 1),
    ("bready", "o", 1), ("arid", "o", "ID_W"), ("araddr", "o", "ADDR_W"), ("arlen", "o", "LEN_W"),
    ("arsize", "o", 3), ("arburst", "o", 2), ("arlock", "o", "LOCK_W"), ("arcache", "o", 4),
    ("arprot", "o", 3), ("arqos", "o", "QOS_W"), ("arregion", "o", 4), ("arvalid", "o", 1), ("arready", "i", 1),
    ("rid", "i", "ID_W"), ("rdata", "i", "DATA_W"), ("rresp", "i", 2), ("ruser", "i", "USER_W"), ("rlast", "i", 1),
    ("rvalid", "i", 1), ("rready", "o", 1),
]
AXI_SLAVE = [(n, "i" if d == "o" else "o", w) for n, d, w in AXI_MASTER]
APB_SLAVE = [("psel", "i", 1), ("penable", "i", 1), ("pwrite", "i", 1), ("paddr", "i", "ADDR_W"),
             ("pwdata", "i", "DATA_W"), ("pstrb", "i", "STRB_W"), ("pprot", "i", 3),
             ("prdata", "o", "DATA_W"), ("pready", "o", 1), ("pslverr", "o", 1)]
AHB_SLAVE = [("hsel", "i", 1), ("haddr", "i", "ADDR_W"), ("htrans", "i", 2), ("hwrite", "i", 1),
             ("hsize", "i", 3), ("hburst", "i", 3), ("hprot", "i", 4), ("hmastlock", "i", 1),
             ("hwdata", "i", "DATA_W"), ("hready", "i", 1), ("hreadyout", "o", 1),
             ("hresp", "o", "HRESP_W"), ("hrdata", "o", "DATA_W")]
APB_MASTER = [(n, "o" if d == "i" else "i", w) for n, d, w in APB_SLAVE]
KINDS = {"axi_master": ("nlv_axi_master", AXI_MASTER), "axi_slave": ("nlv_axi_slave", AXI_SLAVE),
         "apb_master": ("nlv_apb_master", APB_MASTER),
         "apb_slave": ("nlv_apb_slave", APB_SLAVE), "ahb_slave": ("nlv_ahb_slave", AHB_SLAVE)}
SOURCES = ["nlv_axi_master.sv", "nlv_axi_slave.sv", "nlv_apb_master.sv", "nlv_apb_slave.sv",
           "nlv_ahb_slave.sv",
           "nlv_stream_mon.sv"]


class BenchError(ValueError):
    pass


def normalize_components(components):
    """Accepts fixture ports.json components or a CLI map.json (one module)."""
    if isinstance(components, dict) and "ports" in components and "module" in components:
        ports = [dict(name=p["name"], width=p["width"], input=p["direction"] == "input",
                      endpoint=p["endpoint"], signal=p["signal"]) for p in components["ports"]]
        # The whole map stays with the component: text faults may anchor on
        # the internal nets it names.
        return [dict(label="dut", module=components["module"], ports=ports, map=components)]
    return components


def inline_sources():
    common = (SV / "nlv_common.svh").read_text()
    text = []
    for name in SOURCES:
        body = (SV / name).read_text().replace('`include "nlv_common.svh"', common)
        body = body.replace("`timescale 1ns/1ps\n", "")
        text.append(f"// ---- {name} ----\n{body}")
    return "\n".join(text)


def net_name(case, label, port):
    if port["signal"] == "clock":
        domain = port["endpoint"].split(":", 1)[-1] if port["endpoint"].startswith("domain:") else "main"
        return "clk_" + domain
    if port["signal"] == "reset_n":
        # A reset port with its own release (case "resets") gets its own net.
        return "rstp_" + port["name"] if port["name"] in case.get("resets", {}) else "rst_n"
    if port["signal"] == "test":
        # Every test port: low, or high for the whole run with +NLV_TEST.
        return "nlv_test"
    if port["signal"] == "ungate":
        # Every ungate port: low, or high for the whole run with +NLV_UG.
        return "nlv_ungate"
    if port["signal"] == "enable":
        # An enable port: the bench pulse generator of case "enables".
        return "nlv_en_" + port["name"]
    if port["signal"] == "address_input":
        # An address input: zero after time zero, then the value the tape sets (Tape.mode).
        return "nlv_mode_" + port["name"]
    for old, new in case.get("nets", {}).get(label, []):
        if port["name"].startswith(old):
            return new + port["name"][len(old):]
    return port["name"]


def reset_ports(case, nets):
    """Drivers of the reset ports listed in case "resets": each asserts with the
    common reset rst_n, or alone while the tape holds its nlv_alone flag, and is released
    "release" falling edges of its domain clock after that, the list entry chosen by the
    count of earlier releases."""
    text, alone = [], []
    for port, (name, spec) in enumerate(sorted(case.get("resets", {}).items())):
        net, clock, delays = "rstp_" + name, "clk_" + spec["clock"], spec["release"]
        if net not in nets:
            raise BenchError(f"reset port {name} is not a DUT reset input")
        delay = str(delays[-1])
        for k in range(len(delays) - 2, -1, -1):
            delay = f"(nlv_gen_{net} % {len(delays)} == {k}) ? {delays[k]} : {delay}"
        alone.append(f"      {port}: nlv_alone_{net} = v;")
        text.append(
            f"integer nlv_gen_{net} = 0, nlv_cnt_{net} = 0; reg nlv_alone_{net} = 1'b0;\n"
            f"always @(negedge {clock} or negedge rst_n or posedge nlv_alone_{net})\n"
            f"  if (!rst_n || nlv_alone_{net}) begin {net} <= 1'b0; nlv_cnt_{net} <= 0; end\n"
            f"  else if (!{net}) begin\n"
            f"    if (nlv_cnt_{net} >= ({delay})) begin {net} <= 1'b1; "
            f"nlv_gen_{net} <= nlv_gen_{net} + 1; end\n"
            f"    else nlv_cnt_{net} <= nlv_cnt_{net} + 1;\n"
            f"  end")
    text.append("task nlv_alone(input integer k, input reg v);\n  begin\n    case (k)\n"
                + "".join(a + "\n" for a in alone)
                + "      default: nlv_fail_report(\"TB.BAD_PORT\", \"tape\", k);\n    endcase\n  end\nendtask")
    return text


def address_inputs(case, nets):
    """Drivers of the address inputs listed in case "modes" (in tape index order): zero at time
    zero, then each value the tape sets; every address input of the DUT must be listed."""
    names = list(case.get("modes", ()))
    for n in nets:
        if n.startswith("nlv_mode_") and n[len("nlv_mode_"):] not in names:
            raise BenchError(f"address input {n[len('nlv_mode_'):]} is not in case modes")
    sets = []
    for k, name in enumerate(names):
        if "nlv_mode_" + name not in nets:
            raise BenchError(f"address input {name} is not a DUT input")
        sets.append(f"      {k}: nlv_mode_{name} = v;")
    init = "".join(f"initial nlv_mode_{name} = 0;\n" for name in names)
    return [init + "task nlv_mode(input integer k, input [63:0] v);\n  begin\n    case (k)\n"
            + "".join(s + "\n" for s in sets)
            + "      default: nlv_fail_report(\"TB.BAD_PORT\", \"tape\", k);\n    endcase\n"
            "  end\nendtask"]


def clock_period(periods, domain):
    spec = periods.get(domain, 10.0)
    return spec["period"] if isinstance(spec, dict) else spec


def probe_monitors(case, probes, main_clock):
    """White-box probe groups (fixture probes.json of the build): one event
    per group per clock while out of reset, the group's nets concatenated
    in table order. An entry with "variants" (a text-fault build) reads
    each net from the copy +NLV_FAULT selects, the core otherwise. Returns
    (sv text, meta)."""
    if not probes:
        return "", []
    spec = case.get("probes", {})
    text, meta = [], []
    for label, entry in sorted(probes.items()):
        top = "u_" + spec.get("instance", {}).get(label, label)
        instance = top + (".core" if entry.get("wrapped") else "")

        def net(wire, top=top, instance=instance, variants=entry.get("variants", ())):
            expr = f"{instance}.{wire}"
            for k in variants:
                expr = f"({top}.nlv_sel == 8'd{k} ? {top}.variant{k}.{wire} : {expr})"
            return expr

        for group in entry["groups"]:
            port = "q%d" % len(meta)
            nets = ", ".join(net(wire) for _, wire, _ in group["nets"])
            text.append(
                f"always @(posedge {main_clock}) if (rst_n === 1'b1)\n"
                f"  $fwrite(nlv_fd, \"{{\\\"t\\\":%0.0f,\\\"c\\\":%0d,\\\"k\\\":\\\"pr\\\","
                f"\\\"p\\\":\\\"{port}\\\",\\\"x\\\":\\\"%0h\\\"}}\\n\", "
                f"($realtime * 1000.0), nlv_cycle, {{{nets}}});")
            info = {k: v for k, v in group.items() if k != "nets"}
            info.update(port=port, fields=[(f, w) for f, _, w in group["nets"]])
            meta.append(info)
    return "\n".join(text), meta


def idle_function(masters):
    """Sample every initiator's queues when the interpreter calls the function.

    Separate one-bit folds bound the expression size for simulation code generation.
    Bitwise AND preserves an unknown comparison until a known busy queue clears it.
    """
    checks = "\n".join(
        f"    nlv_idle = nlv_idle & ({b['instance']}.counter({idx}) == 0);"
        for b in masters for idx in (10, 11))
    return ("function nlv_idle;\n  input integer unused;\n  begin\n"
            "    nlv_idle = 1'b1;\n" + checks + "\n  end\nendfunction")


def generate(case, components, faulted=False, probes=None):
    """Returns (tb_text, bench_meta). probes: the fixture's probes.json for
    this build (baseline or faulted copy), or None."""
    components = normalize_components(components)
    by_label = {c["label"]: c for c in components}
    nets, drivers, loads = {}, {}, {}
    instances = []
    for c in components:
        conns = []
        for p in c["ports"]:
            n = net_name(case, c["label"], p)
            if n in nets and nets[n] != p["width"]:
                raise BenchError(f"net {n} width {nets[n]} vs {p['width']}")
            nets[n] = p["width"]
            (loads if p["input"] else drivers).setdefault(n, []).append((c["label"], p["name"]))
            conns.append(f".{p['name']}({n})")
        instances.append(f"{c['module']} u_{c['label']} (\n    " + ",\n    ".join(conns) + ");")
    clocks = sorted({n for n in nets if n.startswith("clk_")})
    if not clocks:
        raise BenchError("no clock port")
    periods = case.get("clocks", {})
    bfm_text, bfms = [], {}
    for index, spec in enumerate(case["bfms"]):
        kind = spec["kind"]
        module, table = KINDS[kind]
        comp = by_label[spec.get("component", components[0]["label"])]
        endpoint = spec["endpoint"]
        ports = {p["signal"]: p for p in comp["ports"] if p["endpoint"] == endpoint}
        if "interface" in spec:
            # One port set of a target of several flows: the signals <name>_<interface>.
            suffix = f"_{spec['interface']}"
            ports = {s[:-len(suffix)]: p for s, p in ports.items() if s.endswith(suffix)}
        if not ports:
            raise BenchError(f"{spec['name']}: no ports on endpoint {endpoint}")
        def width_of(*names, default):
            for n in names:
                if n in ports:
                    return ports[n]["width"]
            return default
        params = dict(ID_W=width_of("awid", "arid", "bid", "rid", default=1),
                      ADDR_W=width_of("awaddr", "araddr", "paddr", "haddr", default=32),
                      DATA_W=width_of("wdata", "rdata", "pwdata", "prdata", "hwdata", "hrdata",
                                      default=32),
                      LEN_W=width_of("awlen", "arlen", default=8),
                      LOCK_W=width_of("awlock", "arlock", default=1),
                      QOS_W=width_of("awqos", "arqos", default=4),
                      USER_W=width_of("buser", "ruser", default=1),
                      HRESP_W=width_of("hresp", default=1))
        params["STRB_W"] = params["DATA_W"] // 8
        # A BFM naming a reset port of case "resets" also resets while the tape holds that port
        # alone (its domain's reset), not through the port's delayed release.
        alone = spec.get("reset")
        if alone is not None and alone not in case.get("resets", {}):
            raise BenchError(f"{spec['name']}: reset {alone} is not a case reset port")
        conns = [".clk(" + "clk_" + spec.get("clock", "main") + ")",
                 f".rst_n(rst_n & ~nlv_alone_rstp_{alone})" if alone else ".rst_n(rst_n)"]
        used = set()
        for name, direction, width in table:
            w = params[width] if isinstance(width, str) else width
            if name in ports:
                p = ports[name]
                if p["width"] != w:
                    raise BenchError(f"{spec['name']}.{name}: DUT width {p['width']} vs BFM {w}")
                dut_dir = "i" if p["input"] else "o"
                if dut_dir == direction:
                    raise BenchError(f"{spec['name']}.{name}: direction clash")
                conns.append(f".{name}({net_name(case, comp['label'], p)})")
                used.add(name)
            elif direction == "i":
                tie = spec.get("ties", {}).get(name, 0)
                conns.append(f".{name}({w}'d{tie})")
            else:
                conns.append(f".{name}()")
        sideband = spec.get("sideband")
        if sideband and sideband[0] == comp["label"] and sideband[1] == endpoint:
            used.add(sideband[2])  # a status output of this endpoint, watched as sideband
        extra = set(ports) - used
        if extra:
            raise BenchError(f"{spec['name']}: DUT signals without BFM pins {sorted(extra)}")
        if kind == "axi_slave" or kind in ("apb_slave", "ahb_slave"):
            sb = spec.get("sideband")
            if sb:
                sc, se, ss = sb
                p = next(p for p in by_label[sc]["ports"] if p["endpoint"] == se and p["signal"] == ss)
                conns.append(f".sideband({net_name(case, sc, p)})")
            else:
                conns.append(".sideband(1'b0)")
        overrides = [f".NAME(\"{spec['name']}\")"]
        if kind in ("axi_master", "axi_slave"):
            overrides += [f".ID_W({params['ID_W']})", f".ADDR_W({params['ADDR_W']})",
                          f".DATA_W({params['DATA_W']})", f".LEN_W({params['LEN_W']})",
                          f".USER_W({params['USER_W']})", f".LOCK_W({params['LOCK_W']})",
                          f".USER_PRESENT({int(bool({'ruser','buser'} & set(ports)))})",
                          f".LITE({int(spec.get('lite', False))})"]
        else:
            overrides += [f".ADDR_W({params['ADDR_W']})", f".DATA_W({params['DATA_W']})"]
        if kind == "axi_master":
            overrides.append(f".QOS_W({params['QOS_W']})")
        if kind == "ahb_slave":
            overrides.append(f".HRESP_W({params['HRESP_W']})")
        if kind in ("apb_slave", "apb_master"):
            overrides.append(f".DIALECT({spec.get('dialect', 4)})")
        if kind not in ("axi_master", "apb_master"):
            overrides += [f".MEM_BYTES({spec.get('mem_bytes', 65536)})", f".SALT({spec.get('salt', index)})"]
        bfm_text.append(f"{module} #({', '.join(overrides)}) bfm_{spec['name']} (\n    "
                        + ",\n    ".join(conns) + ");")
        size = (params["DATA_W"] // 8).bit_length() - 1
        clock = spec.get("clock", "main")
        bfms[spec["name"]] = dict(index=index, kind=kind, instance="bfm_" + spec["name"],
                                  size=size, data_bits=params["DATA_W"],
                                  len_bits=params["LEN_W"],
                                  response_user_bits=params["USER_W"] if "ruser" in ports else 0,
                                  # An endpoint without ID ports (AXI4-Lite) carries none.
                                  id_bits=params["ID_W"] if {"awid", "arid", "bid", "rid"} & set(ports)
                                  else 0,
                                  lite=bool(spec.get("lite", False)) or kind == "apb_master",
                                  period_ps=round(clock_period(periods, clock) * 1000))
        for name in used:
            n = net_name(case, comp["label"], ports[name])
            if not ports[name]["input"]:
                loads.setdefault(n, []).append((spec["name"], name))
            else:
                drivers.setdefault(n, []).append((spec["name"], name))
    # Stream monitors on internal channels (white-box component boundaries).
    streams_meta = []
    stream_text = []
    for k, s in enumerate(case.get("streams", [])):
        comp = by_label[s["component"]]
        ports = [p for p in comp["ports"] if p["endpoint"] == s["endpoint"]]
        valid = next(p for p in ports if p["signal"] == "valid")
        ready = next((p for p in ports if p["signal"] == "ready"), None)
        if ready is None:
            # The ready of an input channel is driven by the peer component.
            rname = s["ready_net"]
        else:
            rname = net_name(case, comp["label"], ready)
        fields = [p for p in ports if p["signal"] not in ("valid", "ready")]
        payload = "{" + ", ".join(net_name(case, comp["label"], p) for p in fields) + "}"
        width = sum(p["width"] for p in fields)
        index = len(case["bfms"]) + k
        stream_text.append(
            f"nlv_stream_mon #(.NAME(\"{s['name']}\"), .W({width})) bfm_{s['name']} (.clk(clk_main), "
            f".rst_n(rst_n), .valid({net_name(case, comp['label'], valid)}), .ready({rname}), "
            f".payload({payload}));")
        streams_meta.append(dict(name=s["name"], fields=[(p["signal"], p["width"]) for p in fields]))
        bfms[s["name"]] = dict(index=index, kind="stream", instance="bfm_" + s["name"])
    bench_driven = ("rst_n", "nlv_test", "nlv_ungate")
    for n in nets:
        if n.startswith(("clk_", "rstp_", "nlv_en_", "nlv_mode_")) or n in bench_driven:
            continue
        d = drivers.get(n, [])
        if len(d) != 1:
            raise BenchError(f"net {n} has drivers {d}")
    nets.setdefault("rst_n", 1)  # the common reset, also when every DUT reset port has its own
    decl = []
    for n, w in sorted(nets.items()):
        kind = ("reg" if n.startswith(("clk_", "rstp_", "nlv_en_", "nlv_mode_"))
                or n in bench_driven else "wire")
        decl.append(f"{kind} {'' if w == 1 else f'[{w-1}:0] '}{n};")
    clock_text = []
    for c in clocks:
        # A domain clock: a period in ns, or {period, phase (first edge
        # delay), start (held low until then)}.
        spec = periods.get(c[4:], 10.0)
        spec = spec if isinstance(spec, dict) else dict(period=spec)
        half = spec["period"] / 2
        delay = spec.get("start", 0) + spec.get("phase", 0)
        if delay:
            clock_text.append(f"initial begin {c} = 1'b0; #({delay}); forever #({half}) {c} = ~{c}; end")
        else:
            clock_text.append(f"initial {c} = 1'b0;\nalways #({half}) {c} = ~{c};")
    clock_text += reset_ports(case, nets)
    clock_text += address_inputs(case, nets)
    if "nlv_test" in nets:
        clock_text.append('initial nlv_test = $test$plusargs("NLV_TEST") ? 1\'b1 : 1\'b0;')
    if "nlv_ungate" in nets:
        clock_text.append('initial nlv_ungate = $test$plusargs("NLV_UG") ? 1\'b1 : 1\'b0;')
    # An enable port for edge_qualified domains: one pulse of its clock per ratio cycles.
    # Change it on the falling edge seen by the gate latch.
    for name, spec in sorted(case.get("enables", {}).items()):
        net = "nlv_en_" + name
        if net not in nets:
            raise BenchError(f"enable port {name} is not a DUT enable input")
        clock, ratio = "clk_" + spec["clock"], int(spec["ratio"])
        clock_text.append(
            f"integer nlv_cnt_{net} = 0;\n"
            f"always @(negedge {clock} or negedge rst_n)\n"
            f"  if (!rst_n) begin nlv_cnt_{net} <= 0; {net} <= 1'b0; end\n"
            f"  else begin nlv_cnt_{net} <= nlv_cnt_{net} == {ratio - 1} ? 0 : nlv_cnt_{net} + 1; "
            f"{net} <= nlv_cnt_{net} == {ratio - 1}; end")
    masters = [b for b in bfms.values() if b["kind"] in ("axi_master", "apb_master")]
    probe_text, probe_meta = probe_monitors(case, probes, "clk_" + case.get("main_clock", "main"))
    tb = TEMPLATE
    names = sorted(bfms.values(), key=lambda b: b["index"])
    def cases(fmt, kinds):
        return "\n".join(f"      {b['index']}: {fmt.format(i=b['instance'])}" for b in names if b["kind"] in kinds)
    replacements = {
        "@DECL@": "\n".join(decl),
        "@CLOCKS@": "\n".join(clock_text),
        "@INSTANCES@": "\n".join(instances),
        "@BFMS@": "\n".join(bfm_text + stream_text + [probe_text]),
        "@TAPE_WORDS@": str(TAPE_WORDS),
        "@MAIN_CLOCK@": "clk_" + case.get("main_clock", "main"),
        "@PUSH_WRITE@": cases("{i}.push_write(a0, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11);",
                              ("axi_master", "apb_master")),
        "@PUSH_READ@": cases("{i}.push_read(a0, a1, a2, a3, a4, a5, a6, a7, a8, a9);",
                             ("axi_master", "apb_master")),
        "@SET@": cases("{i}.set_ctl(r, v);", ("axi_master", "axi_slave", "apb_slave", "ahb_slave",
                                               "apb_master")),
        "@COUNTER@": cases("nlv_counter = {i}.counter(idx);", tuple(KINDS) + ("stream",)),
        "@STALL@": "\n".join(f"    {b['instance']}.stall_report(NLV_STALL_MIN);" for b in names
                             if b["kind"] in ("axi_master", "axi_slave")),
        "@IDLE@": idle_function(masters),
    }
    for k, v in replacements.items():
        tb = tb.replace(k, v)
    tb = "`timescale 1ns/1ps\n" + inline_sources().replace(
        '`include "nlv_common.svh"', "") + "\n" + tb.replace(
        "@COMMON@", (SV / "nlv_common.svh").read_text())
    meta = dict(bfms=bfms, streams=streams_meta, nets=nets, clocks=clocks, probes=probe_meta)
    # The numbers of the CLI map behind every target ID (docs/transactions.md#target-identifiers).
    for component in normalize_components(components):
        numbers = component.get("map", {}).get("definition", {}).get("target_ids")
        if numbers:
            meta["target_ids"] = numbers
    return tb, meta


TEMPLATE = r"""
module tb;
@DECL@
integer nlv_fd, nlv_fails, nlv_running, nlv_pc, nlv_wait, nlv_budget, nlv_stall, nlv_base;
integer nlv_settle;
reg [63:0] nlv_cycle, nlv_max;
// A timed-out wait first lets every AXI BFM print its channel state and name
// a broken liveness premise (LIVE.E1..E3: a VALID held at least this many
// cycles); the hang itself is recorded as hang_rule and hang_detail.
localparam NLV_STALL_MIN = 32;
reg [8*48-1:0] nlv_hang_rule;
reg [63:0] nlv_hang_detail;
reg [8*48-1:0] nlv_first_rule;
reg [8*24-1:0] nlv_first_port;
reg [63:0] nlv_first_detail;
reg [8*1024-1:0] nlv_events_path, nlv_verdict_path, nlv_tape_path;
reg [63:0] nlv_tape [0:@TAPE_WORDS@-1];
reg [63:0] nlv_snap [0:63];
reg [63:0] op, b, a0, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11, v;
@COMMON@

task nlv_fail_report;
  input [8*48-1:0] rule;
  input [8*24-1:0] port;
  input [63:0] detail;
  begin
    if (nlv_fails == 0) begin
      nlv_first_rule = rule;
      nlv_first_port = port;
      nlv_first_detail = detail;
    end
    nlv_fails = nlv_fails + 1;
    $fwrite(nlv_fd, "{\"t\":%0.0f,\"k\":\"fail\",\"rule\":\"%0s\",\"p\":\"%0s\",\"x\":%0d}\n",
            ($realtime * 1000.0), rule, port, detail);
    $display("NLV_FAIL %0s port=%0s detail=%0d", rule, port, detail);
  end
endtask

@CLOCKS@

@INSTANCES@

@BFMS@

task nlv_push_write;
  input integer bi;
  begin
    case (bi)
@PUSH_WRITE@
      default: nlv_fail_report("TB.BAD_BFM", "tape", bi);
    endcase
  end
endtask

task nlv_push_read;
  input integer bi;
  begin
    case (bi)
@PUSH_READ@
      default: nlv_fail_report("TB.BAD_BFM", "tape", bi);
    endcase
  end
endtask

task nlv_set;
  input integer bi;
  input integer r;
  input [31:0] v;
  begin
    case (bi)
@SET@
      default: nlv_fail_report("TB.BAD_BFM", "tape", bi);
    endcase
  end
endtask

function [63:0] nlv_counter;
  input integer bi;
  input integer idx;
  begin
    nlv_counter = 0;
    case (bi)
@COUNTER@
      default: nlv_counter = 0;
    endcase
  end
endfunction

task nlv_stall_report;
  input integer unused;
  begin
@STALL@
  end
endtask

@IDLE@

task nlv_finish;
  input [8*16-1:0] status;
  integer vf;
  begin
    vf = $fopen(nlv_verdict_path, "w");
    $fwrite(vf, "{\"status\":\"%0s\",\"fails\":%0d,\"first_rule\":\"%0s\",\"first_port\":\"%0s\",\"first_detail\":%0d,\"hang_rule\":\"%0s\",\"hang_detail\":%0d,\"pc\":%0d,\"cycles\":%0d}\n",
            status, nlv_fails, nlv_first_rule, nlv_first_port, nlv_first_detail, nlv_hang_rule,
            nlv_hang_detail, nlv_pc, nlv_cycle);
    $fclose(vf);
    $fwrite(nlv_fd, "{\"t\":%0.0f,\"k\":\"end\",\"status\":\"%0s\",\"pc\":%0d}\n", ($realtime * 1000.0), status, nlv_pc);
    $fclose(nlv_fd);
    $display("NLV_DONE status=%0s fails=%0d pc=%0d cycles=%0d", status, nlv_fails, nlv_pc, nlv_cycle);
    nlv_running = 0;
    $finish;
  end
endtask

initial begin
  rst_n = 1'b0;
  nlv_fails = 0; nlv_running = 0; nlv_pc = 1; nlv_wait = 0; nlv_cycle = 0;
  nlv_first_rule = 0; nlv_first_port = 0; nlv_first_detail = 0;
  nlv_hang_rule = 0; nlv_hang_detail = 0; nlv_settle = 0;
  if (!$value$plusargs("NLV_EVENTS=%s", nlv_events_path)) nlv_events_path = "events.jsonl";
  if (!$value$plusargs("NLV_VERDICT=%s", nlv_verdict_path)) nlv_verdict_path = "verdict.json";
  if (!$value$plusargs("NLV_TAPE=%s", nlv_tape_path)) nlv_tape_path = "tape.hex";
  nlv_fd = $fopen(nlv_events_path, "w");
  $readmemh(nlv_tape_path, nlv_tape);
  if (nlv_tape[0] !== 64'h4E4C5654) begin
    $display("NLV_FAIL TB.TAPE bad header");
    nlv_fails = 1;
    nlv_first_rule = "TB.TAPE";
  end
  nlv_max = nlv_tape[2];
  if ($value$plusargs("NLV_MAX_CYCLES=%d", v)) nlv_max = v;
  nlv_running = 1;
end

// The interpreter runs on the falling edge; BFMs and the DUT on the rising
// edge. The common reset uses a nonblocking assignment so every port sees it
// at the same edge. Every BFM logs the reset edges it sees; the offline check
// requires each at the first rising edge of its clock after the tb edge and
// reports a miss as an infrastructure error, not a product failure.
always @(negedge @MAIN_CLOCK@) begin
  nlv_cycle = nlv_cycle + 1;
  if (nlv_running) begin
    if (nlv_fails > 0)
      nlv_finish("FAIL");
    else if (nlv_cycle > nlv_max)
      nlv_finish("TIMEOUT");
    else begin
      nlv_stall = 0;
      nlv_budget = 0;
      while (!nlv_stall && nlv_running && nlv_fails == 0 && nlv_budget < 512) begin
        nlv_budget = nlv_budget + 1;
        nlv_base = nlv_pc * 16;
        op = nlv_tape[nlv_base]; b = nlv_tape[nlv_base+1];
        a0 = nlv_tape[nlv_base+2]; a1 = nlv_tape[nlv_base+3]; a2 = nlv_tape[nlv_base+4];
        a3 = nlv_tape[nlv_base+5]; a4 = nlv_tape[nlv_base+6]; a5 = nlv_tape[nlv_base+7];
        a6 = nlv_tape[nlv_base+8]; a7 = nlv_tape[nlv_base+9]; a8 = nlv_tape[nlv_base+10];
        a9 = nlv_tape[nlv_base+11]; a10 = nlv_tape[nlv_base+12]; a11 = nlv_tape[nlv_base+13];
        case (op)
          0: begin nlv_finish("DONE"); nlv_stall = 1; end
          1: begin nlv_push_write(b); nlv_pc = nlv_pc + 1; end
          2: begin nlv_push_read(b); nlv_pc = nlv_pc + 1; end
          3: begin
            v = nlv_counter(b, a0) - (a5 != 0 ? nlv_snap[a5] : 64'd0);
            if (nlv_compare(v, a1, a2)) begin
              nlv_pc = nlv_pc + 1;
              nlv_wait = 0;
            end else begin
              nlv_wait = nlv_wait + 1;
              if (nlv_wait > a3) begin
                if (nlv_fails == 0) begin
                  nlv_stall_report(0);
                  nlv_hang_rule = "TAPE.WAIT_TIMEOUT";
                  nlv_hang_detail = a4;
                end
                nlv_fail_report("TAPE.WAIT_TIMEOUT", "tape", a4);
              end
              nlv_stall = 1;
            end
          end
          4: begin
            if (nlv_wait >= a0) begin
              nlv_pc = nlv_pc + 1;
              nlv_wait = 0;
            end else begin
              nlv_wait = nlv_wait + 1;
              nlv_stall = 1;
            end
          end
          5: begin
            if (nlv_wait == 0) begin
              rst_n <= 1'b0;
              $fwrite(nlv_fd, "{\"t\":%0.0f,\"k\":\"rst\",\"v\":0}\n", ($realtime * 1000.0));
              nlv_wait = 1;
              nlv_stall = 1;
            end else if (nlv_wait < a0) begin
              nlv_wait = nlv_wait + 1;
              nlv_stall = 1;
            end else begin
              rst_n <= 1'b1;
              $fwrite(nlv_fd, "{\"t\":%0.0f,\"k\":\"rst\",\"v\":1}\n", ($realtime * 1000.0));
              nlv_wait = 0;
              nlv_pc = nlv_pc + 1;
              nlv_stall = 1;
            end
          end
          6: begin nlv_set(b, a0, a1); nlv_pc = nlv_pc + 1; end
          7: begin
            v = nlv_counter(b, a0) - (a4 != 0 ? nlv_snap[a4] : 64'd0);
            if (!nlv_compare(v, a1, a2)) nlv_fail_report("TAPE.EXPECT", "tape", a3);
            nlv_pc = nlv_pc + 1;
          end
          8: begin
            $fwrite(nlv_fd, "{\"t\":%0.0f,\"c\":%0d,\"k\":\"mk\",\"m\":%0d,\"v\":%0d}\n",
                    ($realtime * 1000.0), nlv_cycle, a0, a1);
            nlv_pc = nlv_pc + 1;
          end
          9: begin
            if (nlv_idle(0)) begin
              nlv_pc = nlv_pc + 1;
              nlv_wait = 0;
            end else begin
              nlv_wait = nlv_wait + 1;
              if (nlv_wait > a0) nlv_fail_report("TAPE.DRAIN_TIMEOUT", "tape", a1);
              nlv_stall = 1;
            end
          end
          10: begin nlv_snap[a1] = nlv_counter(b, a0); nlv_pc = nlv_pc + 1; end
          // SETTLE: continue once every initiator is idle; if that takes
          // more than a0 cycles, apply the common reset for a1 cycles
          // instead (a reset at a random point of the traffic).
          11: begin
            if (nlv_settle == 0) begin
              if (nlv_idle(0)) begin
                nlv_pc = nlv_pc + 1;
                nlv_wait = 0;
              end else begin
                nlv_wait = nlv_wait + 1;
                if (nlv_wait > a0) begin
                  rst_n <= 1'b0;
                  $fwrite(nlv_fd, "{\"t\":%0.0f,\"k\":\"rst\",\"v\":0}\n", ($realtime * 1000.0));
                  nlv_settle = 1;
                end
                nlv_stall = 1;
              end
            end else if (nlv_settle < a1) begin
              nlv_settle = nlv_settle + 1;
              nlv_stall = 1;
            end else begin
              rst_n <= 1'b1;
              $fwrite(nlv_fd, "{\"t\":%0.0f,\"k\":\"rst\",\"v\":1}\n", ($realtime * 1000.0));
              nlv_settle = 0;
              nlv_wait = 0;
              nlv_pc = nlv_pc + 1;
              nlv_stall = 1;
            end
          end
          // PORT_RESET: hold reset port a0 alone for a1 cycles.
          12: begin
            if (nlv_wait == 0) begin
              nlv_alone(a0, 1'b1);
              nlv_wait = 1;
              nlv_stall = 1;
            end else if (nlv_wait < a1) begin
              nlv_wait = nlv_wait + 1;
              nlv_stall = 1;
            end else begin
              nlv_alone(a0, 1'b0);
              nlv_wait = 0;
              nlv_pc = nlv_pc + 1;
              nlv_stall = 1;
            end
          end
          // MODE: address input a0 takes the value a1.
          13: begin nlv_mode(a0, a1); nlv_pc = nlv_pc + 1; end
          default: begin nlv_fail_report("TB.BAD_OP", "tape", op); nlv_stall = 1; end
        endcase
      end
    end
  end
end
endmodule
"""
