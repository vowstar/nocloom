#pragma once
#include "hardware.hpp"

namespace nocloom::detail {
// Generation-time identities and dependencies; these are not a configuration ABI.
enum class CrossWaitKind { Capacity, Completion, ReservedWrite, Startup = 4, Environment };
// Registers of a resynchronized reset release in each domain.
inline constexpr unsigned reset_release_stages = 2;
// A domain's clock and reset input ports; release_stages is reset_release_stages when the reset
// deasserts through the domain's own resynchronizer and 0 when it resets the flip-flops directly.
struct DomainPlan {
  std::string name;
  std::string clock_port = "clk_" + name, reset_port = "reset_n";
  unsigned release_stages = reset_release_stages;
  // Schema 10 clocked graphs only; selects the default crossing depth.
  std::optional<std::uint64_t> period_ps = std::nullopt;
  // The test port input of the domain's clock gates (empty: none) and whether it is gated.
  std::string test_port = {};
  bool gated = true;
  // InternalDivider counts input edges. EdgeQualified uses the enable input.
  ClockType type = ClockType::Direct;
  unsigned ratio = 1;
  std::string enable_port = {};
  // The ungate port input of the domain's clock gates (empty: none).
  std::string ungate_port = {};
  // Explicit physical root, fixed edge ancestry and total division from the root.
  std::string clock_root = {}, edge_parent = {};
  std::uint64_t edge_ratio = 1;
  bool operator==(const DomainPlan &) const = default;
};
struct CrossResource {
  std::string id, storage, domain, role, owner, release;
  unsigned payload_bits = 0;
  // Words the resource holds at once; a crossing FIFO holds its depth.
  unsigned capacity = 1;
  // Present for link and endpoint pipeline resources.
  std::optional<LinkPipeline> pipe = std::nullopt;
  bool operator==(const CrossResource &) const = default;
};
struct CrossWait {
  std::string from, to, condition;
  CrossWaitKind kind{};
  bool operator==(const CrossWait &) const = default;
};
struct CrossGraph {
  std::vector<CrossResource> resources;
  std::vector<CrossWait> waits;
  bool operator==(const CrossGraph &) const = default;
};
// The storage of one resource in generated hardware: its clock, payload and
// control registers, and the acquire and release events.
struct CrossStorageBinding {
  std::string resource;
  ValueId clock;
  std::vector<ValueId> payload, control;
  ValueId acquire, release;
};
// Reserved writes record an existing exclusive receive permit, not a wait for empty storage.
Result<std::vector<std::string>> order_cross_domain_graph(const CrossGraph &graph);
} // namespace nocloom::detail
