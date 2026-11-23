"""Register access through the window of error log banks, in transaction simulation.

One APB4 initiator reads and writes the registers of every bank, makes records by failed accesses
to the watched targets, clears them, and reads addresses outside every bank and outside every
mapping. The expected values come from the definition and from the table in log_bank_layout.py;
the generated map and the C header must state the same before a simulation runs, and the header
words are read once more at the addresses and with the values the C header gives.

  python3 tests/log_bank_access.py --cli CLI --work DIR [--case NAME ...]
"""

import argparse
import json
from pathlib import Path
import random
import re
import subprocess
import tempfile

import log_bank_layout as layout
import sim_engine

WINDOW, WINDOW_SIZE = 0x100000, 0x100000
MISSES = (WINDOW - 4, WINDOW + WINDOW_SIZE)
REVISION = 0xA5C33C5A
# The drivers an audit flag can have on an APB4 initiator, and the PPROT of the failed access.
DRIVERS = ("0", "1", "privileged", "~privileged", "nonsecure", "~nonsecure", "instruction",
           "~instruction")
PROT = 0b101
# Beats of one access to a bank in the window's domain with no switch and no chain, counted from
# the edge with PENABLE: read, write, inside the window but outside every bank, undecoded.
BEATS = dict(read=3, write=7, outside=2, miss=3)


class Bank:
    def __init__(self, log, logger, base, name, parts):
        self.log, self.logger, self.base, self.name = log, logger, base, name
        self.parts = parts  # the capabilities the bank states


def definition(name, logs, *, clocks=(("main", "clk", None),), log_domain=None, parts=(),
               revision=None, pass_on="always", audits=0, backward_pipe=False):
    """A network of one configuration initiator, one failing target per log and the window.

    logs: per log its logger count, or a list of bank offsets (None: the next free place).
    parts: what the logs have: classes, audit_flags, user_flags, whole_record. With user flags
    and no whole record the data path is AXI4: its ports stay idle."""
    crossed = log_domain is not None
    text = ["schema_version: 10", f"name: {name}", "address_width: 32",
            "reset_ports: [{name: reset_n}]", "test_ports: [{name: test}]"]
    user = "user_flags" in parts
    axi = user and "whole_record" not in parts
    second = crossed or axi
    transport = (["user_flags: [mark]"] if user else []) + \
        ([] if "classes" in parts else ["compact_failure_responses: true"])
    if transport:
        text.append("transport: {" + ", ".join(transport) + "}")
    text.append("clock_domains:")
    for domain, port, period in clocks:
        timing = f", period_ps: {period}" if period else ""
        text.append(f"  - {{name: {domain}, clock_port: {port}, reset_port: reset_n, "
                    f"test_port: test{timing}}}")
    audit = [f"a{k}" for k in range(audits)] if audits else \
        ["audit"] if "audit_flags" in parts else []
    drivers = ", ".join(f"{flag}: '{DRIVERS[k % len(DRIVERS)]}'" for k, flag in enumerate(audit))
    flags = (f", audit_flags: {{{drivers}}}" if audit else "") + \
        (", user_flags: {mark: '1'}" if user else "")
    data = "protocol: axi4, data_width: 32, id_width: 4, len_width: 8" if axi else \
        "protocol: apb4, data_width: 32"
    pipe = ", pipes: {packets: {slice: backward}}" if backward_pipe else ""
    text += ["initiators:",
             f"  - {{name: cfg, clock_domain: main, protocol: apb4, data_width: 32{flags}{pipe}}}"]
    if second:
        fold = ", slverr_only: true" if axi and "classes" not in parts else ""
        text.append(f"  - {{name: data, clock_domain: main, {data}{flags}{fold}}}")
    text.append("targets:")
    for k in range(len(logs)):
        text.append(f"  - {{name: ram{k}, clock_domain: main, {data}, "
                    f"{'' if axi else 'address_width: 16, '}attributes: amba, "
                    f"mappings: [{{base: '{hex(k * 0x1000)}', size: '0x1000'}}]}}")
    text += ["error_logging:",
             f"  registers: {{name: csr, base: '{hex(WINDOW)}', size: '{hex(WINDOW_SIZE)}', "
             "clock_domain: main}"]
    if "classes" not in parts:
        text.append("  capture_class: false")
    if revision is not None:
        text.append(f"  revision: {revision}")
    if audit:
        text.append(f"  audit_flags: [{', '.join(audit)}]")
    text.append("  logs:")
    banks, place = [], 0
    for k, loggers in enumerate(logs):
        offsets = loggers if isinstance(loggers, (list, tuple)) else [None] * loggers
        items = []
        for n, offset in enumerate(offsets):
            at = place if offset is None else offset
            place = at + layout.BANK_BYTES
            keys = [f"pass_on: {pass_on}"] + ([] if offset is None else [f"offset: '{hex(offset)}'"])
            items.append("{" + ", ".join(keys) + "}")
            has = ({"audit_flags"} if audit else set()) | {"audit_flags", "user_flags"} & set(parts)
            if "whole_record" in parts:
                has.add("target_user_flags")
            if n + 1 < len(offsets):
                has.add("next_logger")
            if revision is not None:
                has.add("given_revision")
            label = "csr" if k == 0 and n == 0 else f"log{k}" + (f"_{n}" if n else "")
            banks.append(Bank(k, n, at, label, has))
        extra = f", clock_domain: {log_domain}" if crossed else ""
        extra += ", whole_record: true" if "whole_record" in parts else ""
        text.append(f"    - {{name: log{k}, watch: [ram{k}]{extra}, loggers: [{', '.join(items)}]}}")
    if axi:
        targets = [f"ram{k}" for k in range(len(logs))]
        text += ["connectivity:", "  cfg: [csr]", f"  data: [{', '.join(targets)}]"]
    elif crossed:
        # A clocked network names its nodes; the window and the data path keep apart.
        targets = [f"ram{k}" for k in range(len(logs))]
        text += ["connectivity:", "  cfg: [csr]", f"  data: [{', '.join(targets)}]", "topology:"]
        sides = dict(request=(["cfg"], ["data"], ["csr"], targets),
                     response=(["csr"], targets, ["cfg"], ["data"]))
        for way, (enter_c, enter_d, leave_c, leave_d) in sides.items():
            c, d = "c" + way[0], "d" + way[0]
            text += [f"  {way}:", "    nodes:", f"      - {{name: {c}, clock_domain: main}}",
                     f"      - {{name: {d}, clock_domain: main}}", "    links: []", "    entries:"]
            text += [f"      - {{endpoint: {e}, node: {c}}}" for e in enter_c]
            text += [f"      - {{endpoint: {e}, node: {d}}}" for e in enter_d]
            text.append("    exits:")
            text += [f"      - {{endpoint: {e}, node: {c}}}" for e in leave_c]
            text += [f"      - {{endpoint: {e}, node: {d}}}" for e in leave_d]
    else:
        text.append("connectivity: all")
    text.append("output: {backend: verilog, language: verilog_2001}")
    return "\n".join(text) + "\n", banks


