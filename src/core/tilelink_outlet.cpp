#include "ir_component.hpp"
#include "tilelink.hpp"

namespace nocloom::detail {
namespace {
using logic::Fields;
struct OutletFace {
  logic::Logic l;
  ValueId phase = l.state("tl.phase", 3);
  ValueId error = l.state("tl.error", 1);
  ValueId data = l.state("tl.data", 32);
  ValueId ready = l.active(
      l.either(l.eq(phase, 0), l.either(l.both(l.eq(phase, 4), l.inv(error)), l.eq(phase, 5))));
  Fields bus;
  void build(const NodeHardware &core, unsigned source_bits, unsigned size_bits,
             unsigned sink_bits) {
    IrComponent imported(l.b, core.ir);
    for (const auto &v : core.ir.values)
      if (v.op == Op::Input) {
        const auto &port = core.ir.ports.at(v.input_port.index);
        ValueId value;
        if (port.signal == "clock")
          value = l.clock;
        else if (port.signal == "reset_n")
          value = l.reset;
        else if (port.endpoint != "ahb")
          value = l.in(port.endpoint, port.signal, port.width);
        else if (port.signal == "hreadyout")
          value = ready;
        else if (port.signal == "hrdata")
          value = data;
        else if (port.signal == "hresp")
          value = l.both(error, l.either(l.eq(phase, 4), l.eq(phase, 5)));
        else
          throw std::logic_error("Unconnected internal target input " + port.signal);
        imported.replace(ValueId{static_cast<std::size_t>(&v - core.ir.values.data())}, value);
      }
    for (const auto &driver : core.ir.drivers) {
      const auto &port = core.ir.ports.at(driver.port.index);
      const auto value = imported.get(driver.value);
      if (port.endpoint == "ahb")
        bus[port.signal] = value;
      else
        l.out(port.endpoint, port.signal, value);
    }
    for (const auto &[key, value] : core.bindings)
      l.bind(key, imported.get(value));
    imported.finish();
    channel(source_bits, size_bits, sink_bits);
  }
  void channel(unsigned source_bits, unsigned size_bits, unsigned sink_bits) {
    // AHB address and data phases feed a registered single-beat request.
    const auto address_taken =
        l.both(ready, l.both(bus.at("hsel"), l.b.slice(bus.at("htrans"), 1, 1)));
    const auto address = l.latch("tl.address", bus.at("haddr"), address_taken);
    const auto size = l.latch("tl.size", l.fit(bus.at("hsize"), size_bits), address_taken);
    const auto write = l.latch("tl.write", bus.at("hwrite"), address_taken);
    const auto capture_data = l.eq(phase, 1);
    const auto word = l.latch("tl.write_data", bus.at("hwdata"), capture_data);
    const auto av = l.active(l.eq(phase, 2));
    const auto dr = l.active(l.either(l.eq(phase, 2), l.eq(phase, 3)));
    const auto a_fire = l.bind("tl.a_take", l.both(av, l.in("tl", "a_ready", 1)));
    const auto d_fire = l.bind("tl.d_take", l.both(dr, l.in("tl", "d_valid", 1)));
    const auto denied = l.in("tl", "d_denied", 1);
    const auto corrupt = l.in("tl", "d_corrupt", 1);
    const auto rdata = l.in("tl", "d_data", 32);
    l.in("tl", "d_opcode", 3);
    l.in("tl", "d_param", 2);
    l.in("tl", "d_size", size_bits);
    if (source_bits)
      l.in("tl", "d_source", source_bits);
    if (sink_bits)
      l.in("tl", "d_sink", sink_bits);
    const auto failed = l.either(denied, l.both(l.inv(write), corrupt));
    l.b.update(error, l.pick(d_fire, failed, error));
    l.b.update(data, l.pick(d_fire, l.pick(failed, l.num(32, 0), rdata), data));
    auto next = l.pick(capture_data, l.num(3, 2), phase);
    next = l.pick(a_fire, l.num(3, 3), next);
    next = l.pick(d_fire, l.num(3, 4), next);
    next = l.pick(l.both(l.eq(phase, 4), error), l.num(3, 5), next);
    next = l.pick(ready, l.pick(address_taken, l.num(3, 1), l.num(3, 0)), next);
    l.b.update(phase, next);
    auto mask = l.num(4, 15);
    const auto lane = l.b.slice(address, 0, 2);
    auto byte = l.num(4, 1);
    for (unsigned k = 1; k < 4; ++k)
      byte = l.pick(l.eq(lane, k), l.num(4, 1U << k), byte);
    mask = l.pick(
        l.eq(size, 0), byte,
        l.pick(l.eq(size, 1), l.pick(l.b.slice(address, 1, 1), l.num(4, 12), l.num(4, 3)), mask));
    auto cleaned = l.num(32, 0);
    for (unsigned k = 0; k < 4; ++k) {
      const auto chunk = l.pick(l.b.slice(mask, k, 1), l.b.slice(word, 8 * k, 8), l.num(8, 0));
      const auto widened = l.fit(chunk, 32);
      cleaned = l.either(cleaned, k ? l.b.concat(l.b.slice(widened, 0, 32 - 8 * k), l.num(8 * k, 0))
                                    : widened);
    }
    l.out("tl", "a_valid", av);
    l.out("tl", "a_opcode",
          l.pick(write, l.pick(l.eq(mask, 15), l.num(3, 0), l.num(3, 1)), l.num(3, 4)));
    l.out("tl", "a_param", l.num(3, 0));
    l.out("tl", "a_size", size);
    l.out("tl", "a_address", address);
    l.out("tl", "a_mask", mask);
    l.out("tl", "a_data", l.pick(write, cleaned, l.num(32, 0)));
    l.out("tl", "a_corrupt", l.bit(0));
    if (source_bits)
      l.out("tl", "a_source", l.num(source_bits, 0));
    l.out("tl", "d_ready", dr);
  }
};
} // namespace
Result<NodeHardware> lower_tilelink_outlet(const BurstOutletProfile &profile, unsigned source_bits,
                                           unsigned size_bits, unsigned sink_bits) {
  auto kernel_profile = profile;
  kernel_profile.undefined_length_bursts = false;
  const auto kernel = lower_tl_ul_ahb_kernel(kernel_profile);
  if (!kernel)
    return kernel;
  OutletFace face;
  face.build(kernel.value(), source_bits, size_bits, sink_bits);
  return face.l.finish("nocloom_tl_ul_outlet_" + profile.name);
}
} // namespace nocloom::detail
