#include "diagnostic.hpp"
#include "tilelink.hpp"

namespace nocloom::detail {
namespace {
template <class Endpoint>
void fields(const Endpoint &e, const std::string &path, std::vector<Diagnostic> &out) {
  const auto reject = [&](bool bad, const char *key, const char *message) {
    if (bad)
      out.push_back(error("NCL_TILELINK_PROFILE", path + "." + key, message));
  };
  if (e.protocol != Protocol::TlUl) {
    for (const auto &[key, value] :
         {std::pair{"source_width", e.source_width}, std::pair{"size_width", e.size_width},
          std::pair{"sink_width", e.sink_width},
          std::pair{"max_transfer_bytes", e.max_transfer_bytes}})
      reject(value.has_value(), key, "This field requires protocol tl_ul");
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
  const auto reject = [&](bool bad, const std::string &path, const char *message) {
    if (bad)
      out.push_back(error("NCL_TILELINK_PROFILE", path, message));
  };
  for (unsigned k = 0; k < d.initiators.size(); ++k) {
    const auto &s = d.initiators[k];
    const auto path = "initiators[" + std::to_string(k) + "]";
    fields(s, path, out);
    if (s.protocol != Protocol::TlUl)
      continue;
    reject(d.transport && d.transport->data_width == 64, "transport.data_width",
           "TileLink endpoints require a 32-bit network");
    reject(s.access.has_value(), path + ".access", "TL-UL initiators support both Get and Put");
    reject(s.narrow_bursts.has_value(), path + ".narrow_bursts", "TL-UL has no bursts");
    reject(s.user_width.has_value(), path + ".user_width", "TL-UL has no user ports");
    reject(s.response_user_width.has_value(), path + ".response_user_width",
           "TL-UL has no response user ports");
    reject(s.pipes != InitiatorPipes{}, path + ".pipes", "TL-UL uses its registered protocol face");
  }
  for (unsigned k = 0; k < d.targets.size(); ++k) {
    const auto &t = d.targets[k];
    const auto path = "targets[" + std::to_string(k) + "]";
    fields(t, path, out);
    if (t.protocol != Protocol::TlUl)
      continue;
    reject(d.transport && d.transport->data_width == 64, "transport.data_width",
           "TileLink endpoints require a 32-bit network");
    for (const auto &mapping : t.mappings)
      reject(mapping.size < 4, path + ".mappings",
             "A TileLink target mapping must hold one data word");
    reject(t.max_outstanding.value_or(1) != 1, path + ".max_outstanding",
           "A TL-UL target supports only one transaction in flight");
    reject(t.flows != 1, path + ".flows", "A TL-UL target supports only one flow");
    reject(t.timeout && t.timeout->completion, path + ".timeout.completion",
           "A TileLink target cannot replace a response on timeout");
    reject(t.attributes.has_value(), path + ".attributes", "TL-UL targets have no attribute ports");
    reject(!t.user.empty(), path + ".user", "TL-UL targets have no user ports");
    reject(t.address_width.value_or(d.address_width) > 32, path + ".address_width",
           "A TL-UL target supports at most 32 local address bits");
  }
  return out;
}
} // namespace nocloom::detail
