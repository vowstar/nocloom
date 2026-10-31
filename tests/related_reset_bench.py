"""Common reset of a whole AXI4 to APB4 network with held state in a related-clock adapter.

The target stalls, a Sync adapter (or a half-rate link pipe) holds words, then the only reset port
pulses. After release no old word may reach the target and one new read must complete exactly once.
Minimal keeps the unreset payload. The Sync return counts carry a reset in both register modes: the
divided clocks stop in reset, so an unreset return count would keep its old value through the pulse.
"""
import json
import re
import subprocess
from pathlib import Path

from sim_engine import Simulation

PASS = "RELATED_CLOCK_PASS"

DOMAIN = "{reset_port: reset_n, test_port: test, clock_port: clk"
SYNC_YAML = f"""schema_version: 10
name: related_reset
address_width: 32
reset_coverage: @REGS@
reset_ports: [{{name: reset_n}}]
test_ports: [{{name: test}}]
clock_roots:
  - {{name: root, clock_port: clk, period_ps: 1250}}
clock_domains:
  - {DOMAIN}, name: fast, clock_root: root, period_ps: 1250}}
  - {DOMAIN}, name: div2, clock_root: root, edge_parent: fast, clock_type: internal_divider, ratio: 2, period_ps: 2500}}
  - {DOMAIN}, name: div3, clock_root: root, edge_parent: fast, clock_type: internal_divider, ratio: 3, period_ps: 3750}}
clock_crossings:
  - {{from_domain: div2, to_domain: div3, kind: related, depth: 2}}
  - {{from_domain: div3, to_domain: div2, kind: related, depth: 2}}
  - {{from_domain: div2, to_domain: fast, kind: related, depth: 2}}
  - {{from_domain: fast, to_domain: div2, kind: related, depth: 2}}
initiators:
  - {{name: cpu, clock_domain: div2, protocol: axi4, data_width: 32, len_width: 8, id_width: 2}}
targets:
  - {{name: bank, clock_domain: div3, protocol: apb4, data_width: 32, address_width: 12, attributes: amba, mappings: [{{base: '0x0', size: '0x1000'}}]}}
  - {{name: aux, clock_domain: fast, protocol: apb4, data_width: 32, address_width: 12, attributes: amba, mappings: [{{base: '0x1000', size: '0x1000'}}]}}
connectivity: all
topology:
  request:
    nodes:
      - {{name: entry, clock_domain: div2}}
      - {{name: remote, clock_domain: div3}}
      - {{name: side, clock_domain: fast}}
    links:
      - {{from: entry, to: remote}}
      - {{from: entry, to: side}}
    entries:
      - {{endpoint: cpu, node: entry}}
    exits:
      - {{endpoint: bank, node: remote}}
      - {{endpoint: aux, node: side}}
  response:
    nodes:
      - {{name: entry, clock_domain: div2}}
      - {{name: remote, clock_domain: div3}}
      - {{name: side, clock_domain: fast}}
    links:
      - {{from: remote, to: entry}}
      - {{from: side, to: entry}}
    entries:
      - {{endpoint: bank, node: remote}}
      - {{endpoint: aux, node: side}}
    exits:
      - {{endpoint: cpu, node: entry}}
output: {{backend: verilog, language: verilog_2001, register_reset: @DFF@}}
"""
HALF_YAML = f"""schema_version: 10
name: related_reset
address_width: 32
reset_coverage: @REGS@
reset_ports: [{{name: reset_n}}]
test_ports: [{{name: test}}]
clock_domains:
  - {DOMAIN}, name: main}}
initiators:
  - {{name: cpu, clock_domain: main, protocol: axi4, data_width: 32, len_width: 8, id_width: 2}}
targets:
  - {{name: bank, clock_domain: main, protocol: apb4, data_width: 32, address_width: 12, attributes: amba, mappings: [{{base: '0x0', size: '0x1000'}}]}}
connectivity: all
topology:
  request:
    nodes:
      - {{name: entry, clock_domain: main}}
      - {{name: remote, clock_domain: main}}
    links:
      - {{from: entry, to: remote, pipeline: {{forward: true, backward: true, half_rate: true}}}}
    entries:
      - {{endpoint: cpu, node: entry}}
    exits:
      - {{endpoint: bank, node: remote}}
  response:
    nodes:
      - {{name: entry, clock_domain: main}}
      - {{name: remote, clock_domain: main}}
    links:
      - {{from: remote, to: entry}}
    entries:
      - {{endpoint: bank, node: remote}}
    exits:
      - {{endpoint: cpu, node: entry}}
output: {{backend: verilog, language: verilog_2001, register_reset: @DFF@}}
"""

