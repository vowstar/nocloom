#pragma once
#include "ahb_outlet.hpp"
#include "axi4_initiator.hpp"
#include "logic.hpp"

namespace nocloom::detail {
// Single-beat channel adaptation onto the AXI initiator kernel.
class TileLinkIngress {
public:
  TileLinkIngress(logic::Logic &logic, const Axi4InitiatorProfile &profile);
  logic::Fields inputs;
  void finish(const logic::Fields &outputs);

private:
  logic::Logic &l;
  const Axi4InitiatorProfile &p;
  logic::Fields a, held;
  ValueId pending, aw, w, ar, full, turn, drain, local, local_left;
  ValueId ready, take, first, free_slot, d_take, local_response;
  logic::Fields d;
  struct Slot {
    ValueId busy, source, size;
  };
  std::vector<Slot> slots;
  ValueId select_size(ValueId source);
  void request();
  void response(const logic::Fields &outputs);
};
Result<NodeHardware> lower_tilelink_outlet(const BurstOutletProfile &profile, unsigned source_bits,
                                           unsigned size_bits, unsigned sink_bits);
std::vector<Diagnostic> check_tilelink(const Definition &definition);
} // namespace nocloom::detail
