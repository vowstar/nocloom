#include "wishbone.hpp"
#include "diagnostic.hpp"
#include "ir_component.hpp"
#include "logic.hpp"
#include <stdexcept>

namespace nocloom::detail {
namespace {
using logic::Fields;
using logic::Logic;
// Preserve the kernel's transport faces and bindings. Only its bus face is replaced.
Fields import_kernel(Logic &l, const NodeHardware &kernel, const std::string &face,
                     const Fields &inputs) {
  IrComponent c(l.b, kernel.ir);
  for (const auto &v : kernel.ir.values) {
    if (v.op != Op::Input)
      continue;
    const auto &port = kernel.ir.ports.at(v.input_port.index);
    const auto id = ValueId{static_cast<std::size_t>(&v - kernel.ir.values.data())};
    if (port.endpoint.starts_with("domain:"))
      c.replace(id, port.signal == "clock" ? l.clock : l.reset);
    else if (port.endpoint == face)
      c.replace(id, inputs.at(port.signal));
    else
      c.replace(id, l.b.input(port.name, port.width, port.endpoint, port.signal));
  }
  Fields outputs;
  for (const auto &driver : kernel.ir.drivers) {
    const auto &port = kernel.ir.ports.at(driver.port.index);
    const auto value = c.get(driver.value);
    if (port.endpoint == face || (port.endpoint == "hint" && port.signal.starts_with("wb_")))
      outputs.emplace(port.signal, value);
    else
      l.b.output(port.name, value, port.endpoint, port.signal);
  }
  for (const auto &[name, value] : kernel.bindings)
    l.bind(name, c.get(value));
  c.finish();
  return outputs;
}
// Register the next value of a kernel state expression without adding a bus cycle.
// Substituting every state with its D input preserves the kernel's completion edge.
void registered_face(Logic &l, const Fields &outputs) {
  const auto snapshot = l.b.ir;
  IrComponent next(l.b, snapshot);
  for (std::size_t i = 0; i < snapshot.values.size(); ++i)
    if (snapshot.values[i].op == Op::Input)
      next.replace(ValueId{i}, ValueId{i});
  for (const auto &state : snapshot.states)
    next.replace(state.output, state.next);
  for (const auto &[name, value] : outputs) {
    const auto reg = l.state("wb." + name, l.width(value));
    l.b.update(reg, next.get(value));
    l.out("wb", name, reg);
  }
  next.finish();
}
Fields read_shape(Logic &l, ValueId sel, ValueId base) {
  auto offset = l.num(2, 0), size = l.num(3, 2);
  for (unsigned lane = 0; lane < 4; ++lane) {
    offset = l.pick(l.eq(sel, 1U << lane), l.num(2, lane), offset);
    size = l.pick(l.eq(sel, 1U << lane), l.num(3, 0), size);
  }
  size = l.pick(l.either(l.eq(sel, 3), l.eq(sel, 12)), l.num(3, 1), size);
  offset = l.pick(l.eq(sel, 12), l.num(2, 2), offset);
  return {{"address", l.either(base, l.fit(offset, l.width(base)))}, {"size", size}};
}
} // namespace

static Result<NodeHardware> pipelined_initiator(const Axi4InitiatorProfile &profile, bool word,
                                                bool pipelined) {
  auto kernel = lower_axi4_initiator(profile);
  if (!kernel)
    return kernel;
  Logic l;
  const auto cyc = l.in("wb", "cyc", 1), stb = l.in("wb", "stb", 1);
  const auto we = l.in("wb", "we", 1), sel = l.in("wb", "sel", 4);
  const auto adr = l.in("wb", "adr", profile.address_bits - (word ? 2 : 0));
  const auto base = word ? l.b.concat(adr, l.num(2, 0))
                         : l.both(adr, l.num(profile.address_bits,
                                             (std::uint64_t{1} << profile.address_bits) - 4));
  const auto data = l.in("wb", "dat_w", 32);
  const auto busy = l.state("wb.busy", 1), abandoned = l.state("wb.abandoned", 1);
  const auto ack = l.state("wb.ack", 1), err = l.state("wb.err", 1);
  const auto response = l.either(ack, err);
  const auto aw = l.state("wb.aw", 1), w = l.state("wb.w", 1), ar = l.state("wb.ar", 1);
  const auto count = pipelined ? l.state("wb.count", 7) : busy;
  const auto stall = pipelined ? l.bind("wb.stall", l.b.state(1, l.clock, l.reset, 1)) : busy;
  const auto live = l.both(cyc, stb);
  const auto take = l.both(l.active(live), l.inv(pipelined ? stall : l.either(busy, response)));
  const auto addr = l.latch("wb.address", base, take);
  const auto write = l.latch("wb.write", we, take);
  const auto mask = l.latch("wb.mask", sel, take);
  const auto payload = l.latch("wb.data", data, take);
  const auto shape = read_shape(l, mask, addr);
  auto permitted = l.bit(1);
  if (pipelined) {
    const auto direction = l.state("wb.direction", 1);
    const auto alone = l.eq(count, 1);
    const auto dest_bits =
        std::max(1U, static_cast<unsigned>(std::bit_width(profile.destinations.size())));
    std::vector<ValueId> hits, values;
    for (unsigned i = 0; i < profile.destinations.size(); ++i) {
      const auto &window = profile.destinations[i];
      hits.push_back(l.within(addr, window.base, window.base + window.size));
      values.push_back(l.num(dest_bits, i));
    }
    const auto destination =
        l.first_hit(hits, values, l.num(dest_bits, profile.destinations.size()));
    const auto previous = l.state("wb.destination", dest_bits);
    l.b.update(previous, l.pick(alone, destination, previous));
    permitted = l.either(alone, l.both(l.same(direction, write), l.same(previous, destination)));
    l.b.update(direction, l.pick(alone, write, direction));
  }
  Fields inputs;
  // The full AXI kernel accepts one naturally aligned beat. It supplies all routing and errors.
  for (const auto &p : kernel.value().ir.ports)
    if (p.input && p.endpoint == "axi")
      inputs[p.signal] = l.num(p.width, 0);
  inputs["awaddr"] = addr;
  inputs["araddr"] = shape.at("address");
  inputs["awsize"] = l.num(3, 2);
  inputs["arsize"] = shape.at("size");
  inputs["awburst"] = inputs["arburst"] = l.num(2, 1);
  inputs["awprot"] = inputs["arprot"] = l.num(3, 2);
  inputs["awvalid"] = pipelined ? l.both(aw, permitted) : aw;
  inputs["wvalid"] = pipelined ? l.both(w, permitted) : w;
  inputs["arvalid"] = pipelined ? l.both(ar, permitted) : ar;
  inputs["wdata"] = payload;
  inputs["wstrb"] = mask;
  inputs["wlast"] = inputs["bready"] = inputs["rready"] = l.bit(1);
  const auto out = import_kernel(l, kernel.value(), "axi", inputs);
  l.bind("wb.b_take", out.at("bvalid"));
  std::vector<ValueId> remaining;
  for (const auto &[valid, ready, direction] :
       {std::tuple{aw, out.at("awready"), we}, std::tuple{w, out.at("wready"), we},
        std::tuple{ar, out.at("arready"), l.inv(we)}}) {
    const auto next =
        l.pick(take, direction, l.both(valid, l.inv(pipelined ? l.both(ready, permitted) : ready)));
    l.b.update(valid, next);
    remaining.push_back(next);
  }
  // Network completion drains the request even if its bus response is abandoned.
  const auto done = l.both(pipelined ? l.inv(l.eq(count, 0)) : busy,
                           l.either(out.at("bvalid"), out.at("rvalid")));
  const auto failed = l.pick(pipelined ? out.at("bvalid") : write, l.b.slice(out.at("bresp"), 1, 1),
                             l.b.slice(out.at("rresp"), 1, 1));
  const auto answer = l.every(std::array{done, pipelined ? cyc : live, l.inv(abandoned)});
  l.b.update(busy, l.pick(take, l.bit(1), l.both(busy, l.inv(done))));
  if (pipelined) {
    const auto next_count = l.minus(l.plus(count, l.zext(take, 7)), l.zext(done, 7));
    l.b.update(count, next_count);
    const auto abort = l.both(l.inv(l.eq(count, 0)), l.inv(cyc));
    const auto draining = l.both(l.inv(l.eq(next_count, 0)), l.either(abandoned, abort));
    l.b.update(abandoned, draining);
    l.b.update(stall,
               l.any(std::array{l.inv(l.below(next_count, l.num(7, profile.max_outstanding))),
                                l.any(remaining), draining}));
    l.out("wb", "stall", stall);
  } else
    l.b.update(abandoned, l.pick(take, l.bit(0), l.either(abandoned, l.both(busy, l.inv(live)))));
  l.b.update(ack, l.both(answer, l.inv(failed)));
  l.b.update(err, l.both(answer, failed));
  const auto rdata = l.state("wb.dat_r", 32);
  l.b.update(rdata,
             l.pick(l.both(answer, l.inv(l.either(pipelined ? out.at("bvalid") : write, failed))),
                    out.at("rdata"), l.num(32, 0)));
  l.out("wb", "ack", l.both(ack, cyc));
  l.out("wb", "err", l.both(err, cyc));
  l.out("wb", "dat_r", rdata);
  return Result<NodeHardware>::success({std::move(l.b.ir), std::move(l.bindings)});
}

Result<NodeHardware> lower_wishbone_initiator(const Axi4InitiatorProfile &profile, bool word,
                                              bool pipelined, bool burst_tags) {
  if (pipelined && burst_tags)
    return Result<NodeHardware>::failure(
        error("NCL_WISHBONE_OPTION", "burst_tags", "Burst tags require classic Wishbone cycles"));
  if (pipelined)
    return pipelined_initiator(profile, word, true);
  auto kernel = lower_axi4_initiator(profile);
  if (!kernel)
    return kernel;
  Logic l;
  const auto cyc = l.in("wb", "cyc", 1), stb = l.in("wb", "stb", 1);
  const auto we = l.in("wb", "we", 1), sel = l.in("wb", "sel", 4);
  const auto adr = l.in("wb", "adr", profile.address_bits - (word ? 2 : 0));
  const auto base = word ? l.b.concat(adr, l.num(2, 0))
                         : l.both(adr, l.num(profile.address_bits,
                                             (std::uint64_t{1} << profile.address_bits) - 4));
  const auto data = l.in("wb", "dat_w", 32);
  const auto busy = l.state("wb.busy", 1), abandoned = l.state("wb.abandoned", 1);
  const auto ack = l.state("wb.ack", 1), err = l.state("wb.err", 1);
  const auto response = l.either(ack, err);
  if (burst_tags) {
    l.in("wb", "cti", 3);
    l.in("wb", "bte", 2);
  }
  const auto live = l.both(cyc, stb);
  const auto take = l.both(l.active(live), l.inv(l.either(busy, response)));
  const auto addr = l.latch("wb.address", base, take);
  const auto write = l.latch("wb.write", we, take);
  const auto mask = l.latch("wb.mask", sel, take);
  const auto payload = l.latch("wb.data", data, take);
  const auto shape = read_shape(l, mask, addr);
  const auto aw = l.state("wb.aw", 1), w = l.state("wb.w", 1), ar = l.state("wb.ar", 1);
  Fields inputs;
  // The full AXI kernel accepts one naturally aligned beat. It supplies all routing and errors.
  for (const auto &p : kernel.value().ir.ports)
    if (p.input && p.endpoint == "axi")
      inputs[p.signal] = l.num(p.width, 0);
  inputs["awaddr"] = addr;
  inputs["araddr"] = shape.at("address");
  inputs["awsize"] = l.num(3, 2);
  inputs["arsize"] = shape.at("size");
  inputs["awburst"] = inputs["arburst"] = l.num(2, 1);
  inputs["awprot"] = inputs["arprot"] = l.num(3, 2);
  inputs["awvalid"] = aw;
  inputs["wvalid"] = w;
  inputs["arvalid"] = ar;
  inputs["wdata"] = payload;
  inputs["wstrb"] = mask;
  inputs["wlast"] = inputs["bready"] = inputs["rready"] = l.bit(1);
  const auto out = import_kernel(l, kernel.value(), "axi", inputs);
  l.bind("wb.b_take", out.at("bvalid"));
  for (const auto &[valid, ready, direction] :
       {std::tuple{aw, out.at("awready"), we}, std::tuple{w, out.at("wready"), we},
        std::tuple{ar, out.at("arready"), l.inv(we)}})
    l.b.update(valid, l.pick(take, direction, l.both(valid, l.inv(ready))));
  // Network completion drains the request even if its bus response is abandoned.
  const auto done = l.both(busy, l.either(out.at("bvalid"), out.at("rvalid")));
  const auto failed =
      l.pick(write, l.b.slice(out.at("bresp"), 1, 1), l.b.slice(out.at("rresp"), 1, 1));
  const auto answer = l.every(std::array{done, live, l.inv(abandoned)});
  l.b.update(busy, l.pick(take, l.bit(1), l.both(busy, l.inv(done))));
  l.b.update(abandoned, l.pick(take, l.bit(0), l.either(abandoned, l.both(busy, l.inv(live)))));
  l.b.update(ack, l.both(answer, l.inv(failed)));
  l.b.update(err, l.both(answer, failed));
  const auto rdata = l.state("wb.dat_r", 32);
  l.b.update(rdata,
             l.pick(l.both(answer, l.inv(l.either(write, failed))), out.at("rdata"), l.num(32, 0)));
  l.out("wb", "ack", l.both(ack, live));
  l.out("wb", "err", l.both(err, live));
  l.out("wb", "dat_r", rdata);
  return Result<NodeHardware>::success({std::move(l.b.ir), std::move(l.bindings)});
}

Result<NodeHardware> lower_wishbone_target(const BurstOutletProfile &profile, bool word, bool retry,
                                           bool pipelined, bool burst_tags) {
  if (pipelined && burst_tags)
    return Result<NodeHardware>::failure(
        error("NCL_WISHBONE_OPTION", "burst_tags", "Burst tags require classic Wishbone cycles"));
  if (burst_tags)
    return lower_wishbone_burst_target(profile, word, retry);
  if (pipelined) {
    auto kernel_profile = profile;
    kernel_profile.external_timeout_wait = profile.timeout_cycles != 0;
    auto kernel = lower_wishbone_target(kernel_profile, word, retry, false);
    if (!kernel)
      return kernel;
    Logic l;
    const auto accepted = l.state("wb.accepted", 1);
    const auto stb = l.state("wb.pipeline_stb", 1);
    const auto stall = l.in("wb", "stall", 1);
    const auto take = l.both(stb, l.inv(stall));
    const auto eligible = l.either(accepted, take);
    const auto ack = l.in("wb", "ack", 1), err = l.in("wb", "err", 1);
    const auto rty = retry ? l.in("wb", "rty", 1) : l.bit(0);
    const auto done = l.both(eligible, l.either(ack, l.either(err, rty)));
    Fields inputs{{"ack", l.both(eligible, ack)},
                  {"err", l.both(eligible, err)},
                  {"dat_r", l.in("wb", "dat_r", 32)}};
    if (retry)
      inputs["rty"] = l.both(eligible, rty);
    if (profile.timeout_cycles)
      inputs["target_wait"] = l.either(l.both(stb, stall), l.both(accepted, l.inv(done)));
    for (const auto &port : kernel.value().ir.ports)
      if (port.input && port.endpoint == "wb" && !inputs.contains(port.signal))
        inputs[port.signal] = l.in("wb", port.signal, port.width);
    const auto outputs = import_kernel(l, kernel.value(), "wb", inputs);
    const auto state = std::ranges::find_if(
        l.b.ir.states, [&](const auto &q) { return q.output.index == outputs.at("stb").index; });
    if (state == l.b.ir.states.end())
      throw std::logic_error("Wishbone STB requires a register");
    const auto next_stb = state->next;
    const auto pending = l.both(l.either(accepted, take), l.inv(done));
    const auto failed = profile.timeout_cycles ? l.bindings.at("timeout.failed") : l.bit(0);
    l.b.update(accepted, pending);
    l.b.update(stb,
               l.pick(l.both(stb, stall), stb, l.both(next_stb, l.inv(l.either(pending, failed)))));
    const auto cyc = l.state("wb.pipeline_cyc", 1);
    l.b.update(cyc, l.either(outputs.at("cyc"), failed));
    for (const auto &[name, value] : outputs)
      l.out("wb", name, name == "stb" ? stb : name == "cyc" ? cyc : value);
    return Result<NodeHardware>::success({std::move(l.b.ir), std::move(l.bindings)});
  }
  auto kernel = lower_apb_burst_outlet(profile);
  if (!kernel)
    return kernel;
  Logic l;
  const auto ack = l.in("wb", "ack", 1), err = l.in("wb", "err", 1);
  const auto rty = retry ? l.in("wb", "rty", 1) : l.bit(0);
  const auto bus_error = l.either(err, rty);
  Fields inputs{{"pready", l.either(ack, bus_error)},
                {"pslverr", bus_error},
                {"prdata", l.in("wb", "dat_r", 32)}};
  if (profile.external_timeout_wait)
    inputs["target_wait"] = l.in("wb", "target_wait", 1);
  const auto out = import_kernel(l, kernel.value(), "apb", inputs);
  auto address = out.at("paddr");
  address = word ? l.b.slice(address, 2, profile.address_bits - 2)
                 : l.both(address, l.num(profile.address_bits,
                                         (std::uint64_t{1} << profile.address_bits) - 4));
  registered_face(l, {{"cyc", out.at("wb_cycle")},
                      {"stb", l.both(out.at("psel"), out.at("penable"))},
                      {"we", out.at("pwrite")},
                      {"adr", address},
                      {"sel", out.at("wb_sel")},
                      {"dat_w", out.at("pwdata")}});
  return Result<NodeHardware>::success({std::move(l.b.ir), std::move(l.bindings)});
}
} // namespace nocloom::detail