BENCH = r"""`timescale 1ps/1ps
module tb;
  reg clk=0, reset_n=0, pready=0;
  always #625 clk=~clk;
  reg [31:0] araddr=0, awaddr=0, wdata=0;
  reg [1:0] arid=0, awid=0;
  reg [7:0] awlen=0;
  reg arvalid=0, awvalid=0, wvalid=0, wlast=0;
  wire arready, awready, wready, rvalid, rlast, bvalid;
  wire [1:0] rid, bid, rresp, bresp;
  wire [31:0] rdata;
  wire [11:0] paddr;
  wire psel, penable;
  wire [31:0] prdata = 32'ha5000000 | paddr;
  related_reset_top dut(
    .clk(clk), .reset_n(reset_n), .test(1'b0),
    .nl_i_cpu_awid(awid), .nl_i_cpu_awaddr(awaddr), .nl_i_cpu_awlen(awlen),
    .nl_i_cpu_awsize(3'd2), .nl_i_cpu_awburst(2'b01), .nl_i_cpu_awlock(1'b0),
    .nl_i_cpu_awcache(4'b0011), .nl_i_cpu_awprot(3'b000), .nl_i_cpu_awvalid(awvalid),
    .nl_i_cpu_wdata(wdata), .nl_i_cpu_wstrb(4'hf), .nl_i_cpu_wlast(wlast),
    .nl_i_cpu_wvalid(wvalid), .nl_i_cpu_bready(1'b1),
    .nl_i_cpu_arid(arid), .nl_i_cpu_araddr(araddr), .nl_i_cpu_arlen(8'd0),
    .nl_i_cpu_arsize(3'd2), .nl_i_cpu_arburst(2'b01), .nl_i_cpu_arlock(1'b0),
    .nl_i_cpu_arcache(4'b0011), .nl_i_cpu_arprot(3'b000), .nl_i_cpu_arvalid(arvalid),
    .nl_i_cpu_rready(1'b1), .nl_i_cpu_awready(awready), .nl_i_cpu_wready(wready),
    .nl_i_cpu_bvalid(bvalid), .nl_i_cpu_bresp(bresp), .nl_i_cpu_bid(bid),
    .nl_i_cpu_arready(arready), .nl_i_cpu_rvalid(rvalid), .nl_i_cpu_rdata(rdata),
    .nl_i_cpu_rresp(rresp), .nl_i_cpu_rlast(rlast), .nl_i_cpu_rid(rid),
    .nl_t_bank_pready(pready), .nl_t_bank_prdata(prdata), .nl_t_bank_pslverr(1'b0),
    .nl_t_bank_paddr(paddr), .nl_t_bank_psel(psel), .nl_t_bank_penable(penable)
    @AUX_PORTS@);

  integer failures=0, old_samples=0, old_transfers=0, new_setups=0, new_transfers=0, responses=0;
  integer reset_age=0, reset_checked=0, payload_checked=0, state_seen=0, quiet_cycles=0;
  integer reset_cycles=6, drain=0, negative=0;
  reg dynamic_active=0, in_reset=0, quiet=0, new_expected=0, boot=0, boot_done=0;
  reg [@PAYLOAD_BITS@-1:0] pre_payload=0;
  reg payload_captured=0;

  task check(input ok, input [8*40-1:0] name); begin
    if (ok !== 1'b1) begin
      failures=failures+1;
      $display("CHECK_FAIL name=%0s time=%0t",name,$time);
    end
  end endtask

  wire control_clear = @CONTROL_CLEAR@;
  wire state_held = @STATE_HELD@;
  wire [@PAYLOAD_BITS@-1:0] payload = @PAYLOAD@;

  always @(posedge clk) begin
    #2;
    if (in_reset) begin
      reset_age=reset_age+1;
      if (reset_age>=@CLEAR_AGE@) begin
        reset_checked=reset_checked+1;
        check(control_clear,"RESET_CONTROL_CLEAR");
        if (payload_captured) begin
          payload_checked=payload_checked+1;
          check(@PAYLOAD_RULE@,"RESET_PAYLOAD_RULE");
        end
      end
    end
    if (dynamic_active && reset_n && quiet) quiet_cycles=quiet_cycles+1;
    if (boot) check(psel===1'b0,"BOOT_TARGET_IDLE");
  end

  always @(posedge @TARGET_CLOCK@) if (dynamic_active && reset_n) begin
    check(psel===1'b0 || psel===1'b1,"TARGET_SELECT_KNOWN");
    if (psel===1'b1 && paddr>=12'h040 && paddr<=12'h04c) begin
      old_samples=old_samples+1;
      check(1'b0,"POST_RESET_OLD_APB_SETUP");
      if (penable===1'b1 && pready) old_transfers=old_transfers+1;
    end
    if (psel===1'b1 && penable===1'b0 && paddr==12'h100) new_setups=new_setups+1;
    if (psel===1'b1 && penable===1'b1 && pready && paddr==12'h100) new_transfers=new_transfers+1;
  end

  always @(posedge @SOURCE_CLOCK@) if (dynamic_active && reset_n) begin
    if (rvalid===1'b1) begin
      responses=responses+1;
      check(new_expected===1'b1 && rid==2'd2 && rlast===1'b1 && rresp==2'b00 && rdata==32'ha5000100,
            "POST_RESET_ONLY_NEW_RESPONSE");
    end
    check(!quiet || bvalid===1'b0,"QUIET_NO_WRITE_RESPONSE");
    check(!quiet || rvalid===1'b0,"QUIET_NO_READ_RESPONSE");
  end

  task stop_timeout(input [8*24-1:0] where); begin
    $display("SCENARIO_TIMEOUT at=%0s old=%0d old_transfers=%0d new_setups=%0d new=%0d rsp=%0d failures=%0d",
             where,old_samples,old_transfers,new_setups,new_transfers,responses,failures);
    $display("CHECK_FAIL name=SCENARIO_TIMEOUT");
    $finish;
  end endtask

  // Each wait ends on an accepted handshake, or at once when the wanted state is already held.
  task send_ar(input [1:0] id, input [31:0] addr, input stop_on_state); integer watchdog; begin
    @(negedge clk); arid=id; araddr=addr; arvalid=1; watchdog=0;
    @(posedge @SOURCE_CLOCK@);
    while (arready!==1'b1 && !(stop_on_state && state_held===1'b1)) begin
      @(posedge @SOURCE_CLOCK@); watchdog=watchdog+1;
      if (watchdog>200) stop_timeout("AR");
    end
    @(negedge clk); arvalid=0;
  end endtask

  task send_write(input [1:0] id, input [31:0] addr); integer watchdog, beat; begin
    @(negedge clk); awid=id; awaddr=addr; awlen=1; awvalid=1; watchdog=0;
    @(posedge @SOURCE_CLOCK@);
    while (awready!==1'b1 && !(drain==0 && state_held===1'b1)) begin
      @(posedge @SOURCE_CLOCK@); watchdog=watchdog+1;
      if (watchdog>200) stop_timeout("AW");
    end
    @(negedge clk); awvalid=0;
    for (beat=0; beat<2 && !(drain==0 && state_held===1'b1); beat=beat+1) begin
      wdata=32'h5a000000 | ((addr+beat*4)&32'hffff); wlast=beat==1; wvalid=1; watchdog=0;
      @(posedge @SOURCE_CLOCK@);
      while (wready!==1'b1 && !(drain==0 && state_held===1'b1)) begin
        @(posedge @SOURCE_CLOCK@); watchdog=watchdog+1;
        if (watchdog>200) stop_timeout("W");
      end
      @(negedge clk); wvalid=0; wlast=0;
    end
  end endtask

  integer n, settle, watchdog;
  initial begin
    void'($value$plusargs("RESET_CYCLES=%d",reset_cycles));
    void'($value$plusargs("DRAIN=%d",drain));
    void'($value$plusargs("NEGATIVE=%d",negative));
    if (drain!=0) pready=1;
    repeat (5) @(posedge clk);
    @(negedge clk); reset_n=1;
    // Two release stages, then one edge of the slowest clock.
    repeat (6) @(posedge clk);
    @(negedge clk); boot=1;
    repeat (6) @(posedge clk);
    @(negedge clk); boot=0;
    if (drain!=0) begin
      n=0;
      while (n<3 && state_held!==1'b1) begin
        @SEND_DRAIN@
        repeat (80) @(posedge clk);
        n=n+1;
      end
      check(psel===1'b0,"DRAINED_TARGET_IDLE");
    end else begin
      n=0;
      while (n<4 && state_held!==1'b1) begin
        @SEND_STALLED@
        settle=0;
        while (settle<12 && state_held!==1'b1) begin
          @(posedge clk); #2; settle=settle+1;
        end
        n=n+1;
      end
    end
    check(state_held,"DYNAMIC_STATE_REACHED");
    if (state_held===1'b1) state_seen=1;
    pre_payload=payload; payload_captured=1;
    @(negedge clk);
    dynamic_active=1; arvalid=0; awvalid=0; wvalid=0; wlast=0; in_reset=1;
    if (negative!=2) reset_n=0;
    repeat (reset_cycles) @(posedge clk);
    @(negedge clk);
    if (negative!=2) reset_n=1;
    in_reset=0; quiet=1;
    repeat (8) @(posedge clk);
    @(negedge clk); quiet=0;
    new_expected=negative==0;
    if (negative==1) send_ar(2'd2,32'h044,1'b0);
    else send_ar(2'd2,32'h100,1'b0);
    @(negedge clk); pready=1;
    watchdog=0;
    while ((negative==0 && (new_transfers<1 || responses<1)) || (negative!=0 && failures<1)) begin
      @(posedge clk); watchdog=watchdog+1;
      if (watchdog>200) stop_timeout("NEW_READ");
    end
    repeat (8) @(posedge clk);
    check(reset_checked>0,"RESET_CYCLES_CHECKED");
    check(payload_checked>0,"PAYLOAD_CYCLES_CHECKED");
    check(state_seen>0,"STATE_COVERED");
    check(old_samples==0,"NO_POST_RESET_OLD_ACCESS");
    check(new_setups==1,"EXACTLY_ONE_NEW_SETUP");
    check(new_transfers==1,"EXACTLY_ONE_NEW_ACCESS");
    check(responses==1,"EXACTLY_ONE_NEW_RESPONSE");
    check(quiet_cycles>=7,"QUIET_WINDOW_COVERED");
    $display("SCENARIO_RESULT old=%0d old_transfers=%0d new_setups=%0d new=%0d rsp=%0d quiet=%0d reset_checks=%0d payload_checks=%0d failures=%0d",
             old_samples,old_transfers,new_setups,new_transfers,responses,quiet_cycles,
             reset_checked,payload_checked,failures);
    if (failures==0) $display("@PASS@");
    $finish;
  end
  initial begin
    #4000000;
    $display("CHECK_FAIL name=GLOBAL_TIMEOUT");
    $finish;
  end
endmodule
"""

