// Core consumer: the target timeout definition of
// tests/verif/cases/target_timeouts.py (graph mode) built through the public
// C++ API (verif_config compares its artifacts with the CLI's).
#include "endpoint_ports.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
#include <nocloom/generation.hpp>
#include <string_view>

int main(int argc, char **argv) {
  if (argc != 2)
    return 2;
  using namespace nocloom;
  Definition d;
  d.schema_version = 10;
  d.name = "timeouts";
  d.address_width = 32;
  d.arbitration = ArbitrationPolicy::RoundRobin;
  d.reset_ports = {{"reset_n"}};
  d.test_ports = {{"test"}};
  d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
  // m0 keeps four bursts in flight, for the targets with several (mx, mq, ml, xe).
  d.initiators = {{"s0", "main"}, {"m0", "main"}};
  for (auto &i : d.initiators) {
    i.protocol = Protocol::Axi4;
    i.data_width = 32;
  }
  d.initiators[1].max_outstanding = 4;
  // s0's packets pipe keeps two request words, as in target_timeouts.py.
  d.initiators[0].pipes.packets = {.forward = true, .backward = true};
  struct Spec {
    const char *name = nullptr;
    Protocol protocol = Protocol::Axi4;
    std::uint64_t base = 0, size = 0;
    unsigned cycles = 0;
    bool completion = false;
    unsigned capacity = 0;
  };
  ExchangeTopology topology;
  topology.request.nodes = {{"q0"}};
  topology.request.entries = {{"s0", "q0"}, {"m0", "q0"}};
  topology.response.nodes = {{"p0"}};
  topology.response.exits = {{"s0", "p0"}, {"m0", "p0"}};
  for (const auto &s : {Spec{"pa", Protocol::Apb4, 0x10000, 0x100, 1024, true},
                        Spec{"pb", Protocol::Apb3, 0x11000, 0x1000, 2048, true},
                        Spec{"lt", Protocol::Axi4Lite, 0x12000, 0x1000, 1024, true},
                        Spec{"ah", Protocol::AhbV3, 0x13000, 0x1000, 1024, true},
                        Spec{"ax", Protocol::Axi4, 0x20000, 0x1000, 1024, true},
                        Spec{"ok", Protocol::Apb4, 0x14000, 0x1000, 0, false},
                        Spec{"lg", Protocol::Apb4, 0x15000, 0x1000, 4096, true},
                        Spec{"tn", Protocol::Apb4, 0x1B000, 0x1000, 16, true},
                        Spec{"hg", Protocol::Apb4, 0x1C000, 0x1000, 8192, true},
                        Spec{"ab", Protocol::AhbV3, 0x16000, 0x1000, 1024, true},
                        Spec{"qa", Protocol::Apb4, 0x17000, 0x1000, 1024, false},
                        Spec{"ql", Protocol::Axi4Lite, 0x18000, 0x1000, 1024, false},
                        Spec{"qh", Protocol::AhbV3, 0x19000, 0x1000, 1024, false},
                        Spec{"qx", Protocol::Axi4, 0x21000, 0x1000, 1024, false},
                        Spec{"mx", Protocol::Axi4, 0x26000, 0x1000, 1024, true, 4},
                        Spec{"mq", Protocol::Axi4, 0x27000, 0x1000, 1024, false, 2},
                        Spec{"ml", Protocol::Axi4Lite, 0x1A000, 0x1000, 1024, true, 4},
                        Spec{"xe", Protocol::Axi4, 0x28000, 0x1000, 1024, true, 4}}) {
    Target t{s.name, "main", {{s.base, s.size}}};
    t.protocol = s.protocol;
    t.data_width = 32;
    if (s.cycles)
      t.timeout = TargetTimeout{s.cycles, s.completion};
    if (s.capacity)
      t.max_outstanding = s.capacity;
    // xe keeps its own exclusive monitor.
    if (std::string_view{s.name} == "xe") {
      t.exclusive = ExclusiveAccess::Target;
      t.exclusive_id_flags = {"owner"};
    }
    // INCR packets go out as fixed-length blocks, with the 2-bit AMBA 2 HRESP.
    if (std::string_view{s.name} == "ab") {
      t.undefined_length_bursts = false;
      t.hresp_width = 2;
    }
    d.targets.push_back(t);
    topology.request.exits.push_back({s.name, "q0"});
    topology.response.entries.push_back({s.name, "p0"});
  }
  // Reads and writes share the source's bursts in flight: two keep a write
  // flowing while a read hangs (axi4_read_hang_writes_progress in the bench).
  d.transport = TransportOptions{.max_outstanding = 2,
                                 .transport_word_bytes = 4,
                                 .response_codes = nocloom::ResponseCodes::Distinct};
  d.transport->user_flags = {"owner"};
  d.initiators[0].user_flags["owner"] = {AttributeSource::Zero};
  d.initiators[1].user_flags["owner"] = {AttributeSource::One};
  endpoint_ports(d, 2, 8);
  d.topology = topology;
  d.connectivity = Connectivity{.all = true};
  auto generated = generate(d);
  if (!generated) {
    for (const auto &diagnostic : generated.diagnostics())
      std::cerr << diagnostic.code << ": " << diagnostic.message << '\n';
    return 1;
  }
  std::filesystem::create_directories(argv[1]);
  for (const auto &a : generated.value()) {
    std::ofstream f(std::filesystem::path(argv[1]) / a.path);
    f << a.text;
    if (!f)
      return 1;
  }
  return 0;
}
