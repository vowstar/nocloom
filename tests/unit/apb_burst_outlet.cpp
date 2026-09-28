#include "core/ahb_outlet.hpp"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <map>
#include <numeric>
#include <utility>
#include <vector>

TEST_CASE("Burst APB4 outlet validates identity and window geometry", "[apb-burst-outlet]") {
  using namespace nocloom::detail;
  for (unsigned ids : {0U, 4U, 8U, 9U, 16U})
    for (unsigned length : {4U, 8U}) {
      auto h = lower_apb_burst_outlet(
          {"probe", ids, length, 7, std::vector<nocloom::AddressMapping>{{0x1000, 0x2000}}});
      REQUIRE(h);
      REQUIRE(verify(h.value().ir));
      std::map<std::string, unsigned> ports;
      for (const auto &port : h.value().ir.ports) {
        REQUIRE(port.name.find("nl_ahb_") != 0);
        ports[port.name] = port.width;
      }
      for (const auto &[name, width] : std::map<std::string, unsigned>{{"paddr", 32},
                                                                       {"pwdata", 32},
                                                                       {"prdata", 32},
                                                                       {"pprot", 3},
                                                                       {"pstrb", 4},
                                                                       {"psel", 1},
                                                                       {"penable", 1},
                                                                       {"pwrite", 1},
                                                                       {"pready", 1},
                                                                       {"pslverr", 1}})
        REQUIRE(ports.at("nl_apb_" + name) == width);
      REQUIRE_FALSE(h.value().bindings.contains("bus.size"));
      for (const auto &s : h.value().ir.states)
        REQUIRE(s.reset.has_value());
    }
  for (auto p :
       {ApbBurstOutletProfile{"bad-name"}, ApbBurstOutletProfile{"probe", 17},
        ApbBurstOutletProfile{"probe", 0, 9}, ApbBurstOutletProfile{"probe", 0, 8, 10},
        ApbBurstOutletProfile{"probe", 0, 8, 0, std::vector<nocloom::AddressMapping>{{1, 4096}}},
        ApbBurstOutletProfile{"probe", 0, 8, 0,
                              std::vector<nocloom::AddressMapping>{{0xfffff000, 8192}}}})
    REQUIRE_FALSE(lower_apb_burst_outlet(p));
}

TEST_CASE("Burst APB4 outlet admits narrow ports and page-start windows", "[apb-burst-outlet]") {
  using namespace nocloom::detail;
  using nocloom::AddressWindow;
  auto profile = [](AddressWindow w, unsigned local, unsigned transport) {
    ApbBurstOutletProfile p{"probe", 4, 8, 0, {{w.base, w.size}}};
    p.address_bits = local;
    p.transport_address_bits = transport;
    return p;
  };
  for (const auto &[p, paddr] : std::vector<std::pair<ApbBurstOutletProfile, unsigned>>{
           {profile({0xa0f0400000, 256}, 8, 40), 8},
           {profile({0x1000, 1024}, 10, 32), 10},
           {profile({0x1000, 4}, 2, 32), 2},
           {profile({0xff'ffff'e000, 8192}, 13, 40), 13}}) {
    const auto h = lower_apb_burst_outlet(p);
    REQUIRE(h);
    REQUIRE(verify(h.value().ir));
    const auto port = std::ranges::find(h.value().ir.ports, "nl_apb_paddr", &Port::name);
    REQUIRE(port != h.value().ir.ports.end());
    CHECK(port->width == paddr);
  }
  for (const auto &p : {profile({0x1000, 382}, 10, 32),       // size not word aligned
                        profile({0x1000, 384}, 10, 32),       // size not a power of two
                        profile({0x1080, 256}, 8, 32),        // base not aligned to the size
                        profile({0x1000, 2}, 2, 32),          // below one word
                        profile({0x1000, 256}, 33, 40),       // APB port above 32 bits
                        profile({0x1000, 256}, 8, 49),        // transport above 48 bits
                        profile({0x1000, 256}, 7, 32),        // window exceeds the port
                        profile({0x1102, 256}, 8, 32),        // base not word aligned
                        profile({0xa0f0400000, 256}, 8, 32)}) // outside the transport space
    CHECK_FALSE(lower_apb_burst_outlet(p));
  ApbBurstOutletProfile unbounded{"probe"};
  unbounded.address_bits = 16;
  CHECK_FALSE(lower_apb_burst_outlet(unbounded)); // a narrow port needs a window
}

