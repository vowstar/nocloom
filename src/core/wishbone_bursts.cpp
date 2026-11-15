#include "diagnostic.hpp"
#include "outlet_base.hpp"
#include "packet_context.hpp"
#include "wishbone.hpp"

namespace nocloom::detail {
namespace {
using logic::Fields;
using outlet::next_address;
using outlet::stride;

// Write banks are a FIFO before the single bus owner. Responses keep their own identity.
struct WishboneBurstOutlet : outlet::OutletBase {
  struct Bank {
    Fields descriptor;
    std::vector<ValueId> data, mask;
    ValueId used, ready, count, prefix, uniform, first_mask, fail, exclusive;
  };
  bool word, retry;
  Fields w, r, rd, d, starting;
  std::vector<Bank> banks;
  ValueId fill, head, filling, received, wh, wt, rt;
  ValueId busy, write, finished, phase, cursor, address, result, first;
  ValueId prefix, mode, wrap, exclusive, failing;
  ValueId bus_address, bus_data, bus_mask, bus_cti, bus_bte;
  ValueId ack, error_in, read_data, bus_done, service, packet_done, completion_code;
  ValueId rpop, bpop, rpush, bpush;
  packet_context::Table rq, bq;
  Fields current, following;

  WishboneBurstOutlet(const BurstOutletProfile &profile, bool word_, bool retry_)
      : OutletBase(profile), word(word_), retry(retry_) {}
  ValueId select(ValueId which, ValueId a, ValueId b) { return l.pick(which, b, a); }
  Fields select(ValueId which, const Fields &a, const Fields &b) {
    Fields out;
    for (const auto &[name, value] : a)
      out[name] = select(which, value, b.at(name));
    return out;
  }
  ValueId chosen(ValueId Bank::*field) { return select(head, banks[0].*field, banks[1].*field); }
  ValueId nonzero(ValueId v) { return l.inv(l.eq(v, 0)); }
  ValueId all(std::initializer_list<ValueId> values) {
    return l.every(std::span(values.begin(), values.size()));
  }
  Fields descriptor(const std::string &name, const Fields &shape) {
    auto out = hold(name, shape);
    out["error"] = l.state(name + ".error", 1);
    return out;
  }
  Fields metadata(const Fields &desc) {
    Fields out;
    for (const auto *name : {"size", "id", "source", "tag"})
      if (desc.contains(name))
        out[name] = desc.at(name);
    if (p.response_run_bits)
      out["run_end"] = one;
    return out;
  }
  ValueId wrap_type(const Fields &desc) {
    auto bte = l.num(2, 0);
    for (unsigned k = 1; k <= 3; ++k)
      bte = l.pick(l.eq(desc.at("length"), (2U << k) - 1), l.num(2, k), bte);
    return l.pick(l.eq(desc.at("burst"), 2), bte, l.num(2, 0));
  }
  // A read plan uses one mapping throughout its physical sequence.
  ValueId contained(const Fields &desc) {
    if (p.mappings.empty())
      return one;
    const auto a = desc.at("address");
    const auto span = l.shl(l.inc(l.fit(desc.at("length"), bits)), desc.at("size"), 2);
    const auto start = l.pick(l.eq(desc.at("burst"), 2), l.floor(a, span), a);
    const auto last = l.pick(l.eq(desc.at("burst"), 0), a, l.dec(l.plus(start, span)));
    std::vector<ValueId> hits;
    std::ranges::transform(p.mappings, std::back_inserter(hits), [&](const AddressMapping &m) {
      const auto first_hit = mapping_decode::hit(l, start, bits, m);
      const auto last_hit = mapping_decode::hit(l, last, bits, m);
      return l.both(first_hit, last_hit);
    });
    return l.any(hits);
  }
  void storage();
  void collect();
  void classify();
  Fields at(ValueId index, ValueId addr);
  void issue();
  void complete();
  void retire();
  NodeHardware build();
};

void WishboneBurstOutlet::storage() {
  w = request("wreq");
  r = request("rreq");
  fill = l.state("w.fill_bank", 1);
  head = l.state("w.head_bank", 1);
  filling = l.state("w.filling", 1);
  received = l.state("w.received", 6);
  busy = l.state("bus.busy", 1);
  write = l.state("bus.write", 1);
  finished = l.state("bus.finished", 1);
  phase = l.state("bus.phase", 2); // idle, active, pause, boundary
  cursor = l.state("bus.cursor", 6);
  address = l.address_state("bus.address", bits);
  result = l.state("bus.result", 2);
  first = l.state("bus.first", 1);
  prefix = l.state("bus.prefix", 6);
  mode = l.state("bus.mode", 2); // classic, linear, wrap, fixed
  wrap = l.state("bus.wrap", 2);
  exclusive = monitored ? l.state("bus.exclusive", 1) : zero;
  failing = monitored ? l.state("bus.exclusive_fail", 1) : zero;
  bus_address = l.state("wb.adr", p.address_bits - (word ? 2 : 0));
  bus_data = l.state("wb.dat_w", 32);
  bus_mask = l.state("wb.sel", 4);
  bus_cti = l.state("wb.cti", 3);
  bus_bte = l.state("wb.bte", 2);
  for (unsigned k = 0; k < 2; ++k) {
    const auto stem = "w.bank" + std::to_string(k);
    Bank bank;
    bank.descriptor = descriptor(stem, w);
    bank.used = l.state(stem + ".used", 1);
    bank.ready = l.state(stem + ".ready", 1);
    bank.count = l.state(stem + ".count", 6);
    bank.prefix = l.state(stem + ".prefix", 6);
    bank.uniform = l.state(stem + ".uniform", 1);
    bank.first_mask = l.state(stem + ".first_mask", 4);
    bank.fail = monitored ? l.state(stem + ".fail", 1) : zero;
    bank.exclusive = monitored ? l.state(stem + ".exclusive", 1) : zero;
    for (unsigned j = 0; j < 32; ++j) {
      bank.data.push_back(l.state(stem + ".data" + std::to_string(j), 32));
      bank.mask.push_back(l.state(stem + ".mask" + std::to_string(j), 4));
    }
    banks.push_back(std::move(bank));
  }
  rd = descriptor("r.descriptor", r);
  d = descriptor("bus.descriptor", r);
  auto shape = metadata(d);
  shape.insert({{"address", address},
                {"result", ok},
                {"data", l.num(32, 0)},
                {"first", one},
                {"last", one}});
  if (monitored)
    shape["exclusive_okay"] = zero;
  rq = packet_context::make(l, "r.response", 1, 2, shape);
  bq = packet_context::make(l, "w.response", 1, 2, shape);
}

void WishboneBurstOutlet::collect() {
  const auto free = l.inv(select(fill, banks[0].used, banks[1].used));
  const auto ready = active(l.either(filling, free));
  out("wreq", "ready", ready);
  wt = l.bind("w.accept", l.both(w.at("valid"), ready));
  wh = l.bind("w.head", l.both(wt, l.inv(filling)));
  l.b.update(filling, l.pick(wt, l.inv(w.at("last")), filling));
  l.b.update(received, l.pick(wt, l.pick(w.at("last"), l.num(6, 0), l.inc(received)), received));
  l.b.update(fill, l.pick(l.both(wt, w.at("last")), l.inv(fill), fill));
  const auto fail = monitored ? watch(w) : zero;
  const auto code = local_result(w);
  const auto full = all({l.eq(w.at("be"), 15), l.eq(w.at("size"), 2), l.eq(code, 0), l.inv(fail)});
  for (unsigned k = 0; k < 2; ++k) {
    const auto &bank = banks[k];
    const auto take = l.both(wt, l.eq(fill, k));
    const auto start = l.both(wh, l.eq(fill, k));
    load(bank.descriptor, w, start);
    l.b.update(bank.count, l.pick(take, l.inc(received), bank.count));
    const auto extending = l.same(bank.prefix, received);
    const auto next_prefix = l.pick(l.both(full, l.either(start, extending)), l.inc(received),
                                    l.pick(start, l.num(6, 0), bank.prefix));
    l.b.update(bank.prefix, l.pick(take, next_prefix, bank.prefix));
    l.b.update(bank.uniform,
               l.pick(take,
                      l.either(start, l.both(bank.uniform, l.same(bank.first_mask, w.at("be")))),
                      bank.uniform));
    l.b.update(bank.first_mask, l.pick(start, w.at("be"), bank.first_mask));
    if (monitored) {
      l.b.update(bank.fail, l.pick(start, fail, bank.fail));
      l.b.update(bank.exclusive, l.pick(start, w.at("lock"), bank.exclusive));
    }
    for (unsigned j = 0; j < 32; ++j) {
      const auto put = l.both(take, l.eq(received, j));
      l.b.update(bank.data[j], l.pick(put, w.at("data"), bank.data[j]));
      l.b.update(bank.mask[j], l.pick(put, w.at("be"), bank.mask[j]));
    }
  }
}

void WishboneBurstOutlet::classify() {
  const auto pending = l.state("r.pending", 1);
  const auto rready = active(all({l.inv(pending), l.inv(busy), l.inv(banks[0].used),
                                  l.inv(banks[1].used), l.inv(w.at("valid"))}));
  out("rreq", "ready", rready);
  rt = l.bind("r.accept", l.both(r.at("valid"), rready));
  load(rd, r, rt);
  const auto start_w = all({l.inv(busy), chosen(&Bank::ready)});
  const auto start_r = l.both(l.inv(busy), pending);
  const auto start = l.either(start_w, start_r);
  auto wd = select(head, banks[0].descriptor, banks[1].descriptor);
  wd["length"] = l.fit(l.dec(chosen(&Bank::count)), p.packet_len_bits);
  const auto desc = select(start_w, rd, wd);
  load(d, desc, start);
  starting = desc;
  l.b.update(pending, l.pick(rt, one, l.both(pending, l.inv(start_r))));
  const auto count = l.inc(l.fit(desc.at("length"), 6));
  const auto valid_read =
      all({l.eq(local_result(desc), 0), contained(desc), l.eq(desc.at("size"), 2)});
  const auto full_prefix =
      l.pick(start_w, chosen(&Bank::prefix), l.pick(valid_read, count, l.num(6, 0)));
  const auto full = l.same(full_prefix, count);
  const auto bte = wrap_type(desc);
  const auto wrapping = all({full, nonzero(bte), l.eq(desc.at("size"), 2)});
  const auto fixed =
      all({l.eq(desc.at("burst"), 0), l.eq(desc.at("size"), 2),
           l.pick(start_w, l.both(chosen(&Bank::uniform), nonzero(chosen(&Bank::first_mask))),
                  valid_read)});
  auto next_mode = l.pick(all({l.eq(desc.at("burst"), 1), l.below(l.num(6, 1), full_prefix)}),
                          l.num(2, 1), l.num(2, 0));
  next_mode = l.pick(wrapping, l.num(2, 2), next_mode);
  next_mode = l.pick(l.both(fixed, l.below(l.num(6, 1), count)), l.num(2, 3), next_mode);
  for (const auto &[state, value] : {std::pair{write, start_w},
                                     {cursor, l.num(6, 0)},
                                     {address, desc.at("address")},
                                     {first, one},
                                     {mode, next_mode},
                                     {wrap, bte},
                                     {prefix, full_prefix}})
    l.b.update(state, l.pick(start, value, state));
  if (monitored) {
    const auto rd_exclusive = l.latch("r.exclusive", r.at("lock"), rt);
    l.b.update(exclusive,
               l.pick(start, l.pick(start_w, chosen(&Bank::exclusive), rd_exclusive), exclusive));
    l.b.update(failing, l.pick(start, l.both(start_w, chosen(&Bank::fail)), failing));
    reserve(w, r, rt, l.both(wh, l.inv(l.bindings.at("w.exclusive_failing"))),
            l.both(wh, l.inv(l.bindings.at("w.exclusive_failing"))),
            {{banks[0].used, banks[0].fail, &banks[0].descriptor},
             {banks[1].used, banks[1].fail, &banks[1].descriptor}},
            true);
  }
  l.bind("packet.start", start);
}

Fields WishboneBurstOutlet::at(ValueId index, ValueId addr) {
  auto f = d;
  f["address"] = addr;
  const auto code = local_result(addr, d.at("error"), d.at("burst"), d.at("size"), false);
  auto checked = code;
  if (p.read_only)
    checked = l.pick(l.both(write, l.eq(code, 0)), target_error, checked);
  if (p.write_only)
    checked = l.pick(all({l.inv(write), l.eq(code, 0)}), target_error, checked);
  auto mask = select(head, l.by_index(l.fit(index, 5), banks[0].mask),
                     l.by_index(l.fit(index, 5), banks[1].mask));
  mask = l.pick(write, mask, read_mask(addr, d.at("size")));
  mask = l.pick(l.either(nonzero(checked), failing), l.num(4, 0), mask);
  const auto data = select(head, l.by_index(l.fit(index, 5), banks[0].data),
                           l.by_index(l.fit(index, 5), banks[1].data));
  const auto last = l.same(l.fit(index, p.packet_len_bits), d.at("length"));
  const auto in_prefix = l.below(index, prefix);
  const auto linear = l.both(l.eq(mode, 1), in_prefix);
  const auto tagged = l.either(linear, l.either(l.eq(mode, 2), l.eq(mode, 3)));
  const auto ending = l.either(last, l.both(linear, l.same(l.inc(index), prefix)));
  auto cti = l.pick(l.eq(mode, 3), l.num(3, 1), l.num(3, 2));
  cti = l.pick(tagged, l.pick(ending, l.num(3, 7), cti), l.num(3, 0));
  f.insert({{"code", checked},
            {"mask", mask},
            {"data", data},
            {"last", last},
            {"cti", cti},
            {"bte", l.pick(l.eq(mode, 2), wrap, l.num(2, 0))}});
  return f;
}

void WishboneBurstOutlet::issue() {
  ack = l.in("wb", "ack", 1);
  error_in = l.in("wb", "err", 1);
  if (retry)
    error_in = l.either(error_in, l.in("wb", "rty", 1));
  read_data = l.in("wb", "dat_r", 32);
  bus_done = l.bind("bus.complete", active(l.both(l.eq(phase, 1), l.either(ack, error_in))));
  const auto rv = packet_context::valid(l, rq, zero);
  const auto bv = packet_context::valid(l, bq, zero);
  rpop = l.both(rv, l.in("rrsp", "ready", 1));
  bpop = l.both(bv, l.in("wrsp", "ready", 1));
  current = at(cursor, address);
  const auto following_address = next_address(l, address, d, bits);
  following = at(l.inc(cursor), following_address);
  const auto room = l.either(l.below(rq.count[0], l.num(2, 2)), rpop);
  const auto idle = l.inv(l.eq(phase, 1));
  const auto open = active(all({busy, l.inv(finished), idle, l.either(write, room)}));
  const auto local = l.both(open, l.eq(current.at("mask"), 0));
  service = l.bind("bus.service", l.either(bus_done, local));
  packet_done = l.bind("packet.done", l.both(service, current.at("last")));
  rpush = l.both(service, l.inv(write));
  const auto after = l.plus(l.minus(rq.count[0], l.fit(rpop, 2)), l.fit(rpush, 2));
  const auto next_room = l.below(after, l.num(2, 2));
  const auto continues = all({bus_done, l.either(l.eq(bus_cti, 1), l.eq(bus_cti, 2))});
  const auto advance = l.both(continues, l.either(write, next_room));
  const auto launch = l.both(open, nonzero(current.at("mask")));
  const auto issue = l.bind("bus.issue", l.either(launch, advance));
  const auto selected = select(advance, current, following);
  auto local_addr = p.mappings.empty() ? l.fit(selected.at("address"), p.address_bits)
                                       : localized(selected.at("address"), p.address_bits);
  local_addr =
      word ? l.b.slice(local_addr, 2, p.address_bits - 2)
           : l.both(local_addr, l.num(p.address_bits, (std::uint64_t{1} << p.address_bits) - 4));
  for (const auto &[state, value] : {std::pair{bus_address, local_addr},
                                     {bus_data, l.pick(write, selected.at("data"), l.num(32, 0))},
                                     {bus_mask, selected.at("mask")},
                                     {bus_cti, selected.at("cti")},
                                     {bus_bte, selected.at("bte")}})
    l.b.update(state, l.pick(issue, value, state));
  auto next_phase = l.pick(l.eq(phase, 3), l.num(2, 0), phase);
  next_phase = l.pick(bus_done, l.pick(continues, l.num(2, 2), l.num(2, 3)), next_phase);
  next_phase = l.pick(issue, l.num(2, 1), next_phase);
  l.b.update(phase, next_phase);
  // Only the independent sequencer drives these pins. ACK changes no output until an edge.
  const auto cyc = l.state("wb.cyc", 1), stb = l.state("wb.stb", 1);
  l.b.update(cyc, l.either(l.eq(next_phase, 1), l.eq(next_phase, 2)));
  l.b.update(stb, l.eq(next_phase, 1));
  l.out("wb", "cyc", cyc);
  l.out("wb", "stb", stb);
  l.out("wb", "we", write);
  for (const auto &[name, state] : Fields{{"adr", bus_address},
                                          {"dat_w", bus_data},
                                          {"sel", bus_mask},
                                          {"cti", bus_cti},
                                          {"bte", bus_bte}})
    l.out("wb", name, state);
}

void WishboneBurstOutlet::complete() {
  auto code = l.pick(l.both(bus_done, error_in), target_error, current.at("code"));
  if (monitored)
    code = l.pick(failing, ok, code);
  completion_code = code;
  const auto start = l.bindings.at("packet.start");
  l.b.update(result,
             l.pick(start, ok, l.pick(service, outlet::merge_error(l, result, code), result)));
  l.b.update(cursor, l.pick(start, l.num(6, 0), l.pick(service, l.inc(cursor), cursor)));
  const auto next = next_address(l, address, d, bits);
  l.b.update(address, l.pick(start, starting.at("address"), l.pick(service, next, address)));
  l.b.update(first, l.pick(start, one, l.pick(service, zero, first)));
  auto response = metadata(d);
  response.insert(
      {{"address", address}, {"result", code}, {"first", first}, {"last", current.at("last")}});
  auto data = l.num(32, 0);
  for (unsigned lane = 0; lane < 4; ++lane) {
    const auto byte = l.pick(all({l.eq(code, 0), l.b.slice(current.at("mask"), lane, 1)}),
                             l.b.slice(read_data, lane * 8, 8), l.num(8, 0));
    data = lane ? l.b.concat(byte, l.b.slice(data, 0, lane * 8)) : l.fit(byte, 32);
  }
  response["data"] = data;
  if (p.response_run_bits)
    response["run_end"] = burst_run_end(l, d, address, 32, p.response_run_bits);
  if (monitored)
    response["exclusive_okay"] = l.both(exclusive, l.eq(code, 0));
  packet_context::step(l, rq, rpush, zero, response, rpop, zero);
  const auto front = packet_context::front(l, rq, zero);
  response = front;
  this->response("rrsp", response, packet_context::valid(l, rq, zero), front.at("address"),
                 front.at("result"), front.at("first"), front.at("last"), front.at("data"),
                 monitored ? front.at("exclusive_okay") : zero);
}

void WishboneBurstOutlet::retire() {
  const auto start = l.bindings.at("packet.start");
  const auto bspace = l.either(packet_context::room(l, bq, zero), bpop);
  bpush = l.both(all({busy, write, l.either(finished, packet_done)}), bspace);
  // A bank frees only after its result and identity have entered the completion FIFO.
  const auto freed = bpush;
  for (unsigned k = 0; k < 2; ++k) {
    const auto &bank = banks[k];
    const auto start_bank = l.both(wh, l.eq(fill, k));
    const auto tail_bank = all({wt, l.eq(fill, k), w.at("last")});
    const auto freeing = l.both(freed, l.eq(head, k));
    l.b.update(bank.used, l.pick(start_bank, one, l.both(bank.used, l.inv(freeing))));
    l.b.update(bank.ready, l.pick(tail_bank, one, l.both(bank.ready, l.inv(freeing))));
  }
  l.b.update(head, l.pick(freed, l.inv(head), head));
  const auto done = l.either(freed, l.both(packet_done, l.inv(write)));
  l.b.update(busy, l.pick(start, one, l.both(busy, l.inv(done))));
  l.b.update(finished, l.pick(start, zero, l.either(finished, packet_done)));
  const auto code = l.pick(packet_done, outlet::merge_error(l, result, completion_code), result);
  auto response = metadata(d);
  response.insert({{"address", d.at("address")},
                   {"result", code},
                   {"data", l.num(32, 0)},
                   {"first", one},
                   {"last", one}});
  if (monitored)
    response["exclusive_okay"] = all({exclusive, l.inv(failing), l.eq(code, 0)});
  packet_context::step(l, bq, bpush, zero, response, bpop, zero);
  const auto front = packet_context::front(l, bq, zero);
  this->response("wrsp", front, packet_context::valid(l, bq, zero), front.at("address"),
                 front.at("result"), one, one, front.at("data"),
                 monitored ? front.at("exclusive_okay") : zero);
  l.bind("r.last", packet_context::front(l, rq, zero).at("last"));
  l.bind("packet.retire", done);
}
NodeHardware WishboneBurstOutlet::build() {
  storage();
  collect();
  classify();
  issue();
  complete();
  retire();
  l.b.ir.name = "nocloom_wishbone_burst_outlet_" + p.name;
  return {std::move(l.b.ir), std::move(l.bindings)};
}
} // namespace
Result<NodeHardware> lower_wishbone_burst_target(const BurstOutletProfile &profile, bool word,
                                                 bool retry) {
  if (profile.timeout_cycles || profile.timeout_completion)
    return Result<NodeHardware>::failure(error(
        "NCL_WISHBONE_OPTION", "timeout", "Wishbone burst tags require a target without timeout"));
  if (profile.data_bits != 32 || profile.apb_data_bits != 32 || profile.flows != 1)
    return Result<NodeHardware>::failure(
        error("NCL_WISHBONE_OPTION", "wishbone", "Wishbone burst targets require one 32-bit port"));
  if (auto bad = burst_outlet_error(profile))
    return Result<NodeHardware>::failure(*bad);
  return Result<NodeHardware>::success(WishboneBurstOutlet(profile, word, retry).build());
}
} // namespace nocloom::detail
