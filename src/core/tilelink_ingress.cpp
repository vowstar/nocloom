#include "tilelink.hpp"
#include <algorithm>
#include <numeric>

namespace nocloom::detail {
using logic::Fields;
TileLinkIngress::TileLinkIngress(logic::Logic &logic, const Axi4InitiatorProfile &profile)
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
  pending = l.state("tl.pending", 1);
  aw = l.state("tl.aw", 1);
  w = l.state("tl.w", 1);
  ar = l.state("tl.ar", 1);
  full = l.state("tl.full", 1);
  turn = l.state("tl.turn", 1);
  const unsigned reject_bits = (1U << p.tl_size_bits) - 3;
  drain = l.state("tl.drain", reject_bits);
  local = l.state("tl.local", 1);
  local_response = l.state("tl.local_response", 1);
  local_left = l.state("tl.local_left", reject_bits);
  for (const auto &[name, bits] :
       std::vector<std::pair<std::string, unsigned>>{{"opcode", 3},
                                                     {"size", p.tl_size_bits},
                                                     {"source", std::max(1U, p.tl_source_bits)},
                                                     {"data", 32},
                                                     {"denied", 1},
                                                     {"corrupt", 1}})
    d[name] = l.state("tl.d_" + name, bits);
  const auto dready = l.in("tl", "d_ready", 1);
  d_take = l.bind("tl.d_take", l.both(full, dready));
  std::vector<ValueId> available;
  free_slot = l.num(6, 0);
  for (unsigned k = 0; k < p.max_outstanding; ++k) {
    const auto key = "tl.slot" + std::to_string(k);
    slots.push_back({l.state(key + ".busy", 1),
                     l.state(key + ".source", std::max(1U, p.tl_source_bits)),
                     l.state(key + ".size", p.tl_size_bits)});
    available.push_back(l.inv(slots.back().busy));
  }
  for (unsigned k = p.max_outstanding; k-- > 0;)
    free_slot = l.pick(available[k], l.num(6, k), free_slot);
  ready = l.active(
      l.either(l.inv(l.eq(drain, 0)), l.both(l.inv(l.either(pending, local)), l.any(available))));
  l.out("tl", "a_ready", ready);
  take = l.bind("tl.a_take", l.both(a.at("valid"), ready));
  first = l.both(take, l.eq(drain, 0));
  held = l.latch_fields("tl.a", a, first, {"param", "corrupt"});
  request();
}
void TileLinkIngress::request() {
  const auto opcode = a.at("opcode"), size = a.at("size");
  const auto write = l.either(l.eq(opcode, 0), l.eq(opcode, 1));
  const auto legal =
      l.both(l.either(write, l.eq(opcode, 4)), l.both(l.below(size, l.num(p.tl_size_bits, 3)),
                                                      l.inv(l.both(write, a.at("corrupt")))));
  const auto issue = l.both(first, legal), reject = l.both(first, l.inv(legal));
  const auto has_data = l.below(opcode, l.num(3, 4));
  const unsigned top = (1U << p.tl_size_bits) - 1;
  auto count = l.num(l.width(drain), 0);
  for (unsigned n = 3; n <= top; ++n)
    count = l.pick(l.eq(size, n), l.num(l.width(drain), (1U << (n - 2)) - 1), count);
  l.b.update(drain, l.pick(reject, l.pick(has_data, count, l.num(l.width(drain), 0)),
                           l.pick(l.both(take, l.inv(l.eq(drain, 0))), l.dec(drain), drain)));
  const auto local_done = l.both(d_take, l.both(local_response, l.eq(local_left, 0)));
  l.b.update(local, l.pick(reject, l.bit(1), l.pick(local_done, l.bit(0), local)));
  const auto data_response = l.either(l.eq(opcode, 4), l.either(l.eq(opcode, 2), l.eq(opcode, 3)));
  l.b.update(local_left,
             l.pick(reject, l.pick(data_response, count, l.num(l.width(drain), 0)),
                    l.pick(l.both(l.both(d_take, local_response), l.inv(l.eq(local_left, 0))),
                           l.dec(local_left), local_left)));
  // The adapter holds AW and W independently until the kernel accepts each.
  l.bind("tl.issue", issue);
  for (const auto &[ch, flag] :
       std::vector<std::pair<std::string, ValueId>>{{"aw", aw}, {"w", w}, {"ar", ar}}) {
    const auto start = l.both(issue, ch == "ar" ? l.inv(write) : write);
    l.bind("tl.start_" + ch, start);
    inputs[ch + "valid"] = flag;
  }
  auto address = held.at("address");
  const auto size_held = held.at("size");
  for (unsigned n = 1; n <= 2; ++n)
    address = l.pick(
        l.eq(size_held, n),
        l.both(address, l.num(p.address_bits, ((1ULL << p.address_bits) - 1) & ~((1ULL << n) - 1))),
        address);
  ValueId mask = l.num(4, 15);
  const auto lane = l.b.slice(address, 0, 2);
  auto byte_mask = l.num(4, 1);
  for (unsigned k = 1; k < 4; ++k)
    byte_mask = l.pick(l.eq(lane, k), l.num(4, 1U << k), byte_mask);
  mask = l.pick(l.eq(size_held, 0), byte_mask,
                l.pick(l.eq(size_held, 1),
                       l.pick(l.b.slice(address, 1, 1), l.num(4, 12), l.num(4, 3)), mask));
  for (const auto &ch : {std::string("aw"), std::string("ar")}) {
    inputs[ch + "addr"] = address;
    inputs[ch + "id"] = l.fit(held.at("source"), std::max(1U, p.id_bits));
    inputs[ch + "len"] = l.num(p.axi_len_bits, 0);
    inputs[ch + "size"] = l.fit(size_held, 3);
    inputs[ch + "burst"] = l.num(2, 1);
    inputs[ch + "cache"] = l.num(4, 0);
    inputs[ch + "prot"] = l.num(3, 2);
    inputs[ch + "lock"] = l.bit(0);
  }
  inputs["wdata"] = held.at("data");
  inputs["wstrb"] = l.both(held.at("mask"), mask);
  inputs["wlast"] = l.bit(1);
}
ValueId TileLinkIngress::select_size(ValueId source) {
  return std::accumulate(
      slots.begin(), slots.end(), l.num(p.tl_size_bits, 0), [&](ValueId size, const Slot &slot) {
        return l.pick(l.both(slot.busy, l.same(slot.source, l.fit(source, l.width(slot.source)))),
                      slot.size, size);
      });
}
void TileLinkIngress::response_ready(const Fields &outputs) {
  const auto room = l.inv(l.either(full, l.both(local, local_response)));
  inputs["rready"] = l.both(room, l.either(turn, l.inv(outputs.at("bvalid"))));
  inputs["bready"] = l.both(room, l.either(l.inv(turn), l.inv(outputs.at("rvalid"))));
}
void TileLinkIngress::response(const Fields &outputs) {
  const auto bfire = l.both(outputs.at("bvalid"), inputs.at("bready"));
  const auto rfire = l.both(outputs.at("rvalid"), inputs.at("rready"));
  l.b.update(turn, l.pick(rfire, l.bit(0), l.pick(bfire, l.bit(1), turn)));
  const auto local_fire =
      l.both(l.both(local, l.eq(drain, 0)), l.both(l.inv(full), l.inv(l.either(bfire, rfire))));
  const auto load = l.either(local_fire, l.either(bfire, rfire));
  const auto id = [&](const char *name) { return p.id_bits ? outputs.at(name) : l.bit(0); };
  const auto source = l.fit(l.pick(rfire, id("rid"), id("bid")), std::max(1U, p.tl_source_bits));
  const auto failed = l.inv(l.eq(l.pick(rfire, outputs.at("rresp"), outputs.at("bresp")), 0));
  const auto local_data = l.either(
      l.eq(held.at("opcode"), 4), l.either(l.eq(held.at("opcode"), 2), l.eq(held.at("opcode"), 3)));
  Fields next{
      {"source", l.pick(local_fire, held.at("source"), source)},
      {"size", l.pick(local_fire, held.at("size"), select_size(source))},
      {"opcode", l.pick(local_fire,
                        l.pick(local_data, l.num(3, 1),
                               l.pick(l.eq(held.at("opcode"), 5), l.num(3, 2), l.num(3, 0))),
                        l.pick(rfire, l.num(3, 1), l.num(3, 0)))},
      {"denied", l.either(local_fire, l.both(bfire, failed))},
      {"corrupt", l.either(l.both(local_fire, local_data), l.both(rfire, failed))},
      {"data", l.pick(l.both(rfire, l.inv(failed)), outputs.at("rdata"), l.num(32, 0))}};
  for (const auto &[name, value] : d) {
    l.b.update(value, l.pick(load, next.at(name), value));
    if (name != "source" || p.tl_source_bits)
      l.out("tl", "d_" + name, value);
  }
  l.b.update(local_response, l.pick(load, local_fire, local_response));
  l.b.update(full, l.pick(load, l.bit(1), l.pick(d_take, l.bit(0), full)));
  l.out("tl", "d_valid", l.active(full));
  l.out("tl", "d_param", l.num(2, 0));
  if (p.tl_sink_bits)
    l.out("tl", "d_sink", l.num(p.tl_sink_bits, 0));
  l.bind("wr.b_take", bfire);
}
void TileLinkIngress::finish(const Fields &outputs) {
  for (const auto &[ch, flag] :
       std::vector<std::pair<std::string, ValueId>>{{"aw", aw}, {"w", w}, {"ar", ar}})
    l.b.update(flag, l.pick(l.bindings.at("tl.start_" + ch), l.bit(1),
                            l.pick(l.both(flag, outputs.at(ch + "ready")), l.bit(0), flag)));
  const auto done = l.both(l.both(l.either(aw, w), l.either(l.inv(aw), outputs.at("awready"))),
                           l.either(l.inv(w), outputs.at("wready")));
  l.b.update(pending,
             l.pick(l.bindings.at("tl.issue"), l.bit(1),
                    l.pick(l.either(done, l.both(ar, outputs.at("arready"))), l.bit(0), pending)));
  for (unsigned k = 0; k < slots.size(); ++k) {
    const auto &slot = slots[k];
    const auto allocate = l.both(first, l.eq(free_slot, k));
    const auto release =
        l.both(l.both(d_take, l.either(l.inv(local_response), l.eq(local_left, 0))),
               l.same(slot.source, d.at("source")));
    l.b.update(slot.busy, l.pick(allocate, l.bit(1), l.pick(release, l.bit(0), slot.busy)));
    l.b.update(slot.source, l.pick(allocate, a.at("source"), slot.source));
    l.b.update(slot.size, l.pick(allocate, a.at("size"), slot.size));
  }
  response(outputs);
}
} // namespace nocloom::detail
