#pragma once
#include "network_plan.hpp"
#include <bit>
#include <cstdint>
#include <span>
#include <stdexcept>
#include <string_view>

namespace nocloom::detail {
// What each protocol supports as an initiator (ingress) and as a target (egress). Width sets hold
// bit k for an 8 << k bit data word. The definition checks and the capability rules read this
// table instead of naming protocols.
struct ProtocolCaps {
  Protocol protocol;
  bool ingress, egress;
  std::uint32_t ingress_widths, egress_widths;
  // Target options: timeout, attribute mapping, FIXED bursts as single transfers, the APB family
  // (back-to-back access, 8- and 16-bit faces), AHB burst shaping and HRESP width, AXI exclusives.
  bool timeout, attributes, fixed_single, apb, ahb_shape, lock;
  // Target bursts (AXI4-Lite: packets of one direction) in flight.
  unsigned max_outstanding;
};
const ProtocolCaps &protocol_caps(Protocol protocol);
inline bool width_in(std::uint32_t widths, unsigned bits) {
  return std::has_single_bit(bits) && bits >= 8 && bits <= 1024 &&
         (widths >> (std::bit_width(bits) - 4) & 1U);
}

// The scopes a rule is evaluated over, each in plan order: the network, the declared domains and
// crossings, targets in name order (in definition order for a definition rule), connected
// (source, target) pairs, the physical switch outputs (request then response), per-output
// arbitration entries, switches and links in plan order, and crossing FIFOs.
enum class Scope : std::uint8_t {
  Network,
  Domain,
  Crossing,
  Target,
  Pair,
  Output,
  Override,
  Node,
  Link,
  Cut
};
enum class Stage : std::uint8_t { Definition, Plan };
struct RuleView {
  const Definition &def;
  const NetworkPlan &plan;
  std::size_t at = 0;
};
// A violation: the configuration path and the message.
struct Finding {
  std::string field, message;
};
// One combination limit of a planned network. A row is the only place its rule is judged; lifting
// the limit deletes the row. id is internal and never reaches a diagnostic.
struct CapabilityRule {
  std::string_view id, code;
  Scope scope = Scope::Network;
  // Definition: judged from the definition alone, with the definition checks; Plan: over the plan.
  Stage stage = Stage::Plan;
  std::optional<Finding> (*check)(const RuleView &) = nullptr;
};
std::span<const CapabilityRule> capability_rules();
// Every violation of every definition rule (no plan) or every plan rule, in table order and plan
// order within a rule, each distinct diagnostic once.
std::vector<Diagnostic> check_capabilities(const Definition &def, const NetworkPlan *plan);
// A planning invariant that the definition checks and the capability rules guarantee.
inline void require(bool holds, const char *what) {
  if (!holds)
    throw std::logic_error(what);
}
} // namespace nocloom::detail
