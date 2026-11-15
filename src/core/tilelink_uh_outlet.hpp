#pragma once
#include "ahb_outlet.hpp"
#include "logic.hpp"

namespace nocloom::detail {
struct TileLinkUhOutletProfile {
  BurstOutletProfile packet;
  unsigned source_bits = 0, size_bits = 3, sink_bits = 0;
  unsigned max_transfer_bytes = 64;
};
ValueId tilelink_block_size(logic::Logic &l, ValueId address, ValueId available, unsigned size_bits,
                            unsigned maximum);
Result<NodeHardware> lower_tilelink_uh_outlet(const TileLinkUhOutletProfile &profile);
} // namespace nocloom::detail
