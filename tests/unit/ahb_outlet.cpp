#include "core/ahb_outlet.hpp"
#include "../endpoint_ports.hpp"
#include "../public_artifacts.hpp"
#include <algorithm>
#include <bit>
#include <catch2/catch_test_macros.hpp>
#include <string>
#include <tuple>

TEST_CASE("Dedicated AHB outlet validates identity and window geometry", "[ahb-outlet]") {
  using namespace nocloom::detail;
  for (unsigned ids : {0U, 4U, 8U, 9U, 16U})
    for (unsigned length : {4U, 8U}) {
      auto h = lower_ahb_outlet(
          {"probe", ids, length, 7, std::vector<nocloom::AddressMapping>{{0x1000, 0x2000}}});
      REQUIRE(h);
      REQUIRE(verify(h.value().ir));
      for (const auto &s : h.value().ir.states)
        REQUIRE(s.reset.has_value());
    }
  for (auto p :
       {AhbOutletProfile{"bad-name"}, AhbOutletProfile{"probe", 17},
        AhbOutletProfile{"probe", 0, 9}, AhbOutletProfile{"probe", 0, 8, 10},
        AhbOutletProfile{"probe", 0, 8, 0, std::vector<nocloom::AddressMapping>{{1, 4096}}},
        AhbOutletProfile{"probe", 0, 8, 0,
                         std::vector<nocloom::AddressMapping>{{0xfffff000, 8192}}}})
    REQUIRE_FALSE(lower_ahb_outlet(p));
}

TEST_CASE("Dedicated AHB outlet carries 32- to 1024-bit words", "[ahb-outlet]") {
  using namespace nocloom::detail;
  const auto width = [](const NodeHardware &h, const std::string &name) {
    const auto port = std::ranges::find(h.ir.ports, name, &Port::name);
    return port == h.ir.ports.end() ? 0U : port->width;
  };
  for (unsigned bits : {32U, 64U, 128U, 256U, 512U, 1024U}) {
    AhbOutletProfile profile{"probe", 4, 8, 0,
                             std::vector<nocloom::AddressMapping>{{0x1000, 0x1000}}};
    profile.data_bits = bits;
    for (bool shaped : {false, true}) {
      profile.complete_write_bursts = profile.fixed_single = shaped;
      profile.undefined_length_bursts = !shaped;
      profile.hresp_width = shaped ? 2 : 1;
      profile.timeout_cycles = shaped ? 1024 : 0;
      auto h = lower_ahb_outlet(profile);
      REQUIRE(h);
      REQUIRE(verify(h.value().ir));
      CHECK(width(h.value(), "nl_ahb_hwdata") == bits);
      CHECK(width(h.value(), "nl_ahb_hrdata") == bits);
      CHECK(width(h.value(), "nl_ahb_hsize") == 3);
      CHECK(width(h.value(), "nl_wreq_data") == bits);
      CHECK(width(h.value(), "nl_wreq_be") == bits / 8);
      CHECK(width(h.value(), "nl_rrsp_data") == bits);
      // The response count holds 2^AxSIZE bytes: 7 bits at 512, 8 at 1024.
      CHECK(width(h.value(), "nl_rrsp_count") ==
            (bits > 256 ? static_cast<unsigned>(std::bit_width(bits / 8)) : 6U));
    }
  }
  // Local ports narrower than a step or a WRAP container lower and verify.
  for (const auto &[bits, size, port] :
       {std::tuple{1024U, 128U, 7U}, std::tuple{32U, 8U, 3U}, std::tuple{256U, 32U, 5U}}) {
    AhbOutletProfile profile{"probe", 4, 8, 0, std::vector<nocloom::AddressMapping>{{0x1000, size}},
                             port};
    profile.data_bits = bits;
    auto h = lower_ahb_outlet(profile);
    REQUIRE(h);
    REQUIRE(verify(h.value().ir));
    CHECK(width(h.value(), "nl_ahb_haddr") == port);
  }
  // AHB-Lite lists 8..1024; NocLoom takes 32..1024, powers of two only.
  for (unsigned bits : {8U, 16U, 48U, 2048U}) {
    AhbOutletProfile profile{"probe", 4, 8, 0,
                             std::vector<nocloom::AddressMapping>{{0x1000, 0x1000}}};
    profile.data_bits = bits;
    REQUIRE_FALSE(lower_ahb_outlet(profile));
  }
  // APB stays 32 bits; AXI4-Lite also accepts 64.
  BurstOutletProfile apb{"probe", 4, 8, 0, std::vector<nocloom::AddressMapping>{{0x1000, 0x1000}}};
  apb.data_bits = 64;
  REQUIRE_FALSE(lower_apb_burst_outlet(apb));
  REQUIRE(lower_axi_lite_outlet(apb));
}

