// Emits one asynchronous FIFO crossing and its bindings for the transaction bench.
// Optional designated faults change the emitted IR after verification so the bench
// can show that it detects them; they are never produced by the library.
#include "core/ahb_outlet.hpp"
#include "core/async_fifo.hpp"
#include "core/axi4_initiator.hpp"
#include "core/axi4_outlet.hpp"
#include "core/axi4_packetizer.hpp"
#include "core/axi4_upsize.hpp"
#include "core/axi4_width_clamp.hpp"
#include "core/clock_gating.hpp"
#include "core/exchange_composition.hpp"
#include "core/ir_component.hpp"
#include "core/link_pipeline.hpp"
#include "core/network_plan.hpp"
#include "core/network_plan_profiles.hpp"
#include "core/treadle.hpp"
#include "product_gate_miter.hpp"
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <map>
#include <nlohmann/json.hpp>
#include <nocloom/io.hpp>
#include <set>
using namespace nocloom::detail;
int generate_fifo_levels(const std::filesystem::path &, unsigned, unsigned, const std::string &);
namespace {
State &state(HardwareIR &ir, ValueId id) {
  return ir.states.at(ir.values.at(id.index).state.index);
}
bool apply_fault(AsyncFifo &f, const std::string &fault) {
  Builder b;
  b.ir = std::move(f.ir);
  const auto w = f.count_bits;
  const auto distance = b.ir.values[f.full.index].operands[0];
  const auto limit = b.ir.values[f.full.index].operands[1];
  if (fault == "binary_count") {
    // Both counts in plain binary: correct without sampling skew, unsafe with it.
    for (auto [count, event] : {std::pair{f.write_count, f.write}, std::pair{f.read_count, f.read}})
      b.update(count,
               b.select(event, b.binary(Op::Subtract, count, b.constant(w, (1U << w) - 1)), count));
    b.ir.values[distance.index].operands[0] = f.write_count;
    b.ir.values[distance.index].operands[1] = f.read_count_sync.back();
  } else if (fault == "full_late" || fault == "full_early") {
    const auto depth = fault == "full_late" ? f.depth + 1 : f.depth - 1;
    b.ir.values[limit.index].literal = depth & ((1U << w) - 1);
  } else if (fault == "no_receive_register") {
    // The FIFO head drives the outputs directly; out_valid is the raw nonempty flag.
    auto &read = b.ir.values[f.read.index];
    read.operands[1] = f.out_ready;
    for (auto &driver : b.ir.drivers) {
      if (driver.value.index == f.out_valid.index)
        driver.value = f.nonempty;
      for (const auto &field : f.fields)
        if (driver.value.index == field.output.index)
          driver.value = b.select(f.nonempty, field.selected, b.constant(field.width, 0));
    }
  } else if (fault == "early_nonempty") {
    // The receiver compares its count with the first synchronizer stage instead of the last:
    // one less synchronization stage on the read path, so a fresh word shows one edge early.
    for (auto &v : b.ir.values)
      if (v.op == Op::Equal && v.operands[0].index == f.read_count.index &&
          v.operands[1].index == f.write_count_sync.back().index)
        v.operands[1] = f.write_count_sync.front();
  } else if (fault == "last_slot_wrong") {
    // Reading the last slot returns slot 0 instead: visible only after an index wrap.
    const auto &field = f.fields[0];
    bool found = false;
    for (auto &value : b.ir.values)
      if (value.op == Op::Select && value.operands[2].index == field.storage.back().index &&
          value.operands[1].index != field.input.index) {
        value.operands[2] = field.storage.front();
        found = true;
      }
    if (!found)
      return false;
  } else if (fault == "peer_unmasked") {
    // Each side's enable ignores the other side's announce: a side reset alone leaves the
    // other side's index and count where they were.
    if (!f.reset_handshake)
      return false;
    for (const auto &h : f.handshake)
      b.ir.values[h.enable.index].operands[0] = h.on;
  } else if (fault == "count_reset") {
    // The handshake counts carry async reset pins: at a one-side reset assertion a count
    // jumps several bits at once while the other side is still enabled.
    if (!f.reset_handshake)
      return false;
    state(b.ir, f.write_count).reset = AsyncReset{f.domains[0].reset, 0};
    state(b.ir, f.read_count).reset = AsyncReset{f.domains[1].reset, 0};
  } else if (fault != "none") {
    return false;
  }
  f.ir = std::move(b.ir);
  return true;
}
// out clock_gate fault: a 16-bit counter on a gated clock, the cell model ahead of the module.
// Faults edit the emitted cell model: no_latch gates with the live enable, no_test drops test.
int clock_gate(const std::filesystem::path &out, const std::string &fault) {
  Builder b;
  b.ir.name = "nocloom_clock_gate_probe";
  auto clock = b.input("nl_clk", 1, "domain:main", "clock");
  auto reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
  auto test = b.input("test", 1, "test:test", "test");
  auto gated = b.clock_gate(clock, b.input("nl_enable", 1, "probe", "enable"), test);
  auto count = b.state(16, gated, reset);
  b.update(count, b.binary(Op::Subtract, count, b.constant(16, 0xffff)));
  b.output("nl_count", count, "probe", "count");
  if (!verify_structure(b.ir) || canonicalize(b.ir, {}, {}).empty())
    return 1;
  auto cell = cell_files(b.ir).at(0).text;
  const std::map<std::string, std::pair<std::string, std::string>> edits{
      {"no_latch",
       {"assign clock_out = clock_in & latched;",
        "assign clock_out = clock_in & (enable | test);"}},
      {"no_test", {"latched <= enable | test;", "latched <= enable;"}}};
  if (fault != "none") {
    const auto edit = edits.find(fault);
    if (edit == edits.end() || cell.find(edit->second.first) == std::string::npos)
      return 2;
    cell.replace(cell.find(edit->second.first), edit->second.first.size(), edit->second.second);
  }
  const auto gate = std::find_if(b.ir.values.begin(), b.ir.values.end(),
                                 [](const Value &v) { return v.op == Op::ClockGate; });
  std::filesystem::create_directories(out);
  std::ofstream(out / "network.v") << cell << emit_verilog(b.ir);
  std::ofstream(out / "bindings.json")
      << nlohmann::json{{"module", b.ir.name},
                        {"fault", fault},
                        {"gated", "nl_internal_v" + std::to_string(gate - b.ir.values.begin())}}
             .dump(2)
      << '\n';
  return 0;
}
// out cells: the cell files of a module with a clock gate and a synchronizer chain, unbound
// (default.v) and under each sample binding of tests/cell_library.v: bound_<name>.v the
// wrappers, models_<name>.v the library cell models generated from the binding.
int cells(const std::filesystem::path &out) {
  Builder b;
  b.ir.name = "nocloom_cells_probe";
  auto clock = b.input("nl_clk", 1, "domain:main", "clock");
  auto reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
  auto gated = b.clock_gate(clock, b.input("nl_enable", 1, "probe", "enable"),
                            b.input("test", 1, "test:test", "test"));
  auto first = b.state(1, clock, reset), last = b.state(1, clock, reset);
  b.update(first, b.input("nl_d", 1, "probe", "d"));
  b.update(last, first);
  b.ir.synchronizer_stages = {first, last};
  auto count = b.state(1, gated, reset);
  b.update(count, last);
  b.output("nl_q", count, "probe", "q");
  if (canonicalize(b.ir, {}, {}).empty() || !verify_structure(b.ir))
    return 1;
  const std::map<std::string, nocloom::TechnologyCells> bindings{
      {"te_dff",
       {nocloom::ClockGateCell{"lib_icg_te", "CP", "E", "TE", "Q"},
        nocloom::SynchronizerBinding{
            "lib_sdff_rn", 1, "CK", "D", "RN", true, "Q", {{"SE", false}, {"SI", false}}}}},
      {"rn_sync2",
       {nocloom::ClockGateCell{"lib_icg_rn", "CK", "EN", std::nullopt, "GCK", {{"RN", true}}},
        nocloom::SynchronizerBinding{"lib_sync2_r", 2, "CK", "D", "R", false, "Q"}}}};
  std::filesystem::create_directories(out);
  std::map<std::string, std::string> files;
  for (const auto &cell : cell_files(b.ir))
    files["default.v"] += cell.text;
  for (const auto &[name, binding] : bindings)
    for (const auto &cell : cell_files(b.ir, binding))
      files[(cell.library ? "models_" : "bound_") + name + ".v"] += cell.text;
  for (const auto &[name, text] : files)
    std::ofstream(out / name) << text;
  return 0;
}
// out gate_formal policy fault: a switch region as composition gates it (clock_gating.hpp): two
// inputs through register slices into one arbitrated output slice. nl_gate_safe: the enable is
// high or no gated register (slices, arbiter, gate) would change. Faults: wake_registered (the
// enable without the wake term), idle_ignores_output (the output slice never reads busy: the
// gate may close on a word its consumer can take).
int gate_formal(const std::filesystem::path &out, const std::string &policy,
                const std::string &fault) {
  Builder b;
  b.ir.name = "nocloom_gate_formal";
  auto clock = b.input("nl_clk", 1, "domain:main", "clock");
  auto reset = b.input("nl_reset_n", 1, "domain:main", "reset_n");
  auto test = b.input("test", 1, "test:test", "test");
  auto drive = [&](ValueId to, ValueId from) {
    b.ir.values.at(to.index) = {1, Op::BitOr, {from, b.constant(1, 0), {}}};
  };
  auto inverse = [&](ValueId q) { return b.unary(Op::BitNot, q, 1); };
  GateRegion region{"main", {}, {}, {}};
  std::vector<PipeStage> slices;
  std::vector<ValueId> taken;
  std::vector<treadle::Candidate> candidates;
  for (unsigned i = 0; i < 2; ++i) {
    const auto at = std::to_string(i);
    const auto valid = b.input("nl_in_valid" + at, 1, "in" + at, "valid");
    taken.push_back(b.constant(1, 0));
    slices.push_back(lower_link_pipeline(b, clock, reset, valid,
                                         {b.input("nl_in_last" + at, 1, "in" + at, "last"),
                                          b.input("nl_in_data" + at, 2, "in" + at, "data")},
                                         taken.back(), {true, true}, true));
    b.output("nl_in_ready" + at, slices.back().ready, "in" + at, "ready");
    region.wake.push_back(valid);
    for (auto q : slices.back().control)
      region.idle.push_back(inverse(q));
    candidates.push_back({slices.back().valid, slices.back().data[0]});
  }
  const auto sink_ready = b.constant(1, 0);
  ValueId sink_valid{};
  std::vector<ValueId> data{b.constant(1, 0), b.constant(2, 0)};
  treadle::Datapath path{[] {},
                         [&](unsigned i, ValueId selected) {
                           drive(taken[i], b.binary(Op::BitAnd, selected, sink_ready));
                           for (std::size_t f = 0; f < data.size(); ++f)
                             data[f] = b.select(selected, slices[i].data[f], data[f]);
                         },
                         [&](ValueId valid) {
                           sink_valid = valid;
                           return std::pair{valid, sink_ready};
                         }};
  treadle::lower_output(
      b, clock, reset, {policy == "fifo" ? treadle::Policy::Fifo : treadle::Policy::RoundRobin, {}},
      candidates, path);
  const auto o =
      lower_link_pipeline(b, clock, reset, sink_valid, data,
                          b.input("nl_out_ready", 1, "out", "ready"), {true, false}, true);
  drive(sink_ready, o.ready);
  for (auto q : fault == "idle_ignores_output" ? std::vector<ValueId>{} : o.control)
    region.idle.push_back(inverse(q));
  b.output("nl_out_valid", o.valid, "out", "valid");
  b.output("nl_out_last", o.data[0], "out", "last");
  b.output("nl_out_data", o.data[1], "out", "data");
  region.claim(b, 0);
  const auto g = lower_region_gate(b, clock, reset, test, std::nullopt, region);
  if (fault == "wake_registered") {
    auto &enable = b.ir.values[g.enable.index];
    enable.operands[1] = enable.operands[0];
  } else if (fault != "none" && fault != "idle_ignores_output")
    return 2;
  auto hold = b.constant(1, 1);
  const auto states = b.ir.states;
  for (const auto &s : states)
    if (s.clock.index == g.clock.index)
      hold = b.binary(Op::BitAnd, hold, b.binary(Op::Equal, s.next, s.output));
  b.output("nl_gate_safe", b.binary(Op::BitOr, g.enable, hold), "probe", "safe");
  b.output("nl_gate_enable", g.enable, "probe", "enable");
  b.output("nl_gate_wake", g.wake, "probe", "wake");
  b.output("nl_gate_remaining", g.remaining, "probe", "remaining");
  if (canonicalize(b.ir, {}, {}).empty() || !verify_structure(b.ir))
    return 1;
  std::filesystem::create_directories(out);
  std::ofstream(out / "generated.v") << emit_verilog(b.ir);
  return 0;
}
// Gating-safety miters (clock_gating.hpp, as composition gates a region): a component copied
// twice, copy A clocking every register and copy B, the gated one, holding each register of its
// regions while the region's enable is low, as the gate stops their clock. A register of domain d
// takes its next value only in a cycle whose nl_step<d> input is high (single-domain components
// have no step: every cycle is an edge), so the crossing explores every interleaving of its two
// clocks. Outputs: nl_gate_equal, every output of the two copies equal; nl_state_equal, every
// watched register (or, with none watched, every reset register) of copy B equal to copy A's;
// nl_init_equal, every register without a reset equal in both copies (the model assumes it at
// power-up: the gated and the ungated circuit start alike); nl_gate_rest, every gate that saw an
// idle region without a wake at its last edge still sees it idle; nl_gate_enable/wake/idle of the
// observed (last) gate.
struct Miter {
  Builder b;
  ValueId clock{}, reset{}, test{};
  std::array<std::vector<ValueId>, 2> outputs;
  std::array<std::size_t, 3> first{};                // copy A, copy B, end
  std::vector<std::pair<RegionGate, ValueId>> gates; // gate, step of its domain
  std::map<std::size_t, ValueId> steps;              // domain clock value -> step
  std::vector<std::size_t> watched;                  // states of copy B
};
// Imports ir twice; inputs maps each source input to its replacement. each(copy, imported) runs
// before a copy is finished.
template <class F>
void copy_twice(Miter &m, const HardwareIR &ir, const std::map<std::size_t, ValueId> &inputs,
                F each) {
  for (unsigned copy = 0; copy < 2; ++copy) {
    m.first[copy] = m.b.ir.states.size();
    IrComponent imported(m.b, ir);
    for (const auto &[i, value] : inputs)
      imported.replace(ValueId{i}, value);
    for (const auto &d : ir.drivers)
      m.outputs[copy].push_back(imported.get(d.value));
    each(copy, imported);
    imported.finish();
  }
  m.first[2] = m.b.ir.states.size();
  if (m.first[2] - m.first[1] != m.first[1] - m.first[0])
    throw std::logic_error("copies differ in states");
}
int emit_miter(const std::filesystem::path &out, Miter &m) {
  auto &b = m.b;
  auto both = [&](ValueId x, ValueId y) { return b.binary(Op::BitAnd, x, y); };
  auto either = [&](ValueId x, ValueId y) { return b.binary(Op::BitOr, x, y); };
  std::map<std::size_t, ValueId> edge = m.steps; // state clock -> the condition of its edge
  for (const auto &[g, step] : m.gates)
    edge[g.clock.index] = both(step, g.enable);
  auto rest = b.constant(1, 1);
  for (const auto &[g, step] : m.gates)
    rest = both(rest, either(b.slice(g.remaining, 1, 1), g.idle));
  auto state_equal = b.constant(1, 1), init_equal = b.constant(1, 1);
  for (auto i = m.first[1]; i < m.first[2]; ++i) {
    const auto &a = b.ir.states[i - (m.first[1] - m.first[0])];
    const auto same = b.binary(Op::Equal, a.output, b.ir.states[i].output);
    if (!b.ir.states[i].reset)
      init_equal = both(init_equal, same);
    if (m.watched.empty() ? b.ir.states[i].reset.has_value()
                          : std::ranges::find(m.watched, i) != m.watched.end())
      state_equal = both(state_equal, same);
  }
  for (auto &s : b.ir.states) {
    const auto at = edge.find(s.clock.index);
    if (at == edge.end())
      throw std::logic_error("A state is off every miter clock");
    s.clock = m.clock;
    s.next = b.select(at->second, s.next, s.output);
  }
  auto equal = b.constant(1, 1);
  for (std::size_t i = 0; i < m.outputs[0].size(); ++i)
    equal = both(equal, b.binary(Op::Equal, m.outputs[0][i], m.outputs[1][i]));
  const auto &g = m.gates.back().first;
  b.output("nl_gate_equal", equal, "probe", "equal");
  b.output("nl_state_equal", state_equal, "probe", "state_equal");
  b.output("nl_init_equal", init_equal, "probe", "init_equal");
  b.output("nl_gate_rest", rest, "probe", "rest");
  b.output("nl_gate_enable", g.enable, "probe", "enable");
  b.output("nl_gate_wake", g.wake, "probe", "wake");
  b.output("nl_gate_idle", g.idle, "probe", "idle");
  if (canonicalize(b.ir, {}, {}).empty())
    return 1;
  if (auto status = verify_structure(b.ir); !status) {
    std::cerr << status.diagnostics().front().message << '\n';
    return 1;
  }
  std::filesystem::create_directories(out);
  std::ofstream(out / "generated.v") << emit_verilog(b.ir);
  return 0;
}
Miter miter() {
  Miter m;
  m.b.ir.name = "nocloom_gate_formal";
  m.clock = m.b.input("nl_clk", 1, "domain:main", "clock");
  m.reset = m.b.input("nl_reset_n", 1, "domain:main", "reset_n");
  m.test = m.b.input("test", 1, "test:test", "test");
  return m;
}
// Faults of a gate: wake_registered (the enable without the wake term), idle_held (the region
// reads idle while it holds a word or a transaction step).
void wake_registered(Builder &b, const RegionGate &g) {
  auto &enable = b.ir.values[g.enable.index];
  enable.operands[1] = enable.operands[0];
}
// Named states the helper equality leaves out: an initiator's write beat address and remaining
// count follow its AXI or APB inputs while it rests.
constexpr std::string_view loose_states = "wr.beat_addr wr.remaining";
// A packetizer with several bursts in flight latches the down_rrsp face into r.word.* on every
// cycle no word is held (r.word_full, one of its rest states, is low). Only a held word reaches
// an output, and it is latched on the beat that holds it, which wakes the zone.
constexpr std::string_view loose_packetizer_prefix = "r.word.";
// One endpoint component of a definition, lowered as composition lowers it, with the rest rules
// and free states of its zone. name: initiator:<source> (protocol zone), packets:<source> or
// conversion:<source> (transport zone of the source), outlet:<target> (protocol zone) or
// merge:<target> (transport zone of the target).
struct Zone {
  std::optional<NodeHardware> hw;
  std::string_view rules, free;
};
Zone lower_zone(const NetworkPlan &plan, const std::string &name) {
  const auto colon = name.find(':');
  const auto kind = name.substr(0, colon), endpoint = name.substr(colon + 1);
  auto take = [](nocloom::Result<NodeHardware> r, std::string_view rules,
                 std::string_view free = {}) {
    return r ? Zone{std::move(r).value(), rules, free} : Zone{};
  };
  for (unsigned si = 0; si < plan.sources.size(); ++si) {
    const auto &sp = plan.sources[si];
    if (sp.name != endpoint)
      continue;
    if (kind == "initiator") {
      auto lowered = lower_axi4_initiator(initiator_profile(plan, si));
      const bool multi = lowered && lowered.value().bindings.contains("rr.offered");
      return take(std::move(lowered), multi ? initiator_multi_rest : initiator_rest);
    }
    if (kind == "packets" && sp.packets)
      return take(lower_axi4_packetizer(packet_profile(plan, si)), packetizer_rest);
    if (kind == "conversion" && sp.clamp)
      return take(lower_axi4_width_clamp(clamp_profile(plan, si, true)), width_cut_rest);
    if (kind == "conversion" && !plan.per_endpoint && sp.engine_bits != plan.burst_data_bits) {
      Axi4WidthCutProfile cut;
      cut.name = "s" + std::to_string(si);
      cut.module_prefix = "nocloom_axi4_width_cut_";
      cut.id_bits = plan.burst_id_bits;
      cut.len_bits = plan.burst_len_bits;
      cut.address_bits = plan.burst_address_bits;
      cut.source_bits = plan.source_bits;
      cut.data_bits = sp.engine_bits;
      cut.down_bits = 32;
      cut.tag_bits = plan.burst_tag_bits;
      cut.fixed_width = 2;
      cut.fixed_reject = plan.windows();
      cut.zero_failed_beats = true;
      cut.exclusive = plan.exclusive;
      cut.max_outstanding = sp.outstanding;
      cut.packet_depth = sp.packets ? 4U : 1U;
      return take(lower_axi4_width_cut(cut), width_cut_rest);
    }
  }
  for (unsigned o = 0; o < plan.targets.size(); ++o) {
    if (plan.targets[o].name != endpoint)
      continue;
    if (kind == "merge" && plan.targets[o].upsize)
      return take(lower_axi4_upsize(upsize_profile(plan, o)), upsize_rest);
    if (kind != "outlet")
      continue;
    if (apb_target(plan, endpoint))
      return take(lower_apb_burst_outlet(outlet_profile(plan, o)), outlet_rest, outlet_free);
    if (lite_target(plan, endpoint))
      return take(lower_axi_lite_outlet(outlet_profile(plan, o)), outlet_rest, outlet_free);
    if (ahb_target(plan, endpoint))
      return take(lower_ahb_outlet(outlet_profile(plan, o)), outlet_rest, outlet_free);
    return take(lower_axi4_outlet(axi4_outlet_profile(plan, o)), outlet_rest, outlet_free);
  }
  return {};
}
// out endpoint_formal config component fault: an endpoint component of the definition config
// (lower_zone) in its zone. A valid entering the component (a request or response valid of its
// faces, an AXI AWVALID/WVALID/ARVALID/BVALID/RVALID or an APB PSEL) wakes it, as composition
// wakes the zone; so does a merged read's tail (hint read_tail) at a one-burst outlet.
int endpoint_formal(const std::filesystem::path &out, const std::filesystem::path &config,
                    const std::string &component, const std::string &fault) {
  auto parsed = nocloom::io::load_definition(config);
  if (!parsed)
    return 2;
  auto plan = build_network_plan(parsed.value().definition);
  if (!plan)
    return 2;
  const auto zone = lower_zone(plan.value(), component);
  if (!zone.hw || (fault != "none" && fault != "wake_registered" && fault != "idle_held"))
    return 2;
  const auto &c = *zone.hw;
  auto m = miter();
  auto &b = m.b;
  static const std::set<std::string> wakes{"valid",  "awvalid", "wvalid", "arvalid",
                                           "bvalid", "rvalid",  "psel",   "read_tail"};
  std::map<std::size_t, ValueId> inputs;
  std::optional<ValueId> tail;
  auto wake = b.constant(1, 0);
  for (std::size_t i = 0; i < c.ir.values.size(); ++i)
    if (const auto &v = c.ir.values[i]; v.op == Op::Input) {
      const auto &port = c.ir.ports[v.input_port.index];
      inputs[i] = port.signal == "clock" ? m.clock
                  : port.signal == "reset_n"
                      ? m.reset
                      : b.input(port.name, v.width, port.endpoint, port.signal);
      if (wakes.contains(port.signal))
        wake = b.binary(Op::BitOr, wake, inputs[i]);
      if (port.signal == "read_tail")
        tail = inputs[i];
    }
  std::map<std::string, ValueId> named;
  auto burst_invariant = b.constant(1, 1);
  const bool ahb_blocks = c.bindings.contains("bus.burst");
  copy_twice(m, c.ir, inputs, [&](unsigned copy, IrComponent &imported) {
    // Both copies look up the same values, so they import their states in the same order.
    std::map<std::string, ValueId> states;
    for (const auto &[key, id] : c.bindings)
      if (c.ir.values.at(id.index).op == Op::State)
        states[key] = imported.get(id);
    if (ahb_blocks) {
      const auto both = [&](ValueId a, ValueId d) { return b.binary(Op::BitAnd, a, d); };
      const auto nonzero = [&](ValueId q) {
        return b.unary(Op::BitNot,
                       b.binary(Op::Equal, q, b.constant(b.ir.values[q.index].width, 0)), 1);
      };
      const auto implies = [&](ValueId a, ValueId d) {
        return b.binary(Op::BitOr, b.unary(Op::BitNot, a, 1), d);
      };
      // The block's remaining transfers fit inside its owning packet. BUSY owes a transfer
      // only while that packet retains the bus, including the orphan after a timeout.
      burst_invariant =
          both(burst_invariant, implies(states.at("bus.burst"), states.at("bus.open")));
      const auto block = [&](const std::string &prefix, ValueId live) {
        const auto left = states.at(prefix + ".left");
        const auto after = states.at(prefix + ".block_left");
        auto legal =
            both(nonzero(states.at(prefix + ".hburst")), nonzero(states.at(prefix + ".block")));
        legal = both(legal, b.unary(Op::BitNot, b.binary(Op::UnsignedLess, left, after), 1));
        burst_invariant =
            both(burst_invariant, implies(both(live, states.at(prefix + ".seq")), legal));
      };
      block("r", b.constant(1, 1));
      for (unsigned k = 0; k < 3; ++k) {
        const auto prefix = "w.context" + std::to_string(k);
        block(prefix, states.at(prefix + ".valid"));
      }
    }
    if (copy == 1)
      named = std::move(states);
  });
  if (ahb_blocks)
    b.output("nl_burst_invariant", burst_invariant, "probe", "burst_invariant");
  // The helper equality covers every named reset state but the loose ones, which may load a value
  // no output shows while their zone rests.
  std::set<std::size_t> loose;
  each_rest(loose_states, named,
            [&](ValueId q, unsigned) { loose.insert(b.ir.values.at(q.index).state.index); });
  if (component.starts_with("packets:"))
    for (const auto &[key, q] : named)
      if (key.starts_with(loose_packetizer_prefix))
        loose.insert(b.ir.values.at(q.index).state.index);
  for (const auto &[key, q] : named)
    if (const auto i = b.ir.values.at(q.index).state.index;
        b.ir.states[i].reset && !loose.contains(i))
      m.watched.push_back(i);
  // The merge's last beat of a read leaves only for a burst in the pool. This is an environment
  // assumption of the side proof, discharged by simulation only; the proof takes the wake from this
  // harness, so it cannot detect a composition that misses the read tail's wake.
  if (tail) {
    const auto pool = named.at("pool.count");
    const auto empty = b.binary(Op::Equal, pool, b.constant(b.ir.values.at(pool.index).width, 0));
    b.output("nl_tail_legal",
             b.binary(Op::BitOr, b.unary(Op::BitNot, *tail, 1), b.unary(Op::BitNot, empty, 1)),
             "probe", "tail_legal");
  }
  GateRegion region{"main", {}, {}, {wake}};
  region.claim(b, m.first[1], named, zone.free);
  region.idle.push_back(fault == "idle_held" ? b.constant(1, 1) : at_rest(b, named, zone.rules));
  m.gates.emplace_back(lower_region_gate(b, m.clock, m.reset, m.test, std::nullopt, region),
                       b.constant(1, 1));
  if (fault == "wake_registered")
    wake_registered(b, m.gates.back().first);
  m.steps[m.clock.index] = b.constant(1, 1);
  return emit_miter(out, m);
}
// out crossing_formal mode fault: an asynchronous FIFO crossing (depth 2, 2 synchronizer stages,
// one 2-bit field) with both sides gated as composition gates them in their switches' regions
// (gate_crossing): the sender's index, count and slots wake on the input valid; the receiver's
// index, count and forward register rest without a word and wake while the FIFO is not empty;
// with the reset handshake (mode handshake) a side whose enable is low wakes too. Each side
// resets with its own nl_reset_<d>_n. Faults on the receiver gate: wake_registered (the enable
// loses the wake term), idle_held (the region reads idle while it holds a word) and wake_delayed
// (the wake one receiver-clock edge late, so the gate opens a cycle after the data arrives).
int crossing_formal(const std::filesystem::path &out, const std::string &mode,
                    const std::string &fault) {
  const bool handshake = mode == "handshake";
  auto lowered = lower_async_fifo({"a"}, {"b"}, 2, 2, {{"data", 2}}, handshake, true);
  if (!lowered || (mode != "joint" && !handshake) ||
      (fault != "none" && fault != "wake_registered" && fault != "idle_held" &&
       fault != "wake_delayed"))
    return 2;
  const auto &f = lowered.value();
  auto m = miter();
  auto &b = m.b;
  std::array<ValueId, 2> clocks{}, resets{}, steps{};
  for (unsigned d = 0; d < 2; ++d) {
    const auto at = std::to_string(d);
    clocks[d] = b.input("nl_clk_" + at, 1, "domain:" + at, "clock");
    resets[d] = b.input("nl_reset_" + at + "_n", 1, "domain:" + at, "reset_n");
    steps[d] = b.input("nl_step" + at, 1, "domain:" + at, "step");
    m.steps[clocks[d].index] = steps[d];
  }
  std::map<std::size_t, ValueId> inputs;
  for (std::size_t i = 0; i < f.ir.values.size(); ++i)
    if (const auto &v = f.ir.values[i]; v.op == Op::Input) {
      const auto &port = f.ir.ports[v.input_port.index];
      inputs[i] = i == f.domains[0].clock.index   ? clocks[0]
                  : i == f.domains[1].clock.index ? clocks[1]
                  : i == f.domains[0].reset.index ? resets[0]
                  : i == f.domains[1].reset.index
                      ? resets[1]
                      : b.input(port.name, v.width, port.endpoint, port.signal);
    }
  std::array<GateRegion, 2> regions;
  ValueId nonempty_observed{};
  copy_twice(m, f.ir, inputs, [&](unsigned copy, IrComponent &imported) {
    // Both copies look up the same values, so they import their states in the same order.
    std::array<GateRegion, 2> r{GateRegion{"a", {}, {}, {}}, GateRegion{"b", {}, {}, {}}};
    auto own = [&](GateRegion &region, ValueId q) {
      region.states.push_back(b.ir.values.at(imported.get(q).index).state.index);
    };
    for (auto q : {f.write_index, f.write_count})
      own(r[0], q);
    for (auto q : {f.read_index, f.read_count, f.out_valid})
      own(r[1], q);
    for (const auto &field : f.fields) {
      for (auto q : field.storage)
        own(r[0], q);
      own(r[1], field.received);
    }
    r[0].wake.push_back(imported.get(f.valid));
    r[1].idle.push_back(b.unary(Op::BitNot, imported.get(f.out_valid), 1));
    r[1].wake.push_back(imported.get(f.nonempty));
    for (unsigned d = 0; handshake && d < 2; ++d)
      r[d].wake.push_back(b.unary(Op::BitNot, imported.get(f.handshake[d].enable), 1));
    if (copy == 1) {
      nonempty_observed = imported.get(f.nonempty);
      regions = std::move(r);
    }
  });
  if (fault == "idle_held")
    regions[1].idle = {b.constant(1, 1)};
  if (fault == "wake_delayed") {
    // The delay runs on the ungated domain clock: it must not hold with the receiver's gate.
    auto late = b.state(1, clocks[1], resets[1]);
    b.update(late, nonempty_observed);
    regions[1].wake[0] = late;
  }
  for (unsigned d = 0; d < 2; ++d)
    m.gates.emplace_back(
        lower_region_gate(b, clocks[d], resets[d], m.test, std::nullopt, regions[d]), steps[d]);
  if (fault == "wake_registered")
    wake_registered(b, m.gates.back().first);
  return emit_miter(out, m);
}
} // namespace
int main(int argc, char **argv) {
  if (argc == 6 && std::string(argv[2]) == "levels_formal")
    return generate_fifo_levels(argv[1], static_cast<unsigned>(std::stoul(argv[3])),
                                static_cast<unsigned>(std::stoul(argv[4])), argv[5]);
  if ((argc == 5 || argc == 6) && std::string(argv[2]) == "product_formal") {
    if (argc == 6 && std::string(argv[5]) != "full")
      return 2;
    return product_gate_miter(argv[1], argv[3], argv[4], argc == 5);
  }
  if (argc == 6 && std::string(argv[2]) == "endpoint_formal")
    return endpoint_formal(argv[1], argv[3], argv[4], argv[5]);
  if (argc == 5 && std::string(argv[2]) == "crossing_formal")
    return crossing_formal(argv[1], argv[3], argv[4]);
  if (argc == 3 && std::string(argv[2]) == "cells")
    return cells(argv[1]);
  if (argc == 4 && std::string(argv[2]) == "clock_gate")
    return clock_gate(argv[1], argv[3]);
  if (argc == 5 && std::string(argv[2]) == "gate_formal")
    return gate_formal(argv[1], argv[3], argv[4]);
  // out depth stages fault [narrow] [handshake] [minimal]; "narrow" selects data[7:0], tag[0:0]
  // for formal runs, "handshake" the reset handshake, "minimal" payload registers without resets.
  std::set<std::string> options(argv + std::min(argc, 5), argv + argc);
  const bool narrow = options.erase("narrow") > 0, handshake = options.erase("handshake") > 0;
  const bool minimal = options.erase("minimal") > 0;
  if (argc < 5 || !options.empty())
    return 2;
  try {
    const auto number = [&](int i) { return static_cast<unsigned>(std::stoul(argv[i])); };
    const auto depth = number(2), n = number(3);
    const std::string fault = argv[4];
    const auto layout =
        narrow ? std::vector<std::pair<std::string, unsigned>>{{"data", 8}, {"tag", 1}}
               : std::vector<std::pair<std::string, unsigned>>{{"data", 32}, {"tag", 5}};
    auto lowered = lower_async_fifo({"a"}, {"b"}, depth, n, layout, handshake, !minimal);
    if (!lowered) {
      for (const auto &d : lowered.diagnostics())
        std::cerr << d.code << ": " << d.message << '\n';
      return 1;
    }
    auto f = std::move(lowered).value();
    if (!apply_fault(f, fault))
      return 2;
    auto id = [](ValueId v) { return v.index; };
    nlohmann::json j = {{"module", f.ir.name},
                        {"depth", f.depth},
                        {"count_bits", f.count_bits},
                        {"n", n},
                        {"fault", fault},
                        {"reset_all", !minimal},
                        {"clocks", {id(f.domains[0].clock), id(f.domains[1].clock)}},
                        {"resets", {id(f.domains[0].reset), id(f.domains[1].reset)}},
                        {"write_count", id(f.write_count)},
                        {"read_count", id(f.read_count)},
                        {"write", id(f.write)},
                        {"read", id(f.read)},
                        {"out_valid_state", id(f.out_valid)}};
    // Every synchronizer chain's first stage samples another domain's register.
    auto crossing = [&](const std::vector<ValueId> &chain, unsigned domain) {
      const auto first = chain.front();
      j["first_stages"].push_back({{"stage", id(first)},
                                   {"source", id(state(f.ir, first).next)},
                                   {"width", f.ir.values[first.index].width},
                                   {"domain", domain}});
    };
    crossing(f.write_count_sync, 1);
    crossing(f.read_count_sync, 0);
    j["handshake"] = nlohmann::json::array();
    for (unsigned d = 0; f.reset_handshake && d < 2; ++d) {
      const auto &h = f.handshake[d];
      crossing(h.peer_sync, d);
      crossing(h.ack_sync, d);
      j["handshake"].push_back({{"announce", id(h.announce)},
                                {"on", id(h.on)},
                                {"wait", id(h.wait)},
                                {"enable", id(h.enable)}});
    }
    j["write_index"] = id(f.write_index);
    j["read_index"] = id(f.read_index);
    for (const auto &field : f.fields) {
      nlohmann::json slots = nlohmann::json::array();
      std::transform(field.storage.begin(), field.storage.end(), std::back_inserter(slots), id);
      j["fields"].push_back({{"name", field.name},
                             {"width", field.width},
                             {"input", id(field.input)},
                             {"storage", slots},
                             {"received", id(field.received)}});
    }
    for (const auto &port : f.ir.ports)
      j["ports"].push_back({{"name", port.name}, {"width", port.width}, {"input", port.input}});
    for (const auto &s : f.ir.states)
      j["states"].push_back({{"output", id(s.output)},
                             {"next", id(s.next)},
                             {"clock", id(s.clock)},
                             {"reset", s.reset ? nlohmann::json(id(s.reset->signal)) : nullptr}});
    const auto out = std::filesystem::path(argv[1]);
    std::filesystem::create_directories(out);
    std::ofstream(out / "generated.v") << emit_verilog(f.ir);
    std::ofstream(out / "cells.v") << cell_files(f.ir).at(0).text;
    std::ofstream(out / "bindings.json") << j.dump(2) << '\n';
  } catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
