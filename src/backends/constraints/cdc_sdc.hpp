#pragma once
#include "backends/optional_outputs.hpp"

namespace nocloom::detail {
// Adds <top>.cdc.sdc and <top>.cdc_preserve.txt, derived from the map's network.cdc records, to
// the artifacts of a clocked network and lists them in the bundle. A network without a crossing
// is rejected.
Status process_cdc_constraints(const nlohmann::json &map, OutputFiles *output);
} // namespace nocloom::detail
