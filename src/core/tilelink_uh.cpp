#include "tilelink_uh.hpp"
#include <bit>

namespace nocloom::detail {
using logic::Fields;
TileLinkUhIngress::TileLinkUhIngress(logic::Logic &logic, const Axi4InitiatorProfile &profile)
    : l(logic), p(profile) {
  for (const auto &[name, bits] :
       std::vector<std::pair<std::string, unsigned>>{{"opcode", 3},
                                                     {"param", 3},
                                                     {"size", p.tl_size_bits},
                                                     {"address", p.address_bits},
                                                     {"mask", 4},
                                                     {"data", 32},
                                                     {"corrupt", 1},
                                                     {"valid", 1}})
    a[name] = l.in("tl", "a_" + name, bits);
  a["source"] = p.tl_source_bits ? l.in("tl", "a_source", p.tl_source_bits) : l.bit(0);
  collecting = l.state("tl.collecting", 1);
  collect_local = l.state("tl.collect_local", 1);
  collect_slot = l.state("tl.collect_slot", 6);
  a_left = l.state("tl.a_left", 14);
  writer = l.state("tl.writer", 1);
  aw_pending = l.state("tl.aw_pending", 1);
  ar_pending = l.state("tl.ar_pending", 1);
  w_left = l.state("tl.w_left", 11);
  child_left = l.state("tl.child_left", 9);
  q_count = l.state("tl.q_count", 2);
  q_head = l.state("tl.q_head", 1);
  q_tail = l.state("tl.q_tail", 1);
  owner = l.state("tl.owner", 1);
  owner_slot = l.state("tl.owner_slot", 6);
  full = l.state("tl.full", 1);
  d_ready = l.in("tl", "d_ready", 1);
  d_take = l.bind("tl.d_take", l.both(full, d_ready));
  for (const auto &[name, bits] : std::vector<std::pair<std::string, unsigned>>{
           {"slot", 6}, {"address", p.address_bits}, {"size", 3}, {"len", p.axi_len_bits}}) {
    wr[name] = l.state("tl.wr." + name, bits);
    rd[name] = l.state("tl.rd." + name, bits);
  }
  for (unsigned k = 0; k < 2; ++k) {
    const auto key = "tl.q" + std::to_string(k);
    queue.push_back({{"data", l.state(key + ".data", 32)}, {"mask", l.state(key + ".mask", 4)}});
  }
  for (unsigned k = 0; k < p.tl_message_slots; ++k) {
    Fields slot;
    for (const auto &[name, bits] :
         std::vector<std::pair<std::string, unsigned>>{{"busy", 1},
                                                       {"source", std::max(1U, p.tl_source_bits)},
                                                       {"size", p.tl_size_bits},
                                                       {"opcode", 3},
                                                       {"local", 1},
                                                       {"denied", 1},
                                                       {"error", 1},
                                                       {"a_done", 1},
                                                       {"w_done", 1},
                                                       {"issued", 3},
                                                       {"done", 3},
                                                       {"d_left", 14}})
      slot[name] = l.state("tl.slot" + std::to_string(k) + "." + name, bits);
    slots.push_back(slot);
  }
  for (const auto &[name, bits] :
       std::vector<std::pair<std::string, unsigned>>{{"source", std::max(1U, p.tl_source_bits)},
                                                     {"size", p.tl_size_bits},
                                                     {"opcode", 3},
                                                     {"denied", 1},
                                                     {"corrupt", 1},
                                                     {"data", 32}})
    d[name] = l.state("tl.d_" + name, bits);
  admission();
  continuation();
  request_fields();
}
ValueId TileLinkUhIngress::select(const std::string &field, ValueId index) {
  auto result = l.num(l.width(slots.front().at(field)), 0);
  for (unsigned k = 0; k < slots.size(); ++k)
    result = l.pick(l.eq(index, k), slots[k].at(field), result);
  return result;
}
ValueId TileLinkUhIngress::beat_count(ValueId size) {
  return l.per_size(size, (1U << p.tl_size_bits) - 1,
                    [&](unsigned n) { return l.num(14, n <= 2 ? 1 : 1U << (n - 2)); });
}
void TileLinkUhIngress::admission() {
  write = l.either(l.eq(a.at("opcode"), 0), l.eq(a.at("opcode"), 1));
  const auto get = l.eq(a.at("opcode"), 4);
  data_response = l.either(get, l.either(l.eq(a.at("opcode"), 2), l.eq(a.at("opcode"), 3)));
  beats = beat_count(a.at("size"));
  legal = l.both(l.either(write, get),
                 l.inv(l.below(l.num(p.tl_size_bits, std::bit_width(p.tl_max_transfer_bytes) - 1),
                               a.at("size"))));
  std::vector<ValueId> available, duplicate, reads, long_reads;
  free_slot = l.num(6, 0);
  for (unsigned k = 0; k < slots.size(); ++k) {
    const auto &s = slots[k];
    available.push_back(l.inv(s.at("busy")));
    duplicate.push_back(l.both(s.at("busy"), l.same(s.at("source"), a.at("source"))));
    const auto read = l.both(s.at("busy"), l.both(l.inv(s.at("local")), l.eq(s.at("opcode"), 1)));
    reads.push_back(read);
    long_reads.push_back(l.both(read, l.inv(l.below(s.at("size"), l.num(p.tl_size_bits, 3)))));
  }
  for (unsigned k = static_cast<unsigned>(slots.size()); k-- > 0;)
    free_slot = l.pick(available[k], l.num(6, k), free_slot);
  const auto blocked_read = l.both(
      get, l.either(l.any(long_reads),
                    l.both(l.inv(l.below(a.at("size"), l.num(p.tl_size_bits, 3))), l.any(reads))));
  const auto locked =
      l.both(owner, l.inv(l.below(select("size", owner_slot), l.num(p.tl_size_bits, 3))));
  const auto room = l.inv(l.eq(q_count, 2));
  const auto head_ready = l.every(
      std::vector<ValueId>{l.any(available), l.inv(l.any(duplicate)), l.inv(ar_pending),
                           l.inv(locked), l.inv(blocked_read), l.inv(l.both(write, writer))});
  const auto ready = l.active(l.pick(collecting, l.either(collect_local, room), head_ready));
  l.out("tl", "a_ready", ready);
  take = l.bind("tl.a_take", l.both(a.at("valid"), ready));
  first = l.both(take, l.inv(collecting));
}
void TileLinkUhIngress::continuation() {
  const auto has_data = l.below(a.at("opcode"), l.num(3, 4));
  const auto more = l.both(has_data, l.inv(l.eq(beats, 1)));
  const auto tail = l.both(l.both(take, collecting), l.eq(a_left, 1));
  l.b.update(collecting, l.pick(first, more, l.pick(tail, l.bit(0), collecting)));
  l.b.update(collect_local, l.pick(first, l.inv(legal), collect_local));
  l.b.update(collect_slot, l.pick(first, free_slot, collect_slot));
  l.b.update(a_left,
             l.pick(first, l.dec(beats), l.pick(l.both(take, collecting), l.dec(a_left), a_left)));
  const auto push = l.both(take, l.pick(collecting, l.inv(collect_local), l.both(legal, write)));
  l.bind("tl.w_push", push);
  for (unsigned k = 0; k < queue.size(); ++k) {
    const auto enable = l.both(push, l.eq(q_tail, k));
    l.b.update(queue[k].at("data"), l.pick(enable, a.at("data"), queue[k].at("data")));
    l.b.update(
        queue[k].at("mask"),
        l.pick(enable, l.pick(a.at("corrupt"), l.num(4, 0), a.at("mask")), queue[k].at("mask")));
  }
  l.b.update(q_tail, l.pick(push, l.inv(q_tail), q_tail));
}
void TileLinkUhIngress::request_fields() {
  const auto start_write = l.both(first, l.both(legal, write));
  const auto start_read = l.both(first, l.both(legal, l.inv(write)));
  l.bind("tl.start_write", start_write);
  l.bind("tl.start_read", start_read);
  const auto size =
      l.pick(l.below(a.at("size"), l.num(p.tl_size_bits, 3)), l.fit(a.at("size"), 3), l.num(3, 2));
  const auto len = l.fit(l.dec(beats), p.axi_len_bits);
  for (const auto &[channel, fields, start, valid] :
       std::vector<std::tuple<std::string, Fields, ValueId, ValueId>>{
           {"aw", wr, start_write, aw_pending}, {"ar", rd, start_read, ar_pending}}) {
    const Fields next{
        {"slot", free_slot}, {"address", a.at("address")}, {"size", size}, {"len", len}};
    for (const auto &[name, value] : fields)
      l.b.update(value, l.pick(start, next.at(name), value));
    inputs[channel + "valid"] = valid;
    inputs[channel + "id"] = l.fit(fields.at("slot"), std::max(1U, p.id_bits));
    inputs[channel + "addr"] = fields.at("address");
    inputs[channel + "len"] = fields.at("len");
    inputs[channel + "size"] = fields.at("size");
    inputs[channel + "burst"] = l.num(2, 1);
    inputs[channel + "cache"] = l.num(4, 0);
    inputs[channel + "prot"] = l.num(3, 2);
    inputs[channel + "lock"] = l.bit(0);
  }
  inputs["wvalid"] = l.both(writer, l.both(l.inv(aw_pending), l.inv(l.eq(q_count, 0))));
  inputs["wdata"] = l.pick(q_head, queue[1].at("data"), queue[0].at("data"));
  inputs["wstrb"] = l.pick(q_head, queue[1].at("mask"), queue[0].at("mask"));
  inputs["wlast"] = l.eq(child_left, 1);
  inputs["bready"] = l.active(l.bit(1));
  inputs["rready"] = l.active(
      l.every(std::vector<ValueId>{owner, l.inv(full), l.eq(select("opcode", owner_slot), 1),
                                   l.inv(select("local", owner_slot))}));
}
void TileLinkUhIngress::child_issue(const Fields &outputs) {
  aw_fire = l.both(aw_pending, outputs.at("awready"));
  ar_fire = l.both(ar_pending, outputs.at("arready"));
  w_pop = l.both(inputs.at("wvalid"), outputs.at("wready"));
  last_w = l.both(w_pop, l.eq(w_left, 1));
  const auto start_write = l.bindings.at("tl.start_write");
  const auto start_read = l.bindings.at("tl.start_read");
  l.b.update(aw_pending, l.pick(start_write, l.bit(1), l.pick(aw_fire, l.bit(0), aw_pending)));
  l.b.update(ar_pending, l.pick(start_read, l.bit(1), l.pick(ar_fire, l.bit(0), ar_pending)));
  l.b.update(writer, l.pick(start_write, l.bit(1), l.pick(last_w, l.bit(0), writer)));
  l.b.update(w_left, l.pick(start_write, l.fit(beats, 11), l.pick(w_pop, l.dec(w_left), w_left)));
  l.b.update(child_left,
             l.pick(start_write, l.fit(beats, 9), l.pick(w_pop, l.dec(child_left), child_left)));
  const auto push = l.bindings.at("tl.w_push");
  l.b.update(q_count, l.pick(push, l.pick(w_pop, q_count, l.inc(q_count)),
                             l.pick(w_pop, l.dec(q_count), q_count)));
  l.b.update(q_head, l.pick(w_pop, l.inv(q_head), q_head));
}
void TileLinkUhIngress::child_response(const Fields &outputs) {
  b_fire = l.both(outputs.at("bvalid"), inputs.at("bready"));
  r_fire = l.both(outputs.at("rvalid"), inputs.at("rready"));
  l.bind("wr.b_take", b_fire);
}
void TileLinkUhIngress::d_select(const Fields &outputs) {
  const auto rid = p.id_bits ? outputs.at("rid") : l.bit(0);
  std::vector<ValueId> eligible;
  auto selected = l.num(6, 0);
  for (unsigned k = 0; k < slots.size(); ++k) {
    const auto &s = slots[k];
    const auto read = l.both(l.eq(s.at("opcode"), 1), l.both(outputs.at("rvalid"), l.eq(rid, k)));
    const auto written =
        l.both(s.at("w_done"), l.both(l.eq(s.at("issued"), 1), l.eq(s.at("done"), 1)));
    const auto ready =
        l.pick(s.at("local"), s.at("a_done"), l.pick(l.eq(s.at("opcode"), 1), read, written));
    eligible.push_back(l.both(s.at("busy"), ready));
  }
  for (unsigned k = static_cast<unsigned>(slots.size()); k-- > 0;)
    selected = l.pick(eligible[k], l.num(6, k), selected);
  const auto acquire = l.both(l.inv(owner), l.any(eligible));
  const auto release = l.both(d_take, l.eq(select("d_left", owner_slot), 1));
  l.b.update(owner, l.pick(acquire, l.bit(1), l.pick(release, l.bit(0), owner)));
  l.b.update(owner_slot, l.pick(acquire, selected, owner_slot));
  const auto local = select("local", owner_slot);
  const auto data = l.eq(select("opcode", owner_slot), 1);
  const auto load =
      l.both(l.both(owner, l.inv(full)), l.either(local, l.either(l.inv(data), r_fire)));
  const auto failed = l.inv(l.eq(outputs.at("rresp"), 0));
  const Fields next{
      {"source", select("source", owner_slot)},
      {"size", select("size", owner_slot)},
      {"opcode", select("opcode", owner_slot)},
      {"denied", l.pick(local, select("denied", owner_slot),
                        l.both(l.inv(data), select("error", owner_slot)))},
      {"corrupt", l.both(data, l.either(local, failed))},
      {"data", l.pick(l.both(r_fire, l.inv(failed)), outputs.at("rdata"), l.num(32, 0))}};
  for (const auto &[name, value] : d) {
    l.b.update(value, l.pick(load, next.at(name), value));
    if (name != "source" || p.tl_source_bits)
      l.out("tl", "d_" + name, value);
  }
  l.b.update(full, l.pick(load, l.bit(1), l.pick(d_take, l.bit(0), full)));
  l.out("tl", "d_valid", l.active(full));
  l.out("tl", "d_param", l.num(2, 0));
  if (p.tl_sink_bits)
    l.out("tl", "d_sink", l.num(p.tl_sink_bits, 0));
  std::vector<ValueId> busy;
  for (const auto &s : slots)
    busy.push_back(s.at("busy"));
  l.bind("tl.busy", l.either(l.any(busy), full));
}
void TileLinkUhIngress::slot_updates() {
  const auto opcode =
      l.pick(data_response, l.num(3, 1), l.pick(l.eq(a.at("opcode"), 5), l.num(3, 2), l.num(3, 0)));
  const auto hint_ok = l.both(
      l.eq(a.at("opcode"), 5),
      l.both(l.below(a.at("param"), l.num(3, 2)),
             l.inv(l.below(l.num(p.tl_size_bits, std::bit_width(p.tl_max_transfer_bytes) - 1),
                           a.at("size")))));
  for (unsigned k = 0; k < slots.size(); ++k) {
    const auto &s = slots[k];
    const auto allocate = l.both(first, l.eq(free_slot, k));
    const auto consume = l.both(d_take, l.eq(owner_slot, k));
    const auto retire = l.both(consume, l.eq(s.at("d_left"), 1));
    const auto continued = l.both(l.both(take, collecting), l.eq(collect_slot, k));
    const auto a_done = l.both(continued, l.eq(a_left, 1));
    const auto written = l.both(last_w, l.eq(wr.at("slot"), k));
    const auto issued =
        l.either(l.both(aw_fire, l.eq(wr.at("slot"), k)), l.both(ar_fire, l.eq(rd.at("slot"), k)));
    const auto done = l.both(b_fire, l.eq(bid, k));
    const auto error = l.either(l.both(l.both(done, b_error), l.bit(1)),
                                l.both(l.both(continued, l.inv(collect_local)), a.at("corrupt")));
    const Fields initial{
        {"busy", l.bit(1)},
        {"source", a.at("source")},
        {"size", a.at("size")},
        {"opcode", opcode},
        {"local", l.inv(legal)},
        {"denied", l.inv(hint_ok)},
        {"error", l.both(write, a.at("corrupt"))},
        {"a_done", l.either(l.inv(l.below(a.at("opcode"), l.num(3, 4))), l.eq(beats, 1))},
        {"w_done", l.bit(0)},
        {"issued", l.num(3, 0)},
        {"done", l.num(3, 0)},
        {"d_left", l.pick(data_response, beats, l.num(14, 1))}};
    const Fields next{{"busy", l.pick(retire, l.bit(0), s.at("busy"))},
                      {"a_done", l.either(s.at("a_done"), a_done)},
                      {"w_done", l.either(s.at("w_done"), written)},
                      {"error", l.either(s.at("error"), error)},
                      {"issued", l.pick(issued, l.inc(s.at("issued")), s.at("issued"))},
                      {"done", l.pick(done, l.inc(s.at("done")), s.at("done"))},
                      {"d_left", l.pick(consume, l.dec(s.at("d_left")), s.at("d_left"))}};
    for (const auto &[name, value] : s)
      l.b.update(value,
                 l.pick(allocate, initial.at(name), next.contains(name) ? next.at(name) : value));
  }
}
void TileLinkUhIngress::finish(const Fields &outputs) {
  child_issue(outputs);
  child_response(outputs);
  bid = p.id_bits ? outputs.at("bid") : l.bit(0);
  b_error = l.inv(l.eq(outputs.at("bresp"), 0));
  d_select(outputs);
  slot_updates();
}
} // namespace nocloom::detail
