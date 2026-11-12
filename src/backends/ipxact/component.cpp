#include "backends/output_license.hpp"
#include "core/diagnostic.hpp"
#include "nocloom/ipxact.hpp"
#include <algorithm>
#include <cstdint>
#include <iterator>
#include <map>
#include <nlohmann/json.hpp>
#include <optional>
#include <set>
#include <string_view>

namespace nocloom {
namespace {
using Json = nlohmann::json;
constexpr std::string_view code = "NCL_IPXACT_INPUT";

// A public AMBA bus definition (Arm's IP-XACT library) and its RTL abstraction definition.
struct BusType {
  const char *library = "", *name = "", *version = "", *abstraction = "";
  const char *clock = "", *reset = "";
  // Logical ports of the abstraction, with their width where the definition fixes one (0 when the
  // component chooses it).
  std::vector<std::pair<std::string_view, unsigned>> logical;
};

const BusType &bus_type(Protocol protocol, bool initiator) {
  using L = std::vector<std::pair<std::string_view, unsigned>>;
  const L axi_lite = {{"AWADDR", 0}, {"AWPROT", 3}, {"AWVALID", 1}, {"AWREADY", 1}, {"WDATA", 0},
                      {"WSTRB", 0},  {"WVALID", 1}, {"WREADY", 1},  {"BRESP", 2},   {"BVALID", 1},
                      {"BREADY", 1}, {"ARADDR", 0}, {"ARPROT", 3},  {"ARVALID", 1}, {"ARREADY", 1},
                      {"RDATA", 0},  {"RRESP", 2},  {"RVALID", 1},  {"RREADY", 1}};
  L axi = axi_lite;
  axi.insert(axi.end(),
             {{"AWID", 0},    {"AWLEN", 8}, {"AWSIZE", 3},   {"AWBURST", 2}, {"AWLOCK", 1},
              {"AWCACHE", 4}, {"AWQOS", 4}, {"AWREGION", 4}, {"WLAST", 1},   {"BID", 0},
              {"ARID", 0},    {"ARLEN", 8}, {"ARSIZE", 3},   {"ARBURST", 2}, {"ARLOCK", 1},
              {"ARCACHE", 4}, {"ARQOS", 4}, {"ARREGION", 4}, {"RID", 0},     {"RLAST", 1},
              {"AWUSER", 0},  {"WUSER", 0}, {"BUSER", 0},    {"ARUSER", 0},  {"RUSER", 0}});
  // AMBA 3 AXI: no AxQOS or AxREGION, a four-bit AxLEN and a two-bit AxLOCK, and WID.
  L axi3 = axi_lite;
  axi3.insert(axi3.end(),
              {{"AWID", 0},    {"AWLEN", 4},  {"AWSIZE", 3},  {"AWBURST", 2}, {"AWLOCK", 2},
               {"AWCACHE", 4}, {"WID", 0},    {"WLAST", 1},   {"BID", 0},     {"ARID", 0},
               {"ARLEN", 4},   {"ARSIZE", 3}, {"ARBURST", 2}, {"ARLOCK", 2},  {"ARCACHE", 4},
               {"RID", 0},     {"RLAST", 1},  {"AWUSER", 0},  {"WUSER", 0},   {"BUSER", 0},
               {"ARUSER", 0},  {"RUSER", 0}});
  const L apb2 = {{"PADDR", 0},  {"PSELx", 1},  {"PENABLE", 1},
                  {"PWRITE", 1}, {"PRDATA", 0}, {"PWDATA", 0}};
  L apb3 = apb2;
  apb3.insert(apb3.end(), {{"PREADY", 1}, {"PSLVERR", 1}});
  L apb4 = apb3;
  apb4.insert(apb4.end(), {{"PPROT", 3}, {"PSTRB", 0}});
  // HSELx and HREADYOUT are illegal on an AHB-Lite master interface.
  const L ahb = {{"HADDR", 0},  {"HBURST", 3}, {"HMASTLOCK", 1}, {"HPROT", 4},
                 {"HSIZE", 3},  {"HTRANS", 2}, {"HWDATA", 0},    {"HWRITE", 1},
                 {"HRDATA", 0}, {"HRESP", 1},  {"HREADY", 1}};
  static const std::map<Protocol, BusType> types = {
      {Protocol::Axi4, {"AMBA4", "AXI4", "r0p0_0", "AXI4_rtl", "ACLK", "ARESETn", axi}},
      {Protocol::Axi3, {"AMBA3", "AXI", "r2p0_0", "AXI_rtl", "ACLK", "ARESETn", axi3}},
      {Protocol::Axi4Lite,
       {"AMBA4", "AXI4-Lite", "r0p0_0", "AXI4-Lite_rtl", "ACLK", "ARESETn", axi_lite}},
      {Protocol::Apb4, {"AMBA4", "APB4", "r0p0_0", "APB4_rtl", "PCLK", "PRESETn", apb4}},
      {Protocol::Apb3, {"AMBA3", "APB", "r2p0_0", "APB_rtl", "PCLK", "PRESETn", apb3}},
      {Protocol::Apb2, {"AMBA2", "APB", "r2p0_4", "APB_rtl", "PCLK", "PRESETn", apb2}},
      {Protocol::AhbV3, {"AMBA3", "AHBLite", "r2p0_0", "AHBLite_rtl", "HCLK", "HRESETn", ahb}}};
  // An AHB initiator is a subordinate (slave interface): it adds HSELx and HREADYOUT.
  static const BusType ahb_slave = [] {
    auto t = types.at(Protocol::AhbV3);
    t.logical.insert(t.logical.end(), {{"HSELx", 1}, {"HREADYOUT", 1}});
    return t;
  }();
  return initiator && (protocol == Protocol::AhbV3 || protocol == Protocol::Ahb5)
             ? ahb_slave
             : types.at(protocol == Protocol::Ahb5 ? Protocol::AhbV3 : protocol);
}

std::string hex(std::uint64_t value) {
  static const char digits[] = "0123456789ABCDEF";
  std::string out;
  do {
    out.insert(out.begin(), digits[value % 16]);
    value /= 16;
  } while (value);
  return "0x" + out;
}

bool portable(std::string_view text) {
  return !text.empty() && std::ranges::all_of(text, [](char c) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' ||
           c == '.' || c == '-';
  });
}

struct Port {
  std::string name, direction, endpoint, signal, clock;
  unsigned width = 1;
};
struct Window {
  std::string target;
  std::uint64_t base = 0, size = 0, local = 0;
  std::map<std::string, bool> when; // its mode condition, empty for every mode
  unsigned flow = 0;                // its target flow
};
// The remap state of a mode condition: its name and the address input values that select it. A
// selector that must hold contributes its input values; one that must not hold, the other value of
// its single one-bit input. Nothing else maps onto input values, nor inputs that two selectors
// give different values.
struct RemapState {
  std::string name;
  std::map<std::string, std::uint64_t> ports;
};
std::optional<RemapState> remap_state(const Definition &d,
                                      const std::map<std::string, bool> &when) {
  RemapState out;
  for (const auto &[name, value] : when) {
    const auto flag = std::ranges::find(d.address_selectors, name, &AddressSelector::name);
    if (flag == d.address_selectors.end())
      return std::nullopt;
    out.name += (out.name.empty() ? "" : "_and_") + std::string(value ? "" : "not_") + name;
    for (const auto &[port, level] : flag->equals) {
      const auto p = std::ranges::find(d.address_inputs, port, &AddressInput::name);
      if (!value && (flag->equals.size() != 1 || p == d.address_inputs.end() || p->width != 1))
        return std::nullopt;
      const std::uint64_t want = value ? level : 1 - level;
      if (const auto [at, fresh] = out.ports.emplace(port, want); !fresh && at->second != want)
        return std::nullopt;
    }
  }
  return out;
}
// A bus interface: an endpoint, or one port set (flow) of a target of several (its ports
// `<signal>_<flow>`, the interface `<target>_<flow>`).
struct Endpoint {
  std::string name;
  Protocol protocol = Protocol::Axi4;
  bool initiator = false;
  std::string clock_domain;
  std::string endpoint = name;
  std::optional<unsigned> set = std::nullopt;
};
// Whether a target of several flows has a port set per flow (an AXI4 or AXI3 target carries the
// flow in AxID on its one set).
bool port_sets(const Target &t) {
  const auto protocol = t.protocol.value_or(Protocol::Axi4);
  return t.flows > 1 && protocol != Protocol::Axi4 && protocol != Protocol::Axi3;
}

// The ports, windows and connected pairs the map records, in its order.
struct Map {
  std::string module;
  std::vector<Port> ports;
  std::vector<Window> windows;
  std::set<std::pair<std::string, std::string>> pairs;
  // The target flows each pair reaches, for pairs listing their flow pairs (flow_pairs).
  std::map<std::pair<std::string, std::string>, std::set<unsigned>> flows{};
};

std::optional<Map> read_map(const Json &map) {
  try {
    Map out{map.at("module").get<std::string>(), {}, {}, {}};
    for (const auto &p : map.at("ports"))
      out.ports.push_back({p.at("name"), p.at("direction"), p.at("endpoint"), p.at("signal"),
                           p.at("clock"), p.at("width").get<unsigned>()});
    const auto &definition = map.at("definition");
    for (const auto &w : definition.at("targets")) {
      const auto begin = std::stoull(w.at("begin").get<std::string>());
      const auto end = std::stoull(w.at("end").get<std::string>());
      const auto local = w.contains("local") ? std::stoull(w.at("local").get<std::string>()) : 0;
      if (end <= begin)
        return std::nullopt;
      out.windows.push_back({w.at("name"), begin, end - begin, local,
                             w.value("when", std::map<std::string, bool>{}), w.value("flow", 0U)});
    }
    for (const auto &c : definition.at("connectivity")) {
      out.pairs.emplace(c.at("source"), c.at("target"));
      if (c.contains("flow_pairs")) {
        auto &to = out.flows[{c.at("source"), c.at("target")}];
        for (const auto &fg : c.at("flow_pairs"))
          to.insert(fg.at(1).get<unsigned>());
      }
    }
    return out;
  } catch (const std::exception &) {
    return std::nullopt;
  }
}

class Writer {
public:
  void open(std::string_view tag, std::string_view attributes = {}) {
    line("<spirit:" + std::string(tag) + std::string(attributes) + ">");
    ++depth_;
  }
  void close(std::string_view tag) {
    --depth_;
    line("</spirit:" + std::string(tag) + ">");
  }
  void leaf(std::string_view tag, std::string_view text, std::string_view attributes = {}) {
    line("<spirit:" + std::string(tag) + std::string(attributes) + ">" + std::string(text) +
         "</spirit:" + std::string(tag) + ">");
  }
  void empty(std::string_view tag, std::string_view attributes) {
    line("<spirit:" + std::string(tag) + std::string(attributes) + "/>");
  }
  void raw(std::string_view text) { line(std::string(text)); }
  const std::string &text() const { return out_; }

private:
  void line(const std::string &content) { out_ += std::string(2 * depth_, ' ') + content + "\n"; }
  std::string out_;
  unsigned depth_ = 0;
};

std::string vlnv(std::string_view vendor, std::string_view library, std::string_view name,
                 std::string_view revision) {
  return " spirit:vendor=\"" + std::string(vendor) + "\" spirit:library=\"" + std::string(library) +
         "\" spirit:name=\"" + std::string(name) + "\" spirit:version=\"" + std::string(revision) +
         "\"";
}

class Component {
public:
  Component(const Definition &d, const Map &map, const IpxactOptions &o) : d_(d), map_(map), o_(o) {
    std::ranges::transform(d.initiators, std::back_inserter(endpoints_), [](const Initiator &i) {
      return Endpoint{i.name, i.protocol.value_or(Protocol::Axi4), true, i.clock_domain};
    });
    for (const auto &t : d.targets) {
      // The registers of error logging have no ports.
      if (t.log_registers)
        continue;
      const Endpoint e{t.name, t.protocol.value_or(Protocol::Axi4), false, t.clock_domain};
      if (!port_sets(t)) {
        endpoints_.push_back(e);
        continue;
      }
      for (unsigned k = 0; k < t.flows; ++k)
        endpoints_.push_back(
            {t.name + "_" + std::to_string(k), e.protocol, false, t.clock_domain, t.name, k});
    }
    // Initiators, then targets, each in name order: declaration order never reaches the bytes.
    std::ranges::sort(endpoints_, {},
                      [](const Endpoint &e) { return std::pair{!e.initiator, e.name}; });
    for (const auto &p : map.ports)
      top_.insert(p.name);
  }
  std::string text() {
    w_.raw("<?xml version=\"1.0\" encoding=\"UTF-8\"?>");
    w_.raw(detail::output_license_comment("<!--", " -->"));
    w_.open("component",
            " xmlns:spirit=\"http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009\""
            " xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\""
            " xsi:schemaLocation=\"http://www.spiritconsortium.org/XMLSchema/SPIRIT/"
            "1685-2009 http://www.spiritconsortium.org/XMLSchema/SPIRIT/1685-2009/"
            "index.xsd\"");
    w_.leaf("vendor", o_.vendor);
    w_.leaf("library", o_.library);
    w_.leaf("name", map_.module);
    w_.leaf("version", o_.version.empty() ? std::string("1.0") : o_.version);
    w_.open("busInterfaces");
    for (const auto &e : endpoints_)
      bus_interface(e);
    w_.close("busInterfaces");
    remap_states();
    address_spaces();
    memory_maps();
    model();
    w_.leaf("description", "Generated by NocLoom");
    w_.close("component");
    return w_.text();
  }

private:
  static std::string endpoint_key(const Endpoint &e) {
    return (e.initiator ? "initiator:" : "target:") + e.endpoint;
  }
  // The target bus interface a window belongs to: its target's, or its flow's port set.
  const Endpoint &interface_of(const Window &w) const {
    return *std::ranges::find_if(endpoints_, [&](const Endpoint &e) {
      return !e.initiator && e.endpoint == w.target && (!e.set || *e.set == w.flow);
    });
  }
  bool reaches(const std::string &initiator, const Window &w) const {
    const auto at = map_.flows.find({initiator, w.target});
    return map_.pairs.contains({initiator, w.target}) &&
           (at == map_.flows.end() || at->second.contains(w.flow));
  }
  // The target bus interfaces an initiator reaches, in name order.
  std::vector<std::string> connected(const Endpoint &e) const {
    std::set<std::string> out;
    for (const auto &w : map_.windows)
      if (reaches(e.name, w))
        out.insert(interface_of(w).name);
    return {out.begin(), out.end()};
  }
  // The local ranges a target bus interface answers, as its address space segments, in address
  // order.
  std::vector<std::pair<std::uint64_t, std::uint64_t>> segments(const std::string &face) const {
    std::vector<std::pair<std::uint64_t, std::uint64_t>> out;
    for (const auto &w : map_.windows)
      if (interface_of(w).name == face &&
          std::ranges::find(out, std::pair{w.local, w.size}) == out.end())
        out.emplace_back(w.local, w.size);
    std::ranges::sort(out);
    return out;
  }
  std::string segment_name(const std::string &face, std::uint64_t local, std::uint64_t size) const {
    const auto all = segments(face);
    const auto at = std::ranges::find(all, std::pair{local, size}) - all.begin();
    return face + "_segment" + std::to_string(at);
  }
  // The signal of a port of the interface, without its port set suffix; empty for a port of
  // another set.
  static std::string signal_of(const Endpoint &e, const Port &p) {
    if (!e.set)
      return p.signal;
    const auto suffix = "_" + std::to_string(*e.set);
    return p.signal.ends_with(suffix) ? p.signal.substr(0, p.signal.size() - suffix.size()) : "";
  }
  void port_map(std::string_view logical, const std::string &physical,
                std::optional<unsigned> part = std::nullopt) {
    w_.open("portMap");
    w_.open("logicalPort");
    w_.leaf("name", logical);
    if (part) {
      w_.open("vector");
      w_.leaf("left", std::to_string(*part - 1));
      w_.leaf("right", "0");
      w_.close("vector");
    }
    w_.close("logicalPort");
    w_.open("physicalPort");
    w_.leaf("name", physical);
    w_.close("physicalPort");
    w_.close("portMap");
  }
  void bus_interface(const Endpoint &e) {
    const auto &bus = bus_type(e.protocol, e.initiator);
    w_.open("busInterface");
    w_.leaf("name", e.name);
    w_.empty("busType", vlnv("amba.com", bus.library, bus.name, bus.version));
    w_.empty("abstractionType", vlnv("amba.com", bus.library, bus.abstraction, bus.version));
    if (e.initiator) {
      w_.open("slave");
      if (!connected(e).empty())
        w_.empty("memoryMapRef", " spirit:memoryMapRef=\"" + e.name + "_map\"");
      for (const auto &target : connected(e))
        w_.empty("bridge", " spirit:masterRef=\"" + target + "\" spirit:opaque=\"true\"");
      w_.close("slave");
    } else {
      w_.open("master");
      w_.open("addressSpaceRef", " spirit:addressSpaceRef=\"" + e.name + "_space\"");
      w_.leaf("baseAddress", "0x0");
      w_.close("addressSpaceRef");
      w_.close("master");
    }
    w_.open("portMaps");
    const std::string clock = clock_of(e);
    if (top_.contains(clock))
      port_map(bus.clock, clock);
    if (const auto reset = reset_of(e); top_.contains(reset))
      port_map(bus.reset, reset);
    for (const auto &p : map_.ports) {
      if (p.endpoint != endpoint_key(e) || signal_of(e, p).empty())
        continue;
      std::string logical = signal_of(e, p);
      std::ranges::transform(logical, logical.begin(), [](char c) {
        return static_cast<char>(c >= 'a' && c <= 'z' ? c - 'a' + 'A' : c);
      });
      if ((e.protocol == Protocol::AhbV3 || e.protocol == Protocol::Ahb5) && !e.initiator) {
        // The master abstraction receives HREADY from the target response multiplexor.
        // The physical HREADY output to slaves remains a plain model port.
        if (logical == "HREADY")
          continue;
        if (logical == "HREADYOUT")
          logical = "HREADY";
      }
      if (logical == "PSEL" || logical == "HSEL")
        logical.push_back('x');
      const auto found = std::ranges::find(bus.logical, std::string_view(logical),
                                           [](const auto &x) { return x.first; });
      // A port the abstraction lacks, or wider than its fixed logical width, stays a plain port.
      if (found == bus.logical.end() || (found->second && p.width > found->second))
        continue;
      port_map(found->first, p.name,
               found->second && p.width < found->second ? std::optional{p.width} : std::nullopt);
    }
    w_.close("portMaps");
    w_.close("busInterface");
  }
  std::string clock_of(const Endpoint &e) const {
    const auto at = std::ranges::find(map_.ports, endpoint_key(e), &Port::endpoint);
    return at == map_.ports.end() ? std::string() : at->clock;
  }
  std::string reset_of(const Endpoint &e) const {
    const auto at = std::ranges::find(d_.clock_domains, e.clock_domain, &ClockDomain::name);
    return at == d_.clock_domains.end() ? std::string() : at->reset_port;
  }
  // Every target has a write or a read data port.
  unsigned data_width(const Endpoint &e) const {
    static const std::set<std::string> data = {"wdata", "pwdata", "hwdata",
                                               "rdata", "prdata", "hrdata"};
    const auto at = std::ranges::find_if(map_.ports, [&](const Port &p) {
      return p.endpoint == endpoint_key(e) && data.contains(signal_of(e, p));
    });
    return at == map_.ports.end() ? 32 : at->width;
  }
  void address_spaces() {
    w_.open("addressSpaces");
    for (const auto &e : endpoints_) {
      if (e.initiator)
        continue;
      const auto all = segments(e.name);
      std::uint64_t range = 0;
      for (const auto &[local, size] : all)
        range = std::max(range, local + size);
      w_.open("addressSpace");
      w_.leaf("name", e.name + "_space");
      w_.leaf("range", hex(range));
      w_.leaf("width", std::to_string(data_width(e)));
      w_.open("segments");
      for (const auto &[local, size] : all) {
        w_.open("segment");
        w_.leaf("name", segment_name(e.name, local, size));
        w_.leaf("addressOffset", hex(local));
        w_.leaf("range", hex(size));
        w_.close("segment");
      }
      w_.close("segments");
      w_.close("addressSpace");
    }
    w_.close("addressSpaces");
  }
  // The remap state of each mode condition of a window, by name.
  std::map<std::string, RemapState> states() const {
    std::map<std::string, RemapState> out;
    for (const auto &w : map_.windows)
      if (!w.when.empty())
        if (const auto state = remap_state(d_, w.when))
          out.emplace(state->name, *state);
    return out;
  }
  void remap_states() {
    const auto all = states();
    if (all.empty())
      return;
    w_.open("remapStates");
    for (const auto &[name, state] : all) {
      w_.open("remapState");
      w_.leaf("name", name);
      w_.open("remapPorts");
      for (const auto &[port, value] : state.ports)
        w_.leaf("remapPort", hex(value), " spirit:portNameRef=\"" + port + "\"");
      w_.close("remapPorts");
      w_.close("remapState");
    }
    w_.close("remapStates");
  }
  void subspace_map(const std::string &initiator, const Window &win) {
    const auto &face = interface_of(win).name;
    const auto segment = segment_name(face, win.local, win.size);
    w_.open("subspaceMap",
            " spirit:masterRef=\"" + face + "\" spirit:segmentRef=\"" + segment + "\"");
    w_.leaf("name", initiator + "_" + face + "_" + hex(win.base));
    w_.leaf("baseAddress", hex(win.base));
    w_.close("subspaceMap");
  }
  // Each initiator's memory map places every window of a connected target at its global base; a
  // mode-dependent window sits in the memory remap of its condition's state.
  void memory_maps() {
    bool any = false;
    for (const auto &e : endpoints_)
      any = any || (e.initiator && !connected(e).empty());
    if (!any)
      return;
    w_.open("memoryMaps");
    for (const auto &e : endpoints_) {
      if (!e.initiator || connected(e).empty())
        continue;
      w_.open("memoryMap");
      w_.leaf("name", e.name + "_map");
      for (const auto &win : map_.windows)
        if (win.when.empty() && reaches(e.name, win))
          subspace_map(e.name, win);
      for (const auto &[name, state] : states()) {
        const auto in_state = [&](const Window &win) {
          return !win.when.empty() && remap_state(d_, win.when)->name == name &&
                 reaches(e.name, win);
        };
        if (std::ranges::none_of(map_.windows, in_state))
          continue;
        w_.open("memoryRemap", " spirit:state=\"" + name + "\"");
        w_.leaf("name", e.name + "_map_" + name);
        for (const auto &win : map_.windows)
          if (in_state(win))
            subspace_map(e.name, win);
        w_.close("memoryRemap");
      }
      w_.leaf("addressUnitBits", "8");
      w_.close("memoryMap");
    }
    w_.close("memoryMaps");
  }
  std::optional<std::uint64_t> period(const std::string &clock) const {
    const auto at = std::ranges::find_if(d_.clock_domains, [&](const ClockDomain &c) {
      return c.clock_port == clock && c.period_ps;
    });
    return at == d_.clock_domains.end() ? std::nullopt : at->period_ps;
  }
  void model() {
    w_.open("model");
    w_.open("views");
    w_.open("view");
    w_.leaf("name", "rtl");
    w_.leaf("envIdentifier", ":*Simulation:");
    w_.leaf("envIdentifier", ":*Synthesis:");
    w_.leaf("language", "verilog");
    w_.leaf("modelName", map_.module);
    w_.close("view");
    w_.close("views");
    w_.open("ports");
    for (const auto &p : map_.ports) {
      w_.open("port");
      w_.leaf("name", p.name);
      w_.open("wire");
      w_.leaf("direction", p.direction == "input" ? "in" : "out");
      if (p.width > 1) {
        w_.open("vector");
        w_.leaf("left", std::to_string(p.width - 1));
        w_.leaf("right", "0");
        w_.close("vector");
      }
      if (const auto ps = period(p.name); ps && p.signal == "clock") {
        const auto half = std::to_string(*ps / 2) + (*ps % 2 ? ".5" : "");
        w_.open("driver");
        w_.open("clockDriver");
        w_.leaf("clockPeriod", std::to_string(*ps), " spirit:units=\"ps\"");
        w_.leaf("clockPulseOffset", "0", " spirit:units=\"ps\"");
        w_.leaf("clockPulseValue", "1");
        w_.leaf("clockPulseDuration", half, " spirit:units=\"ps\"");
        w_.close("clockDriver");
        w_.close("driver");
      }
      w_.close("wire");
      w_.close("port");
    }
    w_.close("ports");
    w_.close("model");
  }