BENCH = r"""`timescale 1ps/1ps
module tb;
reg reset_n = 0;
@CLOCKS@
reg [1:0] sel = 0, en = 0, wr = 0;
reg [31:0] addr [0:1];
reg [31:0] wd = 0;
reg [3:0] strb = 0;
reg [2:0] prot = 0;
wire [1:0] ready, err;
wire [31:0] rd [0:1];
reg [@TARGETS@:0] fail = 0;
wire [@TARGETS@:0] alarm;
integer checks = 0, failures = 0, beats = 0;
@TOP@ dut(
@PORTS@
);
// One transfer of initiator m; beats counts from the edge with PENABLE.
task access(input integer m, input w, input [31:0] a, input [31:0] d, input [3:0] s, input bad,
            input [31:0] expected, input [31:0] mask, input integer label);
begin
  @(negedge clk);
  sel[m] = 1; en[m] = 0; wr[m] = w; addr[m] = a; wd = d; strb = w ? s : 4'h0;
  @(negedge clk); en[m] = 1;
  @(posedge clk); beats = 1;
  while (!ready[m] && beats < 4000) begin
    beats = beats + 1; @(posedge clk);
  end
  checks = checks + 1;
  if (beats == 4000 || err[m] !== bad || (!w && !bad && (rd[m] & mask) !== (expected & mask))) begin
    failures = failures + 1;
    $display("CHECK_FAIL check%0d addr=%h write=%b data=%h expected=%h mask=%h error=%b wanted=%b",
             label, a, w, rd[m], expected, mask, err[m], bad);
  end
  @(negedge clk); sel[m] = 0; en[m] = 0; strb = 0;
end
endtask
task took(input integer wanted, input integer label);
begin
  checks = checks + 1;
  if (beats != wanted) begin
    failures = failures + 1;
    $display("CHECK_FAIL check%0d beats=%0d wanted=%0d", label, beats, wanted);
  end
end
endtask
task alarm_is(input integer log_index, input value, input integer label);
begin
  checks = checks + 1;
  if (alarm[log_index] !== value) begin
    failures = failures + 1;
    $display("CHECK_FAIL check%0d alarm=%b wanted=%b", label, alarm[log_index], value);
  end
end
endtask
initial begin
  addr[0] = 0; addr[1] = 0;
  repeat (8) @(negedge clk); reset_n = 1;
  repeat (8) @(negedge clk);
  @CHECKS@
  if (failures == 0) $display("CHECK_PASS checks=%0d", checks);
  $finish;
end
initial begin
  #@LIMIT@; $display("CHECK_FAIL timeout"); $finish;
end
endmodule
"""


