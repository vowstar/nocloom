#pragma once
#include "network_plan.hpp"
#include <nocloom/generation.hpp>

namespace nocloom::detail {
// The RTL, map and bundle of a planned network; composes the plan without planning again.
Result<ArtifactSet> emit_axi4_network(const NetworkPlan &plan,
                                      RegisterReset register_reset = RegisterReset::Asynchronous);
} // namespace nocloom::detail