REGISTER_BLOCK = re.compile(r"always @\(posedge (\w+)(?: or negedge \w+)?\) begin\n(.*?)\nend\n", re.S)


def clock_of(text, register):
    """The clock net of the always block that assigns one generated register."""
    for clock, body in REGISTER_BLOCK.findall(text):
        if re.search(rf"\b{register} <=", body):
            return clock
    raise AssertionError(f"no always block assigns {register}")


def rtl_names(mapping):
    registers = {r["value"]: r for r in mapping["network"]["cdc"]["registers"] if "rtl" in r}
    return lambda value: registers[value]["rtl"]


def reset_only_at_power_up(text, registers):
    """Fault: the named registers ignore every reset after power-up, as if they had no reset pin."""
    for register in registers:
        text, count = re.subn(rf"^( *)({register} <= \d+'d0;)$", r"\1if (!tb.dynamic_active) \2",
                              text, count=1, flags=re.M)
        if count != 1:
            raise AssertionError(f"no reset assignment for {register}")
    return text


def sync_substitutions(mapping, text):
    name = rtl_names(mapping)
    cut = next(c for c in mapping["network"]["cdc"]["cuts"]
               if c["kind"] == "sync" and c["from_domain"] == "div2" and c["to_domain"] == "div3")
    write, read = name(cut["write_count"]), name(cut["read_count"])
    write_return = name(cut["write_count_return"])
    read_return = name(cut["read_count_return"])
    write_index, read_index = name(cut["write_index"]), name(cut["read_index"])
    address = next(f for f in cut["fields"] if f["name"] == "address")
    slots = [f"dut.{name(s)}" for s in address["storage"]]
    held = (f"dut.{write}!=0 && dut.{read_return}!=0 && dut.{write}!=dut.{read_return}")
    # Drained: nothing queued, every count equal and away from its reset value.
    drained = (f"dut.{write}!=0 && dut.{write}==dut.{read} && dut.{write}==dut.{read_return}"
               f" && dut.{write}==dut.{write_return}")
    return {
        "@CONTROL_CLEAR@": " && ".join(f"dut.{r}===0" for r in (write, read, write_index, read_index,
                                                                write_return, read_return)),
        "@STATE_HELD@": f"(drain!=0) ? ({drained}) : ({held})",
        "@PAYLOAD@": "{" + ",".join(slots) + "}",
        "@PAYLOAD_BITS@": str(address["width"] * len(slots)),
        "@SOURCE_CLOCK@": "dut." + clock_of(text, write),
        "@TARGET_CLOCK@": "dut." + clock_of(text, read),
        "@AUX_PORTS@": ", .nl_t_aux_pready(1'b1), .nl_t_aux_prdata(32'd0), .nl_t_aux_pslverr(1'b0)",
        "@SEND_DRAIN@": "send_write(2'd0,32'h40);",
        "@SEND_STALLED@": "send_write(n[1:0],32'h40+n*4);",
    }, [write_return, read_return]


