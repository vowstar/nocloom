#pragma once
#include "exchange_composition.hpp"
#include <nlohmann/json_fwd.hpp>

namespace nocloom::detail {
Status check_read_refinement_scope(const NetworkPlan &plan);
Result<nlohmann::json> read_refinement_map(const ExchangeHardware &hardware);
} // namespace nocloom::detail