def ports(text, top):
    head = text[text.index(f"module {top} ("):]
    head = head[:head.index(");")]
    return [(m[1], int(m[2] or 0) + 1, m[3]) for m in
            re.finditer(r"(input|output) wire (?:\[(\d+):0\] )?(\w+)", head)]


class Run:
    """The checks of one definition, with a model of what each written control bit holds."""

    def __init__(self, cli, root, name, text, banks, clocks, wait, revision=None, classes=False,
                 audits=0, gaps=None):
        self.folder = root / name
        root.mkdir(parents=True, exist_ok=True)
        source = root / (name + ".yaml")
        source.write_text(text)
        subprocess.run([cli, "generate", str(source), "--out-dir", str(self.folder), "--c-header"],
                       check=True, timeout=300, stdout=subprocess.DEVNULL)
        self.top, self.banks, self.clocks, self.wait = "nocloom_" + name, banks, clocks, wait
        self.classes = classes
        # Idle cycles before each access, from a seeded sequence.
        self.gaps = random.Random(gaps) if gaps is not None else None
        # The audit flags of a failed access: each driver's value at PROT, first flag lowest.
        bits = dict(privileged=PROT & 1, nonsecure=PROT >> 1 & 1, instruction=PROT >> 2 & 1)
        value = sum((bits.get(d.lstrip("~"), int(d.lstrip("~") == "1")) ^ d.startswith("~")) << k
                    for k, d in enumerate(DRIVERS[k % len(DRIVERS)] for k in range(audits)))
        self.audits = (audits, value) if audits else None
        self.lines, self.labels, self.stored = [], [], {}
        self.map = json.loads((self.folder / f"{self.top}.map.json").read_text())
        record = self.map["definition"]["log_registers"]["csr"]
        self.records = {r["bank"]: r for r in record.get("banks", [record])}
        self.data = 1 if any(domain != "main" for domain, _, _ in clocks) else 0
        self.given = revision is not None
        self.macros = {}
        for path in sorted(self.folder.glob("*.h")):
            self.macros.update((k, int(v, 16)) for k, v in re.findall(
                r"#define (\w+) \((0x[0-9a-f]+)U(?:LL)?\)", path.read_text()))
        if revision is None:
            version = subprocess.run([cli, "--version"], check=True, capture_output=True,
                                     text=True).stdout.strip()
            revision = layout.generator_revision(version)
        self.revision = revision

    def address(self, bank, offset=0):
        return WINDOW + bank.base + offset

    def emit(self, what, m, write, address, value=0, strobe=0xf, error=False, expected=0,
             mask=0xffffffff):
        self.labels.append(what)
        if self.gaps and self.gaps.random() < 0.5:
            self.idle(self.gaps.randrange(1, 12))
        self.lines.append(f"access({m}, {int(write)}, 32'h{address:x}, 32'h{value:x}, "
                          f"4'h{strobe:x}, {int(error)}, 32'h{expected:x}, 32'h{mask:x}, "
                          f"{len(self.labels) - 1});")

    def read(self, what, address, expected=0, mask=0xffffffff, error=False):
        self.emit(what, 0, False, address, error=error, expected=expected, mask=mask)

    def write(self, what, address, value, strobe=0xf, error=False):
        self.emit(what, 0, True, address, value=value, strobe=strobe, error=error)

    def beats(self, what, wanted):
        self.labels.append(what)
        self.lines.append(f"took({wanted}, {len(self.labels) - 1});")

    def idle(self, cycles):
        self.lines.append(f"repeat ({cycles}) @(negedge clk);")

    def alarm(self, what, alarm):
        self.labels.append(what)
        self.lines.append(f"alarm_is(0, 1'b{int(alarm)}, {len(self.labels) - 1});")

    # ---- The map against the table, before anything is simulated. ----
    def header(self, bank):
        """The constant words of a bank, from the definition and the table alone."""
        return {layout.ID: layout.identification(bank.log, bank.logger, bank.parts),
                layout.REVISION: self.revision}

    def check_map(self):
        widths = {name: (offset, lsb, width, access)
                  for name, offset, lsb, width, access in layout.FIELDS}
        for bank in self.banks:
            record = self.records[bank.name]
            assert record["base"] == bank.base, (bank.name, record["base"], bank.base)
            words = {}
            for f in record["registers"]:
                offset, lsb, width, access = widths[f["name"]]
                assert (f["offset"], f["lsb"], f["access"]) == (offset, lsb, access), f
                assert 0 < f["width"] <= width, f
                if "value" in f:
                    words[offset] = words.get(offset, 0) | f["value"] << lsb
            assert words == self.header(bank), (bank.name, words, self.header(bank))
            assert (record["layout"], record["log_index"], record["logger_index"]) == \
                (layout.LAYOUT, bank.log, bank.logger), bank.name
            assert set(record["capabilities"]) == bank.parts, (bank.name, record["capabilities"])
        whole = self.map["definition"]["log_registers"]["csr"]
        assert whole["bank_bytes"] == layout.BANK_BYTES
        assert whole["error_classes"] == dict(decode=layout.CLASS_DECODE,
                                              target=layout.CLASS_TARGET)

    def check_c_header(self):
        """The C header against the table: the layout's macros, then each bank's place and
        expected words."""
        top = self.top.upper()
        log = top + "_ERROR_LOG_"
        wanted = {log + "LAYOUT": layout.LAYOUT, log + "BANK_BYTES": layout.BANK_BYTES,
                  log + "CLASS_DECODE": layout.CLASS_DECODE,
                  log + "CLASS_TARGET": layout.CLASS_TARGET}
        for name, offset, lsb, width, _ in layout.FIELDS:
            stem = log + name.upper()
            wanted.update({stem + "_OFFSET": offset, stem + "_SHIFT": lsb,
                           stem + "_MASK": ((1 << width) - 1) << lsb})
        window = top + "_CFG_CSR_WINDOW_0_"
        assert self.macros[window + "BASE"] == WINDOW and self.macros[window + "SIZE"] == WINDOW_SIZE
        for bank in self.banks:
            stem = window + bank.name.upper()
            words = self.header(bank)
            wanted.update({stem + "_OFFSET": bank.base, stem + "_ID_VALUE": words[layout.ID]})
            if self.given:
                wanted[stem + "_REVISION_VALUE"] = words[layout.REVISION]
        stated = {k: v for k, v in self.macros.items() if k.startswith((log, window))
                  and not k.endswith(("_BASE", "_SIZE"))}
        assert stated == wanted, sorted(set(stated.items()) ^ set(wanted.items()))[:8]

    def by_c_header(self):
        """Software's view: the address and the expected value of each constant word come from
        the C header's macros alone."""
        top = self.top.upper()
        window = top + "_CFG_CSR_WINDOW_0_"
        for bank in self.banks:
            stem = window + bank.name.upper()
            at = self.macros[window + "BASE"] + self.macros[stem + "_OFFSET"]
            for word, field in (("ID", "LAYOUT"), ("REVISION", "REVISION")):
                if stem + f"_{word}_VALUE" in self.macros:
                    self.read(f"c header {word} {bank.name}",
                              at + self.macros[f"{top}_ERROR_LOG_{field}_OFFSET"],
                              self.macros[stem + f"_{word}_VALUE"])
            held = self.macros[f"{top}_ERROR_LOG_RECORD_HELD_MASK"]
            self.read(f"c header state {bank.name}",
                      at + self.macros[f"{top}_ERROR_LOG_RECORD_HELD_OFFSET"], 0, held)

    # ---- Checks. ----
    def headers(self):
        for bank in self.banks:
            for offset, value in sorted(self.header(bank).items()):
                at = self.address(bank, offset)
                self.read(f"constant {bank.name}+{offset:#x}", at, value)
                self.write(f"constant write {bank.name}+{offset:#x}", at, ~value & 0xffffffff)
                self.read(f"constant kept {bank.name}+{offset:#x}", at, value)

    def control_mask(self):
        return sum(((1 << f["width"]) - 1) << f["lsb"] for f in layout.controls())

    def store(self, bank, value, strobe=0xf, what="control write"):
        """A write to STATE: the control bits of the bytes under a strobe take the value."""
        lanes = sum(0xff << 8 * k for k in range(4) if strobe >> k & 1)
        old = self.stored.get(bank.name, 0)
        self.stored[bank.name] = (value & lanes | old & ~lanes) & self.control_mask()
        self.write(f"{what} {bank.name}", self.address(bank, layout.STATE), value, strobe)

    def state(self, what, bank, held=0, overflow=0, mask=None):
        """STATE holds the control bits the model has and the two status bits as given."""
        value = (self.stored.get(bank.name, 0) | layout.put(layout.HELD, held)
                 | layout.put(layout.OVERFLOW, overflow))
        known = self.control_mask() | layout.mask(layout.HELD) | layout.mask(layout.OVERFLOW)
        self.read(f"{what} {bank.name}", self.address(bank, layout.STATE), value,
                  known if mask is None else mask)

    def verify(self, what, held=(), mask=None):
        for bank in self.banks:
            self.state(what, bank, held=int(bank in held), mask=mask)

    def empty(self):
        """An undecoded access makes a record of its own: clear both status bits everywhere."""
        both = layout.mask(layout.HELD) | layout.mask(layout.OVERFLOW)
        self.idle(self.wait)
        for bank in self.banks:
            self.store(bank, both, 0x1, "clear status")
        self.verify("status cleared")

    def controls(self):
        """Each bank takes a value of its own; then every bank is read: no key or branch mixes."""
        self.verify("control reset")
        fields = layout.controls()
        for k, bank in enumerate(self.banks):
            # Byte 0 holds only status bits: zero there changes nothing.
            self.store(bank, sum((k >> n & 1) << f["lsb"] for n, f in enumerate(fields))
                       | (0xfffffc00 if k % 3 == 0 else 0))
        self.verify("control own value")
        for bank in self.banks[:2] + self.banks[-1:]:
            for strobe in range(16):
                self.store(bank, 0xa5a5a500 ^ (0x01010100 * (strobe & 1)), strobe)
                self.state("control strobe", bank)
        self.verify("control after strobes")
        for bank in self.banks:
            self.store(bank, 0)
        self.verify("control cleared")

    def fail(self, log):
        self.lines.append(f"fail[{log}] = 1; prot = 3'd{PROT};")
        self.emit(f"failed access log{log}", self.data, False, log * 0x1000 + 0x234, error=True)
        self.lines.append(f"fail[{log}] = 0; prot = 0;")
        self.idle(self.wait)

    def record(self, bank):
        """The fields of the record of a failed APB read of four bytes at offset 0x234."""
        paths = self.map["definition"]["error_logs"][f"log{bank.log}"]["route_values"]
        # A whole record keeps the address; another one keeps its low eight bits and says so.
        whole = "target_user_flags" in bank.parts
        state = layout.put("record_address_partial", int(not whole))
        if self.classes:
            state |= layout.put("record_class", layout.CLASS_TARGET)
        at = self.address(bank)
        fields = (layout.mask("record_class") | layout.mask("record_write")
                  | layout.mask("record_wrap") | layout.mask("record_exclusive")
                  | layout.mask("record_address_partial"))
        self.read(f"record state {bank.name}", at + layout.STATE, state, fields)
        self.read(f"record address {bank.name}", at + 0x08,
                  bank.log * 0x1000 + 0x234 if whole else 0x34)
        self.read(f"record extent {bank.name}", at + 0x0c, layout.put("record_bytes", 4))
        source = paths["source_path"]["data" if self.data else "cfg"][0]
        target = paths["target_path"][f"ram{bank.log}"][0]
        self.read(f"record path {bank.name}", at + 0x10,
                  layout.put("record_source_path", source)
                  | layout.put("record_target_path", target))
        self.read(f"record region {bank.name}", at + 0x14, 0, layout.mask("record_region_index"))
        if self.audits:
            self.read(f"record audit flags {bank.name}", at + 0x18, self.audits[1])

    def records_and_clears(self):
        """Every logger passes its records on, so one failed access fills each logger of its
        log's chain and no bank of another log. A second one overflows them. A written one
        under byte 0's strobe clears a status bit of one bank."""
        held, lost = set(), set()
        byte0 = 0x1
        for log in sorted({bank.log for bank in self.banks}):
            self.fail(log)
            held |= {bank for bank in self.banks if bank.log == log}
            for bank in self.banks:
                self.state("first record", bank, held=int(bank in held))
            for bank in self.banks:
                if bank.log == log and (bank.logger < 2 or "next_logger" not in bank.parts):
                    self.record(bank)
        for log in sorted({bank.log for bank in self.banks})[:2]:
            self.fail(log)
            lost |= {bank for bank in self.banks if bank.log == log}
            for bank in self.banks:
                self.state("second record", bank, held=int(bank in held),
                           overflow=int(bank in lost))
        for bank in sorted(held, key=lambda b: (b.log, -b.logger)):
            both = layout.mask(layout.HELD) | layout.mask(layout.OVERFLOW)
            self.store(bank, both, 0xe, "clear without strobe")
            self.store(bank, 0, 0xf, "clear without one")
            self.state("status kept", bank, held=1, overflow=int(bank in lost))
            # Each status bit clears alone, in either order.
            first = layout.OVERFLOW if bank.logger % 2 else layout.HELD
            for name in (first, layout.HELD, layout.OVERFLOW):
                kept = lost if name == layout.OVERFLOW else held
                if bank not in kept:
                    continue
                self.store(bank, layout.mask(name), byte0, "clear " + name)
                kept.discard(bank)
                self.state(name + " cleared", bank, held=int(bank in held),
                           overflow=int(bank in lost))
            for other in self.banks:
                if other.log == bank.log:
                    self.state("record cleared", other, held=int(other in held),
                               overflow=int(other in lost))

    def outside(self):
        """Blocks of the window that hold no bank answer with an error; so does no mapping."""
        used = {bank.base for bank in self.banks}
        near = sorted({block for base in used
                       for block in (base - layout.BANK_BYTES, base + layout.BANK_BYTES)
                       if 0 <= block < WINDOW_SIZE and block not in used}
                      | ({WINDOW_SIZE - layout.BANK_BYTES, WINDOW_SIZE // 2} - used))
        for block in near[:6] + near[-2:]:
            for byte in (0, layout.BANK_BYTES - 4):
                at = WINDOW + block + byte
                self.read(f"outside read {at:#x}", at, error=True)
                self.write(f"outside write {at:#x}", at, 0xffffffff, error=True)
        for at in MISSES:
            self.read(f"undecoded read {at:#x}", at, error=True)
            self.write(f"undecoded write {at:#x}", at, 0xffffffff, error=True)
        self.verify("control after errors", mask=self.control_mask())
        self.empty()

    def latency(self):
        """The beats of each kind of access, on the first bank."""
        bank = self.banks[0]
        self.state("beats read", bank)
        self.beats("read beats", BEATS["read"])
        self.store(bank, 0)
        self.beats("write beats", BEATS["write"])
        free = next(b for b in range(0, WINDOW_SIZE, layout.BANK_BYTES)
                    if b not in {bank.base for bank in self.banks})
        self.read("beats outside", WINDOW + free, error=True)
        self.beats("outside beats", BEATS["outside"])
        self.read("beats undecoded", MISSES[1], error=True)
        self.beats("undecoded beats", BEATS["miss"])
        self.empty()

    # ---- The bench. ----
    def simulate(self, alarm_fault=False):
        text = (self.folder / f"{self.top}.v").read_text()
        own = next(port for domain, port, _ in self.clocks if domain == "main")
        clocks = [f"reg ck_{port} = 0;\nalways #{(period or 10000) // 2} ck_{port} = ~ck_{port};"
                  for _, port, period in self.clocks] + [f"wire clk = ck_{own};"]
        names = {port for _, port, _ in self.clocks}
        wires = dict(psel="sel", penable="en", pwrite="wr", paddr="addr", pready="ready",
                     prdata="rd", pslverr="err")
        conns = []
        for direction, width, name in ports(text, self.top):
            m = re.fullmatch(r"nl_i_(cfg|data)_(\w+)", name)
            t = re.fullmatch(r"nl_t_ram(\d+)_(\w+)", name)
            a = re.fullmatch(r"nl_o_log(\d+)_alarm", name)
            if name in names:
                value = "ck_" + name
            elif name == "reset_n":
                value = "reset_n"
            elif m and m[2] in wires:
                value = f"{wires[m[2]]}[{int(m[1] == 'data')}]"
            elif m and m[2] in ("pwdata", "pstrb", "pprot"):
                value = dict(pwdata="wd", pstrb="strb", pprot="prot")[m[2]]
            elif a:
                value = f"alarm[{a[1]}]"
            elif direction == "output":
                value = ""
            elif t and t[2] == "pready":
                value = "1'b1"
            elif t and t[2] == "pslverr":
                value = f"fail[{t[1]}]"
            else:
                value = f"{width}'d0"
            conns.append(f"  .{name}({value})")
        targets = max(bank.log for bank in self.banks)
        bench = (BENCH.replace("@CLOCKS@", "\n".join(clocks)).replace("@TOP@", self.top)
                 .replace("@PORTS@", ",\n".join(conns)).replace("@TARGETS@", str(targets))
                 .replace("@LIMIT@", str(len(self.lines) * 2000 * 10000))
                 .replace("@CHECKS@", "\n  ".join(self.lines)))
        work = self.folder / ("alarm_fault" if alarm_fault else "transactions")
        work.mkdir()
        (work / "tb.sv").write_text(bench)
        (work / "labels.json").write_text(json.dumps(self.labels, indent=1) + "\n")
        rtl = sim_engine.generated_rtl(self.folder / f"{self.top}.v")
        if alarm_fault:
            rtl, changed = re.subn(r"assign nl_o_log0_alarm = [^;]+;",
                                   "assign nl_o_log0_alarm = 1'b0;", rtl)
            assert changed == 1
        (work / "dut.v").write_text(rtl)
        sim = sim_engine.Simulation(work, "dut.v", compile_timeout=600, run_timeout=900)
        result = sim.run()
        failed = sorted({self.labels[int(m[1])] for m in
                         re.finditer(r"CHECK_FAIL check(\d+)", result.stdout)})
        if alarm_fault:
            assert failed == ["first alarm", "second alarm"] and "CHECK_PASS" not in result.stdout
            return len(failed)
        checks = sum(line.startswith(("access(", "took(", "alarm_is(")) for line in self.lines)
        assert result.returncode == 0 and f"CHECK_PASS checks={checks}" in result.stdout, \
            (failed[:12] or result.stdout[-2000:])
        return checks


def full(run, beats=False, records=True):
    run.check_map()
    run.check_c_header()
    run.headers()
    run.by_c_header()
    run.controls()
    if beats:
        run.latency()
    if records:
        run.records_and_clears()
    run.outside()
    return run.simulate()


def single(cli, root):
    """One bank: every check, with the access beats."""
    text, banks = definition("single", [1], revision=REVISION)
    return full(Run(cli, root, "single", text, banks, (("main", "clk", None),), 24, REVISION),
                beats=True)


def backward(cli, root):
    """One bank behind a backward packet slot: the access beats stay the same."""
    text, banks = definition("backward", [1], backward_pipe=True)
    run = Run(cli, root, "backward", text, banks, (("main", "clk", None),), 24)
    run.latency()
    return run.simulate()


def logs64(cli, root):
    """The most logs of a window, one logger each: every branch of the window's switch."""
    text, banks = definition("logs64", [1] * 64, parts=("classes", "audit_flags"))
    return full(Run(cli, root, "logs64", text, banks, (("main", "clk", None),), 24, classes=True))


def chain32(cli, root):
    """The longest chain: one log of 32 loggers, every key of a frame header."""
    text, banks = definition("chain32", [32], parts=("classes",))
    return full(Run(cli, root, "chain32", text, banks, (("main", "clk", None),), 72, classes=True))


def audit32(cli, root):
    """The most audit flags, each with a driver of its own kind: the record's audit word."""
    text, banks = definition("audit32", [2], audits=32)
    return full(Run(cli, root, "audit32", text, banks, (("main", "clk", None),), 24, audits=32))


def chains(cli, root):
    """Chains with a register stage after every eighth bank, their banks in a faster and in a
    slower clock domain than the window, so that the answers of a chain back up through its
    stages. Every bank is read, written and cleared, with idle gaps of varying length."""
    total = 0
    for loggers in (9, 16, 17, 32):
        for main, other in ((7919, 1000), (1000, 3333)):
            name = f"chain{loggers}_{main}_{other}"
            clocks = (("main", "clk", main), ("slow", "clk_slow", other))
            text, banks = definition(name, [loggers], clocks=clocks, log_domain="slow")
            run = Run(cli, root, name, text, banks, clocks, 24 + 16 * (other // main + 1) + loggers,
                      gaps=loggers * main)
            total += full(run)
    return total


def sparse(cli, root):
    """Banks placed apart in the largest window, the last one in its last block."""
    logs = [[0x0, 0x400, 0x1000], [0x8000, None], [0x10040, 0x1ffc0],
            [WINDOW_SIZE - layout.BANK_BYTES]]
    text, banks = definition("sparse", logs, parts=("whole_record",))
    return full(Run(cli, root, "sparse", text, banks, (("main", "clk", None),), 24))


def domains(cli, root):
    """Six logs of five loggers in another clock domain than the window."""
    clocks = (("main", "clk", 1250), ("slow", "clk_slow", 3333))
    text, banks = definition("domains", [5] * 6, clocks=clocks, log_domain="slow",
                             parts=("classes",))
    return full(Run(cli, root, "domains", text, banks, clocks, 24 + 16 * 3 + 40, classes=True))


# The optional parts of a bank, one definition each: the logs, what they have, a given revision.
PARTS = dict(none=([1], (), None), audit=([1], ("audit_flags",), None),
             user=([1], ("user_flags",), None), whole=([1], ("whole_record",), None),
             revision=([1], (), REVISION), chain=([3], (), None), two=([1, 1], (), None),
             every=([3, 2], ("classes", "audit_flags", "user_flags", "whole_record"), REVISION))


def parts(name):
    """Each capability bit says what its bank has, alone and all together."""
    logs, has, revision = PARTS[name]

    def case(cli, root):
        text, banks = definition("parts_" + name, logs, parts=has, revision=revision)
        run = Run(cli, root, "parts_" + name, text, banks, (("main", "clk", None),), 24, revision)
        # The data path of a definition with user flags is AXI4 or its own initiator: the bench
        # leaves it idle and makes no record there.
        return full(run, records="user_flags" not in has)
    return case


RATIOS = ((1250, 3333), (3333, 1250), (2000, 2000), (1000, 7919))


def crossed(main, slow):
    """A log in another clock domain than the window, at one clock ratio."""
    def case(cli, root):
        name = f"crossed_{main}_{slow}"
        clocks = (("main", "clk", main), ("slow", "clk_slow", slow))
        text, banks = definition(name, [2], clocks=clocks, log_domain="slow")
        return full(Run(cli, root, name, text, banks, clocks, 24 + 16 * (slow // main + 1)))
    return case


# Covered words with automatic and explicit bank placement: the loggers, the place the bank must
# take, and the offset in the window that a one-word mapping of the watched target covers.
COVERED = dict(default_bank=([1], 0x40, 4), fixed_bank=([[0]], 0, 4),
               cut_revision=([1], 0x40, 0x1c))


def covered_bench(cli, root, name):
    """The run of one covered definition, with every access of its bench."""
    offsets, expected, shadow = COVERED[name]
    enable = layout.put("alarm_enable", 1)
    clear = layout.put(layout.HELD, 1)
    text, banks = definition(name, offsets, revision=REVISION, parts=("classes",))
    fault = WINDOW + shadow
    text = text.replace("base: '0x0', size: '0x1000'",
                        f"base: '{fault:#x}', size: '0x4'")
    bank = banks[0]
    bank.base = expected
    run = Run(cli, root, name, text, banks, (("main", "clk", None),), 5,
              revision=REVISION, classes=True)
    run.check_map()
    record = run.records["csr"]
    if expected:
        assert "unreachable_words" not in record and "state_reachable" not in record
        run.read("relocated revision", run.address(bank, layout.REVISION), REVISION)
        run.read("original ID address is outside a bank", WINDOW, error=True)
    else:
        assert record["state_reachable"] is False
        assert record["unreachable_words"] == {"cfg": [4]}
    run.read("shadow selects RAM", fault, 0)
    run.read("ID", run.address(bank), run.header(bank)[0])
    if not expected:
        run.write("shadowed STATE write selects RAM", fault, enable, 2)
        run.lines.append("fail[0] = 1;")
        run.emit("failed RAM access", 0, False, fault, error=True)
        run.lines.append("fail[0] = 0;")
        run.idle(run.wait)
        run.alarm("shadowed STATE cannot enable alarm", False)
        run.read("STATE still reads RAM", fault, 0)
        run.read("captured address remains readable", run.address(bank, 8), shadow)
        return run
    run.store(bank, enable, 2, "enable alarm")
    run.beats("enable cycles", BEATS["write"])
    for phase, writing in (("first", False), ("second", True)):
        run.lines.append("fail[0] = 1;")
        run.emit(f"{phase} failed access", 0, writing, fault, error=True)
        run.lines.append("fail[0] = 0;")
        run.idle(run.wait)
        run.alarm(f"{phase} alarm", True)
        run.state(f"{phase} held", bank, held=1)
        run.beats(f"{phase} STATE cycles", BEATS["read"])
        run.read(f"{phase} captured address", run.address(bank, 8), fault & 0xff)
        run.read(f"{phase} captured direction", run.address(bank, layout.STATE),
                 layout.put("record_write", int(writing)), layout.mask("record_write"))
        run.store(bank, clear, 1, "clear held")
        run.idle(run.wait)
        run.alarm(f"{phase} cleared alarm", False)
        run.state(f"{phase} cleared held", bank)
    unused = run.address(bank, 0x3c)
    run.read("reserved word resets to zero", unused)
    run.write("write reserved word", unused, 0xffffffff)
    run.read("reserved word still reads zero", unused)
    return run


def covered(name):
    def case(cli, root):
        return covered_bench(cli, root, name).simulate()
    return case


def covered_alarm_fault(cli, root):
    """The bench of the default bank sees an alarm output that stays low."""
    assert covered_bench(cli, root, "default_bank").simulate(alarm_fault=True) == 2
    return 2


# One case per definition, so that each test simulates one network.
CASES = dict(logs64=logs64, chain32=chain32, chains=chains, audit32=audit32, sparse=sparse,
             domains=domains, single=single, backward=backward,
             **{"parts_" + name: parts(name) for name in PARTS},
             **{f"crossed_{main}_{slow}": crossed(main, slow) for main, slow in RATIOS},
             **{"covered_" + name: covered(name) for name in COVERED},
             covered_alarm_fault=covered_alarm_fault)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--case", action="append", choices=sorted(CASES))
    args = parser.parse_args()
    Path(args.work).mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=args.work))
    print(f"Work directory: {work}", flush=True)
    results = {}
    for name in args.case or sorted(CASES):
        results[name] = CASES[name](args.cli, work / name)
        print(f"{name}: {results[name]} checks", flush=True)
    (work / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print("CHECK_PASS log_bank_access")


if __name__ == "__main__":
    main()
