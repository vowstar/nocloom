// Compare the generated target's two clock regions with the same hardware clocked freely.
// Both domain edges are independent inputs. Other regions pass every domain edge in both
// copies, retaining the complete request and response crossings and all endpoint wiring.
#pragma once
#include "core/exchange_composition.hpp"
#include "core/ir_component.hpp"
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <nlohmann/json.hpp>
#include <nocloom/io.hpp>

inline int product_gate_miter(const std::filesystem::path &directory,
                              const std::filesystem::path &config, const std::string &fault,
                              bool share_context = true) {
  using namespace nocloom::detail;
  auto parsed = nocloom::io::load_definition(config);
  if (!parsed)
    return 2;
  auto planned = build_network_plan(parsed.value().definition);
  if (!planned)
    return 2;
  auto lowered = lower_axi4_exchange_composition(planned.value());
  if (!lowered) {
    for (const auto &d : lowered.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    return 2;
  }
  const auto &hw = lowered.value();
  auto ir = hw.ir;
  // Import state leaves without recursing from their gate clock back into that state.
  // Domain events below restore the original gate condition for the gated copy.
  for (auto &s : ir.states)
    s.clock = root_clock(ir, s.clock);
  Builder b;
  b.ir.name = "nocloom_product_gate_miter";
  const auto tick = b.input("nl_tick", 1, "domain:formal", "clock");
  const auto zero = b.constant(1, 0), one = b.constant(1, 1);
  auto both = [&](ValueId a, ValueId c) { return b.binary(Op::BitAnd, a, c); };
  auto either = [&](ValueId a, ValueId c) { return b.binary(Op::BitOr, a, c); };
  auto same = [&](ValueId a, ValueId c) { return b.binary(Op::Equal, a, c); };
  auto implies = [&](ValueId a, ValueId c) { return either(b.unary(Op::BitNot, a, 1), c); };
  std::map<std::size_t, ValueId> inputs, steps;
  std::map<std::string, std::string> clock_steps;
  unsigned domain = 0;
  for (std::size_t i = 0; i < ir.values.size(); ++i) {
    const auto &v = ir.values[i];
    if (v.op != Op::Input)
      continue;
    const auto &p = ir.ports.at(v.input_port.index);
    if (p.signal == "clock") {
      inputs[i] = tick;
      const auto step = "nl_step" + std::to_string(domain++);
      steps[i] = b.input(step, 1, "formal", step);
      clock_steps[p.name] = step;
    } else if (p.signal == "test" || p.signal == "ungate") {
      inputs[i] = zero;
    } else {
      inputs[i] = b.input(p.name, v.width, p.endpoint, p.signal);
    }
  }
  if (domain != 2)
    throw std::logic_error("product miter requires two domains");
  const auto protocol = std::string("gate/target/mem/protocol");
  const auto transport = std::string("gate/target/mem");
  const auto busy = hw.bindings.find("burst_service/mem/merge/r.busy");
  const auto full = hw.bindings.at("burst_service/mem/r.full");
  const auto tail = hw.bindings.at("burst_service/mem/outlet.read_tail");
  const auto valid = hw.bindings.at("burst_service/mem/outlet.read_valid");
  const bool occupancy_tail = hw.plan.error_codes && busy != hw.bindings.end();
  const auto tail_term = occupancy_tail ? busy->second : tail;
  const auto face_term = hw.plan.error_codes ? full : valid;
  const auto emitted_state = [&](const State &s) {
    return s.output.index < ir.emitted.value_or(ir.values.size());
  };
  const auto is_target = [&](const State &s) {
    const auto clock = hw.ir.states.at(ir.values.at(s.output.index).state.index).clock;
    return emitted_state(s) && (clock.index == hw.bindings.at(protocol + ".clock").index ||
                                clock.index == hw.bindings.at(transport + ".clock").index);
  };
  if (fault != "none" && fault != "tail_zero" && fault != "face_zero" && fault != "tail_narrow" &&
      fault != "face_narrow" && fault != "tail_or" && fault != "face_or" &&
      fault != "tail_term_zero" && fault != "face_term_zero")
    return 2;
  std::array<std::map<std::size_t, ValueId>, 2> states;
  std::array<std::vector<ValueId>, 2> outputs;
  auto init_equal = one;
  auto context_equal = one;
  std::map<std::size_t, ValueId> next_a;
  std::map<std::string, ValueId> probes;
  std::map<std::string, ValueId> source_observations;
  for (unsigned copy = 0; copy < 2; ++copy) {
    IrComponent imported(b, ir);
    for (const auto &[id, value] : inputs)
      imported.replace(ValueId{id}, value);
    // Share the surrounding circuit's current state, and assert equality of every shared
    // next value below. This relational invariant justifies the sharing at every domain edge.
    if (copy == 1 && share_context)
      for (const auto &s : ir.states)
        if (emitted_state(s) && !is_target(s))
          imported.replace(s.output, states[0].at(s.output.index));
    // Disable the complete wired wake or its read term, or restore the previous term.
    // The rest of the gate, including its registered activity, stays intact.
    if (copy == 1 && fault != "none") {
      const bool is_tail = fault.starts_with("tail_");
      const auto at = hw.bindings.at((is_tail ? protocol : transport) + ".wake");
      const auto term = is_tail ? tail_term : face_term;
      auto replacement = fault.ends_with("term_zero") ? zero : imported.get(is_tail ? tail : valid);
      if (fault.ends_with("or"))
        replacement = either(imported.get(term), replacement);
      unsigned hits = 0;
      std::function<ValueId(ValueId)> rewrite = [&](ValueId id) -> ValueId {
        if (id.index == term.index) {
          ++hits;
          return replacement;
        }
        const auto &v = ir.values.at(id.index);
        if (v.op != Op::BitOr)
          return imported.get(id);
        const auto a = rewrite(v.operands[0]), c = rewrite(v.operands[1]);
        return either(a, c);
      };
      const bool whole_zero = fault == "tail_zero" || fault == "face_zero";
      const auto changed = whole_zero ? zero : rewrite(at);
      if (!whole_zero && hits != 1)
        throw std::logic_error("wake term must occur exactly once");
      imported.replace(at, changed);
    }
    for (const auto &s : ir.states)
      if (emitted_state(s))
        states[copy][s.output.index] = imported.get(s.output);
    for (const auto &s : ir.states) {
      if (!emitted_state(s))
        continue;
      const auto next = imported.get(s.next);
      if (copy == 0)
        next_a[s.output.index] = next;
      else if (share_context && !is_target(s))
        context_equal = both(context_equal, same(next_a.at(s.output.index), next));
    }
    for (const auto &d : ir.drivers) {
      outputs[copy].push_back(imported.get(d.value));
      const auto &p = ir.ports.at(d.port.index);
      if (copy == 0 && p.endpoint == "target:mem" &&
          (p.signal == "arvalid" || p.signal == "arid" || p.signal == "arlen" ||
           p.signal == "rready"))
        b.output("nl_env_" + p.signal, imported.get(d.value), "formal", "env_" + p.signal);
      if (copy == 0 && (p.name == "nl_i_cpu_arready" || p.name == "nl_i_cpu_rvalid" ||
                        p.name == "nl_i_cpu_rlast"))
        source_observations[p.signal] = imported.get(d.value);
    }
    const auto qtail = imported.get(tail), qvalid = imported.get(valid);
    if (copy == 0) {
      const auto pending = imported.get(hw.bindings.at("burst_service/mem/w.aw_pending"));
      probes["nl_aw_pending_busy"] =
          implies(pending, imported.get(hw.bindings.at("burst_service/mem/w.busy")));
      probes["nl_w_valid_full"] = implies(imported.get(hw.bindings.at("burst_service/mem/w.valid")),
                                          imported.get(hw.bindings.at("burst_service/mem/w.full")));
      if (hw.bindings.contains("burst_service/mem/r.slot0.valid")) {
        const auto count = imported.get(hw.bindings.at("burst_service/mem/r.count"));
        const auto unissued = imported.get(hw.bindings.at("burst_service/mem/r.unissued"));
        const auto width = b.ir.values.at(count.index).width;
        auto populated = b.constant(width, 0), waiting = populated;
        std::vector<ValueId> valid_slots, issued_slots;
        for (unsigned k = 0; k < hw.plan.targets.front().outstanding; ++k) {
          const auto prefix = "burst_service/mem/r.slot" + std::to_string(k);
          const auto used = imported.get(hw.bindings.at(prefix + ".valid"));
          const auto issued = imported.get(hw.bindings.at(prefix + ".issued"));
          valid_slots.push_back(used);
          issued_slots.push_back(issued);
          const auto add = [&](ValueId sum, ValueId bit) {
            return b.binary(
                Op::Subtract, sum,
                b.binary(Op::Subtract, b.constant(width, 0), b.unary(Op::ZeroExtend, bit, width)));
          };
          populated = add(populated, used);
          waiting = add(waiting, both(used, b.unary(Op::BitNot, issued, 1)));
        }
        probes["nl_read_count_slots"] = same(count, populated);
        probes["nl_read_unissued_slots"] = same(unissued, waiting);
        probes["nl_read_one_unissued"] =
            b.unary(Op::BitNot, b.binary(Op::UnsignedLess, b.constant(width, 1), unissued), 1);
        const auto owner = [&](ValueId index, bool needs_issue) {
          auto found = zero;
          for (unsigned k = 0; k < valid_slots.size(); ++k) {
            const auto here = same(index, b.constant(b.ir.values.at(index.index).width, k));
            const auto flag =
                needs_issue ? issued_slots[k] : b.unary(Op::BitNot, issued_slots[k], 1);
            found = either(found, both(here, both(valid_slots[k], flag)));
          }
          return found;
        };
        probes["nl_read_issue_owner"] =
            implies(b.unary(Op::BitNot, same(unissued, b.constant(width, 0)), 1),
                    owner(imported.get(hw.bindings.at("burst_service/mem/r.issue")), false));
        auto read = imported.get(full);
        if (const auto write = hw.bindings.find("burst_service/mem/r.out_write");
            write != hw.bindings.end())
          read = both(read, b.unary(Op::BitNot, imported.get(write->second), 1));
        probes["nl_read_main_owner"] = implies(
            read, owner(imported.get(hw.bindings.at("burst_service/mem/r.response_index")), true));
        if (const auto spare = hw.bindings.find("burst_service/mem/r.spare");
            spare != hw.bindings.end() && hw.bindings.contains("burst_service/mem/r.spare_index"))
          probes["nl_read_spare_owner"] =
              implies(imported.get(spare->second),
                      owner(imported.get(hw.bindings.at("burst_service/mem/r.spare_index")), true));
      }
    }
    probes["nl_tail_implies_busy_" + std::to_string(copy)] =
        busy == hw.bindings.end() ? one : implies(qtail, imported.get(busy->second));
    probes["nl_valid_implies_full_" + std::to_string(copy)] = implies(qvalid, imported.get(full));
    for (const auto &key : {protocol, transport}) {
      const auto suffix = key == protocol ? std::string("tail") : std::string("face");
      const auto wake = imported.get(hw.bindings.at(key + ".wake"));
      const auto enable = imported.get(hw.bindings.at(key + ".enable"));
      if (copy == 0) {
        const auto remaining = imported.get(hw.bindings.at(key + ".remaining"));
        const auto idle = imported.get(hw.bindings.at(key + ".idle"));
        probes["nl_" + suffix + "_rest_a"] = either(b.slice(remaining, 1, 1), idle);
      }
      if (copy == 1) {
        const auto remaining = imported.get(hw.bindings.at(key + ".remaining"));
        probes["nl_" + suffix + "_covered"] =
            implies(imported.get(suffix == "tail" ? tail_term : face_term), wake);
        probes["nl_" + suffix + "_closed"] = b.unary(Op::BitNot, enable, 1);
        probes["nl_" + suffix + "_wake"] =
            both(wake, b.binary(Op::Equal, remaining, b.constant(2, 0)));
        probes["nl_" + suffix + "_extra"] =
            both(imported.get(suffix == "tail" ? tail_term : face_term),
                 b.unary(Op::BitNot, suffix == "tail" ? qtail : qvalid, 1));
      }
    }
    if (copy == 1) {
      probes["nl_read_delivered"] = imported.get(hw.bindings.at("source/cpu/read.consume"));
      probes["nl_decode_packet"] = both(
          imported.get(hw.bindings.at("source/cpu/read.return_valid")),
          same(imported.get(hw.bindings.at("source/cpu/read.return_result")), b.constant(2, 3)));
    }
    imported.finish();
    for (const auto &s : ir.states) {
      if (!emitted_state(s) || (copy == 1 && share_context && !is_target(s)))
        continue;
      const auto q = states[copy].at(s.output.index);
      const auto si = b.ir.values.at(q.index).state.index;
      const auto clock = hw.ir.states.at(ir.values.at(s.output.index).state.index).clock;
      auto edge = steps.at(root_clock(ir, clock).index);
      if (copy == 0 && is_target(s)) {
        const auto &gate = ir.values.at(clock.index);
        const auto enable = either(imported.get(gate.operands[1]), imported.get(gate.operands[2]));
        probes["nl_hold_a" + std::to_string(s.output.index)] =
            either(enable, same(imported.get(s.next), q));
      }
      if (copy == 1 && (clock.index == hw.bindings.at(protocol + ".clock").index ||
                        clock.index == hw.bindings.at(transport + ".clock").index)) {
        const auto &gate = ir.values.at(clock.index);
        const auto enable = either(imported.get(gate.operands[1]), imported.get(gate.operands[2]));
        edge = both(edge, enable);
      }
      auto &qs = b.ir.states.at(si);
      qs.clock = tick;
      qs.next = b.select(edge, qs.next, q);
      if (copy == 1 && !s.reset)
        init_equal = both(init_equal, same(states[0].at(s.output.index), q));
      if (copy == 1 && is_target(s)) {
        probes["nl_q" + std::to_string(s.output.index) + "_equal"] =
            same(states[0].at(s.output.index), q);
      }
    }
  }
  if (b.ir.states.size() !=
      static_cast<std::size_t>(std::ranges::count_if(ir.states, emitted_state) +
                               (share_context ? std::ranges::count_if(ir.states, is_target)
                                              : std::ranges::count_if(ir.states, emitted_state))))
    throw std::logic_error("miter observations imported a register absent from the product RTL");
  auto equal = one;
  for (std::size_t i = 0; i < outputs[0].size(); ++i)
    equal = both(equal, same(outputs[0][i], outputs[1][i]));
  b.output("nl_gate_equal", equal, "formal", "equal");
  b.output("nl_init_equal", init_equal, "formal", "init_equal");
  b.output("nl_context_equal", context_equal, "formal", "context_equal");
  for (const auto &[name, value] : probes)
    b.output(name, value, "formal", name);
  for (const auto &[name, value] : source_observations)
    b.output("nl_env_source_" + name, value, "formal", "env_source_" + name);
  const auto mapped = canonicalize(b.ir, {}, {});
  if (mapped.empty()) {
    std::cerr << "product miter canonicalization failed\n";
    return 1;
  }
  if (auto status = verify_structure(b.ir); !status) {
    for (const auto &d : status.diagnostics())
      std::cerr << d.code << ": " << d.message << '\n';
    return 1;
  }
  std::filesystem::create_directories(directory);
  std::ofstream(directory / "generated.v") << emit_verilog(b.ir);
  std::ofstream(directory / "product.v") << emit_verilog(hw.ir);
  std::ofstream cells(directory / "cells.v");
  for (const auto &file : cell_files(hw.ir))
    cells << file.text;
  nlohmann::json metadata{{"fault", fault},
                          {"occupancy_tail", occupancy_tail},
                          {"occupancy_face", hw.plan.error_codes},
                          {"states", std::ranges::count_if(ir.states, emitted_state)},
                          {"gated_states", std::ranges::count_if(ir.states, is_target)},
                          {"context_shared", share_context},
                          {"clock_steps", clock_steps}};
  metadata["has_busy"] = busy != hw.bindings.end();
  metadata["outputs"] = nlohmann::json::object();
  for (std::size_t i = 0; i < ir.drivers.size(); ++i) {
    const auto &port = ir.ports.at(ir.drivers[i].port.index);
    auto id = [&](unsigned copy) -> nlohmann::json {
      const auto at = mapped.at(outputs[copy][i].index);
      return at == removed_value ? nlohmann::json(nullptr) : nlohmann::json(at);
    };
    metadata["outputs"][port.name] = {
        {"signal", port.signal}, {"width", port.width}, {"ungated", id(0)}, {"gated", id(1)}};
  }
  metadata["bindings"] = nlohmann::json::object();
  for (const auto &[key, id] : hw.bindings)
    metadata["bindings"][key] = id.index;
  std::ofstream(directory / "bindings.json") << metadata.dump(2) << '\n';
  return 0;
}