TEST_CASE("Narrow APB physical words retain a 32-bit packet container", "[apb-burst-outlet]") {
  using namespace nocloom::detail;
  for (const auto dialect :
       {nocloom::Protocol::Apb4, nocloom::Protocol::Apb3, nocloom::Protocol::Apb2}) {
    unsigned baseline_bits = 0;
    for (unsigned width : {32U, 8U, 16U}) {
      ApbBurstOutletProfile p{"probe", 4, 8, 0,
                              std::vector<nocloom::AddressMapping>{{0x1000, 4096}}};
      p.dialect = dialect;
      p.apb_data_bits = width;
      const auto h = lower_apb_burst_outlet(p);
      REQUIRE(h);
      REQUIRE(verify(h.value().ir));
      std::map<std::string, unsigned> ports;
      for (const auto &port : h.value().ir.ports)
        ports[port.name] = port.width;
      CHECK(ports.at("nl_apb_pwdata") == width);
      CHECK(ports.at("nl_apb_prdata") == width);
      CHECK(ports.at("nl_wreq_data") == 32);
      CHECK(ports.at("nl_rrsp_data") == 32);
      CHECK(ports.contains("nl_apb_pstrb") == (dialect == nocloom::Protocol::Apb4));
      if (dialect == nocloom::Protocol::Apb4)
        CHECK(ports.at("nl_apb_pstrb") == width / 8);
      // Counted on the back-to-back form, which stores the same words at every width; a serial
      // outlet of whole words also drops its word's address and lanes.
      auto counted = p;
      counted.apb_back_to_back = true;
      const auto form = lower_apb_burst_outlet(counted);
      REQUIRE(form);
      const auto state_bits =
          std::accumulate(form.value().ir.states.begin(), form.value().ir.states.end(), 0U,
                          [&](unsigned sum, const auto &state) {
                            return sum + form.value().ir.values.at(state.output.index).width;
                          });
      // A narrow word gathers 32 bits; without PSTRB a full-width word keeps its lanes in one
      // bit, a narrow one keeps all four.
      const unsigned lane_bits = dialect == nocloom::Protocol::Apb4 ? 0U : 3U;
      if (width == 32)
        baseline_bits = state_bits;
      else
        CHECK(state_bits == baseline_bits + 32 + lane_bits);
      p.data_bits = width == 32 ? 16 : width;
      CHECK_FALSE(lower_apb_burst_outlet(p));
    }
  }
  ApbBurstOutletProfile bad{"probe"};
  for (unsigned width : {0U, 4U, 24U, 64U}) {
    bad.apb_data_bits = width;
    CHECK_FALSE(lower_apb_burst_outlet(bad));
  }
}

TEST_CASE("Back-to-back APB outlets stage two responses per direction, serial ones one",
          "[apb-cadence]") {
  using namespace nocloom::detail;
  for (auto dialect : {nocloom::Protocol::Apb4, nocloom::Protocol::Apb3, nocloom::Protocol::Apb2}) {
    ApbBurstOutletProfile p{"probe", 16, 8, 1,
                            std::vector<nocloom::AddressMapping>{{0x1000, 4096}}};
    p.dialect = dialect;
    const auto plain = lower_apb_burst_outlet(p);
    REQUIRE(plain);
    p.apb_back_to_back = true;
    const auto h = lower_apb_burst_outlet(p);
    INFO([&] {
      std::string message;
      for (const auto &d : h.diagnostics())
        message += d.code + ": " + d.message + "\n";
      return message;
    }());
    REQUIRE(h);
    REQUIRE(verify(h.value().ir));
    // A serial outlet without a timer takes a write head only after the previous B left and issues
    // a read only after the previous response left: it keeps no second entry. Back-to-back and
    // timed outlets keep both.
    for (const auto *name :
         {"r.tail_full", "r.tail_data", "w.response_tail_full", "w.response_tail.address"})
      CHECK_FALSE(plain.value().bindings.contains(name));
    CHECK(h.value().ir.states.size() > plain.value().ir.states.size());
    p.apb_back_to_back = false;
    p.timeout_cycles = 1024;
    const auto timed = dialect == nocloom::Protocol::Apb2 ? h : lower_apb_burst_outlet(p);
    REQUIRE(timed);
    CHECK(timed.value().bindings.contains("r.tail_full"));
    CHECK(timed.value().bindings.contains("w.response_tail_full"));
    p.timeout_cycles = 0;
    p.apb_back_to_back = true;
    for (const auto *name :
         {"r.tail_full", "r.tail_data", "r.tail_address", "r.tail_result", "r.tail_first",
          "r.tail_last", "w.response_full", "w.response_tail_full", "w.response.address",
          "w.response.result", "w.response_tail.address", "w.response_tail.result"}) {
      const auto v = h.value().bindings.at(name);
      const auto &state = h.value().ir.states.at(h.value().ir.values.at(v.index).state.index);
      REQUIRE(state.reset);
      CHECK(state.reset->value == 0);
    }
    for (auto width : {8U, 16U}) {
      p.apb_data_bits = width;
      const auto narrow = lower_apb_burst_outlet(p);
      REQUIRE(narrow);
      REQUIRE(verify(narrow.value().ir));
    }
    p.apb_data_bits = 32;
    for (auto p2 : {p}) {
      p2.max_outstanding = 2;
      CHECK_FALSE(lower_apb_burst_outlet(p2));
      CHECK_FALSE(lower_axi_lite_outlet(p2));
    }
    for (const auto &bad : {lower_axi_lite_outlet(p), lower_ahb_outlet(p)}) {
      REQUIRE_FALSE(bad);
      CHECK(bad.diagnostics().front().field == "apb_back_to_back");
    }
  }
}
