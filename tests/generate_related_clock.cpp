#include "core/related_clock.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <nlohmann/json.hpp>
#include <string>

using namespace nocloom::detail;
namespace {
const std::vector<std::pair<std::string, unsigned>> layout{
    {"data", 32}, {"tag", 3},  {"status", 2},  {"user", 4},
    {"head", 1},  {"tail", 1}, {"pressure", 3}};
std::string name(ValueId id) { return "nl_internal_v" + std::to_string(id.index); }
bool fault(RelatedAdapter &f, const std::string &kind, const std::string &value) {
  if (value == "none")
    return true;
  Builder b;
  b.ir = std::move(f.ir);
  if (kind == "edge_aligned" && value == "missing_admission_open" &&
      (f.slow_from_sender || !f.slow_pipeline)) {
    b.update(f.admission_open, b.constant(1, 1));
  } else if (kind == "sync" && value == "binary_count") {
    for (auto [count, event] :
         {std::pair{f.write_count, f.write}, std::pair{f.read_count, f.read}}) {
      const auto width = b.ir.values.at(count.index).width;
      b.update(count,
               b.select(event, b.binary(Op::Subtract, count, b.constant(width, (1U << width) - 1)),
                        count));
    }
  } else if (kind == "sync" && value == "full_early") {
    bool changed = false;
    for (std::size_t i = 0; i < b.ir.values.size(); ++i)
      if (i == f.full.index) {
        auto &v = b.ir.values[i];
        auto &limit = b.ir.values.at(v.operands[1].index);
        if (limit.op == Op::Constant && limit.literal == f.depth) {
          --limit.literal;
          changed = true;
        }
      }
    if (!changed)
      return false;
  } else {
    return false;
  }
  f.ir = std::move(b.ir);
  return true;
}
} // namespace

int main(int argc, char **argv) {
  if (argc < 5)
    return 2;
  try {
    const std::filesystem::path out = argv[1];
    const std::string kind = argv[2], fault_name = argv[argc - 1];
    nocloom::Result<RelatedAdapter> lowered =
        kind == "sync"
            ? lower_related_sync({"a"}, {"b"}, static_cast<unsigned>(std::stoul(argv[3])),
                                 std::string(argv[4]) == "1", layout, true)
            : lower_edge_aligned({"a"}, {"b"}, std::string(argv[3]) == "1",
                                 std::string(argv[4]) == "1", layout, true);
    if (!lowered || (kind != "sync" && kind != "edge_aligned"))
      return 2;
    auto f = std::move(lowered).value();
    if (!fault(f, kind, fault_name))
      return 2;
    nlohmann::json meta{{"module", f.ir.name},
                        {"kind", kind},
                        {"depth", f.depth},
                        {"fast", f.fast},
                        {"slow_from_sender", f.slow_from_sender},
                        {"slow_pipeline", f.slow_pipeline},
                        {"fault", fault_name},
                        {"fields", nlohmann::json::array()},
                        {"states", nlohmann::json::array()}};
    for (const auto &field : f.fields)
      meta["fields"].push_back({{"name", field.name}, {"width", field.width}});
    for (const auto &state : f.ir.states)
      meta["states"].push_back({{"name", name(state.output)}, {"clock", state.clock.index}});
    if (f.admission_open.index != 0)
      meta["admission_open"] = name(f.admission_open);
    if (kind == "sync") {
      meta["write_count"] = name(f.write_count);
      meta["read_count"] = name(f.read_count);
      meta["write_count_return"] = name(f.returned_write_count());
      meta["read_count_return"] = name(f.returned_read_count());
    }
    std::filesystem::create_directories(out);
    std::ofstream(out / "generated.v") << emit_verilog(f.ir);
    std::ofstream(out / "bindings.json") << meta.dump(2) << '\n';
  } catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
  return 0;
}
