#include "service_outlet.hpp"
#include "diagnostic.hpp"
#include "logic.hpp"
#include "mapping_decode.hpp"
#include "message.hpp"
#include <algorithm>
#include <array>
#include <bit>
#include <iterator>
#include <numeric>

namespace nocloom::detail {
namespace {
constexpr unsigned word_bits = 10, bus_bits = 32, status_bits = 2;
constexpr unsigned command_read = 1, command_write = 2, status_request = 1, status_error = 2;
constexpr unsigned last_address_word = 3, last_data_word = 7;
constexpr unsigned result_miss = 1, result_error = 2, byte_mark = 9, key_low_bits = 9;
} // namespace

Result<NodeHardware> lower_service_outlet(const ServiceOutletProfile &p) {
  if (p.window_bits > 20 || p.bank_bits > p.window_bits || p.route >> 11 != 0)
    return Result<NodeHardware>::failure(
        error("NCL_INTERNAL_PLAN", "service", "The bank does not fit the service window"));
  logic::Logic l;
  auto &b = l.b;
  l.block = p.arrival;
  l.block_bits = p.transport_address_bits;
  BurstProfile layout{p.id_bits, p.len_bits, true, p.transport_address_bits};
  layout.exclusive = p.exclusive;
  layout.tag_bits = p.tag_bits;
  layout.user_bits = p.user_bits;
  layout.response_user_bits = p.response_user_bits;
  const auto w = l.arrive(l.face("wreq", burst_request_fields(bus_bits, layout, p.source_bits)));
  const auto r = l.arrive(l.face("rreq", burst_request_fields(bus_bits, layout, p.source_bits)));
  const auto hint_in = [&](const char *name, unsigned bits) {
    return b.input(std::string("nl_hint_") + name, bits, "hint", name);
  };
  const auto hint_out = [&](const char *name, ValueId value) {
    b.output(std::string("nl_hint_") + name, value, "hint", name);
  };
  const auto bank_ready = hint_in("bank_ready", 1);
  const auto out_valid = hint_in("bank_out_valid", 1);
  const auto out_data = hint_in("bank_out_data", word_bits);
  const auto out_tail = hint_in("bank_out_tail", 1);
  const auto wrsp_ready = l.in("wrsp", "ready", 1), rrsp_ready = l.in("rrsp", "ready", 1);

  // The word in service, one face at a time.
  const auto writing = w.at("valid");
  const auto offered = l.active(l.either(writing, r.at("valid")));
  const auto address = l.pick(writing, w.at("address"), r.at("address"));
  const auto miss = l.bind("bus.miss", l.pick(writing, w.at("error"), r.at("error")));
  const auto offset = p.mapping.local_address
                          ? mapping_decode::local(l, address, p.transport_address_bits, {p.mapping})
                          : l.fit(b.slice(address, 0, p.window_bits), 20);
  // The bank a word is for, with several: its key and its offset in it.
  std::vector<ValueId> hits;
  const auto offset_bits = p.mapping.local_address ? p.transport_address_bits : 20;
  const auto offset_bank = b.slice(offset, p.bank_bits, offset_bits - p.bank_bits);
  std::ranges::transform(p.places, std::back_inserter(hits),
                         [&](const ServiceOutletProfile::Place &at) {
                           return l.eq(offset_bank, at.base >> p.bank_bits);
                         });
  const auto among = [&](unsigned bits, auto &&value) {
    auto joined = l.num(bits, 0);
    for (std::size_t k = 0; k < hits.size(); ++k)
      joined = l.either(joined, l.pick(hits[k], l.num(bits, value(p.places[k])), l.num(bits, 0)));
    return joined;
  };
  const auto inside = l.bind("bus.inside", p.places.empty() ? l.eq(offset_bank, 0) : l.any(hits));
  const auto step = l.state("bus.step", 3);
  const auto in_bank =
      p.places.empty() ? offset : l.bind("bus.in_bank", l.fit(b.slice(offset, 0, p.bank_bits), 20));
  const auto high = l.pick(inside, b.slice(in_bank, 10, word_bits), l.num(word_bits, 0));
  const auto low = l.pick(inside, b.slice(in_bank, 0, word_bits), l.num(word_bits, 0));
  // One route with one bank, else the hit bank's.
  const auto chosen_key =
      p.places.empty()
          ? ValueId{}
          : l.bind("bus.key",
                   among(11, [](const ServiceOutletProfile::Place &at) { return at.route; }));
  const auto command =
      b.concat(l.pick(writing, l.num(4, command_write), l.num(4, command_read)),
               b.concat(p.places.empty() ? l.num(2, p.route >> key_low_bits)
                                         : b.slice(chosen_key, key_low_bits, 2),
                        b.concat(l.num(2, 0), l.pick(inside, l.num(status_bits, status_request),
                                                     l.num(status_bits, status_error)))));
  std::vector<ValueId> arms{p.places.empty()
                                ? l.num(word_bits, p.route & 0x1FFU)
                                : l.fit(b.slice(chosen_key, 0, key_low_bits), word_bits),
                            command, low, high};
  for (unsigned k = 4; k-- > 0;)
    arms.push_back(
        b.concat(b.slice(w.at("be"), k, 1), b.concat(l.bit(0), b.slice(w.at("data"), 8 * k, 8))));
  const auto word = l.by_index(step, arms);
  const auto serving = l.bind("bus.serving", l.both(offered, l.inv(miss)));
  const auto data_words = l.both(writing, inside);
  l.bind("bus.step_three", l.eq(step, last_address_word));
  const auto at_last = l.bind(
      "bus.at_last", l.pick(data_words, l.eq(step, last_data_word), l.eq(step, last_address_word)));
  hint_out("bank_valid", serving);
  hint_out("bank_data", l.bind("bus.word", word));
  hint_out("bank_head", l.eq(step, 0));
  hint_out("bank_tail", at_last);
  if (std::ranges::any_of(p.places, [](const auto &at) { return at.branch != 0; })) {
    const auto most = std::ranges::max(p.places, {}, [](const auto &at) { return at.branch; });
    hint_out("bank_branch",
             l.bind("bus.branch",
                    among(std::max(1U, static_cast<unsigned>(std::bit_width(most.branch))),
                          [](const ServiceOutletProfile::Place &at) { return at.branch; })));
  }
  const auto moved = l.both(serving, bank_ready);
  l.b.update(step, l.pick(l.both(moved, at_last), l.num(3, 0), l.pick(moved, l.inc(step), step)));
  // A word leaves its face at its last serial word; a decode miss at once.
  const auto done = l.either(miss, l.both(bank_ready, at_last));
  l.out("wreq", "ready", l.bind("w.take", done));
  l.out("rreq", "ready", l.bind("r.take", done));
  const auto taken = l.bind("bus.taken", l.both(offered, done));

  // What the response takes from its word.
  const auto stored_write = l.latch("bus.write", writing, taken);
  const auto local = l.state("bus.local", 1);
  const auto failed = l.latch("bus.failed", l.either(miss, l.inv(inside)), taken);
  const auto missed = l.latch("bus.missed", miss, taken);
  // The read data bytes before the last, oldest highest.
  const auto gathered = l.state("r.gather", 24);
  const auto byte = b.slice(out_data, 0, 8);
  const auto is_byte = l.both(out_valid, b.slice(out_data, byte_mark, 1));
  const auto bank_answer = l.both(out_valid, out_tail);
  const auto answer = l.either(local, bank_answer);
  const auto taken_answer = l.pick(stored_write, wrsp_ready, rrsp_ready);
  hint_out("bank_out_ready", l.either(l.inv(out_tail), taken_answer));
  b.update(gathered, l.pick(l.both(is_byte, l.inv(out_tail)),
                            b.concat(b.slice(gathered, 0, 16), byte), gathered));
  b.update(local, l.either(l.both(taken, miss), l.both(local, l.inv(taken_answer))));
  const auto result =
      l.pick(failed, l.pick(missed, l.num(2, result_miss), l.num(2, result_error)), l.num(2, 0));
  const auto data =
      l.bind("bus.read_word", l.pick(failed, l.num(bus_bits, 0), b.concat(gathered, byte)));
  const auto source =
      p.source_bits ? l.latch("bus.source", l.pick(writing, w.at("source"), r.at("source")), taken)
                    : ValueId{};
  const auto id =
      p.id_bits ? l.latch("bus.id", l.pick(writing, w.at("id"), r.at("id")), taken) : ValueId{};
  const auto tag =
      p.tag_bits ? l.latch("bus.tag", l.pick(writing, w.at("tag"), r.at("tag")), taken) : ValueId{};
  for (const auto *face : {"wrsp", "rrsp"}) {
    const std::string name = face;
    const bool for_write = name == "wrsp";
    l.out(name, "valid", l.active(l.both(answer, for_write ? stored_write : l.inv(stored_write))));
    l.out(name, "address", l.num(p.transport_address_bits, p.arrival.base));
    l.out(name, "result", result);
    if (p.exclusive)
      l.out(name, "exclusive_okay", l.bit(0));
    if (p.response_user_bits)
      l.out(name, "user", l.num(p.response_user_bits, 0));
    l.out(name, "count", l.num(burst_count_bits(bus_bits), 4));
    l.out(name, "first", l.bit(1));
    l.out(name, "last", l.bit(1));
    l.out(name, "error", l.bit(0));
    l.out(name, "data", data);
    if (p.tag_bits)
      l.out(name, "tag", tag);
    if (p.id_bits)
      l.out(name, "id", id);
    if (p.source_bits)
      l.out(name, "source", source);
  }
  if (p.response_run_bits)
    hint_out("wrap_end", l.bit(1));
  return l.finish("nocloom_service_outlet_" + p.name);
}

Result<NodeHardware> lower_service_switch(const std::string &name, unsigned branches) {
  if (branches < 2)
    return Result<NodeHardware>::failure(
        error("NCL_INTERNAL_PLAN", "service", "A service switch has at least two branches"));
  logic::Logic l;
  auto &b = l.b;
  const auto hint_in = [&](const std::string &signal, unsigned bits) {
    return b.input("nl_hint_" + signal, bits, "hint", signal);
  };
  const auto hint_out = [&](const std::string &signal, ValueId value) {
    b.output("nl_hint_" + signal, value, "hint", signal);
  };
  const auto branch_bits = std::max(1U, static_cast<unsigned>(std::bit_width(branches - 1)));
  const auto in_valid = hint_in("in_valid", 1), in_head = hint_in("in_head", 1);
  const auto in_tail = hint_in("in_tail", 1), in_data = hint_in("in_data", word_bits);
  const auto in_branch = hint_in("in_branch", branch_bits);
  const auto full = l.state("switch.full", 1);
  const auto data = l.state("switch.data", word_bits);
  const auto head = l.state("switch.head", 1), tail = l.state("switch.tail", 1);
  const auto branch = l.state("switch.branch", branch_bits);
  std::vector<ValueId> readies, valids, words, ends;
  for (unsigned k = 0; k < branches; ++k) {
    const auto out = "out" + std::to_string(k), back = "back" + std::to_string(k);
    const auto chosen = l.eq(branch, k);
    readies.push_back(l.both(chosen, hint_in(out + "_ready", 1)));
    hint_out(out + "_valid", l.both(full, chosen));
    hint_out(out + "_data", data);
    hint_out(out + "_head", head);
    hint_out(out + "_tail", tail);
    const auto answer = hint_in(back + "_valid", 1);
    valids.push_back(answer);
    words.push_back(l.pick(answer, hint_in(back + "_data", word_bits), l.num(word_bits, 0)));
    ends.push_back(l.both(answer, hint_in(back + "_tail", 1)));
  }
  const auto gone = l.any(readies);
  const auto room = l.either(l.inv(full), gone);
  hint_out("in_ready", room);
  const auto load = l.both(in_valid, room);
  b.update(full, l.either(load, l.both(full, l.inv(gone))));
  b.update(data, l.pick(load, in_data, data));
  b.update(head, l.pick(load, in_head, head));
  b.update(tail, l.pick(load, in_tail, tail));
  b.update(branch, l.pick(load, in_branch, branch));
  hint_out("back_valid", l.any(valids));
  hint_out("back_tail", l.any(ends));
  hint_out("back_data",
           std::accumulate(words.begin() + 1, words.end(), words.front(),
                           [&](ValueId joined, ValueId word) { return l.either(joined, word); }));
  return l.finish("nocloom_service_switch_" + name);
}
} // namespace nocloom::detail
