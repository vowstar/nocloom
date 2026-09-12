#pragma once
#include "nocloom/generation.hpp"

namespace nocloom::detail {
// Adds <top>.cdc.sdc and <top>.cdc_preserve.txt, derived from the map's network.cdc records, to
// the artifacts of a clocked network and lists them in the bundle. A network without a crossing
// is rejected.
Result<ArtifactSet> add_cdc_constraints(ArtifactSet artifacts);
} // namespace nocloom::detail
