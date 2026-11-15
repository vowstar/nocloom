#pragma once
#include "ahb_outlet.hpp"
#include "logic.hpp"
#include <string_view>

namespace nocloom::detail {
struct TileLinkUhOutletProfile {
  BurstOutletProfile packet;
  unsigned source_bits = 0, size_bits = 3, sink_bits = 0;
  unsigned max_transfer_bytes = 64;
  bool scheduled = false, segmented = false;
};
bool tilelink_uh_payload(std::string_view name);
ValueId tilelink_block_size(logic::Logic &l, ValueId address, ValueId available, unsigned size_bits,
                            unsigned maximum);
Result<NodeHardware> lower_tilelink_uh_outlet(const TileLinkUhOutletProfile &profile);
Result<NodeHardware> lower_tilelink_uh_contexts(const TileLinkUhOutletProfile &profile);
} // namespace nocloom::detail
