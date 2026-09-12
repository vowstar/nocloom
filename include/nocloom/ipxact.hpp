#pragma once

#include "nocloom/generation.hpp"
#include <string>

namespace nocloom {
// The component identifier written to the IP-XACT description. An empty version
// is written as 1.0. Each field takes letters, digits and "_.-".
struct IpxactOptions {
  std::string vendor = "nocloom";
  std::string library = "nocloom";
  std::string version;
};

// Adds `<top>.ipxact.xml`, an IEEE 1685-2009 component of the generated top module, to the
// artifacts that generate() or emit() returned for the same definition, and lists it in the
// bundle. The description is built from the map's ports, windows and connected pairs; the other
// artifacts keep their bytes. No file I/O.
Result<ArtifactSet> add_ipxact(const Definition &definition, const ArtifactSet &artifacts,
                               const IpxactOptions &options = {});
} // namespace nocloom
