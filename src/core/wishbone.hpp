#pragma once
#include "ahb_outlet.hpp"
#include "axi4_initiator.hpp"

namespace nocloom::detail {
Result<NodeHardware> lower_wishbone_initiator(const Axi4InitiatorProfile &profile, bool word,
                                              bool pipelined = false);
Result<NodeHardware> lower_wishbone_target(const BurstOutletProfile &profile, bool word, bool retry,
                                           bool pipelined = false);
} // namespace nocloom::detail