  const Definition &d_;
  const Map &map_;
  const IpxactOptions &o_;
  std::vector<Endpoint> endpoints_;
  std::set<std::string> top_;
  Writer w_;
};

Diagnostic failure(std::string field, std::string message) {
  return detail::error(std::string(code), std::move(field), std::move(message));
}
} // namespace

Result<ArtifactSet> add_ipxact(const Definition &definition, const ArtifactSet &artifacts,
                               const IpxactOptions &options) {
  for (const auto &[field, value] :
       {std::pair{"ipxact.vendor", &options.vendor}, std::pair{"ipxact.library", &options.library}})
    if (!portable(*value))
      return Result<ArtifactSet>::failure(
          failure(field, "Expected letters, digits, '_', '.' or '-'"));
  if (!options.version.empty() && !portable(options.version))
    return Result<ArtifactSet>::failure(
        failure("ipxact.version", "Expected letters, digits, '_', '.' or '-'"));
  const auto bundle_at = std::ranges::find_if(
      artifacts, [](const Artifact &a) { return a.path.ends_with(".bundle.json"); });
  Json bundle;
  std::optional<Map> map;
  if (bundle_at != artifacts.end() && std::ranges::count_if(artifacts, [](const Artifact &a) {
                                        return a.path.ends_with(".bundle.json");
                                      }) == 1) {
    bundle = Json::parse(bundle_at->text, nullptr, false);
    const auto map_at = std::ranges::find_if(artifacts, [&](const Artifact &a) {
      return bundle.is_object() && a.path == bundle.value("top", "") + ".map.json";
    });
    if (map_at != artifacts.end())
      if (const auto parsed = Json::parse(map_at->text, nullptr, false); !parsed.is_discarded())
        map = read_map(parsed);
  }
  if (!map || !bundle.contains("files") || map->module != "nocloom_" + definition.name)
    return Result<ArtifactSet>::failure(failure(
        "artifacts", "Expected the artifacts generated for this definition, with its map and "
                     "bundle"));
  const auto path = map->module + ".ipxact.xml";
  if (std::ranges::any_of(artifacts, [&](const Artifact &a) { return a.path == path; }))
    return Result<ArtifactSet>::failure(failure("artifacts", "The artifacts already hold " + path));
  // The registers of error logging have no bus interface for their window to belong to.
  std::erase_if(map->windows, [&](const Window &w) {
    const auto at = std::ranges::find(definition.targets, w.target, &Target::name);
    return at != definition.targets.end() && at->log_registers;
  });
  // Every endpoint in the map must be one of the definition's, with the same direction.
  std::set<std::string> endpoints;
  for (const auto &i : definition.initiators)
    endpoints.insert("initiator:" + i.name);
  for (const auto &t : definition.targets)
    endpoints.insert("target:" + t.name);
  const auto stranger = std::ranges::find_if(map->ports, [&](const Port &p) {
    return (p.endpoint.starts_with("initiator:") || p.endpoint.starts_with("target:")) &&
           !endpoints.contains(p.endpoint);
  });
  if (stranger != map->ports.end())
    return Result<ArtifactSet>::failure(failure(
        "artifacts", "The map endpoint " + stranger->endpoint + " is not in the definition"));
  // Every mode condition must map onto a remap state.
  for (std::size_t i = 0; i < definition.targets.size(); ++i)
    for (std::size_t k = 0; k < definition.targets[i].mappings.size(); ++k)
      if (const auto &when = definition.targets[i].mappings[k].when;
          !when.empty() && !remap_state(definition, when))
        return Result<ArtifactSet>::failure(detail::error(
            "NCL_MODE_CONDITION",
            "targets[" + std::to_string(i) + "].mappings[" + std::to_string(k) + "].when",
            "An IP-XACT remap state holds address input values: a false selector needs one one-bit "
            "input, and the selectors of a condition must not require two values of one input"));
  ArtifactSet out = artifacts;
  out.push_back({path, Component(definition, *map, options).text()});
  bundle["files"].push_back({{"path", path}, {"role", "ipxact_component"}});
  std::ranges::find(out, bundle_at->path, &Artifact::path)->text = bundle.dump(2) + "\n";
  std::ranges::sort(out, {}, &Artifact::path);
  return Result<ArtifactSet>::success(std::move(out));
}
} // namespace nocloom