def half_substitutions(mapping):
    name = rtl_names(mapping)
    pipe = next(s for s in mapping["network"]["storage"] if s["resource"].startswith("pipeline/req/"))
    control = name(pipe["control"][0])
    registers = {r["value"]: r for r in mapping["network"]["cdc"]["registers"] if "rtl" in r}
    payload = next(registers[v] for v in pipe["payload"] if v in registers)
    return {
        "@CONTROL_CLEAR@": f"dut.{control}===0",
        "@STATE_HELD@": f"dut.{control}!=0 && pready==0" ,
        "@PAYLOAD@": f"dut.{payload['rtl']}",
        "@PAYLOAD_BITS@": str(payload["width"]),
        "@SOURCE_CLOCK@": "clk",
        "@TARGET_CLOCK@": "clk",
        "@AUX_PORTS@": "",
        "@SEND_DRAIN@": "send_ar(n[1:0],32'h40+n*4,1'b0);",
        "@SEND_STALLED@": "send_ar(n[1:0],32'h40+n*4,1'b1);",
    }, []


def substitute(text, values):
    for key, value in values.items():
        text = text.replace(key, value)
    leftover = re.search(r"@[A-Z_]+@", text)
    if leftover:
        raise AssertionError(f"unsubstituted {leftover.group()}")
    return text