TEST_CASE("No target input reaches a network output combinationally", "[ahb-outlet]") {
  // The pipelined AHB outlet takes write words through a registered ready,
  // so target HREADYOUT reaches only the target HREADY output. The APB,
  // AXI4-Lite and AXI4 targets of the same network keep the same rule.
  using namespace nocloom;
  for (const bool options : {false, true}) {
    Definition d;
    d.connectivity = Connectivity{.all = true};
    d.name = "cone";
    d.reset_ports = {{"reset_n"}};
    d.test_ports = {{"test"}};
    d.clock_domains = {{"main", "clk_main", "reset_n", "test"}};
    d.initiators = {{"s0", "main"}, {"s1", "main"}};
    d.targets = {{"th", "main", {{0x10000, 0x1000}}},
                 {"tp", "main", {{0x20000, 0x1000}}},
                 {"tl", "main", {{0x30000, 0x1000}}},
                 {"ta", "main", {{0x40000, 0x1000}}}};
    for (auto &i : d.initiators) {
      i.protocol = Protocol::Axi4;
      i.data_width = 32;
    }
    for (auto &t : d.targets) {
      t.protocol = Protocol::Axi4;
      t.data_width = 32;
    }
    set_protocol(d.targets[0], Protocol::AhbV3);
    set_protocol(d.targets[1], Protocol::Apb4);
    set_protocol(d.targets[2], Protocol::Axi4Lite);
    endpoint_ports(d, 1, 8);
    if (options) {
      d.transport = TransportOptions{.max_outstanding = 4};
      d.targets[0].timeout = TargetTimeout{1024};
      d.targets[1].timeout = TargetTimeout{1024};
      d.targets[2].timeout = TargetTimeout{1024, true};
      d.targets[0].hresp_width = 2;
      d.targets[0].undefined_length_bursts = false;
      d.targets[0].write_bursts = WriteBursts::Complete;
    }
    const auto reached = public_artifacts(d).combinational_inputs();
    REQUIRE(reached.contains("nl_t_th_hready"));
    CHECK(reached.at("nl_t_th_hready").contains("nl_t_th_hreadyout"));
    REQUIRE(reached.contains("nl_i_s0_wready"));
    for (const auto &[output, inputs] : reached)
      for (const auto &input : inputs)
        if (input.starts_with("nl_t_")) {
          INFO(output << " <- " << input);
          CHECK((output == "nl_t_th_hready" && input == "nl_t_th_hreadyout"));
        }
  }
}

TEST_CASE("Dedicated outlets take only page-start windows", "[ahb-outlet]") {
  using namespace nocloom::detail;
  BurstOutletProfile page{"page", 0, 8, 0, std::vector<nocloom::AddressMapping>{{0x1000, 0x100}}};
  REQUIRE(lower_apb_burst_outlet(page));
  REQUIRE(lower_axi_lite_outlet(page));
  REQUIRE(lower_ahb_outlet(page));
  for (auto window : {nocloom::AddressWindow{0x1100, 0x90}, nocloom::AddressWindow{0x1100, 4096},
                      nocloom::AddressWindow{0x1000, 0x90}}) {
    BurstOutletProfile p{"offpage", 0, 8, 0, {{window.base, window.size}}};
    CHECK_FALSE(lower_apb_burst_outlet(p));
    CHECK_FALSE(lower_axi_lite_outlet(p));
    CHECK_FALSE(lower_ahb_outlet(p));
  }
}

TEST_CASE("The pipelined AXI4-Lite outlet validates its packet capacity", "[ahb-outlet]") {
  using namespace nocloom::detail;
  for (unsigned capacity : {1U, 2U, 3U, 256U}) {
    BurstOutletProfile p{"pipe", 4, 8, 2, std::vector<nocloom::AddressMapping>{{0x1000, 0x1000}}};
    p.max_outstanding = capacity;
    p.tag_bits = 2;
    const auto h = lower_axi_lite_outlet(p);
    REQUIRE(h);
    REQUIRE(verify(h.value().ir));
    for (const auto &s : h.value().ir.states)
      REQUIRE(s.reset.has_value());
  }
  for (unsigned capacity : {0U, 257U}) {
    BurstOutletProfile p{"pipe", 4, 8, 0, std::vector<nocloom::AddressMapping>{{0x1000, 0x1000}}};
    p.max_outstanding = capacity;
    const auto h = lower_axi_lite_outlet(p);
    REQUIRE_FALSE(h);
    CHECK(h.diagnostics().front().code == "NCL_AXI_LITE_OUTLET_PROFILE");
    CHECK(h.diagnostics().front().field == "max_outstanding");
  }
  // Capacity above one takes a timer, and a network monitor combines with it.
  {
    BurstOutletProfile p{"pipe", 4, 8, 0, std::vector<nocloom::AddressMapping>{{0x1000, 0x1000}}};
    p.max_outstanding = 2;
    p.timeout_cycles = 1024;
    CHECK(lower_axi_lite_outlet(p));
    p.timeout_cycles = 0;
    p.exclusive = true;
    p.exclusive_target = {nocloom::ExclusiveAccess::Network, 2, 16};
    CHECK(lower_axi_lite_outlet(p));
  }
  // APB and AHB outlets keep one packet per direction.
  {
    ApbBurstOutletProfile p{"pipe", 4, 8, 0,
                            std::vector<nocloom::AddressMapping>{{0x1000, 0x1000}}};
    p.max_outstanding = 2;
    CHECK_FALSE(lower_apb_burst_outlet(p));
    CHECK_FALSE(lower_ahb_outlet(p));
  }
}
