#include "read_refinement.hpp"
#include "diagnostic.hpp"
#include <algorithm>
#include <array>
#include <nlohmann/json.hpp>

namespace nocloom::detail {
namespace {
using Json = nlohmann::json;

Diagnostic scope_error(std::string message) {
  return error("NCL_REFINEMENT_SCOPE", "output.refinement", std::move(message));
}

bool direct_axi4(const NetworkPlan &p) {
  if (p.sources.size() != 1 || p.targets.size() != 1 || p.exchange.separate())
    return false;
  const auto &s = p.sources.front();
  const auto &t = p.targets.front();
  return s.external_protocol == Protocol::Axi4 && t.external_protocol == Protocol::Axi4 &&
         !s.write_only && !t.write_only && !t.timeout && !t.upsize &&
         t.read_interleave_size.value_or(0) == 0 && s.outstanding == 1 && t.outstanding == 1 &&
         s.flows.size() <= 1 && t.flows == 1 && t.pipes.empty() && !p.per_endpoint && !p.laddered &&
         !p.level_hint_bits() &&
         std::ranges::all_of(p.links, [](const auto &link) { return link.stages.empty(); });
}

// A composition wire copies a value by OR-ing it with zero. If the copy is
// unused, the Verilog can still expose the original value.
std::size_t emitted_value(const HardwareIR &ir, std::size_t i, std::size_t emitted) {
  while (i >= emitted && i < ir.values.size()) {
    const auto &v = ir.values[i];
    if (v.op != Op::BitOr)
      break;
    const auto &zero = ir.values.at(v.operands[1].index);
    if (zero.op != Op::Constant || zero.literal != 0)
      break;
    i = v.operands[0].index;
  }
  return i;
}

Result<Json> face(const ExchangeHardware &h, const std::string &name,
                  const std::array<const char *, 4> &fields) {
  Json row = Json::object();
  for (const auto *field : fields) {
    const auto at = h.bindings.find("refinement/" + name + "." + field);
    const auto emitted = h.ir.emitted.value_or(h.ir.values.size());
    const auto index =
        at == h.bindings.end() ? emitted : emitted_value(h.ir, at->second.index, emitted);
    if (index >= emitted)
      return Result<Json>::failure(
          scope_error("Transport boundary signal " + name + "." + field + " is absent"));
    row[field] = index;
  }
  return Result<Json>::success(std::move(row));
}
} // namespace

Status check_read_refinement_scope(const NetworkPlan &p) {
  const auto &m = p.dependency.model;
  if (!direct_axi4(p) || !m.present || m.routes.size() != 2 || !m.nexts.empty() ||
      m.pools.size() != 1 || !m.bound.empty())
    return Status::failure(scope_error(
        "Read refinement requires a direct AXI4 pair with one outstanding transfer per port"));
  return Status::success({});
}

Result<Json> read_refinement_map(const ExchangeHardware &h) {
  const auto emitted = h.ir.emitted.value_or(h.ir.values.size());
  const auto context = h.bindings.at("refinement/context").index;
  const auto &v = h.ir.values.at(context);
  if (context >= emitted || v.op != Op::State)
    return Result<Json>::failure(scope_error("The AXI4 read context register is absent"));
  const auto &reg = h.ir.states.at(v.state.index);
  if (!reg.reset || reg.reset->value != 0 || !h.ir.synchronizer_stages.empty() ||
      h.ir.values.at(reg.clock.index).op != Op::Input ||
      h.ir.values.at(reg.reset->signal.index).op != Op::Input ||
      std::ranges::any_of(h.ir.states, [&](const State &s) {
        return s.output.index < emitted &&
               (s.clock.index != reg.clock.index ||
                (s.reset && s.reset->signal.index != reg.reset->signal.index));
      }))
    return Result<Json>::failure(scope_error("Read refinement requires one ungated clock and one "
                                             "direct, active-low reset"));

  const auto &p = h.plan;
  const auto &m = p.dependency.model;
  const auto [reqOut, reqDest, rspSlot] = m.pools.front();
  const auto reqRoute = std::ranges::find_if(m.routes, [&](const auto &r) {
    return std::get<1>(r) == reqDest && std::get<2>(r) == reqOut;
  });
  const auto rspRoute =
      std::ranges::find(m.routes, rspSlot, [](const auto &r) { return std::get<0>(r); });
  if (reqRoute == m.routes.end() || rspRoute == m.routes.end() || reqRoute == rspRoute)
    return Result<Json>::failure(scope_error("The request and response model routes are absent"));

  Json map = {{"format_version", 1},
              {"module", h.ir.name},
              {"initiator", p.sources.front().name},
              {"target", p.targets.front().name},
              {"clock", reg.clock.index},
              {"reset", reg.reset->signal.index},
              {"context_count", context},
              {"request", *reqRoute},
              {"response", *rspRoute}};
  for (const auto &[name, fields] :
       {std::pair{"req/entry", std::array{"valid", "ready", "last", "write"}},
        std::pair{"req/exit", std::array{"valid", "ready", "last", "write"}},
        std::pair{"rsp/entry", std::array{"valid", "ready", "last", "source"}},
        std::pair{"rsp/exit", std::array{"valid", "ready", "last", "source"}}}) {
    auto mapped = face(h, name, fields);
    if (!mapped)
      return mapped;
    map["boundaries"][name] = std::move(mapped).value();
  }
  return Result<Json>::success(std::move(map));
}
} // namespace nocloom::detail