def build(cli, root, family, regs, dff, fault=None):
    name = f"{family}_{regs}_{dff}" + (f"_{fault}" if fault else "")
    work = root / name
    work.mkdir(parents=True, exist_ok=True)
    source = (SYNC_YAML if family == "sync" else HALF_YAML)
    (work / "config.yaml").write_text(source.replace("@REGS@", regs).replace("@DFF@", dff))
    # The command line wants a new directory, and a rerun keeps what the last run left.
    runs = 0
    while (work / f"cli{runs}").exists():
        runs += 1
    out = work / f"cli{runs}"
    argv = [str(cli), "generate", str(work / "config.yaml"), "--out-dir", str(out)]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False,
                          stdin=subprocess.DEVNULL)
    (work / "generate.log").write_text(done.stdout + done.stderr)
    if done.returncode:
        raise AssertionError(f"generate failed: {argv}: {done.stderr}")
    mapping = json.loads((out / "nocloom_related_reset.map.json").read_text())
    top = out / "nocloom_related_reset.v"
    cells = sorted(p for p in out.glob("nocloom_*.v") if p != top)
    text = top.read_text() + "".join(p.read_text() for p in cells)
    if family == "sync":
        values, returns = sync_substitutions(mapping, text)
    else:
        values, returns = half_substitutions(mapping)
    if fault == "no_return_reset":
        text = reset_only_at_power_up(text, returns)
    # A synchronous clear needs both release stages to fall and then an edge of the slowest clock.
    values["@CLEAR_AGE@"] = "1" if dff == "asynchronous" else "6"
    # Minimal keeps the slots of a crossing and of a link pipe (docs/clocks-and-resets.md, reset_coverage).
    values["@PAYLOAD_RULE@"] = "payload===pre_payload" if regs == "minimal" else "payload===0"
    values["@PASS@"] = PASS
    (work / "network.v").write_text(text)
    bench = BENCH.replace("related_reset_top", mapping["module"])
    (work / "tb.sv").write_text(substitute(bench, values))
    return work, Simulation(work, "network.v", compile_timeout=300, run_timeout=120, success=[PASS])


