#include "diagnostic.hpp"
#include "ir_component.hpp"
#include "tilelink_uh_outlet.hpp"
#include <array>
#include <bit>

namespace nocloom::detail {
bool tilelink_uh_payload(std::string_view name) {
  if (const auto nested = name.find(".tl."); nested != name.npos)
    name.remove_prefix(nested + 1);
  constexpr std::array fields{
      "tl.cursor",        "tl.base",           "tl.size",      "tl.ddata",
      "tl.dmask",         "tl.assembly",       "tl.assembled", "tl.response.address",
      "tl.response.data", "tl.response_cursor"};
  return name.starts_with("tl.packet.") || name.starts_with("tl.word.") ||
         (name.starts_with("tl.dbuffer") && name.ends_with(".data")) ||
         std::ranges::any_of(fields, [&](std::string_view field) { return name == field; });
}
namespace {
using logic::Fields;
using logic::Logic;
struct ContextPool {
  const TileLinkUhOutletProfile &p;
  const unsigned capacity, index_bits;
  Logic l;
  Fields input, output;
  struct Context {
    Fields inputs, outputs, bound;
    ValueId dependencies{};
  };
  std::vector<Context> contexts;
  struct ReceiveBuffer {
    ValueId valid, owner, data, error;
  };
  std::array<ReceiveBuffer, 2> receive;
  ValueId write_active, write_owner, a_active, a_owner, a_turn;
  ValueId d_active, d_owner, read_active, read_owner, read_turn;
  ValueId free, free_valid, write_take, read_take, allocate_write;
  explicit ContextPool(const TileLinkUhOutletProfile &profile, const NodeHardware &kernel);
  ValueId wire(unsigned width) { return l.num(width, 0); }
  void connect(ValueId to, ValueId from) {
    const auto width = l.width(to);
    const auto zero = l.num(width, 0);
    l.b.ir.values.at(to.index) = {width, Op::BitOr, {from, zero, {}}};
  }
  ValueId selected(ValueId index, const std::vector<ValueId> &values);
  ValueId choose(const std::vector<ValueId> &valid, ValueId turn);
  ValueId successor(ValueId index);
  void import(const NodeHardware &kernel);
  void context_allocate();
  void dependencies();
  void a_schedule();
  void d_receive();
  void d_buffer(ValueId take, ValueId source);
  void packet_response(bool read);
  void finish();
};
ContextPool::ContextPool(const TileLinkUhOutletProfile &profile, const NodeHardware &kernel)
    : p(profile), capacity(p.packet.max_outstanding), index_bits(std::bit_width(capacity - 1)) {
  write_active = l.state("tl.write_active", 1);
  write_owner = l.state("tl.write_owner", index_bits);
  a_active = l.state("tl.a_active", 1);
  a_owner = l.state("tl.a_owner", index_bits);
  a_turn = l.state("tl.a_turn", index_bits);
  d_active = l.state("tl.d_active", 1);
  d_owner = l.state("tl.d_owner", index_bits);
  read_active = l.state("tl.read_active", 1);
  read_owner = l.state("tl.read_owner", index_bits);
  read_turn = l.state("tl.read_turn", index_bits);
  for (unsigned k = 0; k < receive.size(); ++k) {
    const auto name = "tl.dbuffer" + std::to_string(k);
    receive[k] = {l.state(name + ".valid", 1), l.state(name + ".owner", index_bits),
                  l.state(name + ".data", 32), l.state(name + ".error", 1)};
  }
  for (const auto &port : kernel.ir.ports) {
    if (port.endpoint.starts_with("domain:") || port.endpoint == "hint")
      continue;
    const auto key = port.endpoint + "." + port.signal;
    (port.input ? input : output)[key] =
        port.input ? l.in(port.endpoint, port.signal, port.width) : wire(port.width);
  }
  input["tl.d_source"] = l.in("tl", "d_source", p.source_bits);
  import(kernel);
  context_allocate();
  dependencies();
  a_schedule();
  d_receive();
  packet_response(true);
  packet_response(false);
  finish();
}
ValueId ContextPool::selected(ValueId index, const std::vector<ValueId> &values) {
  std::vector<ValueId> hits;
  for (unsigned k = 0; k < capacity; ++k)
    hits.push_back(l.eq(index, k));
  return l.first_hit(hits, values, l.num(l.width(values.front()), 0));
}
ValueId ContextPool::choose(const std::vector<ValueId> &valid, ValueId turn) {
  std::vector<ValueId> later, indices;
  for (unsigned k = 0; k < capacity; ++k) {
    indices.push_back(l.num(index_bits, k));
    later.push_back(l.both(valid[k], l.inv(l.below(indices.back(), turn))));
  }
  return l.first_hit(later, indices, l.first_hit(valid, indices, l.num(index_bits, 0)));
}
ValueId ContextPool::successor(ValueId index) {
  return l.pick(l.eq(index, capacity - 1), l.num(index_bits, 0), l.inc(index));
}
void ContextPool::import(const NodeHardware &kernel) {
  for (unsigned k = 0; k < capacity; ++k) {
    auto &slot = contexts.emplace_back();
    slot.dependencies = l.state("tl.context" + std::to_string(k) + ".dependencies", capacity);
    IrComponent imported(l.b, kernel.ir);
    const auto first = l.both(receive[0].valid, l.eq(receive[0].owner, k));
    imported.replace(kernel.bindings.at("tl.ddata"),
                     l.pick(first, receive[0].data, receive[1].data));
    imported.replace(kernel.bindings.at("tl.derror"),
                     l.pick(first, receive[0].error, receive[1].error));
    for (std::size_t i = 0; i < kernel.ir.values.size(); ++i) {
      const auto &value = kernel.ir.values[i];
      if (value.op != Op::Input)
        continue;
      const auto &port = kernel.ir.ports.at(value.input_port.index);
      const auto key = port.endpoint + "." + port.signal;
      ValueId replacement;
      if (port.endpoint.starts_with("domain:"))
        replacement = port.signal == "clock" ? l.clock : l.reset;
      else if (port.signal == "valid" || port.signal == "ready" || port.signal == "a_ready" ||
               port.signal == "d_valid" || port.endpoint == "hint")
        replacement = slot.inputs[key] = wire(port.width);
      else
        replacement = input.at(key);
      imported.replace(ValueId{i}, replacement);
    }
    for (const auto &driver : kernel.ir.drivers) {
      const auto &port = kernel.ir.ports.at(driver.port.index);
      slot.outputs[port.endpoint + "." + port.signal] = imported.get(driver.value);
    }
    for (const auto &[name, value] : kernel.bindings) {
      const auto mapped = imported.get(value);
      slot.bound[name] = mapped;
      l.bind("tl.context" + std::to_string(k) + "." + name, mapped);
    }
    imported.finish();
  }
}
void ContextPool::context_allocate() {
  std::vector<ValueId> empty, indices, write_ready, read_ready;
  for (unsigned k = 0; k < capacity; ++k) {
    empty.push_back(l.inv(contexts[k].bound.at("tl.busy")));
    indices.push_back(l.num(index_bits, k));
    write_ready.push_back(contexts[k].outputs.at("wreq.ready"));
    read_ready.push_back(contexts[k].outputs.at("rreq.ready"));
  }
  free_valid = l.any(empty);
  free = l.bind("tl.free", l.first_hit(empty, indices, l.num(index_bits, 0)));
  const auto write_slot = l.pick(write_active, write_owner, free);
  const auto wvalid = l.both(input.at("wreq.valid"), l.either(write_active, free_valid));
  const auto rvalid =
      l.both(input.at("rreq.valid"),
             l.both(free_valid, l.either(write_active, l.inv(input.at("wreq.valid")))));
  for (unsigned k = 0; k < capacity; ++k) {
    connect(contexts[k].inputs.at("wreq.valid"), l.both(wvalid, l.eq(write_slot, k)));
    connect(contexts[k].inputs.at("rreq.valid"), l.both(rvalid, l.eq(free, k)));
  }
  const auto wr =
      l.active(l.both(l.either(write_active, free_valid), selected(write_slot, write_ready)));
  const auto rr =
      l.active(l.both(l.both(free_valid, l.either(write_active, l.inv(input.at("wreq.valid")))),
                      selected(free, read_ready)));
  connect(output.at("wreq.ready"), wr);
  connect(output.at("rreq.ready"), rr);
  write_take = l.both(input.at("wreq.valid"), wr);
  read_take = l.both(input.at("rreq.valid"), rr);
  allocate_write = l.both(write_take, l.inv(write_active));
  l.b.update(write_owner, l.pick(allocate_write, free, write_owner));
  l.b.update(write_active, l.pick(write_take, l.inv(input.at("wreq.last")), write_active));
  l.bind("tl.accept", l.either(allocate_write, read_take));
  l.bind("tl.w_take", allocate_write);
}
void ContextPool::dependencies() {
  for (unsigned k = 0; k < capacity; ++k) {
    auto &slot = contexts[k];
    auto next = l.num(capacity, 0);
    for (unsigned j = 0; j < capacity; ++j) {
      if (j == k)
        continue;
      const auto &older = contexts[j].bound;
      const auto older_write = older.at("tl.write");
      auto same_key = l.inv(older_write);
      for (const auto *field : {"source", "id"}) {
        const auto key = std::string("tl.packet.") + field;
        if (older.contains(key))
          same_key =
              l.both(same_key, l.same(older.at(key), input.at(std::string("rreq.") + field)));
      }
      const auto pending_write = l.both(
          older_write, l.either(l.inv(l.eq(older.at("tl.words_left"), 0)), older.at("tl.message")));
      const auto wait =
          l.both(older.at("tl.busy"), l.pick(allocate_write, pending_write, same_key));
      const auto done =
          l.either(older.at("tl.release"), l.both(slot.bound.at("tl.write"), l.inv(pending_write)));
      const auto held = l.both(l.b.slice(slot.dependencies, j, 1), l.inv(done));
      const auto bit = l.pick(slot.bound.at("tl.accept"), wait, held);
      next =
          l.either(next, l.pick(bit, l.num(capacity, std::uint64_t{1} << j), l.num(capacity, 0)));
    }
    l.b.update(slot.dependencies, next);
  }
}
void ContextPool::a_schedule() {
  std::vector<ValueId> eligible, done, transferred;
  for (const auto &slot : contexts) {
    eligible.push_back(l.both(slot.bound.at("tl.candidate"), l.eq(slot.dependencies, 0)));
    done.push_back(slot.bound.at("tl.a_done"));
    transferred.push_back(l.both(slot.bound.at("tl.a_take"), l.eq(slot.bound.at("tl.a_left"), 1)));
  }
  const auto candidate = choose(eligible, a_turn);
  const auto start = l.both(l.inv(a_active), l.any(eligible));
  const auto end =
      l.both(a_active, l.either(selected(a_owner, done), selected(a_owner, transferred)));
  l.b.update(a_active, l.either(start, l.both(a_active, l.inv(end))));
  l.b.update(a_owner, l.pick(start, candidate, a_owner));
  l.b.update(a_turn, l.pick(end, successor(a_owner), a_turn));
  for (unsigned k = 0; k < capacity; ++k) {
    connect(contexts[k].inputs.at("hint.start"), l.both(start, l.eq(candidate, k)));
    connect(contexts[k].inputs.at("tl.a_ready"),
            l.both(input.at("tl.a_ready"), l.both(a_active, l.eq(a_owner, k))));
  }
  for (const auto &[key, value] : output) {
    if (!key.starts_with("tl.a_"))
      continue;
    std::vector<ValueId> values;
    std::ranges::transform(contexts, std::back_inserter(values),
                           [&](const auto &slot) { return slot.outputs.at(key); });
    connect(value, key == "tl.a_valid" ? l.both(a_active, selected(a_owner, values))
                                       : selected(a_owner, values));
  }
  l.out("tl", "a_source", l.fit(a_owner, p.source_bits));
}
void ContextPool::d_receive() {
  const auto source = input.at("tl.d_source");
  const auto available = l.either(l.inv(receive[0].valid), l.inv(receive[1].valid));
  std::vector<ValueId> takes, reads, last, ready;
  for (unsigned k = 0; k < capacity; ++k) {
    auto &slot = contexts[k];
    const auto permitted = l.either(slot.bound.at("tl.write"), available);
    const auto match =
        l.both(l.both(l.eq(source, k), permitted), l.either(l.inv(d_active), l.eq(d_owner, k)));
    connect(slot.inputs.at("tl.d_valid"), l.both(input.at("tl.d_valid"), match));
    ready.push_back(l.both(match, slot.outputs.at("tl.d_ready")));
    takes.push_back(slot.bound.at("tl.d_take"));
    reads.push_back(l.both(takes.back(), l.inv(slot.bound.at("tl.write"))));
    last.push_back(l.both(takes.back(), l.eq(slot.bound.at("tl.d_left"), 1)));
  }
  const auto take = l.any(takes), end = l.any(last);
  l.b.update(d_active, l.pick(take, l.inv(end), d_active));
  l.b.update(d_owner, l.pick(l.both(take, l.inv(d_active)), l.fit(source, index_bits), d_owner));
  d_buffer(l.any(reads), source);
  connect(output.at("tl.d_ready"), l.active(l.any(ready)));
}
void ContextPool::d_buffer(ValueId take, ValueId source) {
  std::vector<ValueId> occupied;
  std::ranges::transform(contexts, std::back_inserter(occupied),
                         [&](const auto &slot) { return slot.bound.at("tl.dfull"); });
  for (unsigned k = 0; k < receive.size(); ++k) {
    const auto &buffer = receive[k];
    const auto chosen = k == 0 ? l.inv(buffer.valid) : receive[0].valid;
    const auto fill = l.both(take, chosen);
    const auto hold = l.both(buffer.valid, selected(buffer.owner, occupied));
    l.b.update(buffer.valid, l.either(fill, hold));
    l.b.update(buffer.owner, l.pick(fill, l.fit(source, index_bits), buffer.owner));
    l.b.update(buffer.data, l.pick(fill, input.at("tl.d_data"), buffer.data));
    l.b.update(
        buffer.error,
        l.pick(fill, l.either(input.at("tl.d_denied"), input.at("tl.d_corrupt")), buffer.error));
  }
}
void ContextPool::packet_response(bool read) {
  const std::string face = read ? "rrsp" : "wrsp";
  std::vector<ValueId> valid, cuts, lasts, wrap;
  for (const auto &slot : contexts) {
    valid.push_back(slot.outputs.at(face + ".valid"));
    cuts.push_back(slot.bound.at("cut"));
    lasts.push_back(slot.outputs.at(face + ".last"));
    wrap.push_back(slot.bound.at("wrap_end"));
  }
  auto owner = choose(valid, read ? read_turn : l.num(index_bits, 0));
  if (read)
    owner = l.pick(read_active, read_owner, owner);
  const auto offered = selected(owner, valid);
  const auto permitted = read ? l.bit(1) : l.inv(output.at("rrsp.valid"));
  const auto ready = l.both(input.at(face + ".ready"), permitted);
  const auto take = l.both(offered, ready);
  const auto cut = selected(owner, cuts);
  if (read) {
    const auto end = l.both(take, l.either(selected(owner, lasts), cut));
    l.b.update(read_active, l.both(l.either(read_active, offered), l.inv(end)));
    l.b.update(read_owner, l.pick(l.both(offered, l.inv(read_active)), owner, read_owner));
    l.b.update(read_turn, l.pick(end, successor(owner), read_turn));
    l.bind("cut", l.both(offered, cut));
    l.bind("wrap_end", selected(owner, wrap));
    if (p.packet.response_run_bits)
      l.out("hint", "wrap_end", l.bindings.at("wrap_end"));
  }
  for (unsigned k = 0; k < capacity; ++k)
    connect(contexts[k].inputs.at(face + ".ready"), l.both(ready, l.eq(owner, k)));
  for (const auto &[key, value] : output) {
    if (!key.starts_with(face + "."))
      continue;
    std::vector<ValueId> values;
    std::ranges::transform(contexts, std::back_inserter(values),
                           [&](const auto &slot) { return slot.outputs.at(key); });
    const auto selected_value = selected(owner, values);
    connect(value, key == "wrsp.valid" ? l.both(permitted, selected_value) : selected_value);
  }
}
void ContextPool::finish() {
  std::vector<ValueId> busy, released, timed_out;
  for (const auto &slot : contexts) {
    busy.push_back(slot.bound.at("tl.busy"));
    released.push_back(slot.bound.at("tl.release"));
    if (p.packet.timeout_cycles)
      timed_out.push_back(slot.outputs.at("status.timed_out"));
  }
  l.bind("tl.busy", l.any(busy));
  l.bind("tl.release", l.any(released));
  if (p.packet.timeout_cycles)
    connect(output.at("status.timed_out"), l.any(timed_out));
  for (const auto &[key, value] : output) {
    const auto dot = key.find('.');
    l.out(key.substr(0, dot), key.substr(dot + 1), value);
  }
}
} // namespace
Result<NodeHardware> lower_tilelink_uh_contexts(const TileLinkUhOutletProfile &p) {
  auto one = p;
  one.packet.max_outstanding = 1;
  one.source_bits = 0;
  one.scheduled = one.segmented = true;
  auto kernel = lower_tilelink_uh_outlet(one);
  if (!kernel)
    return kernel;
  ContextPool pool(p, kernel.value());
  std::vector<ValueId> kept;
  for (const auto &[name, value] : pool.l.bindings)
    kept.push_back(value);
  for (std::size_t i = 0; i < pool.l.b.ir.values.size(); ++i)
    if (pool.l.b.ir.values[i].op == Op::Input)
      kept.push_back(ValueId{i});
  const auto mapped = canonicalize(pool.l.b.ir, kept, {});
  if (mapped.empty())
    return Result<NodeHardware>::failure(
        {error("NCL_INTERNAL_IR", "tilelink_target", "TileLink context connections form a cycle")});
  for (auto &[name, value] : pool.l.bindings)
    value.index = mapped.at(value.index);
  return pool.l.finish("nocloom_tl_uh_outlet_" + p.packet.name);
}
} // namespace nocloom::detail
