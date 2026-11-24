#pragma once
#include "network_plan.hpp"
#include <nlohmann/json_fwd.hpp>
#include <nocloom/generation.hpp>

namespace nocloom::detail {
// Check the composed network and its map without serializing artifacts.
Status check_axi4_network(const NetworkPlan &plan, const Definition &definition,
                          const GenerationOptions &options);
// The RTL, map and bundle of a planned network; composes the plan without planning again.
Result<ArtifactSet> emit_axi4_network(const NetworkPlan &plan,
                                      RegisterReset register_reset = RegisterReset::Asynchronous,
                                      nlohmann::json *output_map = nullptr, bool netlist = false,
                                      bool refinement = false);
} // namespace nocloom::detail
