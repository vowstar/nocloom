#pragma once
#include "axi4_initiator.hpp"
#include "logic.hpp"

namespace nocloom::detail {
class TileLinkUhIngress {
public:
  TileLinkUhIngress(logic::Logic &logic, const Axi4InitiatorProfile &profile);
  logic::Fields inputs;
  void finish(const logic::Fields &outputs);

private:
  logic::Logic &l;
  const Axi4InitiatorProfile &p;
  logic::Fields a, wr, rd, d;
  std::vector<logic::Fields> slots;
  ValueId collecting, collect_slot, a_left, collect_local;
  ValueId writer, aw_pending, ar_pending, w_left, child_left, read_left;
  ValueId q_count, q_head, q_tail;
  std::vector<logic::Fields> queue;
  ValueId owner, owner_slot, full, d_ready, d_take;
  ValueId take, first, free_slot, legal, write, beats, data_response;
  ValueId w_pop, last_w, aw_fire, ar_fire, b_fire, r_fire, bid, b_error, rlast;
  ValueId select(const std::string &field, ValueId index);
  ValueId beat_count(ValueId size);
  void admission();
  void continuation();
  void request_fields();
  void descriptor(const logic::Fields &fields, ValueId start, ValueId advance);
  void child_issue(const logic::Fields &outputs);
  void child_response(const logic::Fields &outputs);
  void d_select(const logic::Fields &outputs);
  void slot_updates();
};
} // namespace nocloom::detail
