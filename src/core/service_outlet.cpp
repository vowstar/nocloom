#include "service_outlet.hpp"
#include "diagnostic.hpp"
#include "link_pipeline.hpp"
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
constexpr unsigned word_bits = register_frame_word_bits, bus_bits = 32;
constexpr unsigned key_bits = register_key_bits, index_bits = register_index_bits;
// The step of a frame: zero at its header, four to seven at its data words.
constexpr unsigned first_data_word = 4, last_data_word = 7;
constexpr unsigned result_miss = 1, result_error = 2;
// The offset bits of the largest window, 1 MiB.
constexpr unsigned window_offset_bits = 20;
} // namespace

Result<NodeHardware> lower_service_outlet(const ServiceOutletProfile &p, bool shared_read) {
  if (p.packet_len_bits < 4 || p.packet_len_bits > 10)
    return Result<NodeHardware>::failure(
        error("NCL_INTERNAL_PLAN", "service.packet_len_bits", "Packet length requires 4..10 bits"));
  if (p.window_bits > window_offset_bits || p.bank_bits > p.window_bits || p.bank_bits < 2 ||
      p.bank_bits > index_bits + 2 || p.key >> key_bits != 0)
    return Result<NodeHardware>::failure(
        error("NCL_INTERNAL_PLAN", "service", "The bank does not fit the service window"));
  logic::Logic l;
  auto &b = l.b;
  l.block = p.arrival;
  l.block_bits = p.transport_address_bits;
  BurstProfile layout{p.id_bits, p.packet_len_bits, true, p.transport_address_bits};
  layout.exclusive = p.exclusive;
  layout.tag_bits = p.tag_bits;
  layout.user_bits = p.user_bits;
  layout.response_user_bits = p.response_user_bits;
  layout.data_user_bits_per_byte = p.data_user_bits_per_byte;
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
  const bool translated = p.mapping.local_address || !std::has_single_bit(p.mapping.size);
  const auto offset = translated
                          ? mapping_decode::local(l, address, p.transport_address_bits, {p.mapping})
                          : l.fit(b.slice(address, 0, p.window_bits), window_offset_bits);
  // The bank a word is for, with several: its key and its offset in it.
  std::vector<ValueId> hits;
  const auto offset_bits = translated ? p.transport_address_bits : window_offset_bits;
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
  // The header: the hit bank's key above the register's index in the bank.
  const auto index = l.fit(b.slice(offset, 2, p.bank_bits - 2), index_bits);
  const auto key =
      p.places.empty()
          ? l.num(key_bits, p.key)
          : l.bind("bus.key",
                   among(key_bits, [](const ServiceOutletProfile::Place &at) { return at.key; }));
  std::vector<ValueId> bytes;
  for (unsigned k = 4; k-- > 0;)
    bytes.push_back(b.concat(b.slice(w.at("be"), k, 1), b.slice(w.at("data"), 8 * k, 8)));
  const auto at_header = l.bind("bus.at_header", l.eq(step, 0));
  const auto word = l.pick(at_header, b.concat(key, index), l.by_index(b.slice(step, 0, 2), bytes));
  // A word for no bank of the window is answered here, like a decode miss.
  const auto unserved = l.bind("bus.unserved", l.either(miss, l.inv(inside)));
  const auto serving = l.bind("bus.serving", l.both(offered, l.inv(unserved)));
  // A read is its header alone; a write ends at its last data word.
  const auto at_last =
      l.bind("bus.at_last", l.pick(writing, l.eq(step, last_data_word), at_header));
  hint_out("bank_valid", serving);
  hint_out("bank_data", l.bind("bus.word", word));
  hint_out("bank_tail", at_last);
  if (std::ranges::any_of(p.places, [](const auto &at) { return at.branch != 0; })) {
    const auto most = std::ranges::max(p.places, {}, [](const auto &at) { return at.branch; });
    hint_out("bank_branch",
             l.bind("bus.branch",
                    among(std::max(1U, static_cast<unsigned>(std::bit_width(most.branch))),
                          [](const ServiceOutletProfile::Place &at) { return at.branch; })));
  }
  const auto moved = l.both(serving, bank_ready);
  l.b.update(
      step, l.pick(l.both(moved, at_last), l.num(3, 0),
                   l.pick(moved, l.pick(at_header, l.num(3, first_data_word), l.inc(step)), step)));
  // A word leaves its face at its last serial word; one for no bank at once.
  const auto done = l.either(unserved, l.both(bank_ready, at_last));
  l.out("wreq", "ready", l.bind("w.take", done));
  l.out("rreq", "ready", l.bind("r.take", done));
  const auto taken = l.bind("bus.taken", l.both(offered, done));

  // What the response takes from its word.
  const auto stored_write = l.latch("bus.write", writing, taken);
  const auto local = l.state("bus.local", 1);
  const auto failed = l.latch("bus.failed", unserved, taken);
  const auto missed = l.latch("bus.missed", miss, taken);
  // The low byte of each answer word before the last, oldest highest: the three bytes after the
  // header push it out.
  const auto gathered = l.state("r.gather", 24);
  // The last answer word is taken at once and waits here for the response path, so no ready of
  // that path runs back through the banks.
  // The outlet keeps the source, the kind and the result of one word, from the word's request
  // handshake to its answer's response handshake. The network lets no other packet in between
  // the two (bus_open in exchange_composition.cpp), and attach_target_service refuses a target
  // that would. A target that admitted more would need these kept for each word under way, and
  // a count of them here to hold the request faces.
  const auto waiting = l.state("bus.answer", 1);
  const auto kept = l.state("r.last", 8);
  const auto arriving = l.both(out_valid, l.inv(waiting));
  const auto tail_now = l.both(arriving, out_tail);
  const auto answer = l.either(local, waiting);
  const auto taken_answer = l.pick(stored_write, wrsp_ready, rrsp_ready);
  hint_out("bank_out_ready", l.inv(waiting));
  b.update(waiting, l.either(tail_now, l.both(waiting, l.inv(taken_answer))));
  if (shared_read) {
    const auto sample = hint_in("bank_read_sample", 1);
    const auto snapshot = hint_in("bank_read_word", bus_bits);
    b.update(gathered, l.pick(sample, b.slice(snapshot, 8, 24), gathered));
    b.update(kept, l.pick(sample, b.slice(snapshot, 0, 8), kept));
  } else {
    b.update(kept, l.pick(tail_now, b.slice(out_data, 0, 8), kept));
    b.update(gathered,
             l.pick(l.both(arriving, l.inv(out_tail)),
                    b.concat(b.slice(gathered, 0, 16), b.slice(out_data, 0, 8)), gathered));
  }
  b.update(local, l.either(l.both(taken, unserved), l.both(local, l.inv(taken_answer))));
  const auto result =
      l.pick(failed, l.pick(missed, l.num(2, result_miss), l.num(2, result_error)), l.num(2, 0));
  const auto data =
      l.bind("bus.read_word", l.pick(failed, l.num(bus_bits, 0), b.concat(gathered, kept)));
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
    if (p.data_user_bits_per_byte)
      l.out(name, "data_user", l.num(bus_bits / 8 * p.data_user_bits_per_byte, 0));
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

Result<NodeHardware> lower_service_read_frame() {
  logic::Logic l;
  auto &b = l.b;
  const auto sample = l.in("read", "sample", 1);
  const auto word = l.latch("word", l.in("read", "word", bus_bits), sample);
  const auto completed = l.in("access", "complete", 1);
  const auto header = l.latch("header", l.in("access", "header", word_bits), completed);
  const auto step = l.state("step", 3);
  const auto active = l.inv(l.eq(step, 0));
  const auto tail = l.eq(step, last_data_word);
  const auto moved = l.both(active, l.in("response", "ready", 1));
  const auto finished = l.both(moved, tail);
  const auto reading = l.state("reading", 1);
  b.update(reading, l.either(sample, l.both(reading, l.inv(finished))));
  // Read: header at step 3, bytes at steps 4 through 7. Write: header at step 7.
  const auto start = l.pick(l.either(reading, sample), l.num(3, 3), l.num(3, last_data_word));
  b.update(step, l.pick(completed, start, l.pick(moved, l.inc(step), step)));
  std::array<ValueId, 4> bytes;
  for (unsigned k = 0; k < bytes.size(); ++k)
    bytes[k] = b.concat(l.bit(0), b.slice(word, 8 * (3 - k), 8));
  const auto sending_data = l.both(reading, b.slice(step, 2, 1));
  l.out("response", "valid", active);
  l.out("response", "data", l.pick(sending_data, l.by_index(b.slice(step, 0, 2), bytes), header));
  l.out("response", "tail", tail);
  return l.finish("nocloom_service_read_frame");
}

Result<NodeHardware> lower_service_switch(const std::string &name, unsigned branches,
                                          bool registered) {
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
  const auto in_valid = hint_in("in_valid", 1), in_tail = hint_in("in_tail", 1);
  const auto in_data = hint_in("in_data", word_bits);
  const auto in_branch = hint_in("in_branch", branch_bits);
  // Without the register the word on the input goes straight to its branch.
  const auto full = registered ? l.state("switch.full", 1) : in_valid;
  const auto data = registered ? l.state("switch.data", word_bits) : in_data;
  const auto tail = registered ? l.state("switch.tail", 1) : in_tail;
  const auto branch = registered ? l.state("switch.branch", branch_bits) : in_branch;
  std::vector<ValueId> readies, valids, words, ends;
  for (unsigned k = 0; k < branches; ++k) {
    const auto out = "out" + std::to_string(k), back = "back" + std::to_string(k);
    const auto chosen = l.eq(branch, k);
    readies.push_back(l.both(chosen, hint_in(out + "_ready", 1)));
    hint_out(out + "_valid", l.both(full, chosen));
    hint_out(out + "_data", data);
    hint_out(out + "_tail", tail);
    const auto answer = hint_in(back + "_valid", 1);
    valids.push_back(answer);
    words.push_back(l.pick(answer, hint_in(back + "_data", word_bits), l.num(word_bits, 0)));
    ends.push_back(l.both(answer, hint_in(back + "_tail", 1)));
  }
  const auto gone = l.any(readies);
  if (registered) {
    const auto room = l.either(l.inv(full), gone);
    hint_out("in_ready", room);
    const auto load = l.both(in_valid, room);
    b.update(full, l.either(load, l.both(full, l.inv(gone))));
    b.update(data, l.pick(load, in_data, data));
    b.update(tail, l.pick(load, in_tail, tail));
    b.update(branch, l.pick(load, in_branch, branch));
  } else
    hint_out("in_ready", gone);
  hint_out("back_valid", l.any(valids));
  hint_out("back_tail", l.any(ends));
  hint_out("back_data",
           std::accumulate(words.begin() + 1, words.end(), words.front(),
                           [&](ValueId joined, ValueId word) { return l.either(joined, word); }));
  return l.finish((registered ? "nocloom_service_switch_" : "nocloom_register_steer_") + name);
}

Result<NodeHardware> lower_service_stage() {
  logic::Logic l;
  auto &b = l.b;
  const auto hint_in = [&](const char *signal, unsigned bits) {
    return b.input(std::string("nl_hint_") + signal, bits, "hint", signal);
  };
  const auto hint_out = [&](const char *signal, ValueId value) {
    b.output(std::string("nl_hint_") + signal, value, "hint", signal);
  };
  const auto in_valid = hint_in("in_valid", 1), in_tail = hint_in("in_tail", 1);
  const auto in_data = hint_in("in_data", word_bits);
  const auto stage =
      lower_link_pipeline(b, l.clock, l.reset, in_valid, {in_data, in_tail},
                          hint_in("out_ready", 1), LinkPipeline{.slice = PipeSlice::Full}, true);
  for (std::size_t k = 0; k < stage.control.size(); ++k)
    l.bind("stage.control" + std::to_string(k), stage.control[k]);
  for (std::size_t k = 0; k < stage.payload.size(); ++k)
    l.bind("stage.payload" + std::to_string(k), stage.payload[k]);
  hint_out("in_ready", l.bind("stage.ready", stage.ready));
  hint_out("out_valid", l.bind("stage.valid", stage.valid));
  hint_out("out_data", stage.data[0]);
  hint_out("out_tail", stage.data[1]);
  return l.finish("nocloom_service_stage");
}
} // namespace nocloom::detail
