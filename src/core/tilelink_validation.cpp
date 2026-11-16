#include "diagnostic.hpp"
#include "tilelink.hpp"
#include <bit>

namespace nocloom::detail {
namespace {
template <class Endpoint>
void uh_fields(const Endpoint &e, const std::string &path, std::vector<Diagnostic> &out) {
  const auto reject = [&](bool bad, const char *key, const char *message) {
    if (bad)
      out.push_back(error("NCL_TILELINK_PROFILE", path + "." + key, message));
  };
  const auto maximum = e.max_transfer_bytes.value_or(64), width = e.size_width.value_or(3);
  reject(e.data_width && e.data_width != 32, "data_width", "TL-UH requires 32-bit data");
  reject(e.source_width > 16U, "source_width", "TileLink source_width must be 0 to 16");
  reject(e.sink_width > 16U, "sink_width", "TileLink sink_width must be 0 to 16");
  reject(width < 2 || width > 4, "size_width", "TileLink size_width must be 2 to 4");
  const bool valid_maximum = maximum >= 4 && maximum <= 4096 && std::has_single_bit(maximum);
  reject(!valid_maximum, "max_transfer_bytes",
         "TL-UH max_transfer_bytes must be a power of two from 4 to 4096");
  reject(valid_maximum && width >= 2 && width <= 4 &&
             static_cast<unsigned>(std::bit_width(maximum)) > (1U << width),
         "size_width", "TileLink size_width cannot encode max_transfer_bytes");
  reject(e.id_width.has_value(), "id_width", "TileLink uses source_width instead of id_width");
  reject(e.len_width.has_value(), "len_width", "TileLink has no length port");
}
template <class Endpoint>
void fields(const Endpoint &e, const std::string &path, std::vector<Diagnostic> &out) {
  const auto reject = [&](bool bad, const char *key, const char *message) {
    if (bad)
      out.push_back(error("NCL_TILELINK_PROFILE", path + "." + key, message));
  };
  if (e.protocol == Protocol::TlUh) {
    uh_fields(e, path, out);
    return;
  }
  if (e.protocol != Protocol::TlUl) {
    for (const auto &[key, value] :
         {std::pair{"source_width", e.source_width}, std::pair{"size_width", e.size_width},
          std::pair{"sink_width", e.sink_width},
          std::pair{"max_transfer_bytes", e.max_transfer_bytes}})
      reject(value.has_value(), key, "This field requires protocol tl_ul or tl_uh");
    return;
  }
  reject(e.data_width && e.data_width != 32, "data_width", "TL-UL supports only 32-bit data");
  reject(e.source_width > 16U, "source_width", "TL-UL source_width must be 0 to 16");
  reject(e.sink_width > 16U, "sink_width", "TL-UL sink_width must be 0 to 16");
  reject(e.size_width.value_or(2) < 2 || e.size_width.value_or(2) > 4, "size_width",
         "TL-UL size_width must be 2 to 4");
  reject(e.max_transfer_bytes.value_or(4) != 4, "max_transfer_bytes",
         "TL-UL max_transfer_bytes must be 4");
  reject(e.id_width.has_value(), "id_width", "TL-UL uses source_width instead of id_width");
  reject(e.len_width.has_value(), "len_width", "TL-UL has only single-beat messages");
}
} // namespace
std::vector<Diagnostic> check_tilelink(const Definition &d) {
  std::vector<Diagnostic> out;
  const auto reject = [&](bool bad, const std::string &path, const std::string &message) {
    if (bad)
      out.push_back(error("NCL_TILELINK_PROFILE", path, message));
  };
  for (unsigned k = 0; k < d.initiators.size(); ++k) {
    const auto &s = d.initiators[k];
    const auto path = "initiators[" + std::to_string(k) + "]";
    fields(s, path, out);
    if (s.protocol != Protocol::TlUl && s.protocol != Protocol::TlUh)
      continue;
    const auto maximum = s.max_transfer_bytes.value_or(64);
    const auto address_bits = s.address_width.value_or(d.address_width);
    reject(s.protocol == Protocol::TlUh && maximum >= 4 && maximum <= 4096 &&
               std::has_single_bit(maximum) && address_bits >= 2 &&
               address_bits < static_cast<unsigned>(std::bit_width(maximum - 1)),
           path + ".address_width",
           "TL-UH address_width must be at least log2(max_transfer_bytes)");
    reject(d.transport && d.transport->data_width == 64, "transport.data_width",
           "TileLink endpoints require a 32-bit network");
    const std::string level = s.protocol == Protocol::TlUh ? "TL-UH" : "TL-UL";
    reject(s.access.has_value(), path + ".access", level + " initiators support both Get and Put");
    reject(s.narrow_bursts.has_value(), path + ".narrow_bursts",
           s.protocol == Protocol::TlUh ? "TL-UH uses its message size" : "TL-UL has no bursts");
    reject(s.user_width.has_value(), path + ".user_width", level + " has no user ports");
    reject(s.response_user_width.has_value(), path + ".response_user_width",
           level + " has no response user ports");
    reject(s.pipes != InitiatorPipes{}, path + ".pipes",
           s.protocol == Protocol::TlUh ? "TL-UH uses its protocol face"
                                        : "TL-UL uses its registered protocol face");
  }
  for (unsigned k = 0; k < d.targets.size(); ++k) {
    const auto &t = d.targets[k];
    const auto path = "targets[" + std::to_string(k) + "]";
    fields(t, path, out);
    if (t.protocol != Protocol::TlUl && t.protocol != Protocol::TlUh)
      continue;
    const bool uh = t.protocol == Protocol::TlUh;
    if (uh) {
      reject(t.exclusive.value_or(ExclusiveAccess::None) != ExclusiveAccess::None,
             path + ".exclusive", "A TL-UH target supports no exclusive monitor");
      const auto maximum = t.max_transfer_bytes.value_or(64);
      if (maximum >= 4 && std::has_single_bit(maximum))
        for (const auto &mapping : t.mappings)
          reject((mapping.base | mapping.size | mapping.local_address) % maximum != 0,
                 path + ".mappings", "A TL-UH target mapping must align to max_transfer_bytes");
    }
    reject(d.transport && d.transport->data_width == 64, "transport.data_width",
           "TileLink endpoints require a 32-bit network");
    for (const auto &mapping : t.mappings)
      reject(mapping.size < 4, path + ".mappings",
             "A TileLink target mapping must hold one data word");
    const auto capacity = t.max_outstanding.value_or(1);
    reject(uh ? capacity < 1 || capacity > 16 : capacity != 1, path + ".max_outstanding",
           uh ? "A TL-UH target supports 1 to 16 packet contexts"
              : "A TL-UL target supports only one transaction in flight");
    if (uh && capacity >= 1 && capacity <= 16)
      reject(t.source_width.value_or(static_cast<unsigned>(std::bit_width(capacity - 1))) <
                 static_cast<unsigned>(std::bit_width(capacity - 1)),
             path + ".source_width", "TileLink source_width cannot encode the packet capacity");
    reject(t.flows != 1, path + ".flows",
           uh ? "A TL-UH target supports one flow" : "A TL-UL target supports only one flow");
    reject(t.timeout && t.timeout->completion, path + ".timeout.completion",
           "A TileLink target cannot replace a response on timeout");
    reject(t.attributes.has_value(), path + ".attributes",
           uh ? "TL-UH targets have no attribute ports" : "TL-UL targets have no attribute ports");
    reject(!t.user.empty(), path + ".user",
           uh ? "TL-UH targets have no user ports" : "TL-UL targets have no user ports");
    reject(t.address_width.value_or(d.address_width) > 32, path + ".address_width",
           uh ? "A TL-UH target supports at most 32 local address bits"
              : "A TL-UL target supports at most 32 local address bits");
  }
  return out;
}
} // namespace nocloom::detail