def run(sim, work, label, options, expected_failure=None):
    result = sim.run(options)
    log = (work / "simulation.log").read_text(errors="replace")
    (work / f"simulation-{label}.log").write_text(log)
    if expected_failure:
        passed = expected_failure in log and PASS not in log.replace("CHECK_FAIL", "")
    else:
        passed = result.returncode == 0 and PASS in log and "CHECK_FAIL" not in log
    row = dict(name=f"{work.name}/{label}", expected=expected_failure or "PASS", passed=passed,
               exit=result.returncode, log=str(work / f"simulation-{label}.log"))
    print(("PASS" if passed else "FAIL"), row["name"], flush=True)
    if not passed:
        print(log[-5000:], flush=True)
    return row


def case_rows(cli, root):
    """Every register and reset-pin mode for Sync and half-rate, with the faults the checker catches."""
    root = Path(root) / "related_reset"
    rows = []
    for family in ("sync", "half"):
        for regs in ("all", "minimal"):
            for dff in ("asynchronous", "synchronous"):
                work, sim = build(cli, root, family, regs, dff)
                rows.append(run(sim, work, "stalled", ["+RESET_CYCLES=6"]))
                if family == "sync" and regs == "minimal" and dff == "asynchronous":
                    rows.append(run(sim, work, "long_reset", ["+RESET_CYCLES=120"]))
                    rows.append(run(sim, work, "drained", ["+RESET_CYCLES=6", "+DRAIN=1"]))
                    rows.append(run(sim, work, "resend_old", ["+NEGATIVE=1"],
                                    "name=POST_RESET_OLD_APB_SETUP"))
                    rows.append(run(sim, work, "no_reset_pulse", ["+NEGATIVE=2"],
                                    "name=RESET_CONTROL_CLEAR"))
    work, sim = build(cli, root, "sync", "minimal", "asynchronous", "no_return_reset")
    rows.append(run(sim, work, "stalled", ["+RESET_CYCLES=6"], "name=POST_RESET_OLD_APB_SETUP"))
    rows.append(run(sim, work, "drained", ["+RESET_CYCLES=6", "+DRAIN=1"],
                    "name=POST_RESET_OLD_APB_SETUP"))
    return rows
