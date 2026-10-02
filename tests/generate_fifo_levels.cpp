#include "core/async_fifo.hpp"
#include "core/fifo_levels.hpp"
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <nlohmann/json.hpp>

using namespace nocloom::detail;

int generate_fifo_levels(const std::filesystem::path &out, unsigned depth, unsigned stages,
                         const std::string &fault) {
  if (fault != "none" && fault != "retain_max")
    return 2;
  auto made = lower_async_fifo({"a"}, {"b"}, depth, stages,
                               {{"data", 8}, {"first", 1}, {"level", 3}}, false, true);
  if (!made)
    return 1;
  auto f = std::move(made).value();
  Builder b;
  b.ir = f.ir;
  auto id = [](ValueId x) { return x.index; };
  auto state = [&](ValueId x) -> State & {
    return b.ir.states.at(b.ir.values.at(x.index).state.index);
  };
  nlohmann::json m = {{"module", f.ir.name},
                      {"depth", depth},
                      {"count_bits", f.count_bits},
                      {"n", stages},
                      {"fault", fault},
                      {"clocks", {id(f.domains[0].clock), id(f.domains[1].clock)}},
                      {"resets", {id(f.domains[0].reset), id(f.domains[1].reset)}},
                      {"write_count", id(f.write_count)},
                      {"read_count", id(f.read_count)},
                      {"write_index", id(f.write_index)},
                      {"read_index", id(f.read_index)},
                      {"write", id(f.write)},
                      {"read", id(f.read)},
                      {"out_valid_state", id(f.out_valid)}};
  for (auto [chain, domain] :
       {std::pair{f.write_count_sync, 1U}, std::pair{f.read_count_sync, 0U}}) {
    auto first = chain.front();
    m["first_stages"].push_back({{"stage", id(first)},
                                 {"source", id(state(first).next)},
                                 {"width", b.ir.values[first.index].width},
                                 {"domain", domain}});
  }
  for (const auto &field : f.fields) {
    nlohmann::json slots = nlohmann::json::array();
    std::transform(field.storage.begin(), field.storage.end(), std::back_inserter(slots), id);
    m["fields"].push_back({{"name", field.name},
                           {"width", field.width},
                           {"input", id(field.input)},
                           {"storage", slots},
                           {"received", id(field.received)}});
  }
  std::vector<ValueId> slots;
  for (unsigned i = 0; i < depth; ++i) {
    auto slot = b.state(3, f.domains[0].clock, f.domains[0].reset);
    slots.push_back(slot);
    auto write = b.binary(Op::BitAnd, f.write,
                          b.binary(Op::Equal, f.write_index, b.constant(f.index_bits, i)));
    b.update(slot, b.select(write, b.select(f.fields[1].input, f.fields[2].input, b.constant(3, 0)),
                            slot));
  }
  auto enabled = b.constant(1, 1);
  auto q =
      lower_fifo_levels(b, f.write_count_sync.back(), f.read_count, f.read_index, enabled, slots);
  auto maximum = [&](ValueId x, ValueId y) {
    return b.select(b.binary(Op::UnsignedLess, x, y), y, x);
  };
  if (fault == "retain_max") {
    auto old = b.state(3, f.domains[1].clock, f.domains[1].reset);
    b.update(old, maximum(old, q.level));
    q.level = old;
  }
  auto held = b.state(3, f.domains[1].clock, f.domains[1].reset);
  auto loads = b.binary(Op::BitOr, b.unary(Op::BitNot, f.out_valid, 1), f.out_ready);
  b.update(held, b.select(loads, b.constant(3, 0), maximum(held, q.level)));
  auto head = b.select(f.fields[1].output, f.fields[2].output, b.constant(3, 0));
  auto effective =
      b.select(f.output_valid, maximum(maximum(held, q.level), head), b.constant(3, 0));
  auto emit = [&](const std::string &name, ValueId value) {
    b.output("nl_" + name, value, "probe", name);
    m[name] = id(value);
  };
  emit("queue", q.level);
  emit("effective", effective);
  emit("count", q.count);
  for (unsigned i = 0; i < depth; ++i) {
    emit("active" + std::to_string(i), q.active[i]);
    m["slots"].push_back(id(slots[i]));
  }
  emit("wake", b.binary(Op::BitOr, b.binary(Op::BitOr, f.nonempty, f.out_valid),
                        b.unary(Op::BitNot, enabled, 1)));
  m["published"] = id(f.write_count_sync.back());
  m["enabled"] = id(enabled);
  m["output_valid"] = id(f.output_valid);
  m["held"] = id(held);
  std::filesystem::create_directories(out);
  std::ofstream(out / "generated.v") << emit_verilog(b.ir);
  std::ofstream(out / "cells.v") << cell_files(b.ir).at(0).text;
  std::ofstream(out / "bindings.json") << m.dump(2) << '\n';
  return 0;
}
